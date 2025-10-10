# app.py — prosta aplikacja Flask: lista tweetów, szybkie etykiety (precompute) i wykres/overlay
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
    per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
    year = request.args.get("year", "all")
    q = (request.args.get("q") or "").strip()
    label = (request.args.get("label", "all") or "all").lower()


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



# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów, overlayu i Z GÓRY policzonych etykiet
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from zoneinfo import ZoneInfo

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# # Ustawienia bazowych etykiet (precompute)
# PRE_MINUTE = 8
# PRE_THRESHOLD = 1.0  # %

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         return s.dt.tz_convert("UTC")
#     tz = ZoneInfo(source_tz)
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

#     # tylko 15:30–21:45 czasu PL (uwzględnia DST)
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue
#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# def _minute_open_at(dt_utc: pd.Timestamp):
#     """Kurs z minuty (używamy 'open' jak w UI)."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"])

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     base = _minute_open_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_open_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)

# def _label_for_change(val: float | None, thr: float) -> str:
#     if val is None:
#         return "neutral"
#     if val >= thr:   return "up"
#     if val <= -thr:  return "down"
#     return "neutral"

# # ===== Precompute: etykiety bazowe dla WSZYSTKICH tweetów =====
# def precompute_labels(df: pd.DataFrame, minute: int = PRE_MINUTE, thr: float = PRE_THRESHOLD) -> pd.DataFrame:
#     print(f"[precompute] Liczę etykiety bazowe: m={minute}, próg={thr}%  (wiersze: {len(df)})")
#     pct, lab = [], []
#     for ts in df["created_at"]:
#         v = impact_at_minute(pd.Timestamp(ts), minute)
#         pct.append(v)
#         lab.append(_label_for_change(v, thr))
#     df = df.copy()
#     df["pre_min"]   = int(minute)
#     df["pre_pct"]   = pct
#     df["pre_label"] = lab
#     # Dla wstecznej zgodności (stare fronty mogą czytać lab_*)
#     df["_lab_min"]   = df["pre_min"]
#     df["_lab_pct"]   = df["pre_pct"]
#     df["_lab_label"] = df["pre_label"]
#     return df

# # Po wczytaniu danych policz etykiety bazowe
# if not TWEETS_DF.empty:
#     TWEETS_DF = precompute_labels(TWEETS_DF, PRE_MINUTE, PRE_THRESHOLD)

# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#         "pre_minute": PRE_MINUTE,
#         "pre_threshold": PRE_THRESHOLD
#     })

# @app.route("/")
# def index():
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # --- Filtr/ocena „imp_*” (opcjonalne liczenie innego okna niż bazowe) ---
#     imp_filter = int(request.args.get("imp_filter", 0) or 0)

#     try:
#         imp_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         imp_min = 10
#     if imp_min not in ALLOWED_IMPACT_MINUTES:
#         imp_min = 10

#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         imp_thr = None  # brak progu => klasyfikacja po znaku
#     else:
#         try:
#             imp_thr = float(thr_raw)
#         except Exception:
#             imp_thr = None

#     sort_impact = int(request.args.get("imp_sort", 0) or 0)
#     imp_in_raw = (request.args.get("imp_in", "") or "").strip()
#     imp_in = set([p.strip().lower() for p in imp_in_raw.split(",") if p.strip()])  # {'up','down','neutral'}

#     # --- baza + podstawowe filtry ---
#     df = TWEETS_DF.copy()

#     def _p(name):
#         try: return int(request.args.get(name, 0) or 0)
#         except ValueError: return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             df[col] = df[col].astype("boolean").fillna(False)

#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     if f_reply == 1:    df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1:  df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:    df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- 1) Opcjonalne liczenie „imp_*” w locie (na inne minuty/próg) ---
#     if imp_filter == 1:
#         imp_pct, imp_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), imp_min)
#             imp_pct.append(v)
#             if v is None:
#                 imp_lbl.append("neutral")
#             else:
#                 if imp_thr is None:
#                     imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
#                 else:
#                     imp_lbl.append("up" if v >= imp_thr else "down" if v <= -imp_thr else "neutral")
#         df["_imp_pct"] = imp_pct
#         df["_imp_label"] = imp_lbl

#         if len(imp_in) > 0:
#             df = df[df["_imp_label"].isin(list(imp_in))]

#         if sort_impact == 1:
#             df = (
#                 df.assign(_abs=df["_imp_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )
#     else:
#         # brak „imp” – jeśli user zaznaczył etykiety, filtruj po PRE (z góry policzonych)
#         if len(imp_in) > 0:
#             df = df[df["pre_label"].isin(list(imp_in))]
#         df["_imp_pct"] = None
#         df["_imp_label"] = None

#         if sort_impact == 1:
#             df = (
#                 df.assign(_abs=df["pre_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )

#     # --- stronicowanie + payload ---
#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),

#             # Precompute (zawsze dostępne)
#             "pre_label": r.pre_label,
#             "pre_min":   int(r.pre_min),
#             "pre_pct":   (None if pd.isna(r.pre_pct) else float(r.pre_pct) if r.pre_pct is not None else None),

#             # Dla zgodności wstecznej (stary front oczekiwał „lab_*”)
#             "lab_label": r._lab_label,
#             "lab_min":   int(r._lab_min),
#             "lab_pct":   (None if pd.isna(r._lab_pct) else float(r._lab_pct) if r._lab_pct is not None else None),

#             # Imp (opcjonalne)
#             "imp_label": (None if pd.isna(getattr(r, "_imp_label", None)) else getattr(r, "_imp_label", None)),
#             "imp_min":   (imp_min if imp_filter == 1 else None),
#             "imp_pct":   (None if pd.isna(getattr(r, "_imp_pct", None)) else float(r._imp_pct) if getattr(r, "_imp_pct", None) is not None else None),
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({"items": items, "page": page, "per_page": per_page, "total": int(total), "years": years})

# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     # siatka do overlay
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     if fmt != "text":
#         return jsonify(payload)

#     # legacy text
#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     # (opcjonalnie) wyłącz cache statycznych w debug, aby mieć świeży app.js
#     app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
#     app.run(debug=True)



# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i overlayu
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         return s.dt.tz_convert("UTC")
#     tz = ZoneInfo(source_tz)
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

#     # tylko 15:30–21:45 czasu PL (uwzględnia DST)
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue
#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# def _minute_open_at(dt_utc: pd.Timestamp):
#     """Kurs z minuty (używamy 'open' jak w UI)."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"])

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     base = _minute_open_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_open_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)

# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#     })

# @app.route("/")
# def index():
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # --- 1) Nadaj etykiety (lab_*) ---
#     lab_enable = int(request.args.get("lab_enable", 0) or 0)
#     try:
#         lab_min = int(request.args.get("lab_min", 8))
#     except Exception:
#         lab_min = 8
#     if lab_min not in ALLOWED_IMPACT_MINUTES:
#         lab_min = 8
#     try:
#         lab_thr = float(request.args.get("lab_thr", 1.0))
#     except Exception:
#         lab_thr = 1.0

#     # --- 2) Filtr/ocena (imp_*) ---
#     imp_filter = int(request.args.get("imp_filter", 0) or 0)
#     try:
#         imp_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         imp_min = 10
#     if imp_min not in ALLOWED_IMPACT_MINUTES:
#         imp_min = 10

#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         imp_thr = None  # brak progu => klasyfikacja po znaku
#     else:
#         try:
#             imp_thr = float(thr_raw)
#         except Exception:
#             imp_thr = None

#     sort_impact = int(request.args.get("imp_sort", 0) or 0)
#     imp_in_raw = (request.args.get("imp_in", "") or "").strip()
#     imp_in = set([p.strip().lower() for p in imp_in_raw.split(",") if p.strip()])

#     # --- baza + podstawowe filtry ---
#     df = TWEETS_DF.copy()

#     def _p(name):
#         try: return int(request.args.get(name, 0) or 0)
#         except ValueError: return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             df[col] = df[col].astype("boolean").fillna(False)

#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     if f_reply == 1:    df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1:  df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:    df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- liczenie etykiet lab_* (Nadaj etykiety) ---
#     if lab_enable == 1:
#         lab_pct, lab_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), lab_min)
#             lab_pct.append(v)
#             if v is None:
#                 lab_lbl.append("neutral")
#             elif v >= lab_thr:
#                 lab_lbl.append("up")
#             elif v <= -lab_thr:
#                 lab_lbl.append("down")
#             else:
#                 lab_lbl.append("neutral")
#         df["_lab_pct"] = lab_pct
#         df["_lab_label"] = lab_lbl
#     else:
#         df["_lab_pct"] = None
#         df["_lab_label"] = None

#     # --- liczenie/filtr imp_* (Zastosuj filtr) ---
#     if imp_filter == 1:
#         imp_pct, imp_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), imp_min)
#             imp_pct.append(v)
#             if v is None:
#                 imp_lbl.append("neutral")
#             else:
#                 if imp_thr is None:
#                     imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
#                 else:
#                     imp_lbl.append("up" if v >= imp_thr else "down" if v <= -imp_thr else "neutral")
#         df["_imp_pct"] = imp_pct
#         df["_imp_label"] = imp_lbl

#         if len(imp_in) > 0:
#             df = df[df["_imp_label"].isin(list(imp_in))]

#         if sort_impact == 1:
#             df = (
#                 df.assign(_abs=df["_imp_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )
#     else:
#         if len(imp_in) > 0 and lab_enable == 1:
#             df = df[df["_lab_label"].isin(list(imp_in))]
#         df["_imp_pct"] = None
#         df["_imp_label"] = None

#         if sort_impact == 1 and lab_enable == 1:
#             df = (
#                 df.assign(_abs=df["_lab_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )

#     # --- stronicowanie + payload ---
#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),

#             "lab_label": (None if pd.isna(getattr(r, "_lab_label", None)) else getattr(r, "_lab_label", None)),
#             "lab_min": lab_min if lab_enable == 1 else None,
#             "lab_pct": (None if pd.isna(getattr(r, "_lab_pct", None)) else float(r._lab_pct) if r._lab_pct is not None else None),

#             "imp_label": (None if pd.isna(getattr(r, "_imp_label", None)) else getattr(r, "_imp_label", None)),
#             "imp_min": imp_min if imp_filter == 1 else None,
#             "imp_pct": (None if pd.isna(getattr(r, "_imp_pct", None)) else float(r._imp_pct) if r._imp_pct is not None else None),
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({"items": items, "page": page, "per_page": per_page, "total": int(total), "years": years})



# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     # siatka do overlay
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     if fmt != "text":
#         return jsonify(payload)

#     # legacy text
#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     app.run(debug=True)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # --- 1) Nadaj etykiety (lab_*) ---
#     lab_enable = int(request.args.get("lab_enable", 0) or 0)
#     try:
#         lab_min = int(request.args.get("lab_min", 8))
#     except Exception:
#         lab_min = 8
#     if lab_min not in ALLOWED_IMPACT_MINUTES:
#         lab_min = 8
#     try:
#         lab_thr = float(request.args.get("lab_thr", 1.0))
#     except Exception:
#         lab_thr = 1.0

#     # --- 2) Filtr/ocena (imp_*) ---
#     imp_filter = int(request.args.get("imp_filter", 0) or 0)
#     try:
#         imp_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         imp_min = 10
#     if imp_min not in ALLOWED_IMPACT_MINUTES:
#         imp_min = 10

#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         imp_thr = None  # brak progu => klasyfikacja po znaku
#     else:
#         try:
#             imp_thr = float(thr_raw)
#         except Exception:
#             imp_thr = None

#     sort_impact = int(request.args.get("imp_sort", 0) or 0)
#     imp_in_raw = (request.args.get("imp_in", "") or "").strip()
#     imp_in = set([p.strip().lower() for p in imp_in_raw.split(",") if p.strip()])

#     # --- baza + podstawowe filtry ---
#     df = TWEETS_DF.copy()

#     def _p(name):
#         try: return int(request.args.get(name, 0) or 0)
#         except ValueError: return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             df[col] = df[col].astype("boolean").fillna(False)

#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     if f_reply == 1:    df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1:  df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:    df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- liczenie etykiet lab_* (Nadaj etykiety) ---
#     if lab_enable == 1:
#         lab_pct, lab_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), lab_min)
#             lab_pct.append(v)
#             if v is None:
#                 lab_lbl.append("neutral")
#             elif v >= lab_thr:
#                 lab_lbl.append("up")
#             elif v <= -lab_thr:
#                 lab_lbl.append("down")
#             else:
#                 lab_lbl.append("neutral")
#         df["_lab_pct"] = lab_pct
#         df["_lab_label"] = lab_lbl
#     else:
#         df["_lab_pct"] = None
#         df["_lab_label"] = None

#     # --- liczenie/filtr imp_* (Zastosuj filtr) ---
#     if imp_filter == 1:
#         imp_pct, imp_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), imp_min)
#             imp_pct.append(v)
#             if v is None:
#                 imp_lbl.append("neutral")
#             else:
#                 if imp_thr is None:
#                     imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
#                 else:
#                     imp_lbl.append("up" if v >= imp_thr else "down" if v <= -imp_thr else "neutral")
#         df["_imp_pct"] = imp_pct
#         df["_imp_label"] = imp_lbl

#         if len(imp_in) > 0:
#             df = df[df["_imp_label"].isin(list(imp_in))]

#         if sort_impact == 1:
#             df = (
#                 df.assign(_abs=df["_imp_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )
#     else:
#         if len(imp_in) > 0 and lab_enable == 1:
#             df = df[df["_lab_label"].isin(list(imp_in))]
#         df["_imp_pct"] = None
#         df["_imp_label"] = None

#         if sort_impact == 1 and lab_enable == 1:
#             df = (
#                 df.assign(_abs=df["_lab_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )

#     # --- stronicowanie + payload ---
#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),

#             "lab_label": (None if pd.isna(getattr(r, "_lab_label", None)) else getattr(r, "_lab_label", None)),
#             "lab_min": lab_min if lab_enable == 1 else None,
#             "lab_pct": (None if pd.isna(getattr(r, "_lab_pct", None)) else float(r._lab_pct) if r._lab_pct is not None else None),

#             "imp_label": (None if pd.isna(getattr(r, "_imp_label", None)) else getattr(r, "_imp_label", None)),
#             "imp_min": imp_min if imp_filter == 1 else None,
#             "imp_pct": (None if pd.isna(getattr(r, "_imp_pct", None)) else float(r._imp_pct) if r._imp_pct is not None else None),
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({"items": items, "page": page, "per_page": per_page, "total": int(total), "years": years})



# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     # siatka do overlay
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     if fmt != "text":
#         return jsonify(payload)

#     # legacy text
#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     app.run(debug=True)


# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i overlayu
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo
# from bisect import bisect_left

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         return s.dt.tz_convert("UTC")
#     tz = ZoneInfo(source_tz)
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue
#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# def _minute_open_at(dt_utc: pd.Timestamp):
#     """Kurs z minuty (używamy 'open' jak w Twoim UI)."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"])

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     base = _minute_open_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_open_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)

# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#     })

# @app.route("/")
# def index():
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # 1) Parametry etykietowania
#     lab_enable = int(request.args.get("lab_enable", 0) or 0)
#     try:
#         lab_min = int(request.args.get("lab_min", 8))
#     except Exception:
#         lab_min = 8
#     if lab_min not in ALLOWED_IMPACT_MINUTES:
#         lab_min = 8
#     try:
#         lab_thr = float(request.args.get("lab_thr", 1.0))
#     except Exception:
#         lab_thr = 1.0

#     # 2) Parametry filtrowania wyników
#     imp_filter = int(request.args.get("imp_filter", 0) or 0)
#     try:
#         imp_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         imp_min = 10
#     if imp_min not in ALLOWED_IMPACT_MINUTES:
#         imp_min = 10
#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         imp_thr = None  # brak progu
#     else:
#         try:
#             imp_thr = float(thr_raw)
#         except Exception:
#             imp_thr = None

#     sort_impact = int(request.args.get("imp_sort", 0) or 0)
#     imp_in_raw = (request.args.get("imp_in", "") or "").strip()
#     imp_in = set([p.strip().lower() for p in imp_in_raw.split(",") if p.strip()])  # np. {'up','down'}

#     df = TWEETS_DF.copy()

#     def _p(name):
#         try:
#             return int(request.args.get(name, 0) or 0)
#         except ValueError:
#             return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             df[col] = df[col].astype("boolean").fillna(False)

#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     if f_reply == 1:   df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1: df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:   df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- 1) Etykietowanie (nadaj lab_label/lab_pct) ---
#     if lab_enable == 1:
#         lab_pct, lab_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), lab_min)
#             lab_pct.append(v)
#             if v is None:
#                 lab_lbl.append("neutral")
#             elif v >= lab_thr:
#                 lab_lbl.append("up")
#             elif v <= -lab_thr:
#                 lab_lbl.append("down")
#             else:
#                 lab_lbl.append("neutral")
#         df["_lab_pct"] = lab_pct
#         df["_lab_label"] = lab_lbl
#     else:
#         df["_lab_pct"] = None
#         df["_lab_label"] = None

#     # --- 2) Filtrowanie (imp_min/imp_thr) + sort opcjonalnie ---
#     if imp_filter == 1:
#         imp_pct, imp_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), imp_min)
#             imp_pct.append(v)
#             if v is None:
#                 imp_lbl.append("neutral")
#             else:
#                 if imp_thr is None:
#                     # bez progu – tylko klasyfikuj po znaku
#                     if v > 0:   imp_lbl.append("up")
#                     elif v < 0: imp_lbl.append("down")
#                     else:       imp_lbl.append("neutral")
#                 else:
#                     if v >= imp_thr:        imp_lbl.append("up")
#                     elif v <= -imp_thr:     imp_lbl.append("down")
#                     else:                   imp_lbl.append("neutral")
#         df["_imp_pct"] = imp_pct
#         df["_imp_label"] = imp_lbl

#         # filtrowanie po etykietach (jeśli użytkownik coś zaznaczył)
#         if len(imp_in) > 0:
#             df = df[df["_imp_label"].isin(list(imp_in))]

#         if sort_impact == 1:
#             df = df.assign(_abs=df["_imp_pct"].abs().fillna(-1)) \
#                    .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#     else:
#         # brak „imp” – jeśli user zaznaczył etykiety, filtruj po lab_label (jeśli są)
#         if len(imp_in) > 0 and lab_enable == 1:
#             df = df[df["_lab_label"].isin(list(imp_in))]
#         df["_imp_pct"] = None
#         df["_imp_label"] = None

#         if sort_impact == 1 and lab_enable == 1:
#             df = df.assign(_abs=df["_lab_pct"].abs().fillna(-1)) \
#                    .sort_values(by=["_abs", "created_at"], ascending=[False, False])

#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),

#             # zwróć obie ścieżki, UI wybierze co pokazywać
#             "lab_label": (None if pd.isna(getattr(r, "_lab_label", None)) else getattr(r, "_lab_label", None)),
#             "lab_min": lab_min if lab_enable == 1 else None,
#             "lab_pct": (None if pd.isna(getattr(r, "_lab_pct", None)) else float(r._lab_pct) if r._lab_pct is not None else None),

#             "imp_label": (None if pd.isna(getattr(r, "_imp_label", None)) else getattr(r, "_imp_label", None)),
#             "imp_min": imp_min if imp_filter == 1 else None,
#             "imp_pct": (None if pd.isna(getattr(r, "_imp_pct", None)) else float(r._imp_pct) if r._imp_pct is not None else None)
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({
#         "items": items,
#         "page": page,
#         "per_page": per_page,
#         "total": int(total),
#         "years": years
#     })

# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     if fmt != "text":
#         return jsonify(payload)

#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     app.run(debug=True)



# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i overlayu
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo
# from bisect import bisect_left

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     """
#     Zamienia kolumnę czasu na tz-aware UTC.
#     - Jeśli wartości mają już strefę -> tylko konwersja do UTC.
#     - Jeśli są naive -> traktuj jako source_tz, potem do UTC.
#     Obsługuje DST.
#     """
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         return s.dt.tz_convert("UTC")
#     tz = ZoneInfo(source_tz)
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     # createdAt -> UTC
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     # --- FILTR zakresu czasowego ---
#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

#     # >>> tylko tweety z godzin 15:30–21:45 czasu PL (uwzględnia DST)
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue
#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
#     """Zwraca (df_window, used_start, reason) — reason in ["ok","fallback_next","no_data"]"""
#     if PRICES_DF.empty:
#         return PRICES_DF.copy(), start_dt_utc, "no_data"

#     end_dt = start_dt_utc + timedelta(minutes=minutes)
#     win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt)].copy()
#     if not win.empty:
#         return win, start_dt_utc, "ok"

#     ts_ns = PRICES_DF["datetime"].astype("int64").values
#     pos = bisect_left(ts_ns, int(start_dt_utc.value))
#     if pos < len(PRICES_DF):
#         new_start = PRICES_DF.iloc[pos]["datetime"]
#         new_end = new_start + timedelta(minutes=minutes)
#         win2 = PRICES_DF[(PRICES_DF["datetime"] >= new_start) & (PRICES_DF["datetime"] <= new_end)].copy()
#         if not win2.empty:
#             return win2, new_start, "fallback_next"

#     return PRICES_DF.iloc[0:0].copy(), start_dt_utc, "no_data"

# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     """Wycinek [start, end] bez fallbacku."""
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# # ===== Procentowe zmiany względem chwili tweeta =====
# def _minute_close_at(dt_utc: pd.Timestamp):
#     """Zwróć kurs (tu: open) w minucie dt_utc. Gdy brak – None."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"])

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     base = _minute_close_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_close_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)

# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#     })

# @app.route("/")
# def index():
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # --- parametry wpływu (domyślnie minuta=10, próg pusty => 0.0%) ---
#     try:
#         impact_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         impact_min = 10
#     if impact_min not in ALLOWED_IMPACT_MINUTES:
#         impact_min = 10

#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         impact_thr = 0.0
#     else:
#         try:
#             impact_thr = float(thr_raw)
#         except Exception:
#             impact_thr = 0.0  # fallback

#     impact_label = (request.args.get("imp_label", "all") or "all").lower()  # all|up|down|neutral
#     sort_impact = int(request.args.get("imp_sort", 0) or 0)  # 1=sortuj po |impact|, 0=nie sortuj
#     imp_enable  = int(request.args.get("imp_enable", 0) or 0)  # 1=licz, 0=nie licz (szybki start)

#     df = TWEETS_DF.copy()

#     def _p(name):
#         try:
#             return int(request.args.get(name, 0) or 0)
#         except ValueError:
#             return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             df[col] = df[col].astype("boolean").fillna(False)

#     # filtr rok
#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     # flagi
#     if f_reply == 1:   df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1: df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:   df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     # search
#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- LICZ IMPACT TYLKO, JEŚLI WŁĄCZONO (po Zastosuj) ---
#     need_impact = (imp_enable == 1)

#     if need_impact:
#         pct_vals, labels = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), impact_min)
#             pct_vals.append(v)
#             if v is None:
#                 labels.append("neutral")  # możesz zmienić na 'nodata' jeśli chcesz osobno to traktować
#             elif v >= impact_thr:
#                 labels.append("up")
#             elif v <= -impact_thr:
#                 labels.append("down")
#             else:
#                 labels.append("neutral")
#         df["_impact_pct"] = pct_vals
#         df["_impact_label"] = labels

#         # Filtrowanie po etykiecie (gdy wybrano up/down/neutral)
#         if impact_label in ("up", "down", "neutral") and impact_label != "all":
#             df = df[df["_impact_label"] == impact_label]

#         # Sort po bezwzględnej wartości % (NA->-1, by leciały na koniec)
#         if sort_impact == 1:
#             df = (
#                 df.assign(_abs=df["_impact_pct"].abs().fillna(-1))
#                   .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#             )
#     else:
#         # nie liczymy -> frontend nie pokazuje pigułki
#         df["_impact_pct"] = None
#         df["_impact_label"] = None

#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),
#             "impact": (None if pd.isna(getattr(r, "_impact_label", None)) else getattr(r, "_impact_label", None)),
#             "impact_min": impact_min,
#             "impact_pct": (None if pd.isna(getattr(r, "_impact_pct", None)) else float(r._impact_pct) if r._impact_pct is not None else None)
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({
#         "items": items,
#         "page": page,
#         "per_page": per_page,
#         "total": int(total),
#         "years": years
#     })

# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     """
#     Query params:
#       start   – unix seconds (UTC) chwili tweeta (wymagane)
#       minutes – ile minut PO tweecie (domyślnie 15)
#       pre     – ile minut PRZED tweetem (domyślnie 0; np. 10)
#       format  – "text" dla legacy listy minut; domyślnie JSON
#       grid    – "1" do zwrócenia siatki minutowej (overlay)
#     """
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     # --- siatka minutowa do overlay (grid=1) ---
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     # JSON domyślnie
#     if fmt != "text":
#         return jsonify(payload)

#     # Legacy: tekstowa lista minut (oddzielne nazwy, by nie kolidować z grid)
#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     app.run(debug=True)



# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i overlayu
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo
# from bisect import bisect_left

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     """
#     Zamienia kolumnę czasu na tz-aware UTC.
#     - Jeśli wartości mają już strefę -> tylko konwersja do UTC.
#     - Jeśli są naive -> traktuj jako source_tz, potem do UTC.
#     Obsługuje DST.
#     """
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         return s.dt.tz_convert("UTC")
#     tz = ZoneInfo(source_tz)
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     # createdAt -> UTC
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     # --- FILTR zakresu czasowego ---
#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)

#     # >>> tylko tweety z godzin 15:30–21:45 czasu PL (uwzględnia DST)
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue
#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
#     """Zwraca (df_window, used_start, reason) — reason in ["ok","fallback_next","no_data"]"""
#     if PRICES_DF.empty:
#         return PRICES_DF.copy(), start_dt_utc, "no_data"

#     end_dt = start_dt_utc + timedelta(minutes=minutes)
#     win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt)].copy()
#     if not win.empty:
#         return win, start_dt_utc, "ok"

#     ts_ns = PRICES_DF["datetime"].astype("int64").values
#     pos = bisect_left(ts_ns, int(start_dt_utc.value))
#     if pos < len(PRICES_DF):
#         new_start = PRICES_DF.iloc[pos]["datetime"]
#         new_end = new_start + timedelta(minutes=minutes)
#         win2 = PRICES_DF[(PRICES_DF["datetime"] >= new_start) & (PRICES_DF["datetime"] <= new_end)].copy()
#         if not win2.empty:
#             return win2, new_start, "fallback_next"

#     return PRICES_DF.iloc[0:0].copy(), start_dt_utc, "no_data"

# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     """Wycinek [start, end] bez fallbacku."""
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) & (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# # ===== Procentowe zmiany względem chwili tweeta =====
# def _minute_close_at(dt_utc: pd.Timestamp):
#     """Zwróć kurs (tu: open) w minucie dt_utc. Gdy brak – None."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"])

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     base = _minute_close_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_close_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)

# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#     })

# @app.route("/")
# def index():
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów ----
# @app.route("/api/tweets")
# def api_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#     # --- parametry wpływu (domyślnie minuta=10, próg pusty => 0.0%) ---
#     # minuta oceny
#     try:
#         impact_min = int(request.args.get("imp_min", 10))
#     except Exception:
#         impact_min = 10
#     if impact_min not in ALLOWED_IMPACT_MINUTES:
#         impact_min = 10

#     # próg: puste / brak => 0.0 (umożliwia filtrowanie bez wpisywania)
#     thr_raw = request.args.get("imp_thr", None)
#     if thr_raw is None or str(thr_raw).strip() == "":
#         impact_thr = 0.0
#     else:
#         try:
#             impact_thr = float(thr_raw)
#         except Exception:
#             impact_thr = 0.0  # bezpieczny fallback

#     impact_label = (request.args.get("imp_label", "all") or "all").lower()  # all|up|down|neutral
#     sort_impact = int(request.args.get("imp_sort", 0) or 0)  # 1=sortuj po |impact|, 0=nie sortuj


#     df = TWEETS_DF.copy()

#     def _p(name):
#         try:
#             return int(request.args.get(name, 0) or 0)
#         except ValueError:
#             return 0
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")

#     # df = TWEETS_DF.copy()
#     # # DEBUG / wydajność: ogranicz liczbę tweetów do np. 200, żeby nie zamulało
#     # df = df.head(200)


#     # # normalizacja flag -> bool (bez ostrzeżeń)
#     # for col in ("isReply", "isRetweet", "isQuote"):
#     #     if col in df.columns:
#     #         df[col] = df[col].fillna(False)
#     #         df[col] = df[col].astype(bool)
#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             # najpierw jawnie rzutujemy na pandas.BooleanDtype (obsługuje NA)
#             df[col] = df[col].astype("boolean").fillna(False)
#             # Dalej, jeśli gdziekolwiek wymagasz bool NumPy, rzutuj w momencie użycia:
#             # bool(r.isReply), itp. (tak już robisz przy budowie JSON-a)


#     # filtr rok
#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     # flagi
#     if f_reply == 1:   df = df[df["isReply"]]
#     elif f_reply == -1: df = df[~df["isReply"]]
#     if f_retweet == 1: df = df[df["isRetweet"]]
#     elif f_retweet == -1: df = df[~df["isRetweet"]]
#     if f_quote == 1:   df = df[df["isQuote"]]
#     elif f_quote == -1: df = df[~df["isQuote"]]

#     # search
#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- LICZ IMPACT TYLKO, JEŚLI POTRZEBA ---
#     need_impact = (impact_label in ("up", "down", "neutral") and impact_label != "all") or (sort_impact == 1)

#     if need_impact:
#         pct_vals, labels = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), impact_min)
#             pct_vals.append(v)
#             if v is None:
#                 labels.append("neutral")
#             elif v >= impact_thr:
#                 labels.append("up")
#             elif v <= -impact_thr:
#                 labels.append("down")
#             else:
#                 labels.append("neutral")
#         df["_impact_pct"] = pct_vals
#         df["_impact_label"] = labels

#         if impact_label in ("up", "down", "neutral") and impact_label != "all":
#             df = df[df["_impact_label"] == impact_label]

#         if sort_impact == 1:
#             df = df.assign(_abs=df["_impact_pct"].abs().fillna(-1)) \
#                 .sort_values(by=["_abs", "created_at"], ascending=[False, False])
#     else:
#         # Pola opcjonalne będą None (frontend to obsłuży)
#         df["_impact_pct"] = None
#         df["_impact_label"] = None
#     # # policz wpływ PRZED paginacją
#     # pct_vals, labels = [], []
#     # for ts in df["created_at"]:
#     #     v = impact_at_minute(pd.Timestamp(ts), impact_min)
#     #     pct_vals.append(v)
#     #     if v is None:
#     #         labels.append("neutral")
#     #     elif v >= impact_thr:
#     #         labels.append("up")
#     #     elif v <= -impact_thr:
#     #         labels.append("down")
#     #     else:
#     #         labels.append("neutral")
#     # df["_impact_pct"]   = pct_vals
#     # df["_impact_label"] = labels

#     # if impact_label in ("up", "down", "neutral"):
#     #     df = df[df["_impact_label"] == impact_label]

#     # if sort_impact == 1:
#     #     df = df.assign(_abs=df["_impact_pct"].abs().fillna(-1)).sort_values(
#     #         by=["_abs", "created_at"], ascending=[False, False]
#     #     )

#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         items.append({
#             "tweet_id": str(r.tweet_id),
#             "text": r.text,
#             "created_at_display": r.created_at_display,
#             "isReply": bool(r.isReply),
#             "isRetweet": bool(r.isRetweet),
#             "isQuote": bool(r.isQuote),
#             "year": int(r.created_at.year),
#             "impact": (None if pd.isna(getattr(r, "_impact_label", None)) else getattr(r, "_impact_label", None)),
#             "impact_min": impact_min,
#             "impact_pct": (None if pd.isna(getattr(r, "_impact_pct", None)) else float(r._impact_pct) if r._impact_pct is not None else None)

#             # "impact": getattr(r, "_impact_label", None),
#             # "impact_min": impact_min,
#             # "impact_pct": (None if pd.isna(getattr(r, "_impact_pct", None)) else float(r._impact_pct))
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)
#     return jsonify({
#         "items": items,
#         "page": page,
#         "per_page": per_page,
#         "total": int(total),
#         "years": years
#     })

# # ---- API: pojedynczy tweet ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# # ---- API: ceny / wykres ----
# @app.route("/api/price")
# def api_price():
#     """
#     Query params:
#       start   – unix seconds (UTC) chwili tweeta (wymagane)
#       minutes – ile minut PO tweecie (domyślnie 15)
#       pre     – ile minut PRZED tweetem (domyślnie 0; np. 10)
#       format  – "text" dla legacy listy minut; domyślnie JSON
#       grid    – "1" do zwrócenia siatki minutowej (overlay)
#     """
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))

#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         payload["pct_changes"] = {}

#     # --- siatka minutowa do overlay (grid=1) ---
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#         if df.empty:
#             aligned_close = [None] * len(idx)
#         else:
#             dfm = df.copy()
#             dfm["minute"] = dfm["datetime"].dt.floor("min")
#             dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#             aligned = dfm.reindex(idx)
#             aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#         payload["grid"] = {
#             "minute_ts": [int(ts.value // 10**9) for ts in idx],
#             "close": aligned_close,
#             "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#         }

#     # JSON domyślnie
#     if fmt != "text":
#         return jsonify(payload)

#     # Legacy: tekstowa lista minut (oddzielne nazwy, by nie kolidować z grid)
#     legacy_start = pd.Timestamp(win_start).floor("min")
#     legacy_end   = pd.Timestamp(win_end).floor("min")
#     legacy_idx = pd.date_range(start=legacy_start, end=legacy_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(legacy_idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# if __name__ == "__main__":
#     app.run(debug=True)



# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i wykresu 15 min
# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo
# from bisect import bisect_left

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# def to_utc(series, source_tz: str):
#     s = pd.to_datetime(series, errors="coerce", utc=False)
#     try:
#         has_tz = s.dt.tz is not None
#     except Exception:
#         has_tz = False

#     if has_tz:
#         return s.dt.tz_convert("UTC")

#     # Naive -> potraktuj jako lokalne Europe/Warsaw
#     tz = ZoneInfo(source_tz)
#     # pandas>=2.2: parametry dla DST; jeśli masz 2.1, usuń je
#     s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
#     return s.dt.tz_convert("UTC")

# # ===== Loader: Tweety =====
# def load_tweets(
#     csv_path: str = TWEETS_CSV,
#     prices_min: str = "2017-09-17 21:00:00+00:00",
#     prices_max: str = "2025-03-07 20:54:00+00:00"
# ) -> pd.DataFrame:
#     if not os.path.exists(csv_path):
#         print(f"[startup] Brak pliku tweetów: {csv_path}")
#         return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

#     df = pd.read_csv(csv_path, low_memory=False)
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     # createdAt -> UTC
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     # --- FILTR zakresu czasowego ---
#     prices_min = pd.to_datetime(prices_min, utc=True)
#     prices_max = pd.to_datetime(prices_max, utc=True)
#     df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)
#     # >>> tylko tweety z godzin 15:30–21:45 czasu PL (uwzględnia DST)
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 35))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 50)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]


# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     if not os.path.isdir(base_dir):
#         print(f"[startup] Brak katalogu cen: {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         print(f"[startup] Nie znaleziono CSV w {base_dir}")
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)

#             # wybierz kolumnę czasu
#             dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
#             if not dt_col:
#                 continue

#             def pick(col):
#                 if col in raw.columns: return raw[col]
#                 if col.capitalize() in raw.columns: return raw[col.capitalize()]
#                 if col.upper() in raw.columns: return raw[col.upper()]
#                 raise KeyError(col)
            
#             part = pd.DataFrame({
#                 "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),  # <--- TYLKO TA ZMIANA
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])

#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()

# # ===== Pomocnicze =====
# def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
#     """Zwraca (df_window, used_start, reason) — reason in ["ok","fallback_next","no_data"]"""
#     if PRICES_DF.empty:
#         return PRICES_DF.copy(), start_dt_utc, "no_data"

#     end_dt = start_dt_utc + timedelta(minutes=minutes)
#     win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
#                     (PRICES_DF["datetime"] <= end_dt)].copy()
#     if not win.empty:
#         return win, start_dt_utc, "ok"

#     # fallback: najbliższy punkt >= start
#     ts_ns = PRICES_DF["datetime"].astype("int64").values
#     pos = bisect_left(ts_ns, int(start_dt_utc.value))
#     if pos < len(PRICES_DF):
#         new_start = PRICES_DF.iloc[pos]["datetime"]
#         new_end = new_start + timedelta(minutes=minutes)
#         win2 = PRICES_DF[(PRICES_DF["datetime"] >= new_start) &
#                          (PRICES_DF["datetime"] <= new_end)].copy()
#         if not win2.empty:
#             return win2, new_start, "fallback_next"

#     return PRICES_DF.iloc[0:0].copy(), start_dt_utc, "no_data"

# # ===== NOWE: wycinek w sztywnych granicach (bez fallbacku) =====
# def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
#     """
#     Zwraca df z PRICES_DF dla [start_dt_utc, end_dt_utc] BEZ żadnego przesuwania.
#     """
#     if PRICES_DF.empty:
#         return PRICES_DF.iloc[0:0].copy()
#     return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
#                      (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# # ===== Procentowe zmiany względem chwili tweeta =====
# def _minute_close_at(dt_utc: pd.Timestamp):
#     """Zwróć kurs close w minucie dt_utc (ostatni tick w tej minucie). Gdy brak – None."""
#     if PRICES_DF.empty:
#         return None
#     minute = pd.Timestamp(dt_utc).floor("min")
#     dfm = PRICES_DF.copy()
#     dfm["minute"] = dfm["datetime"].dt.floor("min")
#     row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
#     return None if row.empty else float(row.iloc[0]["open"]) #wedlug mnie powinno byc w open

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     """
#     Zwraca słownik {minuty: %zmiana} liczony względem ceny w minucie tweeta.
#     Jeśli brak ceny w danej minucie – wartość to None.
#     """
#     base = _minute_close_at(start_dt_utc)
#     out = {}
#     for m in intervals:
#         price = _minute_close_at(start_dt_utc + pd.Timedelta(minutes=m))
#         if base is not None and price is not None:
#             out[m] = round((price - base) / base * 100, 2)
#         else:
#             out[m] = None
#     return out

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]

# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     """% zmiany po 'minute' minutach względem minuty tweeta; None gdy brak danych."""
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     d = percent_changes_from(dt_utc, intervals=(minute,))
#     return d.get(minute)


# # ===== Trasy =====
# @app.route("/health")
# def health():
#     return jsonify({
#         "tweets_rows": int(len(TWEETS_DF)),
#         "prices_rows": int(len(PRICES_DF)),
#         "tweets_min": str(TWEETS_DF["created_at"].min()) if len(TWEETS_DF) else None,
#         "tweets_max": str(TWEETS_DF["created_at"].max()) if len(TWEETS_DF) else None,
#         "prices_min": str(PRICES_DF["datetime"].min()) if len(PRICES_DF) else None,
#         "prices_max": str(PRICES_DF["datetime"].max()) if len(PRICES_DF) else None,
#     })


# @app.route("/")
# def index():
#     """Widok 2-kolumnowy: lewa – lista, prawa – szczegóły."""
#     # Na wejściu pokaż pierwszy tweet (jeśli jest), resztę JS dociągnie.
#     initial_id = None
#     if len(TWEETS_DF):
#         initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
#     return render_template("index.html", initial_id=initial_id)

# # ---- API: lista tweetów z filtrami + paginacja ----
# @app.route("/api/tweets")
# def api_tweets():
#     """
#     Query params:
#       page (int, default 1), per_page (int, default 20)
#       year (int albo 'all')
#       reply, retweet, quote: '1' włącza filtr 'tylko takie'; '0' ignoruje
#       q (szukaj w tekście – opcjonalnie)
#     """
#     page = int(request.args.get("page", 1))
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()

#      # nowe: parametry wpływu
#     impact_min = int(request.args.get("imp_min", 5))
#     if impact_min not in ALLOWED_IMPACT_MINUTES: impact_min = 5
#     try:
#         impact_thr = float(request.args.get("imp_thr", 0.5))
#     except Exception:
#         impact_thr = 0.5
#     impact_label = (request.args.get("imp_label", "all") or "all").lower()  # all|up|down|neutral
#     sort_impact = int(request.args.get("imp_sort", 0) or 0)  # 1=sortuj malejąco po |impact|


#     # bezpieczne parsowanie (-1/0/1)
#     def _p(name):
#         try:
#             return int(request.args.get(name, 0) or 0)
#         except ValueError:
#             return 0
        
#     f_reply   = _p("reply")
#     f_retweet = _p("retweet")
#     f_quote   = _p("quote")


#     df = TWEETS_DF.copy()

#      # --- NORMALIZACJA FLAG -> bool (kluczowe dla ~) ---
#     for col in ("isReply", "isRetweet", "isQuote"):
#         if col in df.columns:
#             # zamień NaN na False i rzutuj na bool (0/1/0.0/1.0 -> False/True)
#             df[col] = df[col].fillna(False).astype(bool)

#     # filtr rok
#     if year != "all":
#         try:
#             y = int(year)
#             df = df[df["created_at"].dt.year == y]
#         except Exception:
#             pass

#     # flagi
#     if f_reply == 1:
#         df = df[df["isReply"]]
#     elif f_reply == -1:
#         df = df[~df["isReply"]]

#     if f_retweet == 1:
#         df = df[df["isRetweet"]]
#     elif f_retweet == -1:
#         df = df[~df["isRetweet"]]

#     if f_quote == 1:
#         df = df[df["isQuote"]]
#     elif f_quote == -1:
#         df = df[~df["isQuote"]]

#     # prosty search w tekście
#     if q:
#         df = df[df["text"].str.contains(q, case=False, na=False)]

#     # --- nowy blok: policz % po imp_min i nadaj label
#     # robimy PRZED paginacją, żeby total był poprawny
#     pct_vals = []
#     labels = []
#     for ts in df["created_at"]:
#         v = impact_at_minute(pd.Timestamp(ts), impact_min)
#         pct_vals.append(v)
#         if v is None:
#             labels.append("neutral")  # brak danych traktujemy neutralnie
#         elif v >= impact_thr:
#             labels.append("up")
#         elif v <= -impact_thr:
#             labels.append("down")
#         else:
#             labels.append("neutral")
#     df["_impact_pct"] = pct_vals
#     df["_impact_label"] = labels

#     if impact_label in ("up","down","neutral"):
#         df = df[df["_impact_label"] == impact_label]

#     if sort_impact == 1:
#         df = df.assign(_abs=df["_impact_pct"].abs().fillna(-1)).sort_values(
#             by=["_abs","created_at"], ascending=[False, False]
#         )

#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    
#     items = []
#     for r in subset.itertuples(index=False):
#             items.append({
#                 "tweet_id": str(r.tweet_id),
#                 "text": r.text,
#                 "created_at_display": r.created_at_display,
#                 "isReply": bool(r.isReply),
#                 "isRetweet": bool(r.isRetweet),
#                 "isQuote": bool(r.isQuote),
#                 "year": int(r.created_at.year),
#                 # nowości
#                 "impact": getattr(r, "_impact_label", None),
#                 "impact_min": impact_min,
#                 "impact_pct": (None if pd.isna(getattr(r, "_impact_pct", None)) else float(r._impact_pct))
#             })
    

#     # lista dostępnych lat (do selecta)
#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)

#     return jsonify({
#         "items": items,
#         "page": page,
#         "per_page": per_page,
#         "total": int(total),
#         "years": years
#     })

# # ---- API: pojedynczy tweet (do prawej kolumny) ----
# @app.route("/api/tweet/<tweet_id>")
# def api_tweet(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0]
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#         .strftime("%Y-%m-%d %H:%M:%S %Z")
#     return jsonify({
#         "tweet_id": str(t["tweet_id"]),
#         "text": t["text"],
#         "isReply": bool(t["isReply"]),
#         "isRetweet": bool(t["isRetweet"]),
#         "isQuote": bool(t["isQuote"]),
#         "created_ts": created_ts,
#         "created_display": created_display
#     })

# @app.route("/api/price")
# def api_price():
#     """
#     Query params:
#       start   – unix seconds (UTC) chwili tweeta (wymagane)
#       minutes – ile minut PO tweecie (domyślnie 15)
#       pre     – ile minut PRZED tweetem (domyślnie 0; np. 10)
#       format  – "text" dla legacy listy minut; domyślnie JSON
#     """
#     start_unix = (request.args.get("start", "") or "").strip()
#     fmt = (request.args.get("format", "") or "").lower()

#     # minutes
#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15
#     minutes = max(1, min(minutes, 24*60))  # bezpieczny limit

#     # pre (minuty przed)
#     try:
#         pre = int(request.args.get("pre", 0))
#     except Exception:
#         pre = 0
#     pre = max(0, min(pre, 120))  # np. pozwól do 120 min wstecz

#     # brak startu
#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     # parsowanie startu
#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text":
#             return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     # --- SZTYWNE okno: [start - pre, start + minutes] ---
#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = slice_prices_between(win_start, win_end)
#     reason = "ok" if not df.empty else "no_data"

#     # punkty do wykresu
#     points = [
#         {
#             "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#             "open": float(r["open"]),
#             "high": float(r["high"]),
#             "low":  float(r["low"]),
#             "close": float(r["close"]),
#         }
#         for _, r in df.iterrows()
#     ]

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),  # NIE PRZESUWAMY
#         # pomoc do zablokowania zakresu osi X w frontendzie
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#     }

#     # % zmiany względem minuty tweeta (jeśli brak ceny w minucie tweeta, będzie None)
#     try:
#         payload["pct_changes"] = percent_changes_from(start_dt)
#     except Exception:
#         # niech API się nie wywala nawet, jeśli helpera brak
#         payload["pct_changes"] = {}

#      # --- NOWE: siatka minutowa do overlay (grid=1)
#     if request.args.get("grid", "0") == "1":
#         grid_start = pd.Timestamp(win_start).floor("min")
#         grid_end   = pd.Timestamp(win_end).floor("min")
#         idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#     if df.empty:
#         aligned_close = [None] * len(idx)
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = dfm.sort_values("datetime").groupby("minute").last()[["close"]]
#         aligned = dfm.reindex(idx)
#         aligned_close = [None if pd.isna(v) else float(v) for v in aligned["close"].values]

#     payload["grid"] = {
#         "minute_ts": [int(ts.value // 10**9) for ts in idx],
#         "close": aligned_close,
#         "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
#     }

#     # --- JSON domyślnie ---
#     if fmt != "text":
#         return jsonify(payload)

   


#     # --- Legacy: wersja tekstowa (lista minut) dla kompatybilności ---
#     # grid_start = pd.Timestamp(win_start).floor("min")
#     # grid_end   = pd.Timestamp(win_end).floor("min")
#     # idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

#     aligned = dfm.reindex(idx)

#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = (row["close"] if isinstance(row, pd.Series) else None)
#         if pd.isna(val):
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = [
#         "Ceny w oknie minutowym:",
#         f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}  →  "
#         f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
#         f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
#     ]
#     if reason == "no_data":
#         header.append("Brak danych cenowych w tym oknie.")

#     body = "\n".join(header + [""] + lines)
#     return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})


# if __name__ == "__main__":
#     app.run(debug=True)

