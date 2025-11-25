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
PREDICTIONS_CSV = os.path.join(BASE_DIR, "data", "finbert_test_predictions_3m.csv")

PRE_MINUTE = 8
PRE_THRESHOLD = 1.0  # %

ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)


def to_utc(series, source_tz: str):
    s = pd.to_datetime(series, errors="coerce", utc=False)
    try:
        has_tz = s.dt.tz is not None
    except Exception:
        has_tz = False
    if has_tz:
        return s.dt.tz_convert("UTC")
    tz = ZoneInfo(source_tz)
    s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    return s.dt.tz_convert("UTC")


# ===== Loader Tweety =====
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

    # tylko 15:30–21:45 czasu PL
    _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
    mask = (
        ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
        ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
    )
    df = df[mask].reset_index(drop=True)

    return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]


# ===== Loader Ceny =====
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
                if col in raw.columns:
                    return raw[col]
                if col.capitalize() in raw.columns:
                    return raw[col.capitalize()]
                if col.upper() in raw.columns:
                    return raw[col.upper()]
                raise KeyError(col)

            part = pd.DataFrame({
                "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
                "open":  pd.to_numeric(pick("open"), errors="coerce"),
                "high":  pd.to_numeric(pick("high"), errors="coerce"),
                "low":   pd.to_numeric(pick("low"),  errors="coerce"),
                "close": pd.to_numeric(pick("close"), errors="coerce"),
            }).dropna(subset=["datetime"])
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


# ===== Loader predykcji FinBERT (zbiór testowy) =====
def load_predictions(csv_path: str = PREDICTIONS_CSV) -> pd.DataFrame:
    """
    Wczytuje finbert_test_predictions_3m.csv z Colaba.
    Zakładamy, że zawiera m.in.:
    tweet_id, text, created_at_local, after_3m, label_str, pred_label_str,
    trade_decision, pnl_model, llm_* kolumny, itp.
    """
    if not os.path.exists(csv_path):
        print(f"[startup] Brak pliku predykcji: {csv_path}")
        return pd.DataFrame()

    df = pd.read_csv(csv_path, low_memory=False)

    if "tweet_id" not in df.columns:
        print("[startup] Plik predykcji nie ma kolumny 'tweet_id'.")
        return pd.DataFrame()

    # dopilnujmy typu
    df["tweet_id"] = df["tweet_id"].astype(str)

    # created_at_local → UTC, jeśli nie ma kolumny created_at
    if "created_at" not in df.columns and "created_at_local" in df.columns:
        dt_local = pd.to_datetime(df["created_at_local"], errors="coerce")
        dt_local = dt_local.dt.tz_localize(DISPLAY_TZ, nonexistent="shift_forward", ambiguous="NaT")
        df["created_at"] = dt_local.dt.tz_convert("UTC")

    # pozbądź się duplikatów tweet_id (na wszelki wypadek)
    df = df.drop_duplicates(subset=["tweet_id"])

    return df


# ===== Bufory =====
MIN_IDX = None
R_MINUTE = None
LOGF_PREFIX = None
MINUTE_TO_POS = None


def _build_minute_buffers(prices_df: pd.DataFrame):
    global MIN_IDX, R_MINUTE, LOGF_PREFIX, MINUTE_TO_POS
    if prices_df.empty or "datetime" not in prices_df.columns:
        MIN_IDX = pd.DatetimeIndex([], tz="UTC")
        R_MINUTE = np.zeros((0,), dtype=float)
        LOGF_PREFIX = np.zeros((1,), dtype=float)
        MINUTE_TO_POS = {}
        return

    df = prices_df.copy()
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

    cand_cols = ["% change", "%change", "pct change", "pct_change"]
    col = next((c for c in cand_cols if c in df.columns), None)
    if col is not None:
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

    MIN_IDX = pd.DatetimeIndex(df["minute"].values)
    one_plus = np.clip(1.0 + r, 1e-9, None)
    logf = np.log(one_plus)
    LOGF_PREFIX = np.concatenate([[0.0], np.cumsum(logf)])
    MINUTE_TO_POS = {int(ts.value): i for i, ts in enumerate(MIN_IDX)}
    R_MINUTE = r


def _pct_change_from_base(pos: int, k: int) -> float | None:
    j = pos + k
    if pos < 0 or j > len(LOGF_PREFIX) - 1:
        return None
    return float(np.exp(LOGF_PREFIX[j] - LOGF_PREFIX[pos]) - 1.0)


def _pct_series_from_base(pos: int, horizons=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 30, 60)):
    return {k: _pct_change_from_base(pos, k) for k in horizons}


def percent_changes_from(start_dt_utc: pd.Timestamp,
                         intervals=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 30, 60)):
    if MIN_IDX is None or len(MIN_IDX) == 0:
        return {k: None for k in intervals}
    minute = pd.Timestamp(start_dt_utc).floor("min")
    pos = MINUTE_TO_POS.get(int(minute.value), None)
    if pos is None:
        return {k: None for k in intervals}
    return _pct_series_from_base(pos, intervals)


def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
    if minute not in ALLOWED_IMPACT_MINUTES:
        return None
    if MIN_IDX is None or len(MIN_IDX) == 0:
        return None
    pos = MINUTE_TO_POS.get(int(pd.Timestamp(dt_utc).floor("min").value), None)
    if pos is None:
        return None
    return _pct_change_from_base(pos, minute)


def _label_for_change(val: float | None, thr: float) -> str:
    if val is None:
        return "neutral"
    if val >= thr / 100.0:
        return "up"
    if val <= -thr / 100.0:
        return "down"
    return "neutral"


# ===== Init =====
TWEETS_DF = load_tweets()
PRICES_DF = load_prices_from_dir()
_build_minute_buffers(PRICES_DF)


def precompute_labels(df: pd.DataFrame, minute: int = PRE_MINUTE, thr: float = PRE_THRESHOLD) -> pd.DataFrame:
    print(f"[precompute] Liczę etykiety bazowe: m={minute}, próg={thr}%  (wiersze: {len(df)})")
    pct, lab = [], []
    for ts in df["created_at"]:
        v = impact_at_minute(pd.Timestamp(ts), minute)
        pct.append(None if v is None else round(100.0 * v, 4))
        lab.append(_label_for_change(v, thr))
    out = df.copy()
    out["pre_min"] = int(minute)
    out["pre_pct"] = pct
    out["pre_label"] = lab
    out["_lab_min"] = out["pre_min"]
    out["_lab_pct"] = out["pre_pct"]
    out["_lab_label"] = out["pre_label"]
    return out


if not TWEETS_DF.empty:
    TWEETS_DF = precompute_labels(TWEETS_DF, PRE_MINUTE, PRE_THRESHOLD)

# ===== Predykcje FinBERT – merge z TWEETS_DF =====
ML_PRED_FULL = load_predictions()

if not TWEETS_DF.empty:
    TWEETS_DF["tweet_id"] = TWEETS_DF["tweet_id"].astype(str)

if not TWEETS_DF.empty and not ML_PRED_FULL.empty:
    # wersja do merge – nie nadpisujemy text/created_at z tweetów
    merge_cols_drop = [c for c in ["text", "created_at"] if c in ML_PRED_FULL.columns]
    ML_FOR_MERGE = ML_PRED_FULL.drop(columns=merge_cols_drop, errors="ignore").copy()

    TWEETS_DF = TWEETS_DF.merge(
        ML_FOR_MERGE,
        on="tweet_id",
        how="left",
        suffixes=("", "_ml"),
    )

    # flaga: czy ten tweet jest w zbiorze testowym modelu
    TWEETS_DF["is_ml_test"] = TWEETS_DF["pred_label_str"].notna()
else:
    if not TWEETS_DF.empty:
        TWEETS_DF["is_ml_test"] = False


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
    imp_sort = int(request.args.get("imp_sort", 0) or 0)

    imp_filter = int(request.args.get("imp_filter", 0) or 0)
    try:
        imp_min = int(request.args.get("imp_min", PRE_MINUTE))
    except Exception:
        imp_min = PRE_MINUTE
    if imp_min not in ALLOWED_IMPACT_MINUTES:
        imp_min = PRE_MINUTE

    thr_raw = (request.args.get("imp_thr", "") or "").strip()
    imp_thr = None if thr_raw == "" else float(thr_raw)  # w %
    ml_test = int(request.args.get("ml_test", 0) or 0)

    df = TWEETS_DF.copy()

    def _p(n):
        try:
            return int(request.args.get(n, 0) or 0)
        except ValueError:
            return 0

    f_reply = _p("reply")
    f_retweet = _p("retweet")
    f_quote = _p("quote")

    for col in ("isReply", "isRetweet", "isQuote"):
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False)

    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    if f_reply == 1:
        df = df[df["isReply"]]
    elif f_reply == -1:
        df = df[~df["isReply"]]
    if f_retweet == 1:
        df = df[df["isRetweet"]]
    elif f_retweet == -1:
        df = df[~df["isRetweet"]]
    if f_quote == 1:
        df = df[df["isQuote"]]
    elif f_quote == -1:
        df = df[~df["isQuote"]]

    if q:
        df = df[df["text"].str.contains(q, case=False, na=False)]

    # filtr: tylko tweety użyte w teście FinBERT
    if ml_test == 1 and "is_ml_test" in df.columns:
        df = df[df["is_ml_test"]]

    # licz w locie imp_* jeśli włączony filtr/etykietowanie
    if imp_filter == 1:
        imp_pct, imp_lbl = [], []
        for ts in df["created_at"]:
            v = impact_at_minute(pd.Timestamp(ts), imp_min)
            pct = None if v is None else (100.0 * v)
            imp_pct.append(None if pct is None else round(pct, 4))
            if v is None:
                imp_lbl.append("neutral")
            else:
                if imp_thr is None:
                    imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
                else:
                    thr = imp_thr / 100.0
                    imp_lbl.append("up" if v >= thr else "down" if v <= -thr else "neutral")
        df["_imp_pct"] = imp_pct
        df["_imp_label"] = imp_lbl
    else:
        df["_imp_pct"] = None
        df["_imp_label"] = None

    # filtr etykiety
    if label in ("up", "down", "neutral"):
        if imp_filter == 1:
            if "_imp_label" in df.columns:
                df = df[df["_imp_label"] == label]
        else:
            if "pre_label" in df.columns:
                df = df[df["pre_label"] == label]

    # sort globalny przed paginacją (top up / down)
    sort_col = None
    if imp_sort == 1 and label in ("up", "down"):
        if imp_filter == 1 and "_imp_pct" in df.columns:
            sort_col = "_imp_pct"
        elif "pre_pct" in df.columns:
            sort_col = "pre_pct"
        if sort_col is not None:
            asc = (label == "down")   # dla "down" rosnąco (bardziej ujemne na górze)
            df = df.sort_values(sort_col, ascending=asc, na_position="last").reset_index(drop=True)
            df["__rank"] = df.index + 1
    else:
        df = df.reset_index(drop=True)
        df["__rank"] = pd.NA

    # statystyki (przed paginacją)
    if imp_filter == 1 and "_imp_label" in df.columns:
        col_lbl = "_imp_label"
        col_pct = "_imp_pct"
        minute_used = imp_min
        threshold_used = imp_thr
        mode = "imp"
    else:
        col_lbl = "pre_label" if "pre_label" in df.columns else None
        col_pct = "pre_pct" if "pre_pct" in df.columns else None
        minute_used = int(PRE_MINUTE)
        threshold_used = PRE_THRESHOLD
        mode = "pre"

    stats = {
        "n": int(len(df)),
        "mode": mode,
        "minute": minute_used,
        "threshold": threshold_used,
        "label_filter": (label if label in ("up", "down", "neutral") else "all")
    }
    if col_lbl is not None and col_lbl in df.columns:
        stats.update({
            "n_up": int((df[col_lbl] == "up").sum()),
            "n_down": int((df[col_lbl] == "down").sum()),
            "n_neutral": int((df[col_lbl] == "neutral").sum()),
        })
    if col_pct is not None and col_pct in df.columns:
        s = pd.to_numeric(df[col_pct], errors="coerce").dropna()
        stats.update({
            "pct_min": (None if s.empty else float(round(s.min(), 4))),
            "pct_max": (None if s.empty else float(round(s.max(), 4))),
            "pct_mean": (None if s.empty else float(round(s.mean(), 4))),
            "pct_median": (None if s.empty else float(round(s.median(), 4))),
        })

    total = len(df)
    start = (page - 1) * per_page
    end = start + per_page
    subset = df.iloc[start:end].copy()

    subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ) \
        .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    items = []
    for r in subset.itertuples(index=False):
        txt = getattr(r, "text", "")
        created_display = getattr(r, "created_at_display", "")
        is_reply = bool(getattr(r, "isReply", False))
        is_retweet = bool(getattr(r, "isRetweet", False))
        is_quote = bool(getattr(r, "isQuote", False))

        pre_label = getattr(r, "pre_label", None)
        pre_min = getattr(r, "pre_min", None)
        pre_pct = getattr(r, "pre_pct", None)
        pre_pct = (None if pre_pct is None or (isinstance(pre_pct, float) and pd.isna(pre_pct)) else float(pre_pct))

        lab_label = getattr(r, "_lab_label", pre_label)
        lab_min = getattr(r, "_lab_min", pre_min)
        lab_pct = getattr(r, "_lab_pct", pre_pct)
        lab_pct = (None if lab_pct is None or (isinstance(lab_pct, float) and pd.isna(lab_pct)) else float(lab_pct))

        imp_lbl = getattr(r, "_imp_label", None)
        imp_pct = getattr(r, "_imp_pct", None)
        imp_pct = (None if imp_pct is None or (isinstance(imp_pct, float) and pd.isna(imp_pct)) else float(imp_pct))
        imp_min_out = imp_min if imp_filter == 1 else None

        rank_val = getattr(r, "__rank", None)

        is_ml_test = bool(getattr(r, "is_ml_test", False))
        ml_pred_label = getattr(r, "pred_label_str", None) if is_ml_test else None

        items.append({
            "tweet_id": str(getattr(r, "tweet_id", "")),
            "text": txt,
            "created_at_display": created_display,
            "isReply": is_reply,
            "isRetweet": is_retweet,
            "isQuote": is_quote,

            "pre_label": pre_label,
            "pre_min": (int(pre_min) if pre_min is not None else None),
            "pre_pct": pre_pct,

            "lab_label": lab_label,
            "lab_min": (int(lab_min) if lab_min is not None else None),
            "lab_pct": lab_pct,

            "imp_label": imp_lbl,
            "imp_min": imp_min_out,
            "imp_pct": imp_pct,

            "rank": (int(rank_val) if rank_val is not None and pd.notna(rank_val) else None),

            # [ML]
            "is_ml_test": is_ml_test,
            "ml_pred_label": ml_pred_label,
        })

    years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True) if len(TWEETS_DF) else []
    return jsonify({
        "items": items,
        "page": page, "per_page": per_page, "total": int(total),
        "years": years,
        "stats": stats
    })


# ---- API: pojedynczy tweet ----
@app.route("/api/tweet/<tweet_id>")
def api_tweet(tweet_id):
    row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
    if row.empty:
        abort(404)

    t = row.iloc[0]
    created_ts = int(pd.Timestamp(t["created_at"]).timestamp())
    created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ) \
        .strftime("%Y-%m-%d %H:%M:%S %Z")

    res = {
        "tweet_id": str(t["tweet_id"]),
        "text": t["text"],
        "isReply": bool(t["isReply"]),
        "isRetweet": bool(t["isRetweet"]),
        "isQuote": bool(t["isQuote"]),
        "created_ts": created_ts,
        "created_display": created_display,
    }

    # [ML] – dodatkowe informacje dla tweetów z testu modelu
    is_ml_test = bool(t.get("is_ml_test", False)) if isinstance(t, pd.Series) else False
    res["is_ml_test"] = is_ml_test

    if is_ml_test:
        def _val(col):
            if col not in TWEETS_DF.columns:
                return None
            v = t[col]
            if isinstance(v, (float, np.floating)) and pd.isna(v):
                return None
            return v

        for col in [
            "combined_quote_info",
            "llm_about_tsla",
            "llm_sent_tweet",
            "llm_sent_quote",
            "llm_stance",
            "llm_impact",
            "llm_drivers",
            "llm_sarcasm",
            "llm_conf",
            "llm_rationale",
            "avg1_3",
            "after_3m",
            "label_str",        # prawdziwy kierunek z avg1_3
            "pred_label_str",   # predykcja modelu
            "trade_decision",
            "pnl_model",
        ]:
            res[col] = _val(col)

    return jsonify(res)


# ---- API: ceny / wykres ----
@app.route("/api/price")
def api_price():
    start_unix = (request.args.get("start", "") or "").strip()
    fmt = (request.args.get("format", "") or "").lower()

    try:
        minutes = int(request.args.get("minutes", 15))
    except Exception:
        minutes = 15
    minutes = max(1, min(minutes, 24 * 60))

    try:
        pre = int(request.args.get("pre", 0))
    except Exception:
        pre = 0
    pre = max(0, min(pre, 120))

    if not start_unix:
        resp = {"points": [], "reason": "no_start"}
        if fmt != "text":
            return jsonify(resp)
        return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    try:
        start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
    except Exception:
        resp = {"points": [], "reason": "bad_start"}
        if fmt != "text":
            return jsonify(resp)
        return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    win_start = start_dt - pd.Timedelta(minutes=pre)
    win_end = start_dt + pd.Timedelta(minutes=minutes)
    df = PRICES_DF[(PRICES_DF["datetime"] >= win_start) & (PRICES_DF["datetime"] <= win_end)].copy()
    reason = "ok" if not df.empty else "no_data"

    points = [{
        "t": int(pd.Timestamp(r["datetime"]).value // 10 ** 9),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
    } for _, r in df.iterrows()]

    pc_raw = percent_changes_from(start_dt)
    pct_changes = {k: (None if v is None else round(100.0 * v, 2)) for k, v in pc_raw.items()}

    payload = {
        "points": points,
        "reason": reason,
        "requested_start": int(pd.Timestamp(start_dt).value // 10 ** 9),
        "used_start": int(pd.Timestamp(start_dt).value // 10 ** 9),
        "x_start": int(pd.Timestamp(win_start).value // 10 ** 9),
        "x_end": int(pd.Timestamp(win_end).value // 10 ** 9),
        "pct_changes": pct_changes
    }

    if request.args.get("grid", "0") == "1":
        grid_start = pd.Timestamp(win_start).floor("min")
        grid_end = pd.Timestamp(win_end).floor("min")
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
            "minute_ts": [int(ts.value // 10 ** 9) for ts in idx],
            "close": aligned_close,
            "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10 ** 9)
        }

    if fmt != "text":
        return jsonify(payload)

    legacy_start = pd.Timestamp(win_start).floor("min")
    legacy_end = pd.Timestamp(win_end).floor("min")
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


# ===== BACKTEST na podstawie finbert_test_predictions_3m.csv =====
@app.route("/backtest")
def backtest_page():
    if ML_PRED_FULL is None or ML_PRED_FULL.empty:
        return render_template(
            "backtest.html",
            error="Brak pliku z predykcjami (finbert_test_predictions_3m.csv).",
            trades=[],
            summary=None,
            min_date=None,
            max_date=None,
            start=None,
            end=None,
            budget=None,
        )

    df = ML_PRED_FULL.copy()

    # upewnij się, że mamy created_at w UTC
    if "created_at" not in df.columns:
        if "created_at_local" in df.columns:
            dt_local = pd.to_datetime(df["created_at_local"], errors="coerce")
            dt_local = dt_local.dt.tz_localize(DISPLAY_TZ, nonexistent="shift_forward", ambiguous="NaT")
            df["created_at"] = dt_local.dt.tz_convert("UTC")
        else:
            df["created_at"] = pd.NaT

    df = df.dropna(subset=["created_at"]).sort_values("created_at")

    if df.empty:
        return render_template(
            "backtest.html",
            error="Brak poprawnych dat w pliku predykcji.",
            trades=[],
            summary=None,
            min_date=None,
            max_date=None,
            start=None,
            end=None,
            budget=None,
        )

    min_dt = df["created_at"].min()
    max_dt = df["created_at"].max()
    min_date_str = min_dt.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d")
    max_date_str = max_dt.tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d")

    # parametry z formularza
    start_str = request.args.get("start", min_date_str)
    end_str = request.args.get("end", max_date_str)
    budget_str = request.args.get("budget", "10000")

    try:
        initial_budget = float(budget_str)
    except Exception:
        initial_budget = 10000.0

    try:
        start_dt = pd.to_datetime(start_str).tz_localize(DISPLAY_TZ).tz_convert("UTC")
        end_dt = pd.to_datetime(end_str).tz_localize(DISPLAY_TZ).tz_convert("UTC") + pd.Timedelta(days=1)
    except Exception:
        start_dt = min_dt
        end_dt = max_dt + pd.Timedelta(days=1)

    mask = (df["created_at"] >= start_dt) & (df["created_at"] < end_dt)
    df_period = df[mask].sort_values("created_at")

    trades = []
    capital = initial_budget
    ret_col = "after_3m"

    for _, r in df_period.iterrows():
        decision = str(r.get("trade_decision", "hold") or "hold")
        if decision not in ("buy", "sell"):
            continue

        raw_ret = r.get(ret_col, np.nan)
        if pd.isna(raw_ret):
            continue

        # raw_ret jest w %, np. 1.23 => 1.23% => 0.0123
        frac = float(raw_ret) / 100.0
        pnl_frac = frac if decision == "buy" else -frac

        capital_before = capital
        capital_after = capital_before * (1.0 + pnl_frac)
        capital = capital_after

        trades.append({
            "tweet_id": str(r.get("tweet_id")),
            "created_at": r["created_at"].tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "text": (str(r.get("text") or "")[:180]),
            "decision": decision,
            "raw_ret": float(raw_ret),
            "pnl_frac": pnl_frac,
            "capital_before": capital_before,
            "capital_after": capital_after,
            "pred_label": r.get("pred_label_str"),
            "true_label": r.get("label_str"),
        })

    summary = None
    if trades:
        summary = {
            "initial": initial_budget,
            "final": capital,
            "n_trades": len(trades),
            "n_buy": sum(1 for t in trades if t["decision"] == "buy"),
            "n_sell": sum(1 for t in trades if t["decision"] == "sell"),
            "return_pct": (capital / initial_budget - 1.0) * 100.0 if initial_budget > 0 else None,
            "start_str": start_str,
            "end_str": end_str,
        }

    return render_template(
        "backtest.html",
        error=None,
        trades=trades,
        summary=summary,
        min_date=min_date_str,
        max_date=max_date_str,
        start=start_str,
        end=end_str,
        budget=initial_budget,
    )


if __name__ == "__main__":
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.run(debug=True)
