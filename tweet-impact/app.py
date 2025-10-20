from flask import Flask, render_template, request, jsonify, abort
import os, glob
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
PRICES_SOURCE_TZ = "Europe/Warsaw"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# Precompute (domyślnie widoczne w UI)
PRE_MINUTE = 8
PRE_THRESHOLD = 1.0  # %

# Limity
ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
H_MAX = 60  # maks. horyzont okna do "szybkich" obliczeń

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

def to_utc(series, source_tz: str):
    """
    Zwróć tz-aware UTC:
    - jeśli wejście ma już tz -> konwersja do UTC,
    - jeśli wejście jest naivem -> traktuj jako source_tz i konwertuj do UTC.
    """
    s = pd.to_datetime(series, errors="coerce", utc=False)

    # Ustal czy ma tz (nie rzucając na pustych seriach)
    has_tz = False
    try:
        has_tz = s.dt.tz is not None
    except Exception:
        has_tz = False

    if has_tz:
        return s.dt.tz_convert("UTC")

    # Naivem -> lokalizuj do source_tz, potem UTC
    tz = ZoneInfo(source_tz)
    s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    return s.dt.tz_convert("UTC")


# ===== Loader: Tweety =====
def load_tweets(
    csv_path: str = TWEETS_CSV,
    prices_min: str = "2017-09-17 21:00:00+00:00",
    prices_max: str = "2025-03-07 20:54:00+00:00"
) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        print(f"[startup] Brak pliku tweetów: {csv_path}")
        return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

    df = pd.read_csv(csv_path, low_memory=False)
    df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
    df["text"] = (df["fullText"] if "fullText" in df.columns else df.get("text")).fillna("")

    if "createdAt" not in df.columns:
        raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

    for flag in ["isReply", "isRetweet", "isQuote"]:
        if flag not in df.columns:
            df[flag] = False

    prices_min = pd.to_datetime(prices_min, utc=True)
    prices_max = pd.to_datetime(prices_max, utc=True)
    df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]
    df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

    # tylko 15:30–21:45 czasu PL (uwzględnia DST)
    _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
    mask = (
        ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
        ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
    )
    df = df[mask].reset_index(drop=True)

    return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# ===== Loader: Ceny =====
def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
    if not os.path.isdir(base_dir):
        print(f"[startup] Brak katalogu cen: {base_dir}")
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
    if not files:
        print(f"[startup] Nie znaleziono CSV w {base_dir}")
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    frames = []
    for path in files:
        try:
            raw = pd.read_csv(path, low_memory=False)
            dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
            if not dt_col:
                continue
            def pick(col):
                if col in raw.columns: return raw[col]
                if col.capitalize() in raw.columns: return raw[col.capitalize()]
                if col.upper() in raw.columns: return raw[col.upper()]
                raise KeyError(col)
            part = pd.DataFrame({
                "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
                "open":  pd.to_numeric(pick("open"), errors="coerce"),
                "high":  pd.to_numeric(pick("high"), errors="coerce"),
                "low":   pd.to_numeric(pick("low"),  errors="coerce"),
                "close": pd.to_numeric(pick("close"),errors="coerce"),
            }).dropna(subset=["datetime"])
            # jeśli CSV już ma "% change", zachowaj:
            for cand in ["% change", "%change", "pct change", "pct_change"]:
                if cand in raw.columns:
                    part[cand] = pd.to_numeric(raw[cand], errors="coerce")
            frames.append(part)
        except Exception as e:
            print(f"[prices] pomijam {path}: {e}")
            continue

    if not frames:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    all_prices = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    return all_prices

# ===== Bufory do szybkich obliczeń z % change =====
MIN_IDX = None           # DatetimeIndex minut (UTC)
R_MINUTE = None          # r_t (ułamek), np. 0.003 = 0.3% na minutę
LOGF_PREFIX = None       # prefiks log(1+r): shape (N+1,), L[0]=0
MINUTE_TO_POS = None     # map: minute_ts(int) -> index

def _build_minute_buffers(prices_df: pd.DataFrame):
    """Zrób wektory do Δ_k = exp(L[t+k]-L[t]) - 1 (szybkie O(1)). Zawsze UTC tz-aware."""
    global MIN_IDX, R_MINUTE, LOGF_PREFIX, MINUTE_TO_POS

    if prices_df.empty or "datetime" not in prices_df.columns:
        MIN_IDX = pd.DatetimeIndex([], tz="UTC")
        R_MINUTE = np.zeros((0,), dtype=float)
        LOGF_PREFIX = np.zeros((1,), dtype=float)  # N+1
        MINUTE_TO_POS = {}
        return

    df = prices_df.copy()

    # 1) WYMUSZ tz-aware UTC na kolumnie datetime (nawet jeśli loader już to zrobił)
    #    Jeśli naivem -> lokalizuj jako UTC; jeśli ma tz -> konwertuj do UTC.
    dt = pd.to_datetime(df["datetime"], errors="coerce", utc=False)
    try:
        has_tz = dt.dt.tz is not None
    except Exception:
        has_tz = False
    if has_tz:
        dt = dt.dt.tz_convert("UTC")
    else:
        dt = dt.dt.tz_localize("UTC")

    df["minute"] = dt.dt.floor("min")
    df = df.sort_values("minute").drop_duplicates(subset=["minute"], keep="last").dropna(subset=["minute"])

    # 2) preferowana kolumna % change (w %); jeśli brak, liczymy z 'open'
    cand_cols = ["% change", "%change", "pct change", "pct_change"]
    col = next((c for c in cand_cols if c in df.columns), None)

    if col is not None:
        # 0.3677 => 0.3677% -> ułamek
        r = pd.to_numeric(df[col], errors="coerce").fillna(0.0).to_numpy(dtype=float) / 100.0
    else:
        o = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
        r = np.zeros_like(o)
        if o.size >= 2:
            prev = o[:-1]
            cur = o[1:]
            rr = np.zeros_like(cur)
            mask = (prev > 0) & np.isfinite(prev) & np.isfinite(cur)
            rr[mask] = (cur[mask] / prev[mask]) - 1.0
            r[1:] = rr

    # 3) Indeks minutowy (już jest tz-aware)
    MIN_IDX = pd.DatetimeIndex(df["minute"].values)  # ma tz=UTC

    # 4) Prefiks log(1+r)
    one_plus = np.clip(1.0 + r, 1e-9, None)
    logf = np.log(one_plus)
    LOGF_PREFIX = np.concatenate([[0.0], np.cumsum(logf)])  # N+1

    # 5) Mapka minute -> pozycja
    MINUTE_TO_POS = {int(ts.value): i for i, ts in enumerate(MIN_IDX)}

    # 6) Zapisz r_t
    R_MINUTE = r


def _pct_change_from_base(pos: int, k: int) -> float | None:
    """Zwróć Δ_k (ułamek, np. 0.005 = 0.5%) dla pozycji 'pos' i horyzontu k."""
    j = pos + k
    # LOGF_PREFIX ma N+1 elementów; pos+k <= N => j <= N
    if pos < 0 or j > len(LOGF_PREFIX) - 1:
        return None
    # Δ = exp(L[pos+k] - L[pos]) - 1
    return float(np.exp(LOGF_PREFIX[j] - LOGF_PREFIX[pos]) - 1.0)

def _pct_series_from_base(pos: int, horizons=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
    return {k: _pct_change_from_base(pos, k) for k in horizons}

def percent_changes_from(start_dt_utc: pd.Timestamp,
                         intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
    """Szybkie Δ% dla wybranych horyzontów względem minuty tweeta."""
    if MIN_IDX is None or len(MIN_IDX) == 0:
        return {k: None for k in intervals}
    minute = pd.Timestamp(start_dt_utc).floor("min")
    pos = MINUTE_TO_POS.get(int(minute.value), None)
    if pos is None:
        return {k: None for k in intervals}
    return _pct_series_from_base(pos, intervals)

def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
    """Δ% po 'minute' minutach (ułamek), z szybkim path-em."""
    if minute not in ALLOWED_IMPACT_MINUTES:
        return None
    if MIN_IDX is None or len(MIN_IDX) == 0:
        return None
    pos = MINUTE_TO_POS.get(int(pd.Timestamp(dt_utc).floor("min").value), None)
    if pos is None:
        return None
    return _pct_change_from_base(pos, minute)

def _label_for_change(val: float | None, thr: float) -> str:
    if val is None:  return "neutral"
    if val >= thr/100.0:   return "up"
    if val <= -thr/100.0:  return "down"
    return "neutral"

# ===== Inicjalizacja =====
TWEETS_DF = load_tweets()
PRICES_DF = load_prices_from_dir()
_build_minute_buffers(PRICES_DF)

# ===== Precompute: etykiety bazowe =====
def precompute_labels(df: pd.DataFrame, minute: int = PRE_MINUTE, thr: float = PRE_THRESHOLD) -> pd.DataFrame:
    print(f"[precompute] Liczę etykiety bazowe: m={minute}, próg={thr}%  (wiersze: {len(df)})")
    pct, lab = [], []
    for ts in df["created_at"]:
        v = impact_at_minute(pd.Timestamp(ts), minute)  # ułamek
        pct.append(None if v is None else round(100.0 * v, 4))  # na %
        lab.append(_label_for_change(v, thr))
    out = df.copy()
    out["pre_min"]   = int(minute)
    out["pre_pct"]   = pct                 # w %
    out["pre_label"] = lab
    # dla zgodności
    out["_lab_min"]   = out["pre_min"]
    out["_lab_pct"]   = out["pre_pct"]
    out["_lab_label"] = out["pre_label"]
    return out

if not TWEETS_DF.empty:
    TWEETS_DF = precompute_labels(TWEETS_DF, PRE_MINUTE, PRE_THRESHOLD)

# ===== Trasy =====
@app.route("/health")
def health():
    return jsonify({
        "tweets_rows": int(len(TWEETS_DF)),
        "prices_rows": int(len(PRICES_DF)),
        "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
        "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
        "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
        "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
        "pre_minute": PRE_MINUTE,
        "pre_threshold": PRE_THRESHOLD
    })

@app.route("/")
def index():
    initial_id = None
    if len(TWEETS_DF):
        initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
    return render_template("index.html", initial_id=initial_id)

# ---- API: lista tweetów ----
@app.route("/api/tweets")
def api_tweets():
    page = int(request.args.get("page", 1))
    per_page = min(max(int(request.args.get("per_page", 20)), 5), 500)
    year = request.args.get("year", "all")
    q = (request.args.get("q") or "").strip()
    label = (request.args.get("label", "all") or "all").lower()
    # --- na początku api_tweets(), obok innych parametrów:
    imp_sort = int(request.args.get("imp_sort", 0) or 0)



    # tryby etykietowania (0 = precompute, 1 = licz w locie wg lab-*)
    imp_filter = int(request.args.get("imp_filter", 0) or 0)
    try:
        imp_min = int(request.args.get("imp_min", PRE_MINUTE))
    except Exception:
        imp_min = PRE_MINUTE
    if imp_min not in ALLOWED_IMPACT_MINUTES:
        imp_min = PRE_MINUTE

    thr_raw = (request.args.get("imp_thr", "") or "").strip()
    imp_thr = None if thr_raw == "" else float(thr_raw)  # w %

    df = TWEETS_DF.copy()

    def _p(n):
        try: return int(request.args.get(n, 0) or 0)
        except ValueError: return 0
    f_reply   = _p("reply")
    f_retweet = _p("retweet")
    f_quote   = _p("quote")

    for col in ("isReply", "isRetweet", "isQuote"):
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False)

    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    if f_reply == 1:    df = df[df["isReply"]]
    elif f_reply == -1: df = df[~df["isReply"]]
    if f_retweet == 1:  df = df[df["isRetweet"]]
    elif f_retweet == -1: df = df[~df["isRetweet"]]
    if f_quote == 1:    df = df[df["isQuote"]]
    elif f_quote == -1: df = df[~df["isQuote"]]

    if q:
        df = df[df["text"].str.contains(q, case=False, na=False)]

    # Etykietowanie: albo precompute (pre_*), albo liczymy imp_* w locie (ułamek -> %)
    if imp_filter == 1:
        imp_pct, imp_lbl = [], []
        for ts in df["created_at"]:
            v = impact_at_minute(pd.Timestamp(ts), imp_min)  # ułamek
            pct = None if v is None else (100.0 * v)
            imp_pct.append(None if pct is None else round(pct, 4))
            if v is None:
                imp_lbl.append("neutral")
            else:
                if imp_thr is None:
                    imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
                else:
                    thr = imp_thr / 100.0
                    imp_lbl.append("up" if v >=  thr else "down" if v <= -thr else "neutral")
        df["_imp_pct"] = imp_pct
        df["_imp_label"] = imp_lbl
    else:
        df["_imp_pct"] = None
        df["_imp_label"] = None

    # --- Filtr etykiety: użyj imp_* jeśli liczone w locie, inaczej pre_* (precompute)
    if label in ("up", "down", "neutral"):
        if imp_filter == 1:
            # Upewnij się, że kolumna istnieje; jeśli nie — nic nie filtruj
            if "_imp_label" in df.columns:
                df = df[df["_imp_label"] == label]
        else:
            if "pre_label" in df.columns:
                df = df[df["pre_label"] == label]

    # --- tuż po bloku filtrowania po etykiecie (po "if label in (...)" ...), a przed:
    # total = len(df); start = ... (czyli PRZED paginacją!)

    if imp_sort == 1 and label in ("up", "down"):
        # wybieramy kolumnę do sortowania:
        sort_col = None
        if imp_filter == 1 and "_imp_pct" in df.columns:
            sort_col = "_imp_pct"
        elif "pre_pct" in df.columns:
            sort_col = "pre_pct"

        if sort_col is not None:
            asc = (label == "down")   # down: rosnąco, up: malejąco
            df = df.sort_values(sort_col, ascending=asc, na_position="last")



    total = len(df)
    start = (page - 1) * per_page
    end = start + per_page
    subset = df.iloc[start:end].copy()

    subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
        .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    items = []
    for r in subset.itertuples(index=False):
        # bezpieczne pobrania z domyślnymi wartościami
        txt = getattr(r, "text", "")
        created_display = getattr(r, "created_at_display", "")
        created_at_val = getattr(r, "created_at", None)
        year_val = None
        try:
            year_val = int(pd.Timestamp(created_at_val).year) if created_at_val is not None else None
        except Exception:
            year_val = None

        is_reply   = bool(getattr(r, "isReply", False))
        is_retweet = bool(getattr(r, "isRetweet", False))
        is_quote   = bool(getattr(r, "isQuote", False))

        pre_label = getattr(r, "pre_label", None)
        pre_min   = getattr(r, "pre_min", None)
        pre_pct   = getattr(r, "pre_pct", None)
        pre_pct   = (None if pre_pct is None or (isinstance(pre_pct, float) and pd.isna(pre_pct)) else float(pre_pct))

        lab_label = getattr(r, "_lab_label", pre_label)
        lab_min   = getattr(r, "_lab_min", pre_min)
        lab_pct   = getattr(r, "_lab_pct", pre_pct)
        lab_pct   = (None if lab_pct is None or (isinstance(lab_pct, float) and pd.isna(lab_pct)) else float(lab_pct))

        imp_lbl = getattr(r, "_imp_label", None)
        imp_pct = getattr(r, "_imp_pct", None)
        imp_pct = (None if imp_pct is None or (isinstance(imp_pct, float) and pd.isna(imp_pct)) else float(imp_pct))
        imp_min_out = imp_min if imp_filter == 1 else None

        items.append({
            "tweet_id": str(getattr(r, "tweet_id", "")),
            "text": txt,
            "created_at_display": created_display,
            "isReply": is_reply,
            "isRetweet": is_retweet,
            "isQuote": is_quote,
            "year": year_val if year_val is not None else (int(pd.Timestamp(created_at_val).year) if created_at_val is not None else None),

            # Precompute (jeśli są)
            "pre_label": pre_label,
            "pre_min":   (int(pre_min) if pre_min is not None else None),
            "pre_pct":   pre_pct,

            # Zgodność wsteczna (lab_*)
            "lab_label": lab_label,
            "lab_min":   (int(lab_min) if lab_min is not None else None),
            "lab_pct":   lab_pct,

            # Opcjonalnie imp_* (liczone w locie)
            "imp_label": imp_lbl,
            "imp_min":   imp_min_out,
            "imp_pct":   imp_pct,
        })


    years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True) if len(TWEETS_DF) else []
    return jsonify({"items": items, "page": page, "per_page": per_page, "total": int(total), "years": years})

# ---- API: pojedynczy tweet ----
@app.route("/api/tweet/<tweet_id>")
def api_tweet(tweet_id):
    row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
    if row.empty:
        abort(404)
    t = row.iloc[0]
    created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
    created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    return jsonify({
        "tweet_id": str(t["tweet_id"]),
        "text": t["text"],
        "isReply": bool(t["isReply"]),
        "isRetweet": bool(t["isRetweet"]),
        "isQuote": bool(t["isQuote"]),
        "created_ts": created_ts,
        "created_display": created_display
    })

# ---- API: ceny / wykres ----
@app.route("/api/price")
def api_price():
    start_unix = (request.args.get("start", "") or "").strip()
    fmt = (request.args.get("format", "") or "").lower()

    try:
        minutes = int(request.args.get("minutes", 15))
    except Exception:
        minutes = 15
    minutes = max(1, min(minutes, 24*60))

    try:
        pre = int(request.args.get("pre", 0))
    except Exception:
        pre = 0
    pre = max(0, min(pre, 120))

    if not start_unix:
        resp = {"points": [], "reason": "no_start"}
        if fmt != "text": return jsonify(resp)
        return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    try:
        start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
    except Exception:
        resp = {"points": [], "reason": "bad_start"}
        if fmt != "text": return jsonify(resp)
        return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    win_start = start_dt - pd.Timedelta(minutes=pre)
    win_end   = start_dt + pd.Timedelta(minutes=minutes)
    # Uwaga: tutaj nadal zwracamy surowe punkty z PRICES_DF (dla wykresu)
    df = PRICES_DF[(PRICES_DF["datetime"] >= win_start) & (PRICES_DF["datetime"] <= win_end)].copy()
    reason = "ok" if not df.empty else "no_data"

    points = [{
        "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low":  float(r["low"]),
        "close": float(r["close"]),
    } for _, r in df.iterrows()]

    # Δ z fast-path jest w UŁAMKU -> przeskaluj do % dla spójności z pre_pct
    pc_raw = percent_changes_from(start_dt)  # {k: fraction or None}
    pct_changes = {k: (None if v is None else round(100.0 * v, 2)) for k, v in pc_raw.items()}

    payload = {
        "points": points,
        "reason": reason,
        "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
        "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
        "x_start": int(pd.Timestamp(win_start).value // 10**9),
        "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
        "pct_changes": pct_changes
    }


    # siatka do overlay
    if request.args.get("grid", "0") == "1":
        grid_start = pd.Timestamp(win_start).floor("min")
        grid_end   = pd.Timestamp(win_end).floor("min")
        idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

        if df.empty:
            aligned_close = [None] * len(idx)
        else:
            dfm = df.copy()
            dfm["minute"] = dfm["datetime"].dt.floor("min")
            dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
            aligned = dfm.reindex(idx)
            aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

        payload["grid"] = {
            "minute_ts": [int(ts.value // 10**9) for ts in idx],
            "close": aligned_close,
            "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
        }

    if fmt != "text":
        return jsonify(payload)

    # legacy: tekst
    legacy_start = pd.Timestamp(win_start).floor("min")
    legacy_end   = pd.Timestamp(win_end).floor("min")
    legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

    if df.empty:
        dfm = pd.DataFrame(columns=["minute", "close"])
    else:
        dfm = df.copy()
        dfm["minute"] = dfm["datetime"].dt.floor("min")
        dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

    aligned = dfm.reindex(legacy_idx)

    lines = []
    for ts_utc, row in aligned.itertuples():
        ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
        val = (row["close"] if isinstance(row, pd.Series) else None)
        if pd.isna(val):
            lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
        else:
            lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

    header = [
        "Ceny w oknie minutowym:",
        f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
        f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
        f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
    ]
    if reason == "no_data":
        header.append("Brak danych cenowych w tym oknie.")

    body = "\n".join(header + [""] + lines)
    return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

if __name__ == "__main__":
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # świeży app.js w debug
    app.run(debug=True)

