# export_dataset.py — eksport tweetów do CSV z % zmianą po 1..20, 30, 60 min
# Tryb podglądu: ograniczenie do pierwszych N tweetów
import os, glob, argparse
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TWEETS_CSV = os.path.join(DATA_DIR, "all_musk_posts.csv")
PRICES_DIR = os.path.join(DATA_DIR, "TSLA_sorted")
OUT_CSV = os.path.join(DATA_DIR, "tweet_impact_dataset.csv")
OUT_CSV_PREVIEW = os.path.join(DATA_DIR, "tweet_impact_dataset_preview.csv")

DISPLAY_TZ = ZoneInfo("Europe/Warsaw")
PRICES_SOURCE_TZ = "Europe/Warsaw"   # tak jak w app.py

# ---- parametry filtra tweetów po czasie (UTC)
PRICES_MIN = "2010-06-29 21:00:00+00:00"
PRICES_MAX = "2025-03-07 20:54:00+00:00"

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

def load_tweets(csv_path: str, prices_min: str, prices_max: str) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Brak pliku z tweetami: {csv_path}")
    df = pd.read_csv(csv_path, low_memory=False)

    df["tweet_id"] = df["id"] if "id" in df.columns else range(1, len(df)+1)
    df["text"] = df["fullText"].fillna("") if "fullText" in df.columns else df.get("text", "").fillna("")

    if "createdAt" not in df.columns:
        raise ValueError("Brakuje kolumny 'createdAt' w pliku z tweetami.")
    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce", utc=True)

    # --- filtr po czasie, dokładnie jak chciałaś
    lo = pd.to_datetime(prices_min, utc=True)
    hi = pd.to_datetime(prices_max, utc=True)
    df = df[(df["created_at"] >= lo) & (df["created_at"] <= hi)]

    df = df.dropna(subset=["created_at"]).sort_values("created_at").reset_index(drop=True)
    return df[["tweet_id", "text", "created_at"]]

def load_prices_from_dir(base_dir: str) -> pd.DataFrame:
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(f"Brak katalogu z cenami: {base_dir}")
    files = glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True)
    if not files:
        raise FileNotFoundError(f"Nie znaleziono plików CSV w {base_dir}")

    frames = []
    for path in files:
        try:
            raw = pd.read_csv(path, low_memory=False)
            dt_col = next((c for c in ["datetime","time","timestamp","date","Date","Time"] if c in raw.columns), None)
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
            frames.append(part)
        except Exception as e:
            print(f"[prices] pomijam {path}: {e}")
    if not frames:
        raise RuntimeError("Nie udało się wczytać żadnych danych cenowych.")
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)

def build_minute_open_series(prices_df: pd.DataFrame) -> pd.Series:
    """
    >>> UWAGA: korzystamy z OPEN (nie close) <<<
    Dla każdej minuty bierzemy ostatni rekord i jego 'open'.
    """
    df = prices_df.copy()
    df["minute"] = df["datetime"].dt.floor("min")
    per_min = df.sort_values("datetime").groupby("minute").last()
    s = per_min["open"].astype(float)   # <--- OPEN
    s.index = pd.to_datetime(s.index)
    return s

def pct_changes_for_tweet(minute_series: pd.Series, tweet_dt_utc: pd.Timestamp,
                          intervals=(1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,30,60)):
    base_minute = pd.Timestamp(tweet_dt_utc).floor("min")
    base = minute_series.get(base_minute, None)  # OPEN w minucie tweeta
    out = {}
    for m in intervals:
        target = base_minute + pd.Timedelta(minutes=m)
        p = minute_series.get(target, None)      # OPEN w minucie +m
        if base is not None and p is not None and base != 0:
            out[m] = round((p - base) / base * 100.0, 2)
        else:
            out[m] = None
    return out, base

def run(limit: int = 0, prices_min: str = PRICES_MIN, prices_max: str = PRICES_MAX, out_path: str = OUT_CSV):
    print("[1/4] Wczytuję tweety…")
    tweets = load_tweets(TWEETS_CSV, prices_min, prices_max)
    if limit and limit > 0:
        tweets = tweets.head(limit).copy()
    print(f"   ✓ {len(tweets)} tweetów po filtrze czasu; limit={limit or 'brak'}")

    print("[2/4] Wczytuję ceny…")
    prices = load_prices_from_dir(PRICES_DIR)
    print(f"   ✓ {len(prices)} wierszy cen")

    print("[3/4] Buduję serię minutową OPEN… (OPEN, nie close)")
    minute_open = build_minute_open_series(prices)
    min_dt, max_dt = minute_open.index.min(), minute_open.index.max()
    print(f"   ✓ Zakres cen: {min_dt} → {max_dt} (UTC)")

    print("[4/4] Liczę zmiany i zapisuję CSV…")
    intervals = tuple(list(range(1,21)) + [30,60])

    rows = []
    for r in tweets.itertuples(index=False):
        dt = pd.Timestamp(r.created_at)  # UTC
        if dt.floor("min") < min_dt or dt.floor("min") > max_dt:
            continue
        pct, base_price = pct_changes_for_tweet(minute_open, dt, intervals=intervals)
        if base_price is None:
            continue
        row = {
            "tweet_id": str(r.tweet_id),
            "datetime": pd.Timestamp(dt).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "text": r.text,
            "price_at_tweet_open": base_price,   # pomocniczo, można usunąć
        }
        for m in intervals:
            row[f"change_{m}m"] = pct[m]
        rows.append(row)

    out_df = pd.DataFrame(rows)
    out_df.to_csv(out_path, index=False)
    print(f"✓ Zapisano: {out_path}  (wierszy: {len(out_df)})")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Eksport tweetów do CSV z % zmianą (OPEN).")
    ap.add_argument("--limit", type=int, default=0, help="Policz tylko pierwsze N tweetów (0 = wszystkie).")
    ap.add_argument("--prices-min", type=str, default=PRICES_MIN, help="Dolna granica czasu (UTC).")
    ap.add_argument("--prices-max", type=str, default=PRICES_MAX, help="Górna granica czasu (UTC).")
    ap.add_argument("--preview", action="store_true", help="Zapisz do pliku preview i nadpisz --limit=3.")
    args = ap.parse_args()

    if args.preview:
        run(limit=3, prices_min=args.prices_min, prices_max=args.prices_max, out_path=OUT_CSV_PREVIEW)
    else:
        run(limit=args.limit, prices_min=args.prices_min, prices_max=args.prices_max, out_path=OUT_CSV)
