# dashboard_clean.py
import os
import time
import threading
import logging
from collections import deque
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import unquote
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import math

import pandas as pd

import dash
from dash import dcc, html, Input, Output, State, ctx
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from kiteconnect import KiteConnect, KiteTicker

# OpenInterest FastAPI app (mounted by wrapper)
import optioninterest as openinterest
from heatmap_impl import build_market_heatmap_figure


# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("turbotrades.dashboard")


# =============================================================================
# CONFIG
# =============================================================================
BASE = "/dash/"
IST = ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("KITE_API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError("Missing KITE_API_KEY / KITE_ACCESS_TOKEN environment variables.")

SEED_SLEEP_SEC = float(os.getenv("SEED_SLEEP_SEC", "0.35"))
LOOKBACK_SESSIONS = 20

# Hot Now window
HOT_WINDOW_SEC = 5 * 60
HOT_SAMPLE_SEC = 5
HOT_HISTORY_MAX_SEC = HOT_WINDOW_SEC + 10 * 60
RFACTOR_LOG_SCALE = float(os.getenv("RFACTOR_LOG_SCALE", "1.95"))
SECTOR_DIRR_DISPLAY_SCALE = float(os.getenv("SECTOR_DIRR_DISPLAY_SCALE", "1.95"))

# Hot Now filters
HOT_MIN_RET_PCT = float(os.getenv("HOT_MIN_RET_PCT", "0.25"))
HOT_MIN_RANGE_PCT = float(os.getenv("HOT_MIN_RANGE_PCT", "0.40"))

HVHR_N = int(os.getenv("HVHR_N", "20"))
HVHR_RFACTOR_Q = float(os.getenv("HVHR_RFACTOR_Q", "0.85"))
DIRR_CLIP = float(os.getenv("DIRR_CLIP", "8.0"))  # max per-stock momentum contribution

# PCR (NFO)
PCR_STRIKES_AROUND_ATM = int(os.getenv("PCR_STRIKES_AROUND_ATM", "12"))
PCR_CACHE_TTL_SEC = int(os.getenv("PCR_CACHE_TTL_SEC", "20"))
PCR_QUOTE_CHUNK = int(os.getenv("PCR_QUOTE_CHUNK", "180"))
NIFTY_SPOT_SYMBOL = os.getenv("NIFTY_SPOT_SYMBOL", "NSE:NIFTY 50")

# Background compute cadence
COMPUTE_CORE_EVERY_SEC = float(os.getenv("COMPUTE_CORE_EVERY_SEC", "2.0"))
COMPUTE_HOT_EVERY_SEC = float(os.getenv("COMPUTE_HOT_EVERY_SEC", "5.0"))
COMPUTE_PCR_EVERY_SEC = float(os.getenv("COMPUTE_PCR_EVERY_SEC", "5.0"))
COMPUTE_RVOL5_EVERY_SEC = float(os.getenv("COMPUTE_RVOL5_EVERY_SEC", "5.0"))
COMPUTE_SLEEP_SEC = float(os.getenv("COMPUTE_SLEEP_SEC", "0.20"))

RFACTOR_EMA: Dict[int, float] = {}
TOP_STICKY_BONUS = float(os.getenv("TOP_STICKY_BONUS", "0.0"))    # 0.00 disables stickiness
RFACTOR_EMA_ALPHA = float(os.getenv("RFACTOR_EMA_ALPHA", "0.45"))

_LAST_TOP15_G: set[str] = set()
_LAST_TOP15_L: set[str] = set()

SECTOR_PLOT_H_PX = int(os.getenv("SECTOR_PLOT_H_PX", "360"))

# Rolling RVOL5 (last 5 minutes)
RVOL5_WINDOW_SEC = int(os.getenv("RVOL5_WINDOW_SEC", "300"))

# Pacing curve (learned)
PACE_CURVE_READY = False
PACE_CUM_FRAC_MIN: List[float] = []
PACE_BUILD_STARTED = False
PACE_LOCK = threading.Lock()
PACE_CACHE_PATH = Path("/tmp/pace_curve_cache.json")

# Fallback U-curve
U_CURVE_READY = False
U_CUM_FRAC: List[float] = []

# Recency settings
RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "1.0"))
RECENCY_WINDOWS = [
    (300,  0.40),   # 5  min
    (900,  0.35),   # 15 min
    (1800, 0.25),   # 30 min
]


# =============================================================================
# KITE INIT
# =============================================================================
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)


# =============================================================================
# SECTORS / SYMBOLS
# =============================================================================
SECTOR_DEFINITIONS = {
    "METAL": [
        "ADANIENT", "APLAPOLLO", "BHARATFORG", "COALINDIA",
        "HINDALCO", "HINDZINC", "JSWSTEEL",
        "JINDALSTEL", "NMDC", "NATIONALUM",
        "SAIL", "TATASTEEL", "VEDL"
    ],
    "REALTY": [
        "PHOENIXLTD", "GODREJPROP", "LODHA",
        "OBEROIRLTY", "DLF", "PRESTIGE",
        "NBCC", "RVNL",
    ],
    "ENERGY": [
        "RELIANCE", "ONGC", "IOC", "BPCL", "OIL",
        "NTPC", "POWERGRID", "POWERINDIA",
        "TATAPOWER","JSWENERGY",
        "ADANIGREEN", "ADANIENSOL",
        "NHPC", "IREDA", "SUZLON", "INOXWIND",
        "WAAREEENER", "PREMIERENE",
        "PETRONET", "GAIL", "HINDPETRO"
    ],
    "AUTO": [
        "BOSCHLTD", "TIINDIA", "HEROMOTOCO",
        "M&M", "EICHERMOT", "EXIDEIND",
        "BAJAJ-AUTO", "ASHOKLEY",
        "MARUTI", "TVSMOTOR",
        "MOTHERSON", "SONACOMS",
        "UNOMINDA", "TMPV", "HYUNDAI", "AMBER"
    ],
    "IT": [
        "INFY", "TCS", "HCLTECH", "WIPRO",
        "TECHM", "LTM", "MPHASIS",
        "KPITTECH", "COFORGE", "PERSISTENT",
        "TATAELXSI", "OFSS", "CAMS", "NAUKRI", "KAYNES"
    ],
    "PHARMA": [
        "CIPLA", "ALKEM", "BIOCON", "DRREDDY",
        "MANKIND", "TORNTPHARM", "ZYDUSLIFE",
        "DIVISLAB", "LUPIN", 
        "LAURUSLABS", "FORTIS",
        "AUROPHARMA", "GLENMARK",
        "SUNPHARMA", 
        "MAXHEALTH", "APOLLOHOSP"
    ],
    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND",
        "BRITANNIA", "DABUR", "MARICO",
        "COLPAL", "GODREJCP",
        "TATACONSUM", "PATANJALI",
        "UNITDSPR", "RADICO",
        "VBL", "DMART", "NYKAA",
        "ETERNAL", "SWIGGY",
        "TITAN", "TRENT", "VMM",
        "KALYANKJIL", "JUBLFOOD",
        "ASIANPAINT"
    ],
    "CEMENT": [
        "ULTRACEMCO", "SHREECEM",
        "AMBUJACEM", "DALBHARAT",
        "GRASIM", "ASTRAL",
        "PIDILITIND", "SUPREMEIND"
    ],
    "FINSERVICE": [
        "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG",
        "ICICIPRULI", "ICICIGI", "SBILIFE",
        "HDFCLIFE", "LICI", "LICHSGFIN",
        "PNBHOUSING", "MUTHOOTFIN",
        "MANAPPURAM", "CHOLAFIN",
        "PFC", "RECLTD", "MOTILALOFS",
        "HDFCAMC", "360ONE",
        "KFINTECH", "NUVAMA",
        "PAYTM", "POLICYBZR",
        "SBICARD",
        "JIOFIN", "SHRIRAMFIN",
        "ANGELONE",
        "BSE", "CDSL", "MCX", "IRFC"
    ],
    "BANK": [
        "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK",
        "IDFCFIRSTB", "FEDERALBNK", "INDUSINDBK",
        "AUBANK", "BANDHANBNK", "RBLBANK"
    ],
    "PSUBANK": [
        "SBIN", "PNB", "BANKBARODA", "CANBK",
        "UNIONBANK", "BANKINDIA", "INDIANB",
    ],
    "DURABLES": [
        "BHARTIARTL", "INDUSTOWER",
        "HAVELLS", "KEI", "POLYCAB",
        "CROMPTON", "VOLTAS",
        "PGEL", "DIXON", "SRF"
    ],
    "LOGISTICS": [
        "CONCOR", "DELHIVERY", "INDIGO",
        "INDHOTEL", "IRCTC",
        "BLUESTARCO", "GMRAIRPORT",
        "PAGEIND", "UPL", "ADANIPORTS"
    ],
    "DEFENCE": [
        "ABB","BEL","BDL", "BHEL",
        "CGPOWER", "CUMMINSIND",
        "HAL", "LT", "MAZDOCK",
        "SIEMENS", "SOLARINDS"
    ],
    "NIFTY_50": [
        "ADANIENT", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
        "BAJAJFINSV", "BEL", "BHARTIARTL", "BPCL", "CIPLA", "COALINDIA",
        "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
        "HINDALCO", "HINDUNILVR", "ICICIBANK", "INFY", "INDIGO", "ITC",
        "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
        "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
        "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM",
        "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
        "TMPV", "ETERNAL"
    ],
}

ALL_SYMBOLS = sorted(set(sum(SECTOR_DEFINITIONS.values(), [])))

# Load instruments (NSE) and map to tokens/names
ins = pd.DataFrame(kite.instruments("NSE"))
ins = ins[ins["tradingsymbol"].isin(ALL_SYMBOLS)].copy()
symbol_to_token: Dict[str, int] = dict(zip(ins["tradingsymbol"], ins["instrument_token"]))
symbol_to_name: Dict[str, str] = (
    dict(zip(ins["tradingsymbol"], ins["name"])) if "name" in ins.columns else {s: "" for s in ALL_SYMBOLS}
)
TOKENS = sorted(symbol_to_token.values())


# =============================================================================
# LIVE / STATE (tick thread writes these)
# =============================================================================
LOCK = threading.Lock()

LAST_PRICE: Dict[int, float] = {}
DAY_VOL: Dict[int, float] = {}
LAST_OHLC: Dict[int, dict] = {}

LAST_TICK_TS = 0.0
LAST_TICK_DT: Optional[datetime] = None
TOTAL_TICKS = 0

TPS_WINDOW_SEC = 1.0
TPS_BUCKETS = deque()

HOT_HISTORY: Dict[int, deque] = {}  # token -> deque[(epoch, ltp, cumvol)]

EOD_SNAPSHOT: Dict[int, Dict[str, Any]] = {}
DAILY_STATS: Dict[int, Dict[str, Optional[float]]] = {}

DAILY_SEED_STARTED = False
DAILY_SEED_DONE = False
DAILY_SEED_PROGRESS = {"done": 0, "total": len(TOKENS)}
DAILY_SEED_ERRORS = 0


# =============================================================================
# HELPERS
# =============================================================================
def market_is_open_ist(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _record_tick_batch(count: int, last_dt: Optional[datetime]):
    global LAST_TICK_TS, LAST_TICK_DT, TOTAL_TICKS
    now = time.time()
    TOTAL_TICKS += int(count)

    TPS_BUCKETS.append((now, int(count)))
    cutoff = now - TPS_WINDOW_SEC
    while TPS_BUCKETS and TPS_BUCKETS[0][0] < cutoff:
        TPS_BUCKETS.popleft()

    LAST_TICK_TS = now
    LAST_TICK_DT = last_dt or datetime.now()


def _hot_history_push(token: int, epoch: float, ltp: float, cumvol: Optional[float]):
    dq = HOT_HISTORY.get(token)
    if dq is None:
        dq = deque()
        HOT_HISTORY[token] = dq

    # sample compression
    if dq and (epoch - dq[-1][0]) < HOT_SAMPLE_SEC:
        last_epoch, _, last_vol = dq[-1]
        dq[-1] = (last_epoch, float(ltp), float(cumvol) if cumvol is not None else last_vol)
    else:
        dq.append((float(epoch), float(ltp), float(cumvol) if cumvol is not None else None))

    cutoff = epoch - HOT_HISTORY_MAX_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()


# =============================================================================
# TICK PROCESSING
# =============================================================================
def update_from_tick(tick: dict):
    token = tick["instrument_token"]
    ltp = tick.get("last_price")
    cumvol = tick.get("volume_traded")
    ohlc = tick.get("ohlc") or {}
    ts = tick.get("exchange_timestamp") or datetime.now()

    if ltp is None:
        return None

    LAST_PRICE[token] = float(ltp)
    if cumvol is not None:
        DAY_VOL[token] = float(cumvol)
    if ohlc:
        LAST_OHLC[token] = ohlc

    _hot_history_push(token, time.time(), float(ltp), float(cumvol) if cumvol is not None else None)
    return ts


# =============================================================================
# U-SHAPED PACING CURVE (fallback)
# =============================================================================
def _build_u_shaped_cum_curve(total_mins: int = 375, a: float = 0.65, b: float = 0.65) -> List[float]:
    weights: List[float] = []
    for i in range(total_mins):
        x = (i + 0.5) / total_mins
        w = (x ** (a - 1.0)) * ((1.0 - x) ** (b - 1.0))
        weights.append(float(w))

    s = sum(weights) + 1e-12
    cum: List[float] = []
    run = 0.0
    for w in weights:
        run += w
        cum.append(run / s)

    cum[-1] = 1.0
    return cum


def init_u_curve_once():
    global U_CURVE_READY, U_CUM_FRAC
    if U_CURVE_READY:
        return
    U_CUM_FRAC = _build_u_shaped_cum_curve(total_mins=375, a=0.65, b=0.65)
    U_CURVE_READY = True


# =============================================================================
# LEARNED PACING CURVE (background build + cache)
# =============================================================================
def _pace_reference_tokens_all_sectors(max_per_sector: int = 2, max_total: int = 30) -> List[int]:
    picked_syms: List[str] = []
    for _sector, syms in SECTOR_DEFINITIONS.items():
        cands = [s for s in syms if s in symbol_to_token]
        if not cands:
            continue
        picked_syms.extend(cands[:max_per_sector])

    out: List[int] = []
    seen = set()
    for s in picked_syms:
        tok = symbol_to_token.get(s)
        if not tok or tok in seen:
            continue
        out.append(tok)
        seen.add(tok)
        if len(out) >= max_total:
            break
    return out if out else TOKENS[:5]


def _build_learned_pace_curve_from_history(days_back: int = 30) -> List[float]:
    total_mins = 375
    bins_5m = total_mins // 5  # 75

    toks = _pace_reference_tokens_all_sectors(max_per_sector=2, max_total=20)
    vol_sum = [0.0] * bins_5m
    vol_n = 0

    to_dt = datetime.now(IST)
    from_dt = to_dt - timedelta(days=days_back)
    today = datetime.now(IST).date()

    for tok in toks:
        candles = kite.historical_data(
            instrument_token=tok,
            from_date=from_dt,
            to_date=to_dt,
            interval="5minute",
            continuous=False,
            oi=False,
        )
        df = pd.DataFrame(candles)
        if df.empty:
            time.sleep(0.35)
            continue

        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        for d, g in df.groupby(df.index.date):
            if market_is_open_ist() and d == today:
                continue
            g = g.sort_index()
            if len(g) < 50:
                continue

            vols = g["volume"].astype(float).tolist()
            vols = vols[:bins_5m] + [0.0] * max(0, bins_5m - len(vols))
            for i in range(bins_5m):
                vol_sum[i] += float(vols[i])
            vol_n += 1

        time.sleep(0.35)

    if vol_n <= 3:
        raise RuntimeError("Not enough intraday history to build learned pacing curve")

    vol_avg = [v / vol_n for v in vol_sum]
    total = sum(vol_avg) + 1e-12
    weights_5m = [v / total for v in vol_avg]

    w_min: List[float] = []
    for w in weights_5m:
        w_min.extend([w / 5.0] * 5)

    cum: List[float] = []
    run = 0.0
    for w in w_min:
        run += w
        cum.append(run)

    cum[-1] = 1.0
    return cum


def _load_pace_cache_today() -> Optional[List[float]]:
    try:
        if not PACE_CACHE_PATH.exists():
            return None
        data = json.loads(PACE_CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("date") != str(datetime.now(IST).date()):
            return None
        curve = data.get("curve")
        if not isinstance(curve, list) or len(curve) != 375:
            return None
        return [float(x) for x in curve]
    except Exception:
        return None


def _save_pace_cache_today(curve: List[float]):
    try:
        PACE_CACHE_PATH.write_text(
            json.dumps({"date": str(datetime.now(IST).date()), "curve": curve}),
            encoding="utf-8",
        )
    except Exception:
        pass


def start_pace_curve_builder_once():
    global PACE_BUILD_STARTED, PACE_CURVE_READY, PACE_CUM_FRAC_MIN
    if PACE_BUILD_STARTED:
        return
    PACE_BUILD_STARTED = True

    def _run():
        global PACE_CURVE_READY, PACE_CUM_FRAC_MIN

        cached = _load_pace_cache_today()
        if cached:
            with PACE_LOCK:
                PACE_CUM_FRAC_MIN = cached
                PACE_CURVE_READY = True
            log.info("PACE curve loaded from cache (%s)", str(PACE_CACHE_PATH))
            return

        try:
            curve = _build_learned_pace_curve_from_history(days_back=30)
            with PACE_LOCK:
                PACE_CUM_FRAC_MIN = curve
                PACE_CURVE_READY = True
            _save_pace_cache_today(curve)
            log.info("PACE curve built from intraday history (len=%s)", len(curve))
        except Exception as e:
            log.warning("PACE curve build failed -> using fallback pacing. err=%r", e)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# TIME FACTOR (paced expected cumulative fraction)
# =============================================================================
def _time_factor_ist_for_rvol(now_ist: Optional[datetime] = None) -> float:
    now_ist = now_ist or datetime.now(IST)

    m_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    m_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    total_mins = 375

    if now_ist <= m_open:
        mins_passed = 1
    elif now_ist >= m_close:
        mins_passed = total_mins
    else:
        mins_passed = int((now_ist - m_open).total_seconds() // 60)
        mins_passed = max(1, min(total_mins, mins_passed))

    idx = mins_passed - 1

    with PACE_LOCK:
        if PACE_CURVE_READY and len(PACE_CUM_FRAC_MIN) == total_mins:
            tf = float(PACE_CUM_FRAC_MIN[idx])
        elif U_CURVE_READY and len(U_CUM_FRAC) == total_mins:
            tf = float(U_CUM_FRAC[idx])
        else:
            tf = mins_passed / float(total_mins)

    return max(0.01, min(1.0, float(tf)))


# =============================================================================
# DAILY STATS SEED
# =============================================================================
def compute_20d_daily_stats_and_eod(token: int, days_back: int = 220) -> Dict[str, Any]:
    to_dt = datetime.now(IST)
    from_dt = to_dt - timedelta(days=days_back)

    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=to_dt,
        interval="day",
        continuous=False,
        oi=False,
    )
    df = pd.DataFrame(candles)
    if df.empty or len(df) < LOOKBACK_SESSIONS + 2:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    df["date"] = pd.to_datetime(df["date"])
    df["d"] = df["date"].dt.date
    today_ist = datetime.now(IST).date()

    if market_is_open_ist() and df.iloc[-1]["d"] == today_ist:
        df = df.iloc[:-1].copy()

    if len(df) < LOOKBACK_SESSIONS + 1:
        return {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    eod = {
        "date": last["d"],
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
        "prev_close": float(prev["close"]),
    }

    df_stats = df.tail(LOOKBACK_SESSIONS).copy()
    df_stats["range"] = (df_stats["high"] - df_stats["low"]).astype(float)
    df_stats["oc_ret_pct"] = (df_stats["close"] - df_stats["open"]) / df_stats["open"] * 100.0
    df_stats = df_stats.dropna()

    return {
        "avg_vol_20": float(df_stats["volume"].mean()) if not df_stats.empty else None,
        "avg_range_20": float(df_stats["range"].mean()) if not df_stats.empty else None,
        "avg_abs_oc_ret_20": float(df_stats["oc_ret_pct"].abs().mean()) if not df_stats.empty else None,
        "eod": eod,
    }


def seed_daily_stats_once(per_req_sleep: float = SEED_SLEEP_SEC):
    global DAILY_SEED_STARTED, DAILY_SEED_DONE, DAILY_SEED_ERRORS
    if DAILY_SEED_STARTED:
        return
    DAILY_SEED_STARTED = True

    def _run():
        global DAILY_SEED_DONE, DAILY_SEED_ERRORS
        DAILY_SEED_PROGRESS["total"] = len(TOKENS)
        DAILY_SEED_PROGRESS["done"] = 0

        for i, tok in enumerate(TOKENS, start=1):
            try:
                st = compute_20d_daily_stats_and_eod(tok)
            except Exception:
                DAILY_SEED_ERRORS += 1
                st = {"avg_vol_20": None, "avg_range_20": None, "avg_abs_oc_ret_20": None, "eod": None}

            with LOCK:
                DAILY_STATS[tok] = {
                    "avg_vol_20": st.get("avg_vol_20"),
                    "avg_range_20": st.get("avg_range_20"),
                    "avg_abs_oc_ret_20": st.get("avg_abs_oc_ret_20"),
                }
                if st.get("eod"):
                    EOD_SNAPSHOT[tok] = st["eod"]

            DAILY_SEED_PROGRESS["done"] = i
            time.sleep(per_req_sleep)

        DAILY_SEED_DONE = True

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# PCR (NFO)
# =============================================================================
NFO_INS_DF: Optional[pd.DataFrame] = None
NFO_LOAD_STARTED = False
NFO_LOAD_ERR: Optional[str] = None
PCR_CACHE: Dict[str, Tuple[dict, float]] = {}


def load_nfo_instruments_once():
    global NFO_LOAD_STARTED
    if NFO_LOAD_STARTED:
        return
    NFO_LOAD_STARTED = True

    def _run():
        global NFO_INS_DF, NFO_LOAD_ERR
        try:
            df = pd.DataFrame(kite.instruments("NFO"))
            df = df[df["instrument_type"].isin(["CE", "PE"])].copy()
            df = df[df["name"] == "NIFTY"].copy()
            df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
            NFO_INS_DF = df
            log.info("Loaded NFO instruments (NIFTY only): %s rows", len(df))
        except Exception as e:
            NFO_LOAD_ERR = repr(e)
            log.exception("Failed to load NFO instruments")

    threading.Thread(target=_run, daemon=True).start()


def _chunk(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


def _quote_many(keys: List[str], chunk_size: int = PCR_QUOTE_CHUNK) -> dict:
    out = {}
    for ch in _chunk(keys, chunk_size):
        out.update(kite.quote(ch))
    return out


def _infer_strike_step(strikes: pd.Series) -> float:
    s = sorted(set(float(x) for x in strikes.dropna().tolist()))
    if len(s) < 3:
        return 50.0
    diffs = [b - a for a, b in zip(s, s[1:]) if (b - a) > 0]
    if not diffs:
        return 50.0
    diffs.sort()
    return float(diffs[len(diffs) // 2])


def compute_real_nifty_oi_pcr(strikes_around_atm: int = PCR_STRIKES_AROUND_ATM) -> Optional[dict]:
    cache_key = f"NIFTY:oi:{strikes_around_atm}"
    cached = PCR_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    if NFO_LOAD_ERR or NFO_INS_DF is None:
        return None

    try:
        spot = float(kite.ltp([NIFTY_SPOT_SYMBOL])[NIFTY_SPOT_SYMBOL]["last_price"])
    except Exception:
        return None

    dfu = NFO_INS_DF
    if dfu is None or dfu.empty:
        return None

    expiry = min(dfu["expiry"].tolist()) if len(dfu) else None
    if not expiry:
        return None

    dfe = dfu[dfu["expiry"] == expiry].copy()
    if dfe.empty:
        return None

    step = _infer_strike_step(dfe["strike"])
    atm = round(spot / step) * step

    lo = atm - strikes_around_atm * step
    hi = atm + strikes_around_atm * step
    dfe = dfe[(dfe["strike"] >= lo) & (dfe["strike"] <= hi)].copy()
    if dfe.empty:
        return None

    ce = dfe[dfe["instrument_type"] == "CE"]
    pe = dfe[dfe["instrument_type"] == "PE"]

    ce_keys = ["NFO:" + s for s in ce["tradingsymbol"].tolist()]
    pe_keys = ["NFO:" + s for s in pe["tradingsymbol"].tolist()]
    keys = ce_keys + pe_keys
    if not keys:
        return None

    try:
        q = _quote_many(keys, chunk_size=PCR_QUOTE_CHUNK)
    except Exception:
        return None

    ce_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in ce_keys)
    pe_oi = sum(float(q.get(k, {}).get("oi") or 0.0) for k in pe_keys)
    pcr = pe_oi / (ce_oi + 1e-9)

    data = {
        "underlying": "NIFTY",
        "expiry": str(expiry),
        "spot": spot,
        "atm": atm,
        "step": step,
        "range": [float(lo), float(hi)],
        "ce_oi": float(ce_oi),
        "pe_oi": float(pe_oi),
        "pcr": float(pcr),
        "strikes": int(len(dfe)),
        "updated_at": datetime.now(IST).strftime("%H:%M:%S"),
    }

    PCR_CACHE[cache_key] = (data, time.time() + PCR_CACHE_TTL_SEC)
    return data


def pcr_label_from_value(pcr: float) -> str:
    if pcr >= 1.40:
        return "STRONG BUY"
    if pcr >= 1.10:
        return "BUY"
    if pcr >= 0.90:
        return "NEUTRAL"
    if pcr >= 0.60:
        return "SELL"
    return "STRONG SELL"


# =============================================================================
# SNAPSHOTS
# =============================================================================
def _snapshot_state(include_hot: bool = False) -> Dict[str, Any]:
    with LOCK:
        snap = {
            "price": dict(LAST_PRICE),
            "vol": dict(DAY_VOL),
            "ohlc": dict(LAST_OHLC),
            "eod": dict(EOD_SNAPSHOT),
            "daily": dict(DAILY_STATS),
            "tokens": list(TOKENS),
        }
        if include_hot:
            snap["hot"] = {tok: list(dq) for tok, dq in HOT_HISTORY.items()}
    return snap


def _get_live_or_eod_state_from_snap(token: int, snap: Dict[str, Any]) -> Optional[Tuple[float, float, dict]]:
    ltp = snap["price"].get(token)
    vol_today = snap["vol"].get(token)
    ohlc = snap["ohlc"].get(token) or {}

    if (
        ltp is not None
        and vol_today is not None
        and ohlc.get("open") is not None
        and ohlc.get("close") is not None
    ):
        return float(ltp), float(vol_today), ohlc

    e = (snap.get("eod") or {}).get(token)
    if not e or e.get("prev_close") is None:
        return None

    ohlc_eod = {"open": e["open"], "high": e["high"], "low": e["low"], "close": e["prev_close"]}
    return float(e["close"]), float(e["volume"]), ohlc_eod


# =============================================================================
# RECENCY HELPERS (from HOT_HISTORY)
# =============================================================================
def _get_price_at_cutoff(series: List[Tuple[float, float, Optional[float]]], cutoff_epoch: float) -> Optional[float]:
    result = None
    for t, p, _v in series:
        if float(t) <= cutoff_epoch:
            result = float(p)
        else:
            break
    return result


def _get_vol_at_cutoff(series: List[Tuple[float, float, Optional[float]]], cutoff_epoch: float) -> Optional[float]:
    result = None
    for t, _p, v in series:
        if float(t) <= cutoff_epoch:
            if v is not None:
                result = float(v)
        else:
            break
    return result


def _compute_recency_factors(
    token: int,
    current_ltp: float,
    current_cumvol: Optional[float],
    avg_vol_20: float,
    window_sec: int,
) -> Dict[str, float]:
    fallback = {"recent_pct": 0.0, "recent_rvolm": 1.0, "has_data": 0.0}
    with LOCK:
        dq = HOT_HISTORY.get(token)
        if not dq or len(dq) < 2:
            return fallback
        series = list(dq)

    now_epoch = float(series[-1][0])
    cutoff = now_epoch - float(window_sec)

    base_price = _get_price_at_cutoff(series, cutoff)
    if base_price is None:
        base_price = float(series[0][1])
    if not base_price or float(base_price) <= 0:
        return fallback

    recent_pct = (float(current_ltp) - float(base_price)) / (float(base_price) + 1e-9) * 100.0

    base_vol = _get_vol_at_cutoff(series, cutoff)
    recent_vol = None
    if base_vol is not None and current_cumvol is not None:
        recent_vol = max(0.0, float(current_cumvol) - float(base_vol))

    # expected volume share (simple time share of day)
    window_mins = float(window_sec) / 60.0
    expected_recent = float(avg_vol_20) * (window_mins / 375.0)
    recent_rvolm = (
        float(recent_vol) / (expected_recent + 1e-9)
        if recent_vol is not None and expected_recent > 0
        else 1.0
    )

    return {"recent_pct": float(recent_pct), "recent_rvolm": float(recent_rvolm), "has_data": 1.0}


def _compute_recency_multiplier(pct_open: float, recent_pct: float, recent_rvolm: float) -> float:
    abs_session = abs(float(pct_open))
    abs_recent = abs(float(recent_pct))

    if abs_session < 1e-6:
        price_recency = 0.5
    else:
        price_recency = min(abs_recent / (abs_session + 1e-9), 1.5)
        price_recency = max(0.0, min(1.0, price_recency))

    same_direction = (
        (float(pct_open) >= 0 and float(recent_pct) >= 0)
        or
        (float(pct_open) < 0 and float(recent_pct) < 0)
    )
    direction_factor = 1.0 if same_direction else 0.35

    vol_recency = min(float(recent_rvolm) / 2.0, 1.0)
    vol_recency = max(0.10, vol_recency)

    recency_multiplier = (0.65 * price_recency * direction_factor) + (0.35 * vol_recency)
    return float(max(0.05, min(1.0, recency_multiplier)))


def _compute_recency_multiplier_multi(
    token: int,
    pct_open: float,
    current_ltp: float,
    current_cumvol: Optional[float],
    avg_vol_20: float,
) -> float:
    total_weight = 0.0
    weighted_sum = 0.0

    for window_sec, weight in RECENCY_WINDOWS:
        rf = _compute_recency_factors(
            token=token,
            current_ltp=current_ltp,
            current_cumvol=current_cumvol,
            avg_vol_20=avg_vol_20,
            window_sec=window_sec,
        )
        if float(rf["has_data"]) == 0.0:
            continue

        mult = _compute_recency_multiplier(
            pct_open=pct_open,
            recent_pct=float(rf["recent_pct"]),
            recent_rvolm=float(rf["recent_rvolm"]),
        )
        weighted_sum += mult * weight
        total_weight += weight

    if total_weight <= 0:
        return 1.0
    return float(weighted_sum / total_weight)


# =============================================================================
# RFACTOR (recency-aware)
# =============================================================================
def _compute_rfactor_row_snap(token: int, snap: Dict[str, Any]) -> Optional[Dict[str, float]]:
    state_ = _get_live_or_eod_state_from_snap(token, snap)
    if not state_:
        return None

    ltp, vol_today, ohlc = state_
    prev_close = ohlc.get("close")
    day_open   = ohlc.get("open")
    day_high   = ohlc.get("high")
    day_low    = ohlc.get("low")

    if prev_close is None or day_open is None or day_high is None or day_low is None:
        return None

    try:
        prev_close = float(prev_close)
        day_open   = float(day_open)
        day_high   = float(day_high)
        day_low    = float(day_low)
        ltp        = float(ltp)
        vol_today  = float(vol_today)
    except Exception:
        return None

    if prev_close <= 0 or day_open <= 0 or ltp <= 0:
        return None

    eps = 1e-9

    gap_pct      = ((day_open - prev_close) / prev_close) * 100.0
    pct_open     = ((ltp - day_open)        / day_open)   * 100.0
    range_pct_day = ((day_high - day_low)   / day_open)   * 100.0   # NEW: used for inactivity check

    st = (snap.get("daily") or {}).get(token) or {}
    avg_vol_20       = st.get("avg_vol_20")
    avg_range_20     = st.get("avg_range_20")
    avg_abs_oc_ret_20 = st.get("avg_abs_oc_ret_20")

    if avg_vol_20 is None or avg_range_20 is None or avg_abs_oc_ret_20 is None:
        return None

    try:
        avg_vol_20        = float(avg_vol_20)
        avg_range_20      = float(avg_range_20)
        avg_abs_oc_ret_20 = float(avg_abs_oc_ret_20)
    except Exception:
        return None

    if avg_vol_20 <= 0 or avg_range_20 <= 0 or avg_abs_oc_ret_20 <= 0:
        return None

    # ---- relative volume vs expected by time-of-day ----
    tf           = _time_factor_ist_for_rvol(datetime.now(IST))
    expected_vol = avg_vol_20 * tf
    rvolm        = vol_today / (expected_vol + eps)

    # ---- relative range ----
    range_today  = max(0.0, day_high - day_low)
    range_factor = range_today / (avg_range_20 + eps)

    # ---- relative move size ----
    move_factor = abs(pct_open) / (avg_abs_oc_ret_20 + eps)

    rfactor_val = rvolm * range_factor * move_factor

    # ---- freshness in day's range (near high if up, near low if down) ----
    range_span       = max(day_high - day_low, eps)
    position_in_range = (ltp - day_low) / range_span
    position_in_range = max(0.0, min(1.0, position_in_range))

    freshness    = (position_in_range ** 3) if pct_open >= 0 else ((1.0 - position_in_range) ** 3)
    rfactor_val *= freshness

    # ==========================================================================
    # SIDEWAYS / CONSOLIDATION DAMPENER
    #
    # Two distinct checks so we never penalise a stock that moved 5% and rested:
    #
    #  1. INACTIVITY  – total day range (High-Low) is tiny all day
    #                   → stock never moved → heavy penalty
    #
    #  2. STAGNATION  – stock DID produce a meaningful range but price has been
    #                   flat in a very tight box over the last N minutes
    #                   → mild penalty so fresh breakouts rank above it
    # ==========================================================================

    # ---- Tunable knobs (override via env vars) ----
    INACTIVE_RANGE_THR   = float(os.getenv("INACTIVE_RANGE_THR",   "0.60"))   # day-range% below = inactive
    INACTIVE_MULT        = float(os.getenv("INACTIVE_MULT",         "0.12"))   # penalty multiplier for inactive
    STAGNATION_BOX_PCT   = float(os.getenv("STAGNATION_BOX_PCT",   "0.15"))   # 15-min box tighter than this → stagnating
    STAGNATION_WINDOW_SEC = float(os.getenv("STAGNATION_WINDOW_SEC","900"))    # look-back window (seconds)
    STAGNATION_MULT      = float(os.getenv("STAGNATION_MULT",       "0.65"))   # penalty multiplier for stagnation

    # ---- 1. INACTIVITY: Has the stock produced any meaningful range today? ----
    if range_pct_day < INACTIVE_RANGE_THR:
        # Never really moved all day → dead stock, suppress heavily
        inactivity_mult = float(INACTIVE_MULT)
    else:
        inactivity_mult = 1.0

    # ---- 2. STAGNATION: Is the stock flat RIGHT NOW vs earlier today? --------
    #
    # Key insight: a stock that moved +5% and is consolidating has
    #   range_pct_day = 5% → passes inactivity check above (inactivity_mult=1.0)
    # We still want a mild dampening if it has been completely flat for 15 min,
    # so that an actively breaking-out stock ranks above it.
    #
    # We do NOT penalise further when range_pct_day < INACTIVE_RANGE_THR
    # (inactivity already handles that case with a harder floor).
    # --------------------------------------------------------------------------
    stagnation_mult = 1.0
    if market_is_open_ist() and inactivity_mult == 1.0:
        try:
            with LOCK:
                dq = HOT_HISTORY.get(token)
                series = list(dq) if dq else None

            if series and len(series) > 4:
                now_epoch  = float(series[-1][0])
                cutoff_rec = now_epoch - float(STAGNATION_WINDOW_SEC)

                prices_rec = [
                    float(p)
                    for (t, p, _v) in series
                    if float(t) >= cutoff_rec and p is not None
                ]

                if len(prices_rec) >= 3:
                    hi_rec   = max(prices_rec)
                    lo_rec   = min(prices_rec)
                    box_pct  = ((hi_rec - lo_rec) / (day_open + eps)) * 100.0

                    if box_pct < float(STAGNATION_BOX_PCT):
                        # Price has barely moved in the last 15 minutes
                        # → mild dampening (not zero — it still had a big day)
                        stagnation_mult = float(STAGNATION_MULT)
        except Exception:
            stagnation_mult = 1.0   # safe fallback — never crash RFactor

    # ---- Combined dampening ----
    sideways_combined = inactivity_mult * stagnation_mult
    rfactor_val      *= sideways_combined
    # ==========================================================================
    # END SIDEWAYS DAMPENER
    # ==========================================================================

    # ---- recency multiplier (only during market hours) ----
    recency_mult = 1.0
    if market_is_open_ist():
        recency_mult = _compute_recency_multiplier_multi(
            token=token,
            pct_open=pct_open,
            current_ltp=ltp,
            current_cumvol=vol_today,
            avg_vol_20=avg_vol_20,
        )

    rfactor_final = rfactor_val * ((1.0 - RECENCY_WEIGHT) + (RECENCY_WEIGHT * recency_mult))

    # ---- log-compress (monotonic, no hard cap) ----
    rfactor_comp = RFACTOR_LOG_SCALE * math.log1p(max(0.0, float(rfactor_final)))
    dirr         = (1.0 if pct_open >= 0 else -1.0) * rfactor_comp

    return {
        "gap_pct":        float(gap_pct),
        "pct_open":       float(pct_open),
        "rfactor":        float(rfactor_comp),
        "rfactor_raw":    float(rfactor_final),
        "recency_mult":   float(recency_mult),
        "inactivity_mult": float(inactivity_mult),   # debug
        "stagnation_mult": float(stagnation_mult),   # debug
        "dirr":           float(dirr),
        "ltp":            float(ltp),
        "day_open":       float(day_open),
        "vol_today":      float(vol_today),
    }

# =============================================================================
# MARKET SENTIMENT (proxy)
# =============================================================================
def _compute_market_sentiment_proxy_snap(snap: Dict[str, Any]) -> Dict[str, Any]:
    adv = dec = unch = 0
    for tok in snap.get("tokens") or []:
        st = _get_live_or_eod_state_from_snap(tok, snap)
        if not st:
            continue
        ltp, _v, ohlc = st
        op = ohlc.get("open")
        if op is None:
            continue
        try:
            opf = float(op)
            ltp = float(ltp)
        except Exception:
            continue
        if opf <= 0 or ltp <= 0:
            continue
        pct_open = (ltp - opf) / opf * 100.0
        if pct_open > 0:
            adv += 1
        elif pct_open < 0:
            dec += 1
        else:
            unch += 1

    total = adv + dec + unch
    score = (adv - dec) / total if total > 0 else 0.0
    if score >= 0.20:
        label = "BULLISH"
    elif score <= -0.20:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {"adv": adv, "dec": dec, "unch": unch, "total": total, "score": float(score), "label": label}


# =============================================================================
# SECTOR AGGREGATES (includes rolling %CHANGE)
# =============================================================================
def _compute_sector_aggregates_from_rr_with_daily(
    rr_by_tok: Dict[int, Dict[str, float]],
    daily_map: Dict[int, Dict[str, Optional[float]]],
) -> Dict[str, Dict[str, float]]:
    """
    Sector aggregates computed from per-token rr rows + daily stats:

      - DirR: turnover-weighted momentum (sqrt(turnover) weighted mean of stock DirR)
      - %ChangeMean: turnover-weighted mean of stock %Change (pct_open)
      - RVOLm*: participation aggregates based on pct_open sign (buy/sell splits)
      - RVOL5*: rolling 5-minute RVOL aggregates (buy/sell splits)

    Returns:
      dict[sector] -> metrics dict
    """
    DIRR_CLIP_LOCAL = float(os.getenv("DIRR_CLIP", "8.0"))  # cap per-stock DirR contribution

    now_ist = datetime.now(IST)
    now_epoch = time.time()

    tf_day = _time_factor_ist_for_rvol(now_ist)
    tf_now = _time_factor_ist_for_rvol(now_ist)

    want_rvol5 = market_is_open_ist(now_ist)
    cutoff_epoch = now_epoch - float(RVOL5_WINDOW_SEC)

    def _cumvol_at_or_before(series: List[Tuple[float, float, Optional[float]]], cutoff: float):
        base_t = None
        base_v = None
        for t, _p, v in series:
            if float(t) <= float(cutoff):
                if v is not None:
                    base_t = float(t)
                    base_v = float(v)
            else:
                break

        if base_v is not None:
            return base_t, base_v

        # fallback: earliest known cumvol
        for t, _p, v in series:
            if v is not None:
                return float(t), float(v)

        return None, None

    # Snapshot HOT_HISTORY once to avoid locking per cell while computing RVOL5
    hot_snap: Dict[int, List[Tuple[float, float, Optional[float]]]] = {}
    if want_rvol5:
        with LOCK:
            for tok in rr_by_tok.keys():
                dq = HOT_HISTORY.get(tok)
                if dq:
                    hot_snap[tok] = list(dq)

    out: Dict[str, Dict[str, float]] = {}

    for sector, syms in SECTOR_DEFINITIONS.items():
        # ---- DirR (money-flow / turnover weighted) ----
        dirr_num = 0.0
        dirr_den = 0.0

        # ---- %Change mean (turnover weighted) ----
        chg_num = 0.0
        chg_den = 0.0

        # ---- RVOLm aggregates ----
        buy_sum = sell_sum = 0.0
        buy_n = sell_n = 0

        # ---- RVOL5 aggregates ----
        buy5_sum = sell5_sum = 0.0
        buy5_n = sell5_n = 0

        for s in syms:
            tok = symbol_to_token.get(s)
            if not tok:
                continue

            rr = rr_by_tok.get(tok)
            if not rr:
                continue

            # ---------- shared fields ----------
            ltp_ = float(rr.get("ltp") or 0.0)
            vol_ = float(rr.get("vol_today") or 0.0)
            pct_open = rr.get("pct_open")  # may be None

            turnover = max(0.0, ltp_ * vol_)
            w = math.sqrt(turnover + 1e-9)  # soft weight so one stock doesn't dominate

            # ---------- DirR weighted ----------
            mom = float(rr.get("dirr") or 0.0)
            if mom > DIRR_CLIP_LOCAL:
                mom = DIRR_CLIP_LOCAL
            elif mom < -DIRR_CLIP_LOCAL:
                mom = -DIRR_CLIP_LOCAL

            dirr_num += mom * w
            dirr_den += w

            # ---------- %Change weighted ----------
            if pct_open is not None:
                try:
                    chg_num += float(pct_open) * w
                    chg_den += w
                except Exception:
                    pass

            # ---------- RVOLm / RVOL5 buy-sell splits ----------
            st = daily_map.get(tok) or {}
            avg_vol_20 = st.get("avg_vol_20")
            if avg_vol_20 is None or pct_open is None:
                continue

            try:
                av = float(avg_vol_20)
                if av <= 0:
                    continue

                expected = av * float(tf_day)
                rvolm = float(vol_) / (expected + 1e-9)

                if float(pct_open) >= 0:
                    buy_sum += rvolm
                    buy_n += 1
                else:
                    sell_sum += rvolm
                    sell_n += 1

                if want_rvol5:
                    series = hot_snap.get(tok)
                    if series and len(series) >= 2:
                        base_t, base_v = _cumvol_at_or_before(series, cutoff_epoch)
                        if base_t is not None and base_v is not None:
                            vol5 = float(vol_) - float(base_v)
                            if vol5 >= 0:
                                eff_sec = max(
                                    5.0,
                                    min(float(RVOL5_WINDOW_SEC), now_epoch - float(base_t)),
                                )
                                then_ist = now_ist - timedelta(seconds=eff_sec)
                                tf_then = _time_factor_ist_for_rvol(then_ist)

                                frac = max(1e-4, float(tf_now) - float(tf_then))
                                exp5 = av * frac
                                rvol5 = float(vol5) / (float(exp5) + 1e-9)

                                if float(pct_open) >= 0:
                                    buy5_sum += rvol5
                                    buy5_n += 1
                                else:
                                    sell5_sum += rvol5
                                    sell5_n += 1

            except Exception:
                continue

        dirr_mean = (dirr_num / (dirr_den + 1e-9)) if dirr_den > 0 else 0.0
        chg_mean = (chg_num / (chg_den + 1e-9)) if chg_den > 0 else 0.0

        n_total = buy_n + sell_n
        net_sum = float(buy_sum - sell_sum)
        gross_sum = float(buy_sum + sell_sum)
        net_mean = float(net_sum / n_total) if n_total > 0 else 0.0
        gross_mean = float(gross_sum / n_total) if n_total > 0 else 0.0

        n5_total = buy5_n + sell5_n
        net5_sum = float(buy5_sum - sell5_sum)
        gross5_sum = float(buy5_sum + sell5_sum)
        net5_mean = float(net5_sum / n5_total) if n5_total > 0 else 0.0
        gross5_mean = float(gross5_sum / n5_total) if n5_total > 0 else 0.0

        out[sector] = {
            "DirR": float(dirr_mean),
            "%ChangeMean": float(chg_mean),

            "RVOLmBuySum": float(buy_sum),
            "RVOLmSellSum": float(sell_sum),
            "RVOLmNetSum": float(net_sum),
            "RVOLmGrossSum": float(gross_sum),
            "RVOLmNetMean": float(net_mean),
            "RVOLmGrossMean": float(gross_mean),
            "N": float(n_total),

            "RVOL5BuySum": float(buy5_sum),
            "RVOL5SellSum": float(sell5_sum),
            "RVOL5NetSum": float(net5_sum),
            "RVOL5GrossSum": float(gross5_sum),
            "RVOL5NetMean": float(net5_mean),
            "RVOL5GrossMean": float(gross5_mean),
            "N5": float(n5_total),
        }

    return out

def _quantile_threshold(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    q = min(max(float(q), 0.0), 1.0)
    vs = sorted(values)
    if len(vs) == 1:
        return float(vs[0])
    idx = int(round(q * (len(vs) - 1)))
    idx = min(max(idx, 0), len(vs) - 1)
    return float(vs[idx])


# =============================================================================
# HOT NOW computation
# =============================================================================
def _compute_hot_row_from_series(series: List[Tuple[float, float, Optional[float]]]) -> Optional[dict]:
    if not series or len(series) < 2:
        return None

    now_epoch = float(series[-1][0])
    cutoff = now_epoch - float(HOT_WINDOW_SEC)

    base = None
    for t, p, v in series:
        if float(t) <= cutoff:
            base = (float(t), p, v)
        else:
            break
    if base is None:
        base = (float(series[0][0]), series[0][1], series[0][2])

    base_t, base_p, base_v = base
    _t_last, last_p, last_v = series[-1]

    if base_p is None or float(base_p) <= 0 or last_p is None:
        return None

    prices = [float(p) for (t, p, _v) in series if float(t) >= base_t and p is not None]
    if len(prices) < 2:
        return None

    lo = float(min(prices))
    hi = float(max(prices))
    rng = float(hi - lo)

    base_pf = float(base_p)
    range_pct = (rng / (base_pf + 1e-9)) * 100.0
    up_spike_pct = (hi - base_pf) / (base_pf + 1e-9) * 100.0
    down_spike_pct = (lo - base_pf) / (base_pf + 1e-9) * 100.0
    spike_pct = up_spike_pct if abs(up_spike_pct) >= abs(down_spike_pct) else down_spike_pct

    vol_win = None
    if base_v is not None and last_v is not None:
        vol_win = float(last_v) - float(base_v)
        if vol_win < 0:
            vol_win = None

    return {"range_pct": float(range_pct), "spike_pct": float(spike_pct), "vol_win": vol_win}

# =============================================================================
# BACKGROUND COMPUTE CACHE
# =============================================================================
CACHE_LOCK = threading.Lock()
CACHE: Dict[str, Any] = {
    "sector_agg": {},
    "top15_gainers": [],
    "top15_losers": [],
    "hvhr_gainers": [],
    "hvhr_losers": [],
    "hot_gainers": [],
    "momo_sector_gainers": [],   # [{"sector": "FMCG", "rows": [...]}, {"sector": "PHARMA", "rows": [...]}]
    "momo_sector_losers": [],
    "momo_sector_label": "",
    "hot_losers": [],
    "heatmap_rows": [],
    "sentiment": {"adv": 0, "dec": 0, "unch": 0, "total": 0, "score": 0.0, "label": "NEUTRAL"},
    "pcr": None,

    # rolling last 5 min RVOL5 leaderboard
    "rvol5_buy": [],
    "rvol5_sell": [],
    "rvol5_label": "RVOL5: collecting…",

    "updated": {"core": 0.0, "hot": 0.0, "pcr": 0.0, "rvol5": 0.0},
}


# =============================================================================
# BACKGROUND COMPUTE LOOP
# =============================================================================
_compute_started = False


def start_compute_loop_once():
    global _compute_started
    if _compute_started:
        return
    _compute_started = True

    def _run():
        global _LAST_TOP15_G, _LAST_TOP15_L

        last_core = 0.0
        last_hot = 0.0
        last_pcr = 0.0

        while True:
            now = time.time()

            # --------------------
            # CORE (RFactor, sector agg, heatmap, + /volm sector leaders)
            # --------------------
            if (now - last_core) >= COMPUTE_CORE_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=False)

                    rr_by_tok: Dict[int, Dict[str, float]] = {}
                    rows_basic: List[dict] = []
                    rfactor_vals: List[float] = []

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue

                        rr = _compute_rfactor_row_snap(tok, snap)
                        if not rr:
                            continue

                        pct_raw = float(rr["pct_open"])
                        rf_raw = float(rr["rfactor"])

                        # ---- EMA smoothing (stabilizes rankings) ----
                        prev = RFACTOR_EMA.get(tok)
                        rf_ema = rf_raw if prev is None else (
                            (RFACTOR_EMA_ALPHA * rf_raw) + ((1.0 - RFACTOR_EMA_ALPHA) * prev)
                        )
                        RFACTOR_EMA[tok] = float(rf_ema)

                        # stable rr (used by sector agg / heatmap / volm)
                        rr_stable = dict(rr)
                        rr_stable["rfactor"] = float(rf_ema)
                        rr_stable["dirr"] = (1.0 if pct_raw >= 0 else -1.0) * float(rf_ema)
                        rr_by_tok[tok] = rr_stable

                        rows_basic.append({
                            "Symbol": sym,
                            "%Change": round(pct_raw, 2),          # display
                            "RFactor": round(float(rf_ema), 2),    # display
                            "_pct_raw": pct_raw,                   # raw sort
                            "_rf_sort": float(rf_ema),             # raw sort
                            "Vol": int(rr.get("vol_today") or 0),
                        })
                        rfactor_vals.append(float(rf_ema))

                    # ---- Top15 with stickiness (reduces churn) ----
                    def sort_score(row: dict, prev_set: set[str]) -> float:
                        base = float(row.get("_rf_sort") or 0.0)
                        if TOP_STICKY_BONUS > 0 and row.get("Symbol") in prev_set:
                            return base * (1.0 + TOP_STICKY_BONUS)
                        return base

                    gainers = [r for r in rows_basic if float(r.get("_pct_raw") or 0.0) > 0.0]
                    losers  = [r for r in rows_basic if float(r.get("_pct_raw") or 0.0) < 0.0]

                    gainers.sort(key=lambda r: sort_score(r, _LAST_TOP15_G), reverse=True)
                    losers.sort(key=lambda r: sort_score(r, _LAST_TOP15_L), reverse=True)

                    top15_gainers = gainers[:15]
                    top15_losers  = losers[:15]

                    _LAST_TOP15_G = set(r["Symbol"] for r in top15_gainers)
                    _LAST_TOP15_L = set(r["Symbol"] for r in top15_losers)

                    # ---- HVHR bucket (top quantile of rfactor) ----
                    thr = _quantile_threshold(rfactor_vals, float(HVHR_RFACTOR_Q)) if rfactor_vals else None
                    if thr is None:
                        hvhr_gainers, hvhr_losers = [], []
                    else:
                        bucket = [r for r in rows_basic if float(r.get("_rf_sort") or 0.0) >= float(thr)]
                        bucket_g = [r for r in bucket if float(r.get("_pct_raw") or 0.0) > 0.0]
                        bucket_l = [r for r in bucket if float(r.get("_pct_raw") or 0.0) < 0.0]

                        bucket_g.sort(key=lambda r: (int(r.get("Vol") or 0), float(r.get("_rf_sort") or 0.0)), reverse=True)
                        bucket_l.sort(key=lambda r: (int(r.get("Vol") or 0), float(r.get("_rf_sort") or 0.0)), reverse=True)

                        hvhr_gainers = bucket_g[: int(HVHR_N)]
                        hvhr_losers  = bucket_l[: int(HVHR_N)]

                    # ---- sector aggregates + sentiment ----
                    sector_agg = _compute_sector_aggregates_from_rr_with_daily(
                        rr_by_tok=rr_by_tok,
                        daily_map=(snap.get("daily") or {}),
                    )
                    sentiment = _compute_market_sentiment_proxy_snap(snap)

                    # ---- sector order (for heatmap grouping) ----
                    sector_order = sorted(
                        SECTOR_DEFINITIONS.keys(),
                        key=lambda sec: float((sector_agg.get(sec) or {}).get("DirR") or 0.0),
                        reverse=True,
                    )

                    # ---- heatmap rows ----
                    heat_rows: List[dict] = []
                    for sec in sector_order:
                        sym_scored: List[Tuple[float, str]] = []
                        for sym in SECTOR_DEFINITIONS.get(sec, []):
                            tok = symbol_to_token.get(sym)
                            if not tok:
                                continue
                            rr = rr_by_tok.get(tok)
                            if not rr:
                                continue
                            sym_scored.append((float(rr.get("dirr") or 0.0), sym))

                        sym_scored.sort(key=lambda x: x[0], reverse=True)

                        for _dirr, sym in sym_scored:
                            tok = symbol_to_token.get(sym)
                            if not tok:
                                continue
                            rr = rr_by_tok.get(tok)
                            if not rr:
                                continue

                            ltp_ = float(rr.get("ltp") or 0.0)
                            vol_ = float(rr.get("vol_today") or 0.0)
                            turnover = ltp_ * vol_
                            if turnover <= 0:
                                continue

                            heat_rows.append({
                                "sector_key": sec,
                                "sector_label": sec.replace("_", " ").upper(),
                                "symbol": sym,
                                "pct": float(rr.get("pct_open") or 0.0),
                                "dirr": float(rr.get("dirr") or 0.0),
                                "value": float(turnover),
                            })

                    # ---- /volm: Top 3 gainer sectors + Top 5 momentum stocks (abs), and Top 3 loser sectors + Top 5 momentum stocks (abs) ----
                    sec_scored = []
                    for sec in SECTOR_DEFINITIONS.keys():
                        d = (sector_agg.get(sec) or {})
                        sec_scored.append((sec, float(d.get("DirR") or 0.0)))

                    sec_scored.sort(key=lambda x: x[1], reverse=True)
                    top3_secs = [s for s, _ in sec_scored[:3]]
                    bot3_secs = [s for s, _ in sec_scored[-3:]]

                    def _top5_by_abs_momentum_in_sector(sec: str) -> List[dict]:
                        rows: List[dict] = []
                        for sym in SECTOR_DEFINITIONS.get(sec, []):
                            tok = symbol_to_token.get(sym)
                            if not tok:
                                continue
                            rr = rr_by_tok.get(tok)
                            if not rr:
                                continue

                            mom = float(rr.get("dirr") or 0.0)
                            rows.append({
                                "Symbol": sym,
                                "%Change": round(float(rr.get("pct_open") or 0.0), 2),
                                "Momentum": round(mom, 2),  # signed display
                            })

                        rows.sort(key=lambda r: abs(float(r.get("Momentum") or 0.0)), reverse=True)
                        return rows[:5]

                    momo_sector_gainers = [{"sector": s, "rows": _top5_by_abs_momentum_in_sector(s)} for s in top3_secs]
                    momo_sector_losers  = [{"sector": s, "rows": _top5_by_abs_momentum_in_sector(s)} for s in bot3_secs]

                    with CACHE_LOCK:
                        CACHE["sector_agg"] = sector_agg
                        CACHE["top15_gainers"] = top15_gainers
                        CACHE["top15_losers"] = top15_losers
                        CACHE["hvhr_gainers"] = hvhr_gainers
                        CACHE["hvhr_losers"] = hvhr_losers
                        CACHE["sentiment"] = sentiment
                        CACHE["heatmap_rows"] = heat_rows

                        # /volm data
                        CACHE["momo_sector_gainers"] = momo_sector_gainers
                        CACHE["momo_sector_losers"] = momo_sector_losers

                        CACHE["updated"]["core"] = now

                except Exception:
                    log.exception("compute loop: CORE crashed")

                last_core = now

            # --------------------
            # HOT NOW
            # --------------------
            if (now - last_hot) >= COMPUTE_HOT_EVERY_SEC:
                try:
                    snap = _snapshot_state(include_hot=True)
                    hot = snap.get("hot") or {}

                    rows = []
                    min_spike = float(HOT_MIN_RET_PCT)
                    min_rng = float(HOT_MIN_RANGE_PCT)

                    for sym in ALL_SYMBOLS:
                        tok = symbol_to_token.get(sym)
                        if not tok:
                            continue
                        series = hot.get(tok)
                        if not series:
                            continue
                        hr = _compute_hot_row_from_series(series)
                        if not hr:
                            continue

                        spike = float(hr["spike_pct"])
                        range_pct = float(hr["range_pct"])
                        if abs(spike) < min_spike or range_pct < min_rng:
                            continue

                        rows.append({
                            "Symbol": sym,
                            "_spike": spike,
                            "_abs_spike": abs(spike),
                            "SPIKE%": round(spike, 2),
                            "RNG5%": round(range_pct, 2),
                            "DAY RNG%": None,
                        })

                    gain = [r for r in rows if float(r["_spike"]) > 0]
                    loss = [r for r in rows if float(r["_spike"]) < 0]
                    gain.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)
                    loss.sort(key=lambda r: (float(r["_abs_spike"]), float(r["RNG5%"])), reverse=True)

                    hot_gainers = [{k: v for k, v in r.items() if not k.startswith("_")} for r in gain[:15]]
                    hot_losers  = [{k: v for k, v in r.items() if not k.startswith("_")} for r in loss[:15]]

                    with CACHE_LOCK:
                        CACHE["hot_gainers"] = hot_gainers
                        CACHE["hot_losers"] = hot_losers
                        CACHE["updated"]["hot"] = now

                except Exception:
                    log.exception("compute loop: HOT crashed")

                last_hot = now

            # --------------------
            # PCR
            # --------------------
            if (now - last_pcr) >= COMPUTE_PCR_EVERY_SEC:
                try:
                    p = compute_real_nifty_oi_pcr(strikes_around_atm=PCR_STRIKES_AROUND_ATM)
                    with CACHE_LOCK:
                        CACHE["pcr"] = p
                        CACHE["updated"]["pcr"] = now
                except Exception:
                    log.exception("compute loop: PCR crashed")

                last_pcr = now

            time.sleep(COMPUTE_SLEEP_SEC)

    threading.Thread(target=_run, daemon=True).start()

# =============================================================================
# TICKER
# =============================================================================
_started = False


def start_ticker_once():
    global _started
    if _started:
        return
    _started = True

    def _run():
        while True:
            try:
                kws = KiteTicker(API_KEY, ACCESS_TOKEN)

                def on_connect(ws, _resp):
                    log.info("WS CONNECTED")
                    ws.subscribe(TOKENS)
                    ws.set_mode(ws.MODE_FULL, TOKENS)

                def on_ticks(ws, ticks):
                    try:
                        last_dt = None
                        with LOCK:
                            for t in ticks:
                                ts = update_from_tick(t)
                                if ts and (last_dt is None or ts > last_dt):
                                    last_dt = ts
                            _record_tick_batch(len(ticks), last_dt)
                    except Exception:
                        log.exception("on_ticks crashed")

                kws.on_connect = on_connect
                kws.on_ticks = on_ticks
                kws.connect(threaded=True)

                while True:
                    time.sleep(2)

            except Exception:
                log.exception("Ticker loop crashed; restarting in 5s")
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()


# =============================================================================
# DASH APP
# =============================================================================
dash_app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
    requests_pathname_prefix=BASE,
    routes_pathname_prefix="/",
    assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    suppress_callback_exceptions=True,
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
)
server = dash_app.server


# =============================================================================
# UI: Shared components
# =============================================================================
def dial_component(prefix: str, title: str):
    return html.Div(
        html.Div(
            [
                html.Div(
                    [
                        html.Div([html.Div(className=f"dial-arc dial-arc-{prefix}")], className="dial-arc-clip"),
                        html.Div(id=f"{prefix}-needle", className="dial-needle", style={"--rot": "0deg"}),
                        html.Div(className="dial-center"),
                        html.Div(["STRONG", html.Br(), "SELL"], className="dial-label dial-ss"),
                        html.Div("SELL", className="dial-label dial-s"),
                        html.Div("NEUTRAL", className="dial-label dial-n"),
                        html.Div("BUY", className="dial-label dial-b"),
                        html.Div(["STRONG", html.Br(), "BUY"], className="dial-label dial-sb"),
                    ],
                    className="dial-arc-wrap",
                ),
                html.Div(title, className="dial-title"),
                html.Div("—", id=f"{prefix}-sub", className="dial-sub"),
            ],
            className=f"dial-card dial-{prefix}",
        )
    )


def _extract_sector_from_path(pn: str) -> Optional[str]:
    pn = (pn or "").strip()
    if "/sector/" not in pn:
        return None
    sector = unquote(pn.split("/sector/", 1)[1]).strip("/").upper()
    return sector or None


def _sector_modal_coldefs_desktop():
    return [
        {"field": "Symbol", "headerName": "STOCK", "minWidth": 120, "flex": 1, "cellRenderer": "SymbolCell"},
        {"field": "Company", "headerName": "COMPANY", "minWidth": 180, "flex": 2, "cellRenderer": "CompanyLinkCell"},
        {
            "field": "DirR", "headerName": "MOMENTUM", "minWidth": 110, "flex": 1, "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {"field": "Price", "headerName": "PRICE", "minWidth": 110, "flex": 1, "type": "rightAligned", "cellRenderer": "Num2Cell"},
        {
            "field": "%Change", "headerName": "%CHG", "minWidth": 110, "flex": 1, "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "RVOL5", "headerName": "RVOLm5", "minWidth": 110, "flex": 1, "type": "rightAligned",
            "valueFormatter": {"function": "params.value == null ? '—' : (params.value.toFixed(1) + 'x')"},
        },
        {"field": "RVOLm", "headerName": "RVOLm", "minWidth": 100, "flex": 1, "type": "rightAligned", "cellRenderer": "Num2Cell"},
    ]


def _sector_modal_coldefs_mobile():
    return [
        {"field": "Symbol", "headerName": "STOCK", "minWidth": 92, "flex": 2, "cellRenderer": "SymbolCell"},
        {
            "field": "DirR", "headerName": "MOMENTUM", "minWidth": 88, "flex": 1, "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {"field": "Price", "headerName": "Price", "minWidth": 72, "flex": 1, "type": "rightAligned", "cellRenderer": "Num2Cell"},
        {
            "field": "%Change", "headerName": "%CHG", "minWidth": 72, "flex": 1, "type": "rightAligned",
            "cellRenderer": "Pct2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
        {
            "field": "RVOL5", "headerName": "RVOLm5", "minWidth": 76, "flex": 1, "type": "rightAligned",
            "valueFormatter": {"function": "params.value == null ? '—' : (params.value.toFixed(1) + 'x')"},
        },
    ]


def sector_modal_component():
    grid_opts_desktop = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": True,
        "alwaysShowVerticalScroll": True,
        "domLayout": "normal",
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 120);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 120);"},
    }

    grid_opts_mobile = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": True,
        "alwaysShowVerticalScroll": False,
        "domLayout": "normal",
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
    }

    header = html.Div(
        [
            html.Div(id="sector-modal-title", className="tt-modal-title", children="SECTOR"),
            dcc.Link(
                dbc.Button("Close", color="secondary", outline=True, size="sm", className="tt-modal-close-btn"),
                href=BASE,
                refresh=False,
            ),
        ],
        className="d-flex justify-content-between align-items-center w-100",
    )

    return dbc.Modal(
        [
            dbc.ModalHeader(header, close_button=False),
            dbc.ModalBody(
                html.Div(
                    [
                        html.Div(
                            dag.AgGrid(
                                id="sector-modal-grid",
                                className="ag-theme-alpine-dark tt-modal-grid",
                                columnDefs=_sector_modal_coldefs_desktop(),
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": True, "resizable": True, "flex": 1},
                                dashGridOptions=grid_opts_desktop,
                                style={"height": "65vh", "width": "100%"},
                            ),
                            className="desktop-only",
                        ),
                        html.Div(
                            dag.AgGrid(
                                id="sector-modal-grid-m",
                                className="ag-theme-alpine-dark tt-modal-grid",
                                columnDefs=_sector_modal_coldefs_mobile(),
                                rowData=[],
                                defaultColDef={"sortable": True, "filter": False, "resizable": True, "flex": 1},
                                dashGridOptions=grid_opts_mobile,
                                style={"height": "72vh", "width": "100%"},
                            ),
                            className="mobile-only",
                        ),
                    ]
                )
            ),
        ],
        id="sector-modal",
        is_open=False,
        size="xl",
        centered=True,
        fullscreen="md-down",
        backdrop=True,
        keyboard=True,
    )


# =============================================================================
# PAGES
# =============================================================================
def sectors_page():
    top15_cols_desktop = [
        {"field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell", "minWidth": 140, "flex": 2,
         "headerClass": "h-left", "cellClass": "c-left"},
        {"field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill", "minWidth": 110, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
        {"field": "RFactor", "headerName": "MOMENTUM", "cellRenderer": "RfactorPill", "minWidth": 110, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
        {"field": "Vol", "headerName": "VOLUME", "cellRenderer": "VolPill", "minWidth": 120, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
    ]

    top15_cols_mobile = [
        {"field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell", "minWidth": 88, "flex": 2,
         "headerClass": "h-left", "cellClass": "c-left"},
        {"field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill", "minWidth": 70, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
        {"field": "RFactor", "headerName": "MOMENTUM", "cellRenderer": "RfactorPill", "minWidth": 74, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
        {"field": "Vol", "headerName": "VOL", "cellRenderer": "VolPill", "minWidth": 78, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
    ]

    grid_options_desktop = {"getRowId": {"function": "params.data.Symbol"}, "animateRows": False, "rowHeight": 40, "headerHeight": 40}
    grid_options_mobile = {
        "getRowId": {"function": "params.data.Symbol"},
        "animateRows": False,
        "rowHeight": 40,
        "headerHeight": 38,
        "onGridReady": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
        "onGridSizeChanged": {"function": "setTimeout(() => params.api.sizeColumnsToFit(), 80);"},
    }

    def build_grid(grid_id: str, height: str, coldefs: list, grid_opts: dict):
        return dag.AgGrid(
            id=grid_id,
            className="ag-theme-alpine-dark grid-wrap",
            columnDefs=coldefs,
            rowData=[],
            defaultColDef={"sortable": True, "resizable": True, "flex": 1},
            dashGridOptions=grid_opts,
            style={"height": height, "width": "100%"},
        )

    return html.Div(
        [
            dcc.Interval(id="refresh_sectors", interval=5000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(html.H4("Sectors", className="page-title page-title--pill mb-0"), width="auto"),
                    dbc.Col(
                        html.Div(
                            [
                                html.Div(
                                    dbc.RadioItems(
                                        id="sectors-sort",
                                        options=[
                                            {"label": "Sort: RVOLm", "value": "RVOLm"},
                                            {"label": "Sort: RVOLm Mean", "value": "RVOLmMean"},
                                            {"label": "Sort: %Change", "value": "%Change"},
                                            {"label": "Sort: Momentum", "value": "DirR"},
                                        ],
                                        value="DirR",
                                        inline=True,
                                        className="sectors-sort ms-2",
                                    ),
                                    className="desktop-only",
                                ),
                                html.Div(
                                    dbc.Select(
                                        id="sectors-sort-dd",
                                        options=[
                                            {"label": "RVOLm", "value": "RVOLm"},
                                            {"label": "RVOLm Mean", "value": "RVOLmMean"},
                                            {"label": "RVOLm5 Mean", "value": "RVOL5Mean"},
                                            {"label": "Momentum", "value": "DirR"},
                                        ],
                                        value="DirR",
                                        size="sm",
                                        className="sectors-sort-dd",
                                    ),
                                    className="mobile-only",
                                ),
                                dbc.Button(
                                    html.I(className="bi bi-sliders2-vertical"),
                                    id="baseline-toggle",
                                    color="secondary",
                                    outline=True,
                                    size="sm",
                                    className="ms-2",
                                ),
                            ],
                            className="d-flex align-items-center justify-content-start flex-wrap gap-2",
                        ),
                        width=True,
                    ),
                ],
                className="align-items-center g-2 mb-2",
            ),

            html.Div(id="sector-bars", className="sector-bars-wrap"),
            html.Hr(),

            html.Div(
                dbc.Row(
                    [
                        dbc.Col(
                            [
                                html.H6("Top 15 Gainers", className="tt-top15-title tt-top15-gainers"),
                                build_grid("top15-gainers-grid", "min(350px, 42vh)", top15_cols_desktop, grid_options_desktop),
                            ],
                            md=6,
                        ),
                        dbc.Col(
                            [
                                html.H6("Top 15 Losers", className="tt-top15-title tt-top15-losers"),
                                build_grid("top15-losers-grid", "350px", top15_cols_desktop, grid_options_desktop),
                            ],
                            md=6,
                        ),
                    ],
                    className="g-3",
                ),
                className="desktop-only",
            ),

            html.Div(
                dbc.Tabs(
                    [
                        dbc.Tab(label="Top 15 Gainers",
                                children=build_grid("top15-gainers-grid-m", "60vh", top15_cols_mobile, grid_options_mobile)),
                        dbc.Tab(label="Top 15 Losers",
                                children=build_grid("top15-losers-grid-m", "60vh", top15_cols_mobile, grid_options_mobile)),
                    ],
                    className="top15-tabs",
                ),
                className="mobile-only",
            ),

            html.Hr(),
            html.H6("Heatmap"),
            dcc.Graph(
                id="market-heatmap",
                config={"displayModeBar": True, "displaylogo": False, "responsive": True},
                style={"height": "75vh", "width": "100%"},
            ),

            html.Hr(),
            dbc.Row([dbc.Col(dial_component("sentiment", "BIAS"), md=6),
                     dbc.Col(dial_component("pcr", "PCR"), md=6)],
                    className="g-3"),
        ],
        className="page-wrap",
    )


def volm_page():
    cols = [
        {"field": "Symbol", "headerName": "STOCK", "cellRenderer": "SymbolCell", "minWidth": 120, "flex": 2},
        {"field": "%Change", "headerName": "%CHG", "cellRenderer": "PctPill", "minWidth": 90, "flex": 1,
         "headerClass": "ag-right-aligned-header", "cellClass": "ag-right-aligned-cell"},
        {
            "field": "Momentum", "headerName": "MOMENTUM", "minWidth": 110, "flex": 1, "type": "rightAligned",
            "cellRenderer": "Num2Cell",
            "cellClassRules": {"cell-pos": "params.value > 0", "cell-neg": "params.value < 0"},
        },
    ]

    grid_opts = {
        "getRowId": {"function": "params.data.Symbol"},
        "alwaysShowVerticalScroll": False,
        "animateRows": False,
        "onGridReady": {"function": "params.api.sizeColumnsToFit();"},
        "onGridSizeChanged": {"function": "params.api.sizeColumnsToFit();"},
    }

    def _grid(grid_id: str):
        return dag.AgGrid(
            id=grid_id,
            className="ag-theme-alpine-dark grid-wrap compact-grid",
            columnDefs=cols,
            rowData=[],
            defaultColDef={"sortable": True, "filter": True, "resizable": True},
            dashGridOptions=grid_opts,
            style={"height": "min(280px, 34vh)", "width": "100%"},
        )

    return html.Div(
        [
            dcc.Interval(id="refresh_volm", interval=5000, n_intervals=0),

            dbc.Row(
                [
                    dbc.Col(
                        dcc.Link("← Back", href=BASE, className="stat-chip", style={"textDecoration": "none"}),
                        width="auto",
                    ),
                ],
                className="align-items-center g-2 mb-2",
            ),

           html.H6("Top 3 Gainer Sectors", className="momo-section-heading mt-2"),
            dbc.Row(
                [
                    dbc.Col([html.H6(id="momo-g1-title", className="momo-sector-title mt-1"), _grid("momo-g1-grid")], md=4),
dbc.Col([html.H6(id="momo-g2-title", className="momo-sector-title mt-1"), _grid("momo-g2-grid")], md=4),
dbc.Col([html.H6(id="momo-g3-title", className="momo-sector-title mt-1"), _grid("momo-g3-grid")], md=4),
                ],
                className="g-2",
            ),

            html.Hr(),

            html.H6("Top 3 Loser Sectors", className="momo-section-heading mt-2"),
            dbc.Row(
                [
                   dbc.Col([html.H6(id="momo-l1-title", className="momo-sector-title mt-1"), _grid("momo-l1-grid")], md=4),
dbc.Col([html.H6(id="momo-l2-title", className="momo-sector-title mt-1"), _grid("momo-l2-grid")], md=4),
dbc.Col([html.H6(id="momo-l3-title", className="momo-sector-title mt-1"), _grid("momo-l3-grid")], md=4),
                ],
                className="g-2",
            ),
        ],
        className="page-wrap",
    )

# =============================================================================
# DASH ROOT LAYOUT
# =============================================================================
dash_app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Location(id="url"),
        dcc.Store(id="page-store"),
        dcc.Store(id="baseline-store", data="CENTER"),  # AUTO or CENTER
        dcc.Interval(id="top_refresh", interval=1000, n_intervals=0),

        html.Div(
            dbc.Row(
                [
                    dbc.Col(
                        html.Div([html.Img(src=dash.get_asset_url("turbotrades.svg"), className="tt-logo")], className="tt-brand"),
                        width="auto",
                        className="top-brand-col",
                    ),
                    dbc.Col(
                        html.Div(id="top-stats"),
                        width=True,
                        className="top-stats-col",
                        style={"minWidth": 0},
                    ),
                    dbc.Col(
                        html.Button(
                            [html.I(className="bi bi-sun icon-sun", **{"aria-hidden": "true"}),
                             html.I(className="bi bi-moon-stars icon-moon", **{"aria-hidden": "true"})],
                            id="themeToggle",
                            className="theme-toggle",
                            type="button",
                            title="Toggle theme",
                            **{"aria-label": "Toggle theme"},
                        ),
                        width="auto",
                        className="top-theme-col",
                    ),
                    dbc.Col(
                        dbc.Button(
                            "LogOff",
                            href="/auth/logout",
                            external_link=True,
                            color="danger",
                            outline=True,
                            size="sm",
                            className="tt-logout-btn",
                            style={"fontWeight": "700"},
                        ),
                        width="auto",
                        className="top-logoff-col",
                    ),
                ],
                className="topbar-row align-items-center g-2",
            ),
            className="topbar-wrap",
        ),

        html.Div(id="app-body"),
        sector_modal_component(),
    ],
)

# =============================================================================
# ROUTER
# =============================================================================
def _classify_page(pathname: str) -> str:
    pn = (pathname or "").strip() or "/"
    volm_paths = {"/volm", "/volm/", f"{BASE}volm", f"{BASE}volm/"}
    oi_paths = {"/openinterest", "/openinterest/", f"{BASE}openinterest", f"{BASE}openinterest/"}

    if pn in volm_paths:
        return "volm"
    if pn in oi_paths:
        return "openinterest"
    return "sectors"


@dash_app.callback(
    Output("app-body", "children"),
    Output("page-store", "data"),
    Input("url", "pathname"),
    State("page-store", "data"),
)
def route(pathname, current_page):
    page = _classify_page(pathname)
    if current_page == page:
        return dash.no_update, current_page

    if page == "volm":
        return volm_page(), "volm"

    if page == "openinterest":
        return html.Iframe(
            src="/openinterest",
            style={
                "width": "100%",
                "height": "calc(100vh - 140px)",
                "border": "0",
                "borderRadius": "16px",
            },
        ), "openinterest"

    return sectors_page(), "sectors"


# =============================================================================
# TOP CHIPS
# =============================================================================
def _oi_inference_chip():
    try:
        with openinterest.state_lock:
            s = dict(openinterest.state)
    except Exception:
        s = {}

    baseline_ok = (s.get("baseline_price") is not None) and (s.get("baseline_oi") is not None)
    bt_raw = (s.get("buildup_type") or "NO_CLEAR")
    bt = bt_raw.replace("_", " ")
    bias = (s.get("bias") or "NEUTRAL").upper()
    label = s.get("label") or ""

    if not baseline_ok:
        return html.Div("OI: WAITING BASELINE", className="stat-chip", title=label)

    text = f"OI: {bt} • {bias}"

    if bias == "BULLISH":
        style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif bias == "BEARISH":
        style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        style = {}

    return html.Div(text, className="stat-chip", style=style, title=label)


@dash_app.callback(Output("top-stats", "children"), Input("top_refresh", "n_intervals"))
def update_top_stats(_):
    updated_str = datetime.now(IST).strftime("%H:%M:%S")

    with LOCK:
        offline = (time.time() - LAST_TICK_TS) > 10 if LAST_TICK_TS else True
        tot = TOTAL_TICKS
        d_done = DAILY_SEED_DONE
        d_done_n = int(DAILY_SEED_PROGRESS.get("done", 0) or 0)
        d_total = int(DAILY_SEED_PROGRESS.get("total", 0) or 0)
        d_err = int(DAILY_SEED_ERRORS or 0)

    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        # pn = CACHE.get("pcr")  # kept if you want to show in top bar

    sent_label = str(sm.get("label") or "NEUTRAL").upper()
    sent_score = float(sm.get("score") or 0.0)
    adv = int(sm.get("adv", 0) or 0)
    dec = int(sm.get("dec", 0) or 0)
    unch = int(sm.get("unch", 0) or 0)

    if sent_label == "BULLISH":
        sent_style = {"color": "var(--good)", "borderColor": "rgba(46, 213, 115, 0.55)"}
    elif sent_label == "BEARISH":
        sent_style = {"color": "var(--bad)", "borderColor": "rgba(255, 71, 87, 0.55)"}
    else:
        sent_style = {}

    sentiment_chip = html.Div(
        f"BIAS: {sent_label} ({sent_score:+.2f}) • {adv} ↑ • {dec} ↓",
        className="stat-chip",
        style=sent_style,
        title=f"Adv {adv} • Dec {dec} • Unch {unch}",
    )

    chips = [
        dbc.Badge("Offline" if offline else "Live", color=("danger" if offline else "success"), className="stat-badge"),
        html.A(
            "Volm",
            href=f"{BASE}volm",
            target="_blank",
            className="stat-chip",
            style={"textDecoration": "none", "marginLeft": "8px", "cursor": "pointer"},
        ),
        _oi_inference_chip(),
        sentiment_chip,
    ]

    if not d_done:
        chips.append(
            dbc.Badge(
                f"Seeding {d_done_n}/{d_total} (err {d_err})",
                color="warning",
                className="stat-badge",
                style={"marginLeft": "8px"},
            )
        )

    chips += [
        html.Div(f"Ticks {tot:,}", className="stat-chip"),
        html.Div(f"Time {updated_str}", className="stat-chip"),
    ]

    return html.Div(chips, className="top-stats-wrap")


@dash_app.callback(
    Output("baseline-store", "data"),
    Output("baseline-toggle", "children"),
    Output("baseline-toggle", "title"),
    Input("baseline-toggle", "n_clicks"),
    State("baseline-store", "data"),
    prevent_initial_call=True,
)
def toggle_baseline_mode(_n, mode):
    mode = (mode or "AUTO").upper()
    new_mode = "CENTER" if mode == "AUTO" else "AUTO"
    icon_cls = "bi bi-sliders2-vertical" if new_mode == "AUTO" else "bi bi-align-center"
    title = f"Baseline: {new_mode} (click to switch)"
    return new_mode, html.I(className=icon_cls), title


# =============================================================================
# SECTOR BARS
# =============================================================================
@dash_app.callback(
    Output("sector-bars", "children"),
    Input("refresh_sectors", "n_intervals"),
    Input("sectors-sort", "value"),
    Input("sectors-sort-dd", "value"),
    Input("baseline-store", "data"),
)
def render_sector_bars(_n, sort_by_radio, sort_by_dd, baseline_mode):
    """
    Requested behavior:
      - Only in Momentum mode (metric == 'DirR'): tooltip shows the scaled DirR number (no 'DirR' text).
      - In all other sorts: tooltip shows ONLY the selected metric value (no DirR shown).
    """
    try:
        baseline_mode = (baseline_mode or "AUTO").upper().strip()

        trig = ctx.triggered_id
        if trig == "sectors-sort":
            sort_by = sort_by_radio
        elif trig == "sectors-sort-dd":
            sort_by = sort_by_dd
        else:
            sort_by = sort_by_radio or sort_by_dd

        sort_by = (sort_by or "DirR").strip()

        # ----- map UI choice -> metric key in sector_agg -----
        if sort_by == "DirR":
            metric = "DirR"
        elif sort_by == "%Change":
            metric = "%ChangeMean"
        elif sort_by == "RVOLmMean":
            metric = "RVOLmNetMean"
        elif sort_by == "RVOL5Mean":
            metric = "RVOL5NetMean"
        else:
            metric = "RVOLmNetSum"  # "RVOLm"

        metric_pretty = {
            "DirR": "Momentum",
            "%ChangeMean": "%Change",
            "RVOLmNetMean": "RVOLm Mean",
            "RVOLmNetSum": "RVOLm Net",
            "RVOL5NetMean": "RVOLm5 Mean",
        }.get(metric, metric)

        with CACHE_LOCK:
            agg = dict(CACHE.get("sector_agg") or {})

        items = sorted(
            agg.items(),
            key=lambda kv: float((kv[1] or {}).get(metric, 0.0) or 0.0),
            reverse=True,
        )
        if not items:
            return html.Div("Loading sector bars…", className="hint")

        # Only scale in Momentum mode
        plot_scale = float(SECTOR_DIRR_DISPLAY_SCALE) if metric == "DirR" else 1.0

        # values for auto-capping/scaling the chart
        vals = [float(((m or {}).get(metric, 0.0)) or 0.0) * plot_scale for _, m in items]
        pos_vals = [v for v in vals if v > 0]
        neg_abs = [abs(v) for v in vals if v < 0]

        def pct(sorted_list, p: float) -> float:
            if not sorted_list:
                return 0.0
            i = int(p * (len(sorted_list) - 1))
            i = max(0, min(len(sorted_list) - 1, i))
            return float(sorted_list[i])

        def cap_from_abs(abs_vals, q: float, min_cap: float, mul: float) -> float:
            av = sorted(float(x) for x in abs_vals if x is not None and float(x) > 0)
            if not av:
                return float(min_cap)
            raw = av[-1] if len(av) < 5 else pct(av, q)
            return float(max(raw * mul, min_cap))

        def soft01(x: float) -> float:
            return 1.0 - math.exp(-max(0.0, float(x)))

        # formatters + caps by metric
        if metric == "DirR":
            CAP_Q, CAP_MUL, MIN_CAP = 0.92, 1.15, 0.03
            BAR_MIN_PX = 0.0
            fmt_tick = lambda v: f"{float(v):+.1f}"
            fmt_tip = lambda v: f"{float(v):+.2f}"
        elif metric == "%ChangeMean":
            CAP_Q, CAP_MUL, MIN_CAP = 0.92, 1.15, 0.20
            BAR_MIN_PX = 0.0
            fmt_tick = lambda v: f"{float(v):+.1f}%"
            fmt_tip = lambda v: f"{float(v):+.2f}%"
        else:
            CAP_Q, CAP_MUL, MIN_CAP = 0.88, 1.20, 0.50
            BAR_MIN_PX = 4.0
            fmt_tick = lambda v: f"{float(v):.2f}"
            fmt_tip = lambda v: f"{float(v):.2f}"

        PLOT_H = int(SECTOR_PLOT_H_PX)
        LABEL_BAND = 28
        TRACK_H = max(160, PLOT_H - LABEL_BAND)

        pos_cap = cap_from_abs(pos_vals, CAP_Q, MIN_CAP, CAP_MUL)
        neg_cap = cap_from_abs(neg_abs, CAP_Q, MIN_CAP, CAP_MUL)
        eps = 1e-9

        # ----- Scaling & baseline -> converts metric value to pixel height -----
        if metric == "DirR" and baseline_mode == "CENTER":
            abs_cap = float(max(pos_cap, neg_cap, 1e-9))
            tick_max = abs_cap
            tick_min = -abs_cap
            axis_span_ticks = 2.0 * abs_cap

            zero_pct = 50.0
            half_px = max(0.0, (TRACK_H / 2.0) - 1.0)

            def to_px(val: float) -> float:
                v = float(val)
                if v == 0.0 or half_px <= 0:
                    return 0.0
                x = abs(v) / abs_cap
                y = soft01(x) ** 0.55
                px = y * half_px
                if BAR_MIN_PX > 0 and 0.0 < px < BAR_MIN_PX:
                    px = BAR_MIN_PX
                return float(max(0.0, min(px, half_px)))

        elif metric == "DirR":
            axis_span = float(pos_cap + neg_cap) + eps
            zero_pct = (float(pos_cap) / axis_span) * 100.0
            zero_pct = max(1.0, min(99.0, zero_pct))

            pos_px = max(0.0, (TRACK_H * (zero_pct / 100.0)) - 1.0)
            neg_px = max(0.0, (TRACK_H - (TRACK_H * (zero_pct / 100.0))) - 1.0)

            px_per_unit = float(TRACK_H) / (float(pos_cap) + float(neg_cap) + eps)

            tick_max = float(pos_cap)
            tick_min = -float(neg_cap)
            axis_span_ticks = float(tick_max - tick_min) or 1.0

            def to_px(val: float) -> float:
                v = float(val)
                if v == 0.0:
                    return 0.0
                if v > 0:
                    vv = min(v, float(pos_cap))
                    px = min(vv * px_per_unit, pos_px)
                else:
                    vv = min(-v, float(neg_cap))
                    px = min(vv * px_per_unit, neg_px)
                if BAR_MIN_PX > 0 and 0.0 < px < BAR_MIN_PX:
                    px = BAR_MIN_PX
                return float(max(0.0, px))

        else:
            pos_cap = float(max(pos_cap, 1e-9))
            neg_cap = float(max(neg_cap, 1e-9))

            tick_max = pos_cap
            tick_min = -neg_cap
            axis_span_ticks = float(tick_max - tick_min) or 1.0

            zero_pct = ((tick_max - 0.0) / axis_span_ticks) * 100.0
            zero_pct = max(0.0, min(100.0, zero_pct))

            pos_px = max(0.0, (TRACK_H * (zero_pct / 100.0)) - 1.0)
            neg_px = max(0.0, (TRACK_H - (TRACK_H * (zero_pct / 100.0))) - 1.0)

            def to_px(val: float) -> float:
                v = float(val)
                if v == 0.0:
                    return 0.0
                if v > 0:
                    x = v / (tick_max + eps)
                    px = (soft01(x) ** 0.65) * pos_px
                    px = min(px, pos_px)
                else:
                    x = (-v) / ((-tick_min) + eps)
                    px = (soft01(x) ** 0.65) * neg_px
                    px = min(px, neg_px)
                if BAR_MIN_PX > 0 and 0.0 < px < BAR_MIN_PX:
                    px = BAR_MIN_PX
                return float(max(0.0, px))

        # ----- Axis ticks -----
        ticks = [tick_max, tick_max / 2.0, 0.0, tick_min / 2.0, tick_min]
        axis_ticks = []
        for tv in ticks:
            top_pct = ((tick_max - float(tv)) / axis_span_ticks) * 100.0
            axis_ticks.append(html.Div(fmt_tick(tv), className="sector-axis-tick", style={"top": f"{top_pct:.2f}%"}))

        axis = html.Div(axis_ticks, className="sector-hist-axis", style={"height": f"{TRACK_H}px"})
        children = [axis, html.Div(className="sector-hist-zero-line")]

        # ----- Columns -----
        for sector, m in items:
            m = m or {}

            metric_raw = float(m.get(metric) or 0.0)
            metric_plot = metric_raw * plot_scale  # only scales in Momentum mode

            disp = sector.replace("_", " ").upper()
            bar_px = to_px(metric_plot)

            # Tooltip:
            # - Momentum mode: show scaled DirR number ONLY (no text)
            # - Other modes  : show selected metric only
            tip_val = fmt_tip(metric_plot) if metric == "DirR" else fmt_tip(metric_raw)

            children.append(
                dcc.Link(
                    href=f"{BASE}sector/{sector}",
                    className="sector-hist-link",
                    refresh=False,
                    children=html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(disp, className="sector-hist-tip-name"),
                                    html.Div(tip_val, className="sector-hist-tip-val"),
                                ],
                                className="sector-hist-tooltip",
                                title=f"{metric_pretty} {tip_val}",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        className=("sector-hist-bar pos" if metric_plot >= 0 else "sector-hist-bar neg"),
                                        style={"height": f"{bar_px:.2f}px"},
                                    )
                                ],
                                className="sector-hist-track",
                                style={
                                    "height": f"{TRACK_H}px",
                                    "overflow": "hidden",
                                    "clipPath": "inset(0 round 18px)",
                                    "position": "relative",
                                },
                            ),
                            html.Div(
                                disp,
                                className="sector-hist-name",
                                style={
                                    "height": f"{LABEL_BAND}px",
                                    "display": "flex",
                                    "alignItems": "center",
                                    "justifyContent": "center",
                                    "position": "relative",
                                    "zIndex": 5,
                                    "marginTop": "6px",
                                },
                            ),
                        ],
                        className="sector-hist-col",
                        style={"display": "flex", "flexDirection": "column", "alignItems": "center"},
                    ),
                )
            )

        return html.Div(
            children,
            className="sector-hist-plot",
            style={
                "--zero": f"{zero_pct:.2f}%",
                "--axisW": "68px",
                "--plotH": f"{TRACK_H}px",
                "--labelH": f"{LABEL_BAND}px",
            },
        )

    except Exception as e:
        log.exception("render_sector_bars crashed")
        return html.Div(
            f"Sector bars error: {type(e).__name__}: {e}",
            className="hint",
            style={"color": "red", "padding": "20px", "fontSize": "14px"},
        )

# =============================================================================
# SECTOR MODAL ROWS
# =============================================================================
def sector_rows_sorted(sector: str, sort_by: str = "RFactor"):
    def _cumvol_at_or_before(series: List[Tuple[float, float, Optional[float]]], cutoff_epoch: float):
        base_t = None
        base_v = None
        for t, _p, v in series:
            if float(t) <= float(cutoff_epoch):
                if v is not None:
                    base_t = float(t)
                    base_v = float(v)
            else:
                break
        if base_v is not None:
            return base_t, base_v

        for t, _p, v in series:
            if v is not None:
                return float(t), float(v)
        return None, None

    rows = []
    now_ist = datetime.now(IST)
    now_epoch = time.time()
    tf_now = _time_factor_ist_for_rvol(now_ist)

    snap = _snapshot_state(include_hot=False)

    for s in SECTOR_DEFINITIONS.get(sector, []):
        tok = symbol_to_token.get(s)
        if not tok:
            continue

        rr = _compute_rfactor_row_snap(tok, snap)
        if not rr:
            continue

        pct_open = float(rr["pct_open"])
        ltp = float(rr["ltp"])
        vol_today = float(rr["vol_today"])

        tf_day = _time_factor_ist_for_rvol(now_ist)
        st = (snap.get("daily") or {}).get(tok) or {}
        avg_vol_20 = st.get("avg_vol_20")

        rvolm = None
        if avg_vol_20 is not None:
            try:
                av = float(avg_vol_20)
                if av > 0:
                    expected = av * float(tf_day)
                    rvolm = float(vol_today) / (expected + 1e-9)
            except Exception:
                rvolm = None

        rvol5 = None
        if market_is_open_ist(now_ist) and avg_vol_20 is not None:
            try:
                av = float(avg_vol_20)
                if av > 0:
                    cutoff_epoch = now_epoch - float(RVOL5_WINDOW_SEC)
                    with LOCK:
                        dq = HOT_HISTORY.get(tok)
                        series = list(dq) if dq else None

                    if series and len(series) >= 2:
                        base_t, base_v = _cumvol_at_or_before(series, cutoff_epoch)
                        if base_t is not None and base_v is not None:
                            vol5 = float(vol_today) - float(base_v)
                            if vol5 >= 0:
                                eff_sec = max(5.0, min(float(RVOL5_WINDOW_SEC), now_epoch - float(base_t)))
                                then_ist = now_ist - timedelta(seconds=eff_sec)
                                tf_then = _time_factor_ist_for_rvol(then_ist)
                                frac = max(1e-4, float(tf_now) - float(tf_then))
                                exp5 = av * frac
                                rvol5 = float(vol5) / (float(exp5) + 1e-9)
            except Exception:
                rvol5 = None

        rows.append({
            "Symbol": s,
            "Company": symbol_to_name.get(s, ""),
            "DirR": float(rr["dirr"]),
            "Price": float(ltp),
            "%Change": float(pct_open),
            "RVOL5": (float(rvol5) if rvol5 is not None else None),
            "RVOLm": (float(rvolm) if rvolm is not None else None),
            "RFactor": float(rr["rfactor"]),
        })

    if not rows:
        return []

    sb = (sort_by or "").strip().upper()
    if sb in ("RVOL5", "RVOLM5", "RVOL_5"):
        key = "RVOL5"
    elif sb in ("RVOL", "RVOLM"):
        key = "RVOLm"
    elif sb in ("DIRR", "DIR R"):
        key = "DirR"
    elif sb in ("%CHANGE", "%CHG", "CHG"):
        key = "%Change"
    else:
        key = "RFactor"

    def sort_val(x):
        v = x.get(key)
        return float(v) if v is not None else float("-inf")

    rows.sort(key=sort_val, reverse=True)
    return rows


@dash_app.callback(
    Output("sector-modal", "is_open"),
    Output("sector-modal-title", "children"),
    Output("sector-modal-grid", "rowData"),
    Output("sector-modal-grid-m", "rowData"),
    Input("url", "pathname"),
    Input("top_refresh", "n_intervals"),
)
def sync_sector_modal(pathname, _tick):
    sector = _extract_sector_from_path(pathname)
    if sector and sector in SECTOR_DEFINITIONS:
        rows = sector_rows_sorted(sector, sort_by="RFactor")
        title = sector.replace("_", " ").title()
        return True, title, rows, rows
    return False, "Sector", [], []


# =============================================================================
# DIALS + LEADERBOARDS
# =============================================================================
def _state_class(label: str) -> str:
    L = (label or "").upper().strip()
    L = " ".join(L.split())
    if L == "STRONG SELL": return "state-ss"
    if L == "SELL":        return "state-sell"
    if L == "NEUTRAL":     return "state-neutral"
    if L == "BUY":         return "state-buy"
    if L == "STRONG BUY":  return "state-sb"
    if L == "BEARISH":     return "state-sell"
    if L == "BULLISH":     return "state-buy"
    return "state-neutral"


def _fmt_oi_compact(v: Optional[float]) -> str:
    if v is None:
        return "—"
    n = float(v)
    a = abs(n)
    if a >= 1e7: return f"{n/1e7:.2f}Cr"
    if a >= 1e5: return f"{n/1e5:.2f}L"
    if a >= 1e3: return f"{n/1e3:.2f}K"
    return str(int(round(n)))


@dash_app.callback(
    Output("sentiment-needle", "style"),
    Output("sentiment-sub", "children"),
    Output("pcr-needle", "style"),
    Output("pcr-sub", "children"),
    Input("refresh_sectors", "n_intervals"),
)
def update_dials(_):
    with CACHE_LOCK:
        sm = dict(CACHE.get("sentiment") or {})
        pn = CACHE.get("pcr")

    score = float(sm.get("score") or 0.0)
    sent_angle = max(-90.0, min(90.0, score * 90.0))
    sent_style = {"--rot": f"{sent_angle:.2f}deg"}
    sent_label = str(sm.get("label") or "NEUTRAL")

    sent_sub = html.Span(
        [
            html.Span(sent_label, className=f"dial-state {_state_class(sent_label)}"),
            html.Span(f"{score:+.2f} • {sm.get('adv',0)} ↑ • {sm.get('dec',0)} ↓", className="dial-meta"),
        ],
        className="dial-sub-inner",
    )

    if pn and pn.get("pcr") is not None:
        pcr = float(pn["pcr"])
        label = pcr_label_from_value(pcr)
        pcr_clamped = max(0.0, min(2.0, pcr))
        pcr_angle = (pcr_clamped - 1.0) * 90.0
        pcr_style = {"--rot": f"{pcr_angle:.2f}deg"}
        pe_txt = _fmt_oi_compact(pn.get("pe_oi"))
        ce_txt = _fmt_oi_compact(pn.get("ce_oi"))

        pcr_sub = html.Span(
            [
                html.Span(label, className=f"dial-state {_state_class(label)}"),
                html.Span(f"PCR {pcr:.2f} • PE {pe_txt} • CE {ce_txt}", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )
    else:
        pcr_style = {"--rot": "0deg"}
        pcr_sub = html.Span(
            [
                html.Span("LOADING", className="dial-state state-neutral"),
                html.Span("PCR", className="dial-meta"),
            ],
            className="dial-sub-inner",
        )

    return sent_style, sent_sub, pcr_style, pcr_sub


@dash_app.callback(
    Output("top15-gainers-grid", "rowData"),
    Output("top15-losers-grid", "rowData"),
    Output("top15-gainers-grid-m", "rowData"),
    Output("top15-losers-grid-m", "rowData"),
    Input("refresh_sectors", "n_intervals"),
)
def update_rfactor_leaderboards(_):
    with CACHE_LOCK:
        g = list(CACHE.get("top15_gainers") or [])
        l = list(CACHE.get("top15_losers") or [])
    return g, l, g, l


@dash_app.callback(
    Output("momo-g1-grid", "rowData"),
    Output("momo-g2-grid", "rowData"),
    Output("momo-g3-grid", "rowData"),
    Output("momo-l1-grid", "rowData"),
    Output("momo-l2-grid", "rowData"),
    Output("momo-l3-grid", "rowData"),
    Output("momo-g1-title", "children"),
    Output("momo-g2-title", "children"),
    Output("momo-g3-title", "children"),
    Output("momo-l1-title", "children"),
    Output("momo-l2-title", "children"),
    Output("momo-l3-title", "children"),
    Input("refresh_volm", "n_intervals"),
)
def update_volm_grids(_):
    with CACHE_LOCK:
        g = list(CACHE.get("momo_sector_gainers") or [])
        l = list(CACHE.get("momo_sector_losers") or [])
        agg = dict(CACHE.get("sector_agg") or {})  # has DirR per sector

    def _get(lst, i):
        return lst[i] if len(lst) > i else {"sector": "—", "rows": []}

    def _title(sec: str, color_var: str):
        if not sec or sec == "—":
            return "—"
        dirr = float((agg.get(sec) or {}).get("DirR") or 0.0) * float(SECTOR_DIRR_DISPLAY_SCALE)

        return html.Span(
            [
                html.Span(sec.replace("_", " ")),
                html.Span(
                    f" ({dirr:+.2f})",
                    style={"color": color_var, "fontWeight": "950"},
                ),
            ]
        )

    g1, g2, g3 = _get(g, 0), _get(g, 1), _get(g, 2)
    l1, l2, l3 = _get(l, 0), _get(l, 1), _get(l, 2)

    return (
        g1["rows"], g2["rows"], g3["rows"],
        l1["rows"], l2["rows"], l3["rows"],
        _title(g1["sector"], "var(--good)"),
        _title(g2["sector"], "var(--good)"),
        _title(g3["sector"], "var(--good)"),
        _title(l1["sector"], "var(--bad)"),
        _title(l2["sector"], "var(--bad)"),
        _title(l3["sector"], "var(--bad)"),
    )

@dash_app.callback(
    Output("market-heatmap", "figure"),
    Input("refresh_sectors", "n_intervals"),
)
def update_market_heatmap(_):
    with CACHE_LOCK:
        rows = list(CACHE.get("heatmap_rows") or [])
    return build_market_heatmap_figure(rows)


# =============================================================================
# STARTUP/SHUTDOWN FOR WRAPPER
# =============================================================================
async def _startup():
    init_u_curve_once()
    start_pace_curve_builder_once()
    seed_daily_stats_once(per_req_sleep=SEED_SLEEP_SEC)
    start_ticker_once()
    load_nfo_instruments_once()
    start_compute_loop_once()
    await openinterest.on_startup()


async def _shutdown():
    await openinterest.on_shutdown()