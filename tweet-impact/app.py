# app.py — minimalistyczna aplikacja Flask do przeglądu tweetów i wykresu 15 min
from flask import Flask, render_template, request, jsonify, abort
import os
import glob
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo   # Python 3.9+
DISPLAY_TZ = ZoneInfo("Europe/Warsaw")  # ustaw jak chcesz


# ===== Konfiguracja ścieżek =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWEETS_CSV = os.path.join(BASE_DIR, "data", "all_musk_posts.csv")
PRICES_DIR = os.path.join(BASE_DIR, "data", "TSLA_sorted")

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

# ===== Loader: Tweety =====
def load_tweets(csv_path: str = TWEETS_CSV) -> pd.DataFrame:
    """
    Oczekiwane kolumny: id, fullText, createdAt, isReply, isRetweet, isQuote
    Zwraca kolumny: tweet_id, text, created_at, isReply, isRetweet, isQuote
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Nie znaleziono pliku: {csv_path}")

    df = pd.read_csv(csv_path, low_memory=False)
    # mapowanie kolumn
    df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df) + 1)
    df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else ""

    if "createdAt" not in df.columns:
        raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

    for flag in ["isReply", "isRetweet", "isQuote"]:
        if flag not in df.columns:
            df[flag] = False

    # porządki
    df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)
    return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]

# ===== Loader: Ceny (rekurencyjnie z wielu plików) =====
def load_prices_from_dir(base_dir: str = PRICES_DIR) -> pd.DataFrame:
    """
    Szuka .csv rekurencyjnie w base_dir i normalizuje do kolumn:
    datetime (UTC), open, high, low, close
    """
    if not os.path.isdir(base_dir):
        # Pusta ramka – aplikacja nadal działa, tylko wykresy mogą być puste
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
    if not files:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    frames = []
    for path in files:
        try:
            raw = pd.read_csv(path, low_memory=False)

            # znajdź kolumnę czasu
            dt_col = None
            for c in ["datetime", "time", "timestamp", "date", "Date", "Time"]:
                if c in raw.columns:
                    dt_col = c
                    break
            if not dt_col:
                # pomijamy plik bez czasu
                continue

            def pick(col: str):
                if col in raw.columns:
                    return raw[col]
                if col.capitalize() in raw.columns:
                    return raw[col.capitalize()]
                if col.upper() in raw.columns:
                    return raw[col.upper()]
                raise KeyError(col)

            part = pd.DataFrame({
                "datetime": pd.to_datetime(raw[dt_col], errors="coerce", utc=True),
                "open":  pd.to_numeric(pick("open"), errors="coerce"),
                "high":  pd.to_numeric(pick("high"), errors="coerce"),
                "low":   pd.to_numeric(pick("low"),  errors="coerce"),
                "close": pd.to_numeric(pick("close"),errors="coerce"),
            }).dropna(subset=["datetime"])

            frames.append(part)
        except Exception:
            # pomiń problematyczne pliki, nie wstrzymuj startu
            continue

    if not frames:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

    all_prices = pd.concat(frames, ignore_index=True)
    all_prices = all_prices.sort_values("datetime").reset_index(drop=True)
    return all_prices

# ===== Inicjalizacja danych =====
try:
    TWEETS_DF = load_tweets()
except Exception as e:
    print(f"[startup] BŁĄD wczytywania tweetów: {e}")
    TWEETS_DF = pd.DataFrame(columns=["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"])

try:
    PRICES_DF = load_prices_from_dir()
except Exception as e:
    print(f"[startup] BŁĄD wczytywania cen: {e}")
    PRICES_DF = pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])

# ===== Pomocnicze =====
from bisect import bisect_left

def slice_prices_for_window(start_dt_utc: pd.Timestamp, minutes: int = 15):
    """Zwraca (df_window, used_start, reason).
    reason in ["ok", "fallback_next", "no_data"]"""
    if PRICES_DF.empty:
        return PRICES_DF.copy(), start_dt_utc, "no_data"

    end_dt = start_dt_utc + timedelta(minutes=minutes)
    win = PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
                    (PRICES_DF["datetime"] <= end_dt)].copy()
    if not win.empty:
        return win, start_dt_utc, "ok"

    # --- Fallback: najbliższy dostępny punkt >= start_dt_utc (np. następna sesja) ---
    times = PRICES_DF["datetime"].values
    # bisect po numpy datetime64 (zamieniamy na int64 ns)
    ts_ns = PRICES_DF["datetime"].astype("int64").values
    pos = bisect_left(ts_ns, int(start_dt_utc.value))  # .value = ns

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

@app.route("/")
def list_tweets():
    page = int(request.args.get("page", 1))
    per_page = 10
    start = (page - 1) * per_page
    end = start + per_page

    df = TWEETS_DF
    subset = df.iloc[start:end].copy()

    # lokalny czas do wyświetlania
    subset["created_at_display"] = subset["created_at"].dt.tz_convert(DISPLAY_TZ)\
                                                     .dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    tweets = subset.to_dict(orient="records")

    return render_template(
        "tweets.html",
        tweets=tweets,
        page=page,
        has_next=end < len(df),
        has_prev=start > 0
    )


@app.route("/tweet/<tweet_id>")
def tweet_detail(tweet_id):
    row = TWEETS_DF[TWEETS_DF["tweet_id"].astype(str) == str(tweet_id)]
    if row.empty:
        abort(404)
    t = row.iloc[0].to_dict()

    # timestamp do API (UTC)
    created_ts = int(pd.Timestamp(t["created_at"]).timestamp())

    # string do wyświetlenia (lokalnie)
    created_display = pd.Timestamp(t["created_at"]).tz_convert(DISPLAY_TZ)\
                                                  .strftime("%Y-%m-%d %H:%M:%S %Z")

    return render_template("tweet_detail.html", tweet=t,
                           created_ts=created_ts,
                           created_display=created_display)


@app.route("/api/price")
def api_price():
    start_unix = request.args.get("start", "").strip()
    try:
        minutes = int(request.args.get("minutes", 15))
    except Exception:
        minutes = 15

    if not start_unix:
        return jsonify({"points": [], "reason": "no_start"})

    try:
        start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
    except Exception:
        return jsonify({"points": [], "reason": "bad_start"})

    df, used_start, reason = slice_prices_for_window(start_dt, minutes=minutes)
    points = [
        {"t": int(pd.Timestamp(r["datetime"]).value // 10**9),
         "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"])}
        for _, r in df.iterrows()
    ]
    return jsonify({
        "points": points,
        "reason": reason,
        "requested_start": int(start_dt.value // 10**9),
        "used_start": int(pd.Timestamp(used_start).value // 10**9)
    })

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
