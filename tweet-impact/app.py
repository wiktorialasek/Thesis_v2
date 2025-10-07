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
    df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

    if "createdAt" not in df.columns:
        raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
    # createdAt -> UTC
    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

    for flag in ["isReply", "isRetweet", "isQuote"]:
        if flag not in df.columns:
            df[flag] = False

    # --- FILTR zakresu czasowego ---
    prices_min = pd.to_datetime(prices_min, utc=True)
    prices_max = pd.to_datetime(prices_max, utc=True)
    df = df[(df["created_at"] >= prices_min) & (df["created_at"] <= prices_max)]

    df = df.dropna(subset=["created_at"]).sort_values("created_at", ascending=False).reset_index(drop=True)
    # >>> tylko tweety z godzin 15:30–21:45 czasu PL (uwzględnia DST)
    _local = df["created_at"].dt.tz_convert(DISPLAY_TZ)
    mask = (
        ((_local.dt.hour > 15) | ((_local.dt.hour == 15) & (_local.dt.minute >= 35))) &
        ((_local.dt.hour < 21) | ((_local.dt.hour == 21) & (_local.dt.minute <= 50)))
    )
    df = df[mask].reset_index(drop=True)

    return df[["tweet_id", "text", "created_at", "isReply", "isRetweet", "isQuote"]]


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

# ===== NOWE: wycinek w sztywnych granicach (bez fallbacku) =====
def slice_prices_between(start_dt_utc: pd.Timestamp, end_dt_utc: pd.Timestamp):
    """
    Zwraca df z PRICES_DF dla [start_dt_utc, end_dt_utc] BEZ żadnego przesuwania.
    """
    if PRICES_DF.empty:
        return PRICES_DF.iloc[0:0].copy()
    return PRICES_DF[(PRICES_DF["datetime"] >= start_dt_utc) &
                     (PRICES_DF["datetime"] <= end_dt_utc)].copy()

# ===== Procentowe zmiany względem chwili tweeta =====
def _minute_close_at(dt_utc: pd.Timestamp):
    """Zwróć kurs close w minucie dt_utc (ostatni tick w tej minucie). Gdy brak – None."""
    if PRICES_DF.empty:
        return None
    minute = pd.Timestamp(dt_utc).floor("min")
    dfm = PRICES_DF.copy()
    dfm["minute"] = dfm["datetime"].dt.floor("min")
    row = (dfm[dfm["minute"] == minute].sort_values("datetime").tail(1))
    return None if row.empty else float(row.iloc[0]["open"]) #wedlug mnie powinno byc w open

def percent_changes_from(start_dt_utc: pd.Timestamp,
                         intervals=(1,2,3,4,5,6,7,8,9,10,15,30,60)):
    """
    Zwraca słownik {minuty: %zmiana} liczony względem ceny w minucie tweeta.
    Jeśli brak ceny w danej minucie – wartość to None.
    """
    base = _minute_close_at(start_dt_utc)
    out = {}
    for m in intervals:
        price = _minute_close_at(start_dt_utc + pd.Timedelta(minutes=m))
        if base is not None and price is not None:
            out[m] = round((price - base) / base * 100, 2)
        else:
            out[m] = None
    return out


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


    # bezpieczne parsowanie (-1/0/1)
    def _p(name):
        try:
            return int(request.args.get(name, 0) or 0)
        except ValueError:
            return 0
        
    f_reply   = _p("reply")
    f_retweet = _p("retweet")
    f_quote   = _p("quote")


    df = TWEETS_DF.copy()

     # --- NORMALIZACJA FLAG -> bool (kluczowe dla ~) ---
    for col in ("isReply", "isRetweet", "isQuote"):
        if col in df.columns:
            # zamień NaN na False i rzutuj na bool (0/1/0.0/1.0 -> False/True)
            df[col] = df[col].fillna(False).astype(bool)

    # filtr rok
    if year != "all":
        try:
            y = int(year)
            df = df[df["created_at"].dt.year == y]
        except Exception:
            pass

    # flagi
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

@app.route("/api/price")
def api_price():
    """
    Query params:
      start   – unix seconds (UTC) chwili tweeta (wymagane)
      minutes – ile minut PO tweecie (domyślnie 15)
      pre     – ile minut PRZED tweetem (domyślnie 0; np. 10)
      format  – "text" dla legacy listy minut; domyślnie JSON
    """
    start_unix = (request.args.get("start", "") or "").strip()
    fmt = (request.args.get("format", "") or "").lower()

    # minutes
    try:
        minutes = int(request.args.get("minutes", 15))
    except Exception:
        minutes = 15
    minutes = max(1, min(minutes, 24*60))  # bezpieczny limit

    # pre (minuty przed)
    try:
        pre = int(request.args.get("pre", 0))
    except Exception:
        pre = 0
    pre = max(0, min(pre, 120))  # np. pozwól do 120 min wstecz

    # brak startu
    if not start_unix:
        resp = {"points": [], "reason": "no_start"}
        if fmt != "text":
            return jsonify(resp)
        return ("Brak parametru start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    # parsowanie startu
    try:
        start_dt = pd.to_datetime(int(float(start_unix)), unit="s", utc=True)
    except Exception:
        resp = {"points": [], "reason": "bad_start"}
        if fmt != "text":
            return jsonify(resp)
        return ("Zły parametr start.", 400, {"Content-Type": "text/plain; charset=utf-8"})

    # --- SZTYWNE okno: [start - pre, start + minutes] ---
    win_start = start_dt - pd.Timedelta(minutes=pre)
    win_end   = start_dt + pd.Timedelta(minutes=minutes)
    df = slice_prices_between(win_start, win_end)
    reason = "ok" if not df.empty else "no_data"

    # punkty do wykresu
    points = [
        {
            "t": int(pd.Timestamp(r["datetime"]).value // 10**9),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low":  float(r["low"]),
            "close": float(r["close"]),
        }
        for _, r in df.iterrows()
    ]

    payload = {
        "points": points,
        "reason": reason,
        "requested_start": int(pd.Timestamp(start_dt).value // 10**9),
        "used_start":      int(pd.Timestamp(start_dt).value // 10**9),  # NIE PRZESUWAMY
        # pomoc do zablokowania zakresu osi X w frontendzie
        "x_start": int(pd.Timestamp(win_start).value // 10**9),
        "x_end":   int(pd.Timestamp(win_end).value   // 10**9),
    }

    # % zmiany względem minuty tweeta (jeśli brak ceny w minucie tweeta, będzie None)
    try:
        payload["pct_changes"] = percent_changes_from(start_dt)
    except Exception:
        # niech API się nie wywala nawet, jeśli helpera brak
        payload["pct_changes"] = {}

    # --- JSON domyślnie ---
    if fmt != "text":
        return jsonify(payload)

    # --- Legacy: wersja tekstowa (lista minut) dla kompatybilności ---
    grid_start = pd.Timestamp(win_start).floor("min")
    grid_end   = pd.Timestamp(win_end).floor("min")
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
    app.run(debug=True)

