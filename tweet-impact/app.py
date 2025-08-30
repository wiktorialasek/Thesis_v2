# app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i wykresu 15 min
from flask import Flask, render_template, request, jsonify, abort
import os, glob
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo
from bisect import bisect_left

DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
PRICES_SOURCE_TZ = "Europe/Warsaw"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

def to_utc(series, source_tz: str):
    """
    Zamienia kolumnę czasu na tz-aware UTC.
    - Jeśli wartości mają już strefę (np. '2025-03-10 15:31:00+01:00') -> tylko konwersja do UTC.
    - Jeśli są 'naive' (bez strefy), interpretuj je jako source_tz (u Ciebie Europe/Warsaw), potem do UTC.
    Obsługuje zmiany czasu (DST).
    """
    s = pd.to_datetime(series, errors="coerce", utc=False)

    # tz-aware?
    try:
        has_tz = s.dt.tz is not None
    except Exception:
        has_tz = False

    if has_tz:
        return s.dt.tz_convert("UTC")

    # Naive -> potraktuj jako lokalne Europe/Warsaw
    tz = ZoneInfo(source_tz)
    # pandas>=2.2: parametry dla DST; jeśli masz 2.1, usuń je
    s = s.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    return s.dt.tz_convert("UTC")


# ===== Loader: Tweety =====
def load_tweets(csv_path: str = TWEETS_CSV) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        print(f"[startup] Brak pliku tweetów: {csv_path}")
        return pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

    df = pd.read_csv(csv_path, low_memory=False)
    df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
    df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

    if "createdAt" not in df.columns:
        raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
    # createdAt -> UTC
    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

    for flag in ["isReply", "isRetweet", "isQuote"]:
        if flag not in df.columns:
            df[flag] = False

    df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)
    return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# ===== Loader: Ceny (rekurencyjnie z wielu plików) =====
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

            # wybierz kolumnę czasu
            dt_col = next((c for c in ["datetime", "time", "timestamp", "date", "Date", "Time"] if c in raw.columns), None)
            if not dt_col:
                continue

            def pick(col):
                if col in raw.columns: return raw[col]
                if col.capitalize() in raw.columns: return raw[col.capitalize()]
                if col.upper() in raw.columns: return raw[col.upper()]
                raise KeyError(col)
            
            part = pd.DataFrame({
                "datetime": to_utc(raw[dt_col], PRICES_SOURCE_TZ),  # <--- TYLKO TA ZMIANA
                "open":  pd.to_numeric(pick("open"), errors="coerce"),
                "high":  pd.to_numeric(pick("high"), errors="coerce"),
                "low":   pd.to_numeric(pick("low"),  errors="coerce"),
                "close": pd.to_numeric(pick("close"),errors="coerce"),
            }).dropna(subset=["datetime"])

            frames.append(part)
        except Exception as e:
            print(f"[prices] pomijam {path}: {e}")
            continue

    if not frames:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    all_prices = pd.concat(frames, ignore_index=True)
    all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
    return all_prices

# ===== Inicjalizacja =====
TWEETS_DF = load_tweets()
PRICES_DF = load_prices_from_dir()

# ===== Pomocnicze =====
def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
    """Zwraca (df_window, used_start, reason) — reason in ["ok","fallback_next","no_data"]"""
    if PRICES_DF.empty:
        return PRICES_DF.copy(), start_dt_utc, "no_data"

    end_dt = start_dt_utc + timedelta(minutes=minutes)
    win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
                    (PRICES_DF["datetime"] <= end_dt)].copy()
    if not win.empty:
        return win, start_dt_utc, "ok"

    # fallback: najbliższy punkt >= start
    ts_ns = PRICES_DF["datetime"].astype("int64").values
    pos = bisect_left(ts_ns, int(start_dt_utc.value))
    if pos < len(PRICES_DF):
        new_start = PRICES_DF.iloc[pos]["datetime"]
        new_end = new_start + timedelta(minutes=minutes)
        win2 = PRICES_DF[(PRICES_DF["datetime"] >= new_start) &
                         (PRICES_DF["datetime"] <= new_end)].copy()
        if not win2.empty:
            return win2, new_start, "fallback_next"

    return PRICES_DF.iloc[0:0].copy(), start_dt_utc, "no_data"

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
    })

# @app.route("/")
# def list_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = 10
#     start = (page - 1) * per_page
#     end = start + per_page
#     df = TWEETS_DF
#     subset = df.iloc[start:end].copy()

#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#                                                   .dt.strftime("%Y-%m-%d %H:%M:%S %Z")
#     tweets = subset.to_dict(orient="records")
#     return render_template("tweets.html", tweets=tweets, page=page,
#                            has_next=end < len(df), has_prev=start > 0)

# --- NA GÓRZE MASZ JUŻ IMPORTY I INNE FUNKCJE ---
# ... (ZOSTAW tak jak było w Twojej wersji, łącznie z /api/price itd.)

@app.route("/")
def index():
    """Widok 2-kolumnowy: lewa – lista, prawa – szczegóły."""
    # Na wejściu pokaż pierwszy tweet (jeśli jest), resztę JS dociągnie.
    initial_id = None
    if len(TWEETS_DF):
        initial_id = str(TWEETS_DF.iloc[0]["tweet_id"])
    return render_template("index.html", initial_id=initial_id)

# ---- API: lista tweetów z filtrami + paginacja ----
@app.route("/api/tweets")
def api_tweets():
    """
    Query params:
      page (int, default 1), per_page (int, default 20)
      year (int albo 'all')
      reply, retweet, quote: '1' włącza filtr 'tylko takie'; '0' ignoruje
      q (szukaj w tekście – opcjonalnie)
    """
    page = int(request.args.get("page", 1))
    per_page = min(max(int(request.args.get("per_page", 20)), 5), 100)
    year = request.args.get("year", "all")
    q = (request.args.get("q") or "").strip()

    f_reply   = request.args.get("reply", None) == "1"
    f_retweet = request.args.get("retweet", None) == "1"
    f_quote   = request.args.get("quote", None) == "1"

    df = TWEETS_DF.copy()

    # filtr rok
    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    # flagi – jeśli flaga zaznaczona, pokazuj tylko takie
    if f_reply:
        df = df[df["isReply"] == True]
    if f_retweet:
        df = df[df["isRetweet"] == True]
    if f_quote:
        df = df[df["isQuote"] == True]

    # prosty search w tekście
    if q:
        df = df[df["text"].str.contains(q, case=False, na=False)]

    total = len(df)
    start = (page - 1) * per_page
    end = start + per_page
    subset = df.iloc[start:end].copy()

    subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
        .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    items = [{
        "tweet_id": str(r.tweet_id),
        "text": r.text,
        "created_at_display": r.created_at_display,
        "isReply": bool(r.isReply),
        "isRetweet": bool(r.isRetweet),
        "isQuote": bool(r.isQuote),
        "year": int(r.created_at.year)
    } for r in subset.itertuples(index=False)]

    # lista dostępnych lat (do selecta)
    years = sorted(TWEETS_DF["created_at"].dt.year.unique().tolist(), reverse=True)

    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": int(total),
        "years": years
    })

# ---- API: pojedynczy tweet (do prawej kolumny) ----
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


# @app.route("/tweet/<tweet_id>")
# def tweet_detail(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0].to_dict()

#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())  # UTC sekundy
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#                          .strftime("%Y-%m-%d %H:%M:%S %Z")

#     return render_template("tweet_detail.html",
#                            tweet=t, created_ts=created_ts, created_display=created_display)

@app.route("/api/price")
def api_price():
    start_unix = request.args.get("start", "").strip()
    fmt = (request.args.get("format", "") or "").lower()  # "text" => wypisz ręcznie
    try:
        minutes = int(request.args.get("minutes", 15))
    except Exception:
        minutes = 15

    if not start_unix:
        resp = {"points": [], "reason": "no_start"}
        return (jsonify(resp) if fmt != "text" else ("Brak parametru start.", 400, {"Content-Type":"text/plain; charset=utf-8"}))

    try:
        start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
    except Exception:
        resp = {"points": [], "reason": "bad_start"}
        return (jsonify(resp) if fmt != "text" else ("Zły parametr start.", 400, {"Content-Type":"text/plain; charset=utf-8"}))

    df, used_start, reason = slice_prices_for_window(start_dt, minutes=minutes)

    points = [
        {"t": int(pd.Timestamp(r["datetime"]).value // 10**9),
         "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"])}
        for _, r in df.iterrows()
    ]
    payload = {
        "points": points,
        "reason": reason,
        "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
        "used_start": int(pd.Timestamp(used_start).value // 10**9),
    }

    if fmt != "text":
        return jsonify(payload)

    # Tekst: minuta tweeta + kolejne minuty (do 'minutes')
    grid_start = pd.Timestamp(used_start).floor("min")
    grid_end = grid_start + pd.Timedelta(minutes=minutes)
    idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

    if df.empty:
        dfm = pd.DataFrame(columns=["minute", "close"])
    else:
        dfm = df.copy()
        dfm["minute"] = dfm["datetime"].dt.floor("min")
        dfm = (dfm.sort_values("datetime").groupby("minute").last()[["close"]])

    aligned = dfm.reindex(idx)

    lines = []
    for ts_utc, row in aligned.itertuples():
        ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
        val = (row["close"] if isinstance(row, pd.Series) else None)
        if pd.isna(val):
            lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
        else:
            lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

    header = ["Ceny w minucie tweeta i przez kolejne minuty (czas lokalny):"]
    if reason == "fallback_next":
        used_local = pd.Timestamp(used_start).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
        header.append(f"[Uwaga] Tweet poza sesją. Pokazuję najbliższe dostępne okno od: {used_local}.")
    if reason == "no_data":
        header.append("Brak danych cenowych w repozytorium.")

    body = "\n".join(header + [""] + lines)
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}

if __name__ == "__main__":
    app.run(debug=True)





# # app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i wykresu 15 min
# from flask import Flask, render_template, request, jsonify, abort
# import os
# import glob
# import pandas as pd
# from datetime import timedelta
# from zoneinfo import ZoneInfo   # Python 3.9+
# DISPLAY_TZ = ZoneInfo("Europe/Warsaw")  # ustaw jak chcesz


# # ===== Konfiguracja ścieżek =====
# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
# PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

# app = Flask(
#     __name__,
#     template_folder=os.path.join(BASE_DIR, "templates"),
#     static_folder=os.path.join(BASE_DIR, "static"),
# )

# # ===== Loader: Tweety =====
# def load_tweets(csv_path: str = TWEETS_CSV) -> pd.DataFrame:
#     """
#     Oczekiwane kolumny: id, fullText, createdAt, isReply, isRetweet, isQuote
#     Zwraca kolumny: tweet_id, text, created_at, isReply, isRetweet, isQuote
#     """
#     if not os.path.exists(csv_path):
#         raise FileNotFoundError(f"Nie znaleziono pliku: {csv_path}")

#     df = pd.read_csv(csv_path, low_memory=False)
#     # mapowanie kolumn
#     df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else ""

#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     for flag in ["isReply", "isRetweet", "isQuote"]:
#         if flag not in df.columns:
#             df[flag] = False

#     # porządki
#     df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)
#     return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# # ===== Loader: Ceny (rekurencyjnie z wielu plików) =====
# def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
#     """
#     Szuka .csv rekurencyjnie w base_dir i normalizuje do kolumn:
#     datetime (UTC), open, high, low, close
#     """
#     if not os.path.isdir(base_dir):
#         # Pusta ramka – aplikacja nadal działa, tylko wykresy mogą być puste
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
#     if not files:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     frames = []
#     for path in files:
#         try:
#             raw = pd.read_csv(path, low_memory=False)

#             # znajdź kolumnę czasu
#             dt_col = None
#             for c in ["datetime", "time", "timestamp", "date", "Date", "Time"]:
#                 if c in raw.columns:
#                     dt_col = c
#                     break
#             if not dt_col:
#                 # pomijamy plik bez czasu
#                 continue

#             def pick(col: str):
#                 if col in raw.columns:
#                     return raw[col]
#                 if col.capitalize() in raw.columns:
#                     return raw[col.capitalize()]
#                 if col.upper() in raw.columns:
#                     return raw[col.upper()]
#                 raise KeyError(col)

#             part = pd.DataFrame({
#                 "datetime": pd.to_datetime(raw[dt_col], errors="coerce", utc=True),
#                 "open":  pd.to_numeric(pick("open"), errors="coerce"),
#                 "high":  pd.to_numeric(pick("high"), errors="coerce"),
#                 "low":   pd.to_numeric(pick("low"),  errors="coerce"),
#                 "close": pd.to_numeric(pick("close"),errors="coerce"),
#             }).dropna(subset=["datetime"])

#             frames.append(part)
#         except Exception:
#             # pomiń problematyczne pliki, nie wstrzymuj startu
#             continue

#     if not frames:
#         return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

#     all_prices = pd.concat(frames, ignore_index=True)
#     all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
#     return all_prices

# # ===== Inicjalizacja danych =====
# try:
#     TWEETS_DF = load_tweets()
# except Exception as e:
#     print(f"[startup] BŁĄD wczytywania tweetów: {e}")
#     TWEETS_DF = pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

# try:
#     PRICES_DF = load_prices_from_dir()
# except Exception as e:
#     print(f"[startup] BŁĄD wczytywania cen: {e}")
#     PRICES_DF = pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

# # ===== Pomocnicze =====
# from bisect import bisect_left

# def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
#     """Zwraca (df_window, used_start, reason).
#     reason in ["ok", "fallback_next", "no_data"]"""
#     if PRICES_DF.empty:
#         return PRICES_DF.copy(), start_dt_utc, "no_data"

#     end_dt = start_dt_utc + timedelta(minutes=minutes)
#     win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
#                     (PRICES_DF["datetime"] <= end_dt)].copy()
#     if not win.empty:
#         return win, start_dt_utc, "ok"

#     # --- Fallback: najbliższy dostępny punkt >= start_dt_utc (np. następna sesja) ---
#     times = PRICES_DF["datetime"].values
#     # bisect po numpy datetime64 (zamieniamy na int64 ns)
#     ts_ns = PRICES_DF["datetime"].astype("int64").values
#     pos = bisect_left(ts_ns, int(start_dt_utc.value))  # .value = ns

#     if pos < len(PRICES_DF):
#         new_start = PRICES_DF.iloc[pos]["datetime"]
#         new_end = new_start + timedelta(minutes=minutes)
#         win2 = PRICES_DF[(PRICES_DF["datetime"] >= new_start) &
#                          (PRICES_DF["datetime"] <= new_end)].copy()
#         if not win2.empty:
#             return win2, new_start, "fallback_next"

#     return PRICES_DF.iloc[0:0].copy(), start_dt_utc, "no_data"


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
# def list_tweets():
#     page = int(request.args.get("page", 1))
#     per_page = 10
#     start = (page - 1) * per_page
#     end = start + per_page

#     df = TWEETS_DF
#     subset = df.iloc[start:end].copy()

#     # lokalny czas do wyświetlania
#     subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
#                                                      .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

#     tweets = subset.to_dict(orient="records")

#     return render_template(
#         "tweets.html",
#         tweets=tweets,
#         page=page,
#         has_next=end < len(df),
#         has_prev=start > 0
#     )


# @app.route("/tweet/<tweet_id>")
# def tweet_detail(tweet_id):
#     row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
#     if row.empty:
#         abort(404)
#     t = row.iloc[0].to_dict()

#     # timestamp do API (UTC)
#     created_ts = int(pd.Timestamp(t["created_at"]).timestamp())

#     # string do wyświetlenia (lokalnie)
#     created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
#                                                   .strftime("%Y-%m-%d %H:%M:%S %Z")

#     return render_template("tweet_detail.html", tweet=t,
#                            created_ts=created_ts,
#                            created_display=created_display)

# # --- PODMIEN tę funkcję w app.py ---
# @app.route("/api/price")
# def api_price():
#     start_unix = request.args.get("start", "").strip()
#     fmt = (request.args.get("format", "") or "").lower()  # "text" => wypisz ręcznie
#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15

#     if not start_unix:
#         resp = {"points": [], "reason": "no_start"}
#         return (jsonify(resp) if fmt != "text" else ("Brak parametru start.", 400, {"Content-Type":"text/plain; charset=utf-8"}))

#     # start w UTC (sekundy)
#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         resp = {"points": [], "reason": "bad_start"}
#         return (jsonify(resp) if fmt != "text" else ("Zły parametr start.", 400, {"Content-Type":"text/plain; charset=utf-8"}))

#     # okno cen
#     df, used_start, reason = slice_prices_for_window(start_dt, minutes=minutes)

#     # standardowa odpowiedź JSON (dla wykresu) – bez zmian względem Twojego app.js
#     points = [
#         {"t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#          "open": float(r["open"]), "high": float(r["high"]),
#          "low": float(r["low"]), "close": float(r["close"])}
#         for _, r in df.iterrows()
#     ]
#     payload = {
#         "points": points,
#         "reason": reason,
#         "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
#         "used_start": int(pd.Timestamp(used_start).value // 10**9),
#     }

#     if fmt != "text":
#         return jsonify(payload)

#     # === Tryb tekstowy: „minuta tweeta + 15 minut” co minutę ===
#     # budujemy siatkę minut: [0..minutes], zaokrąglając do początku minuty
#     grid_start = pd.Timestamp(used_start).floor("min")
#     grid_end = grid_start + pd.Timedelta(minutes=minutes)
#     idx = pd.date_range(start=grid_start, end=grid_end, freq="1min", tz="UTC")

#     # przygotuj ceny z okna i zredukuj do 1 punktu na minutę (ostatni tick w minucie)
#     if df.empty:
#         dfm = pd.DataFrame(columns=["minute", "close"])
#     else:
#         dfm = df.copy()
#         dfm["minute"] = dfm["datetime"].dt.floor("min")
#         dfm = (dfm.sort_values("datetime")
#                   .groupby("minute", as_index=True)
#                   .last()[["close"]])  # close z końca minuty

#     # reindeksuj do pełnej siatki minut
#     aligned = dfm.reindex(idx)

#     # przygotuj linie tekstu – pokazuj także lokalny czas dla czytelności
#     lines = []
#     for ts_utc, row in aligned.itertuples():
#         # ts_utc to indeks (UTC)
#         ts_local = pd.Timestamp(ts_utc).tz_convert(DISPLAY_TZ)
#         val = row["close"] if pd.notna(row["close"]) else None
#         if val is None:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  — brak notowań")
#         else:
#             lines.append(f"{ts_local:%Y-%m-%d %H:%M}  close: {float(val):.4f}")

#     header = []
#     header.append("Ceny w minucie tweeta i przez kolejne minuty (czas lokalny):")
#     if reason == "fallback_next":
#         used_local = pd.Timestamp(used_start).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
#         header.append(f"[Uwaga] Tweet był poza sesją. Pokazuję najbliższe dostępne okno od: {used_local}.")
#     if reason == "no_data":
#         header.append("Brak danych cenowych w repozytorium.")

#     body = "\n".join(header + [""] + lines)
#     return body, 200, {"Content-Type": "text/plain; charset=utf-8"}




# PRZERWA>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>


# @app.route("/api/price")
# def api_price():
#     start_unix = request.args.get("start", "").strip()
#     try:
#         minutes = int(request.args.get("minutes", 15))
#     except Exception:
#         minutes = 15

#     if not start_unix:
#         return jsonify({"points": [], "reason": "no_start"})

#     try:
#         start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
#     except Exception:
#         return jsonify({"points": [], "reason": "bad_start"})

#     df, used_start, reason = slice_prices_for_window(start_dt, minutes=minutes)
#     points = [
#         {"t": int(pd.Timestamp(r["datetime"]).value // 10**9),
#          "open": float(r["open"]), "high": float(r["high"]),
#          "low": float(r["low"]), "close": float(r["close"])}
#         for _, r in df.iterrows()
#     ]
#     return jsonify({
#         "points": points,
#         "reason": reason,
#         "requested_start": int(start_dt.value // 10**9),
#         "used_start": int(pd.Timestamp(used_start).value // 10**9)
#     })

# ===== Main =====
if __name__ == "__main__":
    # Flask dev server
    app.run(debug=True)



# # PRICES_CSV = "data/TSLA_1min_in_correalation.csv"  # ZMIEŃ dla innych spółek

# # PRZYJMUJEMY domyślnie takie kolumny:
# # all_musk_posts.csv: [id,url,twitterUrl,fullText,retweetCount,replyCount,likeCount,quoteCount,viewCount,createdAt,bookmarkCount,isReply,inReplyToId,conversationId,inReplyToUserId,inReplyToUsername,isPinned,isRetweet,isQuote,isConversationControlled,possiblySensitive,quoteId,quote,retweet]
# # TSLA_1min_in_correalation.csv: ['datetime','open','high','low','close']
# # -> jeżeli masz inne nazwy, popraw mapowanie w funkcjach load_* poniżej.

# def load_tweets():
#     df = pd.read_csv(TWEETS_CSV)
#     # Mapowanie nazw kolumn (DOPASUJ do swoich plików)
#     # mapowanie najważniejszych kolumn
#     if "id" in df.columns:
#         df["tweet_id"] = df["id"]
#     else:
#         df["tweet_id"] = range(1, len(df) + 1)

#     df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else ""
    
#     if "createdAt" not in df.columns:
#         raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
#     df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

#     # zachowaj tylko potrzebne kolumny
#     keep = ["tweet_id", "text", "created_at"]
#     for col in ["isReply", "isRetweet", "isQuote"]:
#         if col in df.columns:
#             keep.append(col)

#     df = df.dropna(subset=["created_at"])
#     df = df.sort_values("created_at", ascending=False).reset_index(drop=True)

#     return df[keep]

# def load_prices(symbol="TSLA"):
#     # Na start obsługujemy TSLA z podanego CSV
#     df = pd.read_csv(PRICES_CSV)
#     # dopasowanie kolumn
#     if 'datetime' not in df.columns:
#         for alt in ['time', 'timestamp', 'date']:
#             if alt in df.columns:
#                 df['datetime'] = df[alt]
#                 break
#     if 'datetime' not in df.columns:
#         raise ValueError("Brakuje kolumny datetime w danych minutowych.")

#     for col in ['open','high','low','close']:
#         if col not in df.columns:
#             # jeśli masz np. 'Open','High',... (z wielką literą)
#             if col.capitalize() in df.columns:
#                 df[col] = df[col.capitalize()]
#             else:
#                 raise ValueError(f"Brakuje kolumny {col} w danych minutowych.")

#     df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce', utc=True)
#     df = df.dropna(subset=['datetime'])
#     # Uporządkuj
#     df = df.sort_values('datetime').reset_index(drop=True)
#     df['symbol'] = symbol
#     return df

# TWEETS_DF = load_tweets()
# PRICES_DF = load_prices(symbol="TSLA")

# @app.route("/")
# def list_tweets():
#     # prosta paginacja
#     page = int(request.args.get("page", 1))
#     per_page = 10
#     start = (page-1)*per_page
#     end = start + per_page
#     subset = TWEETS_DF.iloc[start:end].copy()

#     # dane do szablonu
#     tweets = subset.to_dict(orient='records')
#     has_next = end < len(TWEETS_DF)
#     has_prev = start > 0
#     return render_template("tweets.html",
#                            tweets=tweets,
#                            page=page,
#                            has_next=has_next,
#                            has_prev=has_prev)

# if __name__ == "__main__":
#     app.run(debug=True)
