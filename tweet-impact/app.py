# from flask import Flask, render_template, request, jsonify, abort
# import os, glob
# import pandas as pd
# import numpy as np
# from zoneinfo import ZoneInfo

# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
# PRICES_SOURCE_TZ = "Europe/Warsaw"

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# PRE_MINUTE = 8
# PRE_THRESHOLD = 1.0  # %

# ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
# H_MAX = 60

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

# # ===== Loader Tweety =====
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
#     df["text"] = (df["fullText"] if "fullText" in df.columns else df.get("text")).fillna("")

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

#     # tylko 15:30–21:45 czasu PL
#     _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
#     mask = (
#         ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 30))) &
#         ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 45)))
#     )
#     df = df[mask].reset_index(drop=True)

#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# # ===== Loader Ceny =====
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
#             for cand in ["% change", "%change", "pct change", "pct_change"]:
#                 if cand in raw.columns:
#                     part[cand] = pd.to_numeric(raw[cand], errors="coerce")
#             frames.append(part)
#         except Exception as e:
#             print(f"[prices] pomijam {path}: {e}")
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Bufory =====
# MIN_IDX = None
# R_MINUTE = None
# LOGF_PREFIX = None
# MINUTE_TO_POS = None

# def _build_minute_buffers(prices_df: pd.DataFrame):
#     global MIN_IDX, R_MINUTE, LOGF_PREFIX, MINUTE_TO_POS
#     if prices_df.empty or "datetime" not in prices_df.columns:
#         MIN_IDX = pd.DatetimeIndex([], tz="UTC")
#         R_MINUTE = np.zeros((0,), dtype=float)
#         LOGF_PREFIX = np.zeros((1,), dtype=float)
#         MINUTE_TO_POS = {}
#         return

#     df = prices_df.copy()
#     dt = pd.to_datetime(df["datetime"], errors="coerce", utc=False)
#     try:
#         has_tz = dt.dt.tz is not None
#     except Exception:
#         has_tz = False
#     if has_tz:
#         dt = dt.dt.tz_convert("UTC")
#     else:
#         dt = dt.dt.tz_localize("UTC")

#     df["minute"] = dt.dt.floor("min")
#     df = df.sort_values("minute").drop_duplicates(subset=["minute"], keep="last").dropna(subset=["minute"])

#     cand_cols = ["% change", "%change", "pct change", "pct_change"]
#     col = next((c for c in cand_cols if c in df.columns), None)
#     if col is not None:
#         r = pd.to_numeric(df[col], errors="coerce").fillna(0.0).to_numpy(dtype=float) / 100.0
#     else:
#         o = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
#         r = np.zeros_like(o)
#         if o.size >= 2:
#             prev = o[:-1]; cur = o[1:]
#             rr = np.zeros_like(cur)
#             mask = (prev > 0) & np.isfinite(prev) & np.isfinite(cur)
#             rr[mask] = (cur[mask] / prev[mask]) - 1.0
#             r[1:] = rr

#     MIN_IDX = pd.DatetimeIndex(df["minute"].values)
#     one_plus = np.clip(1.0 + r, 1e-9, None)
#     logf = np.log(one_plus)
#     LOGF_PREFIX = np.concatenate([[0.0], np.cumsum(logf)])
#     MINUTE_TO_POS = {int(ts.value): i for i, ts in enumerate(MIN_IDX)}
#     R_MINUTE = r

# def _pct_change_from_base(pos: int, k: int) -> float | None:
#     j = pos + k
#     if pos < 0 or j > len(LOGF_PREFIX) - 1:
#         return None
#     return float(np.exp(LOGF_PREFIX[j] - LOGF_PREFIX[pos]) - 1.0)

# def _pct_series_from_base(pos: int, horizons=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     return {k: _pct_change_from_base(pos, k) for k in horizons}

# def percent_changes_from(start_dt_utc: pd.Timestamp,
#                          intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
#     if MIN_IDX is None or len(MIN_IDX) == 0:
#         return {k: None for k in intervals}
#     minute = pd.Timestamp(start_dt_utc).floor("min")
#     pos = MINUTE_TO_POS.get(int(minute.value), None)
#     if pos is None:
#         return {k: None for k in intervals}
#     return _pct_series_from_base(pos, intervals)

# def impact_at_minute(dt_utc: pd.Timestamp, minute: int):
#     if minute not in ALLOWED_IMPACT_MINUTES:
#         return None
#     if MIN_IDX is None or len(MIN_IDX) == 0:
#         return None
#     pos = MINUTE_TO_POS.get(int(pd.Timestamp(dt_utc).floor("min").value), None)
#     if pos is None:
#         return None
#     return _pct_change_from_base(pos, minute)

# def _label_for_change(val: float | None, thr: float) -> str:
#     if val is None:  return "neutral"
#     if val >= thr/100.0:   return "up"
#     if val <= -thr/100.0:  return "down"
#     return "neutral"

# # ===== Init =====
# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices_from_dir()
# _build_minute_buffers(PRICES_DF)

# def precompute_labels(df: pd.DataFrame, minute: int = PRE_MINUTE, thr: float = PRE_THRESHOLD) -> pd.DataFrame:
#     print(f"[precompute] Liczę etykiety bazowe: m={minute}, próg={thr}%  (wiersze: {len(df)})")
#     pct, lab = [], []
#     for ts in df["created_at"]:
#         v = impact_at_minute(pd.Timestamp(ts), minute)
#         pct.append(None if v is None else round(100.0 * v, 4))
#         lab.append(_label_for_change(v, thr))
#     out = df.copy()
#     out["pre_min"]   = int(minute)
#     out["pre_pct"]   = pct
#     out["pre_label"] = lab
#     out["_lab_min"]   = out["pre_min"]
#     out["_lab_pct"]   = out["pre_pct"]
#     out["_lab_label"] = out["pre_label"]
#     return out

# if not TWEETS_DF.empty:
#     TWEETS_DF = precompute_labels(TWEETS_DF, PRE_MINUTE, PRE_THRESHOLD)

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
#     per_page = min(max(int(request.args.get("per_page", 20)), 5), 500)
#     year = request.args.get("year", "all")
#     q = (request.args.get("q") or "").strip()
#     label = (request.args.get("label", "all") or "all").lower()
#     imp_sort = int(request.args.get("imp_sort", 0) or 0)

#     imp_filter = int(request.args.get("imp_filter", 0) or 0)
#     try:
#         imp_min = int(request.args.get("imp_min", PRE_MINUTE))
#     except Exception:
#         imp_min = PRE_MINUTE
#     if imp_min not in ALLOWED_IMPACT_MINUTES:
#         imp_min = PRE_MINUTE

#     thr_raw = (request.args.get("imp_thr", "") or "").strip()
#     imp_thr = None if thr_raw == "" else float(thr_raw)  # w %

#     df = TWEETS_DF.copy()

#     def _p(n):
#         try: return int(request.args.get(n, 0) or 0)
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

#     # [NEW] licz w locie imp_*
#     if imp_filter == 1:
#         imp_pct, imp_lbl = [], []
#         for ts in df["created_at"]:
#             v = impact_at_minute(pd.Timestamp(ts), imp_min)
#             pct = None if v is None else (100.0 * v)
#             imp_pct.append(None if pct is None else round(pct, 4))
#             if v is None:
#                 imp_lbl.append("neutral")
#             else:
#                 if imp_thr is None:
#                     imp_lbl.append("up" if v > 0 else "down" if v < 0 else "neutral")
#                 else:
#                     thr = imp_thr / 100.0
#                     imp_lbl.append("up" if v >=  thr else "down" if v <= -thr else "neutral")
#         df["_imp_pct"] = imp_pct
#         df["_imp_label"] = imp_lbl
#     else:
#         df["_imp_pct"] = None
#         df["_imp_label"] = None

#     # [NEW] filtr etykiety
#     if label in ("up", "down", "neutral"):
#         if imp_filter == 1:
#             if "_imp_label" in df.columns:
#                 df = df[df["_imp_label"] == label]
#         else:
#             if "pre_label" in df.columns:
#                 df = df[df["pre_label"] == label]

#     # [NEW] GLOBALNY SORT PRZED PAGINACJĄ
#     sort_col = None
#     if imp_sort == 1 and label in ("up", "down"):
#         if imp_filter == 1 and "_imp_pct" in df.columns:
#             sort_col = "_imp_pct"
#         elif "pre_pct" in df.columns:
#             sort_col = "pre_pct"
#         if sort_col is not None:
#             asc = (label == "down")   # dla "down" rosnąco (bardziej ujemne na górze)
#             df = df.sort_values(sort_col, ascending=asc, na_position="last").reset_index(drop=True)
#             df["__rank"] = df.index + 1
#     else:
#         df = df.reset_index(drop=True)
#         df["__rank"] = pd.NA

#     # [NEW] STATYSTYKI (przed paginacją)
#     if imp_filter == 1 and "_imp_label" in df.columns:
#         col_lbl = "_imp_label"; col_pct = "_imp_pct"
#         minute_used = imp_min; threshold_used = imp_thr
#         mode = "imp"
#     else:
#         col_lbl = "pre_label" if "pre_label" in df.columns else None
#         col_pct = "pre_pct" if "pre_pct" in df.columns else None
#         minute_used = int(PRE_MINUTE); threshold_used = PRE_THRESHOLD
#         mode = "pre"

#     stats = {"n": int(len(df)), "mode": mode, "minute": minute_used,
#              "threshold": threshold_used, "label_filter": (label if label in ("up","down","neutral") else "all")}
#     if col_lbl is not None and col_lbl in df.columns:
#         stats.update({
#             "n_up": int((df[col_lbl] == "up").sum()),
#             "n_down": int((df[col_lbl] == "down").sum()),
#             "n_neutral": int((df[col_lbl] == "neutral").sum()),
#         })
#     if col_pct is not None and col_pct in df.columns:
#         s = pd.to_numeric(df[col_pct], errors="coerce").dropna()
#         stats.update({
#             "pct_min": (None if s.empty else float(round(s.min(), 4))),
#             "pct_max": (None if s.empty else float(round(s.max(), 4))),
#             "pct_mean": (None if s.empty else float(round(s.mean(), 4))),
#             "pct_median": (None if s.empty else float(round(s.median(), 4))),
#         })

#     total = len(df)
#     start = (page - 1) * per_page
#     end = start + per_page
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#         .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     items = []
#     for r in subset.itertuples(index=False):
#         txt = getattr(r, "text", "")
#         created_display = getattr(r, "created_at_display", "")
#         created_at_val = getattr(r, "created_at", None)
#         is_reply   = bool(getattr(r, "isReply", False))
#         is_retweet = bool(getattr(r, "isRetweet", False))
#         is_quote   = bool(getattr(r, "isQuote", False))

#         pre_label = getattr(r, "pre_label", None)
#         pre_min   = getattr(r, "pre_min", None)
#         pre_pct   = getattr(r, "pre_pct", None)
#         pre_pct   = (None if pre_pct is None or (isinstance(pre_pct, float) and pd.isna(pre_pct)) else float(pre_pct))

#         lab_label = getattr(r, "_lab_label", pre_label)
#         lab_min   = getattr(r, "_lab_min", pre_min)
#         lab_pct   = getattr(r, "_lab_pct", pre_pct)
#         lab_pct   = (None if lab_pct is None or (isinstance(lab_pct, float) and pd.isna(lab_pct)) else float(lab_pct))

#         imp_lbl = getattr(r, "_imp_label", None)
#         imp_pct = getattr(r, "_imp_pct", None)
#         imp_pct = (None if imp_pct is None or (isinstance(imp_pct, float) and pd.isna(imp_pct)) else float(imp_pct))
#         imp_min_out = imp_min if imp_filter == 1 else None

#         rank_val = getattr(r, "__rank", None)

#         items.append({
#             "tweet_id": str(getattr(r, "tweet_id", "")),
#             "text": txt,
#             "created_at_display": created_display,
#             "isReply": is_reply,
#             "isRetweet": is_retweet,
#             "isQuote": is_quote,

#             "pre_label": pre_label,
#             "pre_min":   (int(pre_min) if pre_min is not None else None),
#             "pre_pct":   pre_pct,

#             "lab_label": lab_label,
#             "lab_min":   (int(lab_min) if lab_min is not None else None),
#             "lab_pct":   lab_pct,

#             "imp_label": imp_lbl,
#             "imp_min":   imp_min_out,
#             "imp_pct":   imp_pct,

#             "rank": (int(rank_val) if rank_val is not None and pd.notna(rank_val) else None),
#         })

#     years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True) if len(TWEETS_DF) else []
#     return jsonify({
#         "items": items,
#         "page": page, "per_page": per_page, "total": int(total),
#         "years": years,
#         "stats": stats
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
#         if fmt != "text": return jsonify(resp)
#         return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         if fmt != "text": return jsonify(resp)
#         return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

#     win_start = start_dt - pd.Timedelta(minutes=pre)
#     win_end   = start_dt + pd.Timedelta(minutes=minutes)
#     df = PRICES_DF[(PRICES_DF["datetime"] >= win_start) & (PRICES_DF["datetime"] <= win_end)].copy()
#     reason = "ok" if not df.empty else "no_data"

#     points = [{
#         "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#         "open": float(r["open"]),
#         "high": float(r["high"]),
#         "low":  float(r["low"]),
#         "close": float(r["close"]),
#     } for _, r in df.iterrows()]

#     pc_raw = percent_changes_from(start_dt)
#     pct_changes = {k: (None if v is None else round(100.0 * v, 2)) for k, v in pc_raw.items()}

#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start":      int(pd.Timestamp(start_dt).value // 10**9),
#         "x_start": int(pd.Timestamp(win_start).value // 10**9),
#         "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
#         "pct_changes": pct_changes
#     }

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
#     app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
#     app.run(debug=True)


from flask import Flask, render_template, request, jsonify, abort
import os, glob, json
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo
from pathlib import Path
import joblib

# ====== KONFIG ======
DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
PRICES_SOURCE_TZ = "Europe/Warsaw"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# precompute: etykieta bazowa
PRE_MINUTE = 8
PRE_THRESHOLD = 1.0  # %
ALLOWED_IMPACT_MINUTES = list(range(1, 21)) + [30, 60]
H_MAX = 60

# ML / LLM / modele
LLM_PARQUET = os.path.join(BASE_DIR, "data", "combined_top_with_llm.parquet")
MODELS_DIR = os.path.join(BASE_DIR, "models_balanced")

# ====== APP ======
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

# ===== Utils: daty =====
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
                if col in raw.columns: return raw[col]
                if col.capitalize() in raw.columns: return raw[col.capitalize()]
                if col.upper() in raw.columns: return raw[col.upper()]
                raise KeyError(col)

            part = pd.DataFrame({
                "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),
                "open":  pd.to_numeric(pick("open"),  errors="coerce"),
                "high":  pd.to_numeric(pick("high"),  errors="coerce"),
                "low":   pd.to_numeric(pick("low"),   errors="coerce"),
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

# ===== Bufory/minuty =====
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
            prev = o[:-1]; cur = o[1:]
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

def _pct_series_from_base(pos: int, horizons=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
    return {k: _pct_change_from_base(pos, k) for k in horizons}

def percent_changes_from(start_dt_utc: pd.Timestamp, intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
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
    if val >= thr/100.0:
        return "up"
    if val <= -thr/100.0:
        return "down"
    return "neutral"

# ===== Init: dane bazowe =====
TWEETS_DF = load_tweets()
PRICES_DF = load_prices_from_dir()
_build_minute_buffers(PRICES_DF)

def precompute_labels(df: pd.DataFrame, minute: int = PRE_MINUTE, thr: float = PRE_THRESHOLD) -> pd.DataFrame:
    print(f"[precompute] Liczę etykiety bazowe: m={minute}, próg={thr}% (wiersze: {len(df)})")
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

# ===== ML/Model: LLM + MODELE =====

# cechy jak w treningu
CAT_FEATS = ["llm_impact", "llm_stance"]
BIN_FEATS = ["llm_sarcasm", "isQuote", "isRetweet"]
NUM_FEATS = [
    "S_raw", "llm_conf",
    "before_1m", "before_3m", "before_5m",
    "avg1_3", "stability_before_3m", "stability_before_4m"
]
ALL_FEATS = CAT_FEATS + BIN_FEATS + NUM_FEATS

# ---- LLM parquet: spróbuj kilku kandydatów i zaloguj co się dzieje
LLM_CANDIDATES = [
    os.path.join(BASE_DIR, "data", "combined_top_with_llm.parquet"),
    os.path.join(BASE_DIR, "data", "combined_top_with_llm_labels.parquet"),
]
LLM_DF = pd.DataFrame()
LLM_PARQUET = None

for cand in LLM_CANDIDATES:
    print(f"[ml] sprawdzam LLM parquet: {cand} (exists={os.path.exists(cand)})")
    if os.path.exists(cand):
        try:
            LLM_DF = pd.read_parquet(cand)
            LLM_PARQUET = cand
            print(f"[ml] OK: wczytano {len(LLM_DF)} wierszy z {cand}")
            break
        except Exception as e:
            print(f"[ml] BŁĄD wczytywania {cand}: {e}")

if LLM_DF.empty:
    print("[ml] UWAGA: nie wczytano żadnego parquet z LLM (brak pliku lub błąd).")

if not LLM_DF.empty:
    if "tweet_id" not in LLM_DF.columns:
        if "id" in LLM_DF.columns:
            LLM_DF["tweet_id"] = LLM_DF["id"]
        elif "tweetId" in LLM_DF.columns:
            LLM_DF["tweet_id"] = LLM_DF["tweetId"]
        else:
            LLM_DF["tweet_id"] = pd.NA
    LLM_DF["tweet_id"] = LLM_DF["tweet_id"].astype(str)
    LLM_IDX = LLM_DF.set_index("tweet_id")
else:
    LLM_IDX = pd.DataFrame()


# ===== ML: wczytywanie modeli (odporne na index/nazwy) =====
import re, glob

MODELS: dict[int, object] = {}
MODEL_INDEX = {}
idx_path = os.path.join(MODELS_DIR, "model_index.json")

def _try_load_model(path: str):
    try:
        mdl = joblib.load(path)
        return mdl
    except Exception as e:
        print(f"[ml] nie wczytałem {path}: {e}")
        return None

def _normalize_key_to_int(k) -> int | None:
    try:
        if isinstance(k, int):
            return k
        s = str(k).strip().lower()
        m = re.search(r"(\d+)", s)  # łapie '1' z '1m'
        return int(m.group(1)) if m else None
    except Exception:
        return None

# 1) spróbuj model_index.json
if os.path.exists(idx_path):
    try:
        with open(idx_path, "r") as f:
            MODEL_INDEX = json.load(f) or {}
    except Exception as e:
        print(f"[ml] problem z model_index.json: {e}")
        MODEL_INDEX = {}

for k, p in (MODEL_INDEX or {}).items():
    k_int = _normalize_key_to_int(k)
    if not k_int:
        continue
    mp = os.path.join(MODELS_DIR, os.path.basename(str(p)))
    print(f"[ml] próba wczytania modelu {k} -> {mp} (exists={os.path.exists(mp)})")
    if os.path.exists(mp) and k_int not in MODELS:
        mdl = _try_load_model(mp)
        if mdl is not None:
            MODELS[k_int] = mdl
            print(f"[ml] model {k_int}m załadowany: {mp}")

# 2) fallback: przeskanuj *.pkl jeśli nic nie weszło
if not MODELS:
    for mp in glob.glob(os.path.join(MODELS_DIR, "*.pkl")):
        base = os.path.basename(mp).lower()
        m = re.search(r"(\d+)m", base)   # np. logreg_bal_5m.pkl -> 5
        if not m:
            continue
        k_int = int(m.group(1))
        if k_int in MODELS:
            continue
        mdl = _try_load_model(mp)
        if mdl is not None:
            MODELS[k_int] = mdl
            print(f"[ml] (fallback) model {k_int}m załadowany: {mp}")


def _has_llm_for(tweet_id: str) -> bool:
    return (not LLM_IDX.empty) and (str(tweet_id) in LLM_IDX.index)

def _safe_bool(v):
    try:
        # Nullable boolean może być <NA>; traktuj jako False
        return bool(v) if (v is not pd.NA) else False
    except:
        return False

def _S_from_llm_row(row: pd.Series) -> float:
    def to_num_sent(label):
        return {"positive":1, "neutral":0, "negative":-1}.get(str(label).lower(), 0)
    def stance_adj(s):
        s = str(s).lower()
        return 0.25 if s=="bullish" else (-0.25 if s=="bearish" else 0.0)
    def impact_mul(s):
        s = str(s).lower()
        return 1.30 if s=="high" else (1.15 if s=="medium" else 1.0)
    S = to_num_sent(row.get("llm_sent_tweet", "neutral"))
    sq = row.get("llm_sent_quote", None)
    if sq not in (None, "null", "none", pd.NA):
        S += 0.5 * to_num_sent(sq)
    S += stance_adj(row.get("llm_stance", "unclear"))
    S *= impact_mul(row.get("llm_impact", "none"))
    if _safe_bool(row.get("llm_sarcasm", False)):
        S *= 0.7
    try:
        return float(S)
    except:
        return 0.0

def _make_feature_row(llm_row: pd.Series, tw_row: pd.Series) -> pd.DataFrame:
    is_quote = _safe_bool(tw_row.get("isQuote", False))
    is_ret   = _safe_bool(tw_row.get("isRetweet", False))

    llm_conf = llm_row.get("llm_conf", 0.0)
    try:
        llm_conf = float(llm_conf)
    except:
        llm_conf = 0.0

    S_raw = _S_from_llm_row(llm_row)

    def _num_or_zero(name):
        v = llm_row.get(name, 0.0)
        try:
            return float(v)
        except:
            return 0.0

    feats = {
        "llm_impact": str(llm_row.get("llm_impact", "none")),
        "llm_stance": str(llm_row.get("llm_stance", "unclear")),
        "llm_sarcasm": _safe_bool(llm_row.get("llm_sarcasm", False)),
        "isQuote": is_quote,
        "isRetweet": is_ret,
        "S_raw": S_raw,
        "llm_conf": llm_conf,
        "before_1m": _num_or_zero("before_1m"),
        "before_3m": _num_or_zero("before_3m"),
        "before_5m": _num_or_zero("before_5m"),
        "avg1_3": _num_or_zero("avg1_3"),
        "stability_before_3m": _num_or_zero("stability_before_3m"),
        "stability_before_4m": _num_or_zero("stability_before_4m"),
    }
    return pd.DataFrame([feats], columns=ALL_FEATS)

def _realized_dict_from_prices(created_at_utc: pd.Timestamp):
    pc = percent_changes_from(pd.Timestamp(created_at_utc), intervals=(1,5))
    out = {
        "after_1m": None if pc.get(1) is None else round(100.0*pc[1], 3),
        "after_5m": None if pc.get(5) is None else round(100.0*pc[5], 3),
    }
    def _lab(v):
        if v is None: return None
        if v > 0: return "UP"
        if v < 0: return "DOWN"
        return "NO_CHANGE"
    out["y_1m_auto"] = _lab(out["after_1m"])
    out["y_5m_auto"] = _lab(out["after_5m"])
    return out

def _predict_for(tweet_id: str, window_m: int):
    if window_m not in MODELS:
        return None, "model_not_loaded"

    base = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
    if base.empty:
        return None, "tweet_not_found"
    tw = base.iloc[0]

    if not _has_llm_for(tweet_id):
        return None, "no_llm_row"

    llm_row = LLM_IDX.loc[str(tweet_id)]
    # jeśli multi-index selection zwróci DataFrame (duplikaty) -> bierz pierwszy wiersz
    if isinstance(llm_row, pd.DataFrame):
        llm_row = llm_row.iloc[0]

    x = _make_feature_row(llm_row, tw)
    pipe = MODELS[window_m]
    try:
        probs = pipe.predict_proba(x)[0]
        labels = pipe.classes_.tolist()
        top_i = int(np.argmax(probs))
        pred = labels[top_i]
        res = {
            "prediction": str(pred),
            "confidence": float(probs[top_i]),
            "probs": {str(lbl): float(p) for lbl, p in zip(labels, probs)},
        }
    except Exception as e:
        return None, f"predict_error: {e}"

    realized = _realized_dict_from_prices(pd.Timestamp(tw["created_at"]))
    res["realized"] = realized
    return res, "ok"

# ===== ROUTES =====

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

    df = TWEETS_DF.copy()

    def _p(n):
        try: return int(request.args.get(n, 0) or 0)
        except ValueError: return 0

    f_reply = _p("reply"); f_retweet = _p("retweet"); f_quote = _p("quote")

    for col in ("isReply", "isRetweet", "isQuote"):
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False)

    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    if f_reply == 1: df = df[df["isReply"]]
    elif f_reply == -1: df = df[~df["isReply"]]

    if f_retweet == 1: df = df[df["isRetweet"]]
    elif f_retweet == -1: df = df[~df["isRetweet"]]

    if f_quote == 1: df = df[df["isQuote"]]
    elif f_quote == -1: df = df[~df["isQuote"]]

    if q:
        df = df[df["text"].str.contains(q, case=False, na=False)]

    # licz w locie imp_*
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

    # sort globalny
    sort_col = None
    if imp_sort == 1 and label in ("up", "down"):
        if imp_filter == 1 and "_imp_pct" in df.columns:
            sort_col = "_imp_pct"
        elif "pre_pct" in df.columns:
            sort_col = "pre_pct"
    if sort_col is not None:
        asc = (label == "down")
        df = df.sort_values(sort_col, ascending=asc, na_position="last").reset_index(drop=True)
        df["__rank"] = df.index + 1
    else:
        df = df.reset_index(drop=True)
        df["__rank"] = pd.NA

    # statystyki (przed paginacją)
    if imp_filter == 1 and "_imp_label" in df.columns:
        col_lbl = "_imp_label"; col_pct = "_imp_pct"; minute_used = imp_min; threshold_used = imp_thr; mode = "imp"
    else:
        col_lbl = "pre_label" if "pre_label" in df.columns else None
        col_pct = "pre_pct" if "pre_pct" in df.columns else None
        minute_used = int(PRE_MINUTE); threshold_used = PRE_THRESHOLD; mode = "pre"

    stats = {"n": int(len(df)), "mode": mode, "minute": minute_used, "threshold": threshold_used,
             "label_filter": (label if label in ("up","down","neutral") else "all")}
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
    subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ).dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    items = []
    for r in subset.itertuples(index=False):
        txt = getattr(r, "text", "")
        created_display = getattr(r, "created_at_display", "")
        created_at_val = getattr(r, "created_at", None)
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
        })

    years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True) if len(TWEETS_DF) else []
    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": int(total),
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
    created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
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
    win_end = start_dt + pd.Timedelta(minutes=minutes)
    df = PRICES_DF[(PRICES_DF["datetime"] >= win_start) & (PRICES_DF["datetime"] <= win_end)].copy()
    reason = "ok" if not df.empty else "no_data"
    points = [{
        "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
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
        "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
        "used_start": int(pd.Timestamp(start_dt).value // 10**9),
        "x_start": int(pd.Timestamp(win_start).value // 10**9),
        "x_end": int(pd.Timestamp(win_end).value // 10**9),
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
            "minute_ts": [int(ts.value // 10**9) for ts in idx],
            "close": aligned_close,
            "tweet_minute_ts": int(pd.Timestamp(start_dt.floor("min")).value // 10**9)
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
            lines.append(f"{ts_local:%Y-%m-%d %H:%M} — brak notowań")
        else:
            lines.append(f"{ts_local:%Y-%m-%d %H:%M} close: {float(val):.4f}")

    header = [
        "Ceny w oknie minutowym:",
        f"Zakres: {pd.Timestamp(win_start).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z} → "
        f"{pd.Timestamp(win_end).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M %Z}",
        f"Chwila tweeta: {pd.Timestamp(start_dt).tz_convert(DISPLAY_TZ):%Y-%m-%d %H:%M:%S %Z}",
    ]
    if reason == "no_data":
        header.append("Brak danych cenowych w tym oknie.")
    body = "\n".join(header + [""] + lines)
    return (body, 200, {"Content-Type": "text/plain; charset=utf-8"})

# ---- API: model info ----
@app.route("/api/model/info")
def api_model_info():
    return jsonify({
        "llm_rows": 0 if LLM_DF.empty else int(len(LLM_DF)),
        "has_llm_tweet_id": (not LLM_DF.empty and "tweet_id" in LLM_DF.columns),
        "llm_parquet_path": LLM_PARQUET,
        "models_loaded": sorted(list(MODELS.keys())),
        "models_dir": MODELS_DIR,
        "features": { "cat": CAT_FEATS, "bin": BIN_FEATS, "num": NUM_FEATS }
    })


# ---- API: predykcja ----
@app.route("/api/model/predict", methods=["POST"])
def api_model_predict():
    data = request.get_json(silent=True) or {}
    tweet_id = str(data.get("tweet_id", "")).strip()
    try:
        window_m = int(data.get("window", 5))
    except Exception:
        window_m = 5
    if not tweet_id:
        return jsonify({"ok": False, "reason": "bad_request", "msg": "missing tweet_id"}), 400

    res, status = _predict_for(tweet_id, window_m)
    if status != "ok":
        return jsonify({"ok": False, "reason": status}), 200
    return jsonify({"ok": True, "result": res})

# ---- API: prosty backtest ----
@app.route("/api/strategy/backtest", methods=["POST"])
def api_strategy_backtest():
    data = request.get_json(silent=True) or {}
    try:
        window_m = int(data.get("window", 5))
    except Exception:
        window_m = 5
    cost_bps = float(data.get("cost_bps", 2.0))
    min_prob = float(data.get("min_prob", 0.0))
    only_test = bool(data.get("only_test_set", False))

    year = str(data.get("year", "all"))
    q = (data.get("q") or "").strip()
    label = (data.get("label", "all") or "all").lower()

    def _p(n):
        try: return int(data.get(n, 0) or 0)
        except: return 0
    f_reply = _p("reply"); f_retweet = _p("retweet"); f_quote = _p("quote")

    if window_m not in MODELS:
        return jsonify({"ok": False, "reason": "model_not_loaded"}), 200

    df = TWEETS_DF.copy()
    for col in ("isReply", "isRetweet", "isQuote"):
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False)

    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    if f_reply == 1: df = df[df["isReply"]]
    elif f_reply == -1: df = df[~df["isReply"]]

    if f_retweet == 1: df = df[df["isRetweet"]]
    elif f_retweet == -1: df = df[~df["isRetweet"]]

    if f_quote == 1: df = df[df["isQuote"]]
    elif f_quote == -1: df = df[~df["isQuote"]]

    if q:
        df = df[df["text"].str.contains(q, case=False, na=False)]

    if label in ("up", "down", "neutral") and "pre_label" in df.columns:
        df = df[df["pre_label"] == label]

    # tylko test-set (jeśli w parquet masz set_1m / set_5m)
    if only_test:
        set_col = f"set_{window_m}m"
        if (not LLM_DF.empty) and (set_col in LLM_DF.columns):
            j = df.copy()
            j["tweet_id"] = j["tweet_id"].astype(str)
            llm_sub = LLM_DF[["tweet_id", set_col]].copy()
            llm_sub["tweet_id"] = llm_sub["tweet_id"].astype(str)
            j = j.merge(llm_sub, on="tweet_id", how="left")
            df = j[j[set_col] == "test"].copy()

    n_trades = 0
    raw_returns = []
    strat_returns = []
    rows_for_table = []

    COST = cost_bps / 10000.0
    has_llm = (not LLM_IDX.empty)

    for r in df.itertuples(index=False):
        tid = str(getattr(r, "tweet_id", ""))
        if not tid:
            continue
        if not has_llm or tid not in LLM_IDX.index:
            continue

        pred_res, st = _predict_for(tid, window_m)
        if st != "ok":
            continue

        max_prob = float(pred_res["confidence"])
        signal  = str(pred_res["prediction"])
        if max_prob < min_prob:
            continue
        if signal == "NO_CHANGE":
            continue

        ret_pct = pred_res["realized"].get(f"after_{window_m}m", None)
        if ret_pct is None:
            continue

        trade_ret = ret_pct if signal == "UP" else (-ret_pct if signal == "DOWN" else 0.0)

        n_trades += 1
        raw_returns.append(trade_ret)
        strat_returns.append(trade_ret - COST)

        rows_for_table.append({
            "tweet_id": tid,
            "created_at": pd.Timestamp(getattr(r, "created_at")).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "text": getattr(r, "text", "")[:280],
            "signal": signal,
            "ret_%": float(trade_ret),
            "ret_after_cost_%": float(trade_ret - COST),
            "prob": max_prob
        })

    if n_trades == 0:
        return jsonify({"ok": True, "summary": {
            "trades": 0, "hit_rate": None, "avg_ret": None, "total_ret": 0.0, "sharpe": None
        }, "top5": [], "bottom5": []})

    raw = np.array(raw_returns, dtype=float)
    strat = np.array(strat_returns, dtype=float)
    avg_ret = float(np.mean(strat))
    tot_ret = float(np.sum(strat))
    std = float(np.std(strat, ddof=1)) if len(strat) > 1 else np.nan
    sharpe = float(avg_ret / std * np.sqrt(len(strat))) if (std and np.isfinite(std)) else None

    hits = 0
    for row in rows_for_table:
        s = row["signal"]; rret = row["ret_%"]
        if (s == "UP" and rret > 0) or (s == "DOWN" and rret < 0):
            hits += 1
    hit_rate = float(hits) / float(n_trades)

    top5 = sorted(rows_for_table, key=lambda x: x["ret_after_cost_%"], reverse=True)[:5]
    bottom5 = sorted(rows_for_table, key=lambda x: x["ret_after_cost_%"])[:5]

    return jsonify({
        "ok": True,
        "summary": {
            "trades": int(n_trades),
            "hit_rate": round(hit_rate, 3),
            "avg_ret": round(avg_ret, 3),
            "total_ret": round(tot_ret, 3),
            "sharpe": (None if sharpe is None else round(sharpe, 2))
        },
        "top5": top5,
        "bottom5": bottom5
    })

# ===== MAIN =====
if __name__ == "__main__":
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.run(debug=True)
