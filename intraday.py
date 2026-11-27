"""Intraday bots: the 1-minute strategies from the "QQQ bots" reel (@nickafischer), run on every market.

Each bot watches 1-minute candles with the 9, 21 and 50 EMAs and VWAP. When price crosses and everything
lines up, it buys (a call, shares, a futures contract or a coin) or goes the other way (a put or a short).
Every trade logs why it bought, where the EMAs and VWAP were, and how long it held.

The exits are the part that matters: a stop on every trade, a profit lock, a trailing stop, a quick target,
and per bot a daily loss brake and profit brake ("done for the day").

Paper only: nothing here can place an order. The agent checks in every 5 minutes on GitHub; each check plays
through the 1-minute candles that closed since the last one, in time order, using only what was known at
each candle's close. Entries are only taken on candles from the last 35 minutes; stops are followed through
any gap, the way resting stop orders would be.
"""
import datetime as dt
import math
import time
import urllib.parse

from core import (FUTURES, RULES, EARLY_CLOSE, is_trading_day, next_trading_day, to_ny, epoch_to_utc,
                  ny_offset_hours, crypto_yahoo, get_json, ysym, log, money)

# ---------------------------------------------------------------- the bots: one per market
BOTS = {
    "stocks": {"name": "Stocks", "inst": "stock", "hours": "us",
               "symbols": ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD", "AVGO", "PLTR", "NFLX", "MU"]},
    "etfs": {"name": "ETFs", "inst": "etf", "hours": "us",
             "symbols": ["DIA", "SMH", "XLK", "XLF", "XLE", "XLV", "TLT", "EEM"]},
    "options": {"name": "Options", "inst": "option", "hours": "us", "symbols": ["QQQ", "SPY", "IWM"]},
    "futures": {"name": "Futures", "inst": "future", "hours": "fut", "symbols": ["MES", "MNQ", "MYM", "M2K", "M6E"]},
    "commodities": {"name": "Commodities", "inst": "future", "hours": "fut", "symbols": ["MGC", "SIL", "MCL"]},
    "crypto": {"name": "Crypto", "inst": "crypto", "hours": "24h", "symbols": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
               "long_only": True},
}

# The reel's controls. config.json "intraday" can change any of them.
CONTROLS = {"auto": True, "stop_loss": True, "loss_brake": True, "profit_brake": True, "trend_filter": True,
            "chop_filter": True, "trailing_stop": True, "profit_lock": True, "quick_target": True,
            "original_exit": False, "cost_filter": True, "risk": "medium", "style": "both"}
RISK = {"low": 0.005, "medium": 0.01, "high": 0.02}   # share of the account each trade may lose at its stop
LOSS_BRAKE, PROFIT_BRAKE = 0.02, 0.03                   # per bot per day, share of the day's starting value
MAX_TRADES, MAX_OPEN = 15, 3                            # per bot per day / at once
STOP_ATR = 1.5                                          # stop 1.5 one-minute ranges away...
MIN_STOP = {"stock": 0.0008, "etf": 0.0006, "option": 0.0006, "future": 0.0005, "commodity": 0.0008,
            "fx": 0.0002, "crypto": 0.0015}             # ...but never closer than this share of the price
LOCK_AT, LOCK_KEEP = 1.0, 0.25      # profit lock: at +1R the stop moves to +0.25R
TRAIL_AT, TRAIL_GAP = 1.5, 1.0      # trailing stop: from +1.5R the stop follows 1R behind the best price
QUICK_R = 2.0                       # quick target on the crossing setups
MAX_HOLD = {"scalp": 30, "trend": 90}   # minutes
COOL = 3                            # minutes before the same symbol can be traded again
CATCHUP = 35                        # minutes of candles a check may enter on
COST_MAX = 0.25                     # skip a trade when the round-trip cost is over a quarter of the risk
WARM = 60                           # candles needed before signals count

# Costs. Liquid names only, so tighter than the swing desk's flat 0.1%.
SHARE_SLIP = 0.0002
CRYPTO_COST = 0.004                 # Kraken Pro's lowest-tier taker fee, per side
OPT_FEE, FUT_FEE = RULES["option_fee"], RULES["future_fee"]
OPT_HALF_SPREAD = 0.015
TICK = {"MES": 0.25, "MNQ": 0.25, "MYM": 1.0, "M2K": 0.1, "MGC": 0.1, "SIL": 0.005, "MCL": 0.01, "M6E": 0.0001,
        "MBT": 5.0, "MET": 0.5}
COMMODITY = {"MGC", "SIL", "MCL"}
VOL_INDEX = {"QQQ": "^VXN", "SPY": "^VIX", "IWM": "^RVX"}
OPT_CAP = 0.15                      # option premium per trade: at most 15% of the account
SHARE_CAP = 0.5                     # one share position: at most half the USD sleeve (no margin)
CRYPTO_CAP = 0.4

SETUP_ORDER = ("orb", "ema50", "vwap", "trend")
SETUP_NAMES = {"vwap": "VWAP cross", "ema50": "EMA50 cross", "orb": "Opening-range break", "trend": "Trend continuation"}
EXIT_NAMES = ("Stop loss", "Profit lock", "Trailing stop", "Quick target", "Original exit", "Time stop", "End of session")


def controls(cfg):
    c = dict(CONTROLS)
    c.update({k: v for k, v in ((cfg or {}).get("intraday") or {}).get("controls", {}).items() if k in CONTROLS})
    if c["risk"] not in RISK:
        c["risk"] = "medium"
    if c["style"] not in ("scalp", "trend", "both"):
        c["style"] = "both"
    return c


def bots(cfg):
    out = {}
    over = ((cfg or {}).get("intraday") or {}).get("bots", {})
    for k, b in BOTS.items():
        o = over.get(k) or {}
        if o.get("on") is False:
            continue
        out[k] = dict(b, symbols=[s for s in (o.get("symbols") or b["symbols"]) if s][:15])
    return out


def ysymbol(inst, sym):
    if inst == "future":
        return FUTURES[sym][0]
    if inst == "crypto":
        return crypto_yahoo(sym)
    return sym


def stop_kind(inst, sym):
    if inst == "future":
        return "commodity" if sym in COMMODITY else "fx" if sym == "M6E" else "future"
    return inst


def side_word(inst, d):
    if inst == "option":
        return "CALL" if d > 0 else "PUT"
    return "BUY" if d > 0 else "SHORT"


# ---------------------------------------------------------------- 1-minute candles
_MCACHE = {}


def minutes(ysym_, rng="2d", start=None, end=None):
    """1-minute candles from Yahoo: the last `rng`, or start..end (UTC datetimes, at most 7 days, within 30 days)."""
    if start is not None:
        q = {"period1": int((start - dt.datetime(1970, 1, 1)).total_seconds()),
             "period2": int((end - dt.datetime(1970, 1, 1)).total_seconds()), "interval": "1m", "includePrePost": "false"}
    else:
        q = {"range": rng, "interval": "1m", "includePrePost": "false"}
    key = (ysym_, tuple(sorted(q.items())))
    if key in _MCACHE:
        return _MCACHE[key]
    out = []
    for host in ("query2", "query1"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (host, urllib.parse.quote(ysym(ysym_)), urllib.parse.urlencode(q))
        d, err = get_json(url, {"Accept": "application/json"})
        time.sleep(0.2)
        res = ((d or {}).get("chart") or {}).get("result")
        if res:
            r = res[0]
            ts = r.get("timestamp") or []
            qd = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            cols = [qd.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")]
            for i, t in enumerate(ts):
                o, h, l, c, v = (col[i] for col in cols)
                if None in (o, h, l, c) or t % 60:
                    continue
                out.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0)})
            break
    _MCACHE[key] = out
    return out


def daily_closes(ysym_, rng="3mo"):
    q = urllib.parse.urlencode({"range": rng, "interval": "1d"})
    for host in ("query2", "query1"):
        d, err = get_json("https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (host, urllib.parse.quote(ysym_), q))
        res = ((d or {}).get("chart") or {}).get("result")
        if res:
            r = res[0]
            ts = r.get("timestamp") or []
            cl = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
            return {to_ny(epoch_to_utc(t)).date(): float(c) for t, c in zip(ts, cl) if c}
    return {}


# ---------------------------------------------------------------- indicators
def ema(xs, n):
    k, e, out = 2.0 / (n + 1), None, []
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def wilder(vals, n):
    out, a = [], None
    for i, v in enumerate(vals):
        if i < n:
            a = (a or 0) + v
            out.append(a / (i + 1))
            if i == n - 1:
                a = a / n
        else:
            a = (a * (n - 1) + v) / n
            out.append(a)
    return out


def rsi(c, n=14):
    up = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    dn = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    au, ad = wilder(up, n), wilder(dn, n)
    return [100.0 if d == 0 else 100 - 100 / (1 + u / d) for u, d in zip(au, ad)]


def atr(bars, n=14):
    tr = [bars[0]["h"] - bars[0]["l"]] + [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(bars, bars[1:])]
    return wilder(tr, n)


def clock(hours, t):
    """(session key, New York date, New York minute of the day, UTC minute of the day) for a candle."""
    u = epoch_to_utc(t)
    ny = u + dt.timedelta(hours=ny_offset_hours(u))
    m = ny.hour * 60 + ny.minute
    if hours == "us":
        key = ny.date()
    elif hours == "fut":
        key = (ny + dt.timedelta(hours=6)).date()      # CME's day starts at 6 PM New York
    else:
        key = u.date()                                 # crypto: the UTC day
    return key, ny.date(), m, u.hour * 60 + u.minute


def us_close(d):
    return 13 * 60 if d.isoformat() in EARLY_CLOSE else 16 * 60


def indicators(bars, hours):
    c = [b["c"] for b in bars]
    ind = {"c": c, "e9": ema(c, 9), "e21": ema(c, 21), "e50": ema(c, 50), "rsi": rsi(c), "atr": atr(bars),
           "key": [], "date": [], "m": [], "um": [], "vw": [], "n": [], "x": []}
    key = None
    pv = vv = tps = 0.0
    cnt, ors = 0, {}
    for i, b in enumerate(bars):
        k, d, m, um = clock(hours, b["t"])
        if k != key:
            key, pv, vv, tps, cnt = k, 0.0, 0.0, 0.0, 0
        tp = (b["h"] + b["l"] + b["c"]) / 3
        cnt += 1
        tps += tp
        if b["v"] > 0:
            pv += tp * b["v"]
            vv += b["v"]
        vw = pv / vv if vv > 0 else tps / cnt
        ind["key"].append(k)
        ind["date"].append(d)
        ind["m"].append(m)
        ind["um"].append(um)
        ind["vw"].append(vw)
        ind["n"].append(cnt)
        # VWAP crossings, for the chop filter
        side = 1 if b["c"] > vw else -1
        prev = ind["x"][-1][0] if ind["x"] and cnt > 1 else side
        ind["x"].append((side, 1 if side != prev else 0))
        if hours != "24h" and 570 <= m < 585 and d == k:
            o = ors.setdefault(d, [b["h"], b["l"]])
            o[0], o[1] = max(o[0], b["h"]), min(o[1], b["l"])
    ind["or"] = ors
    return ind


def crosses(ind, i, n=30):
    lo = max(0, i - n + 1, i - ind["n"][i] + 1)
    return sum(ind["x"][j][1] for j in range(lo, i + 1))


def stack(ind, i):
    a, b, c = ind["e9"][i], ind["e21"][i], ind["e50"][i]
    if a > b > c:
        return "Stacked up (9 > 21 > 50)", 1
    if a < b < c:
        return "Stacked down (9 < 21 < 50)", -1
    return "Mixed", 0


# ---------------------------------------------------------------- windows
def window(hours, ind, i):
    """(may enter, opening range ready, must be flat) for candle i."""
    d, m, um, key = ind["date"][i], ind["m"][i], ind["um"][i], ind["key"][i]
    if hours == "24h":
        return um < 23 * 60 + 30, False, um >= 23 * 60 + 55
    if not is_trading_day(d):
        return False, False, hours == "us"
    close = us_close(d)
    start = 9 * 60 + 35 if hours == "us" else 8 * 60
    if hours == "fut" and key != d:      # the evening part of CME's day
        return False, False, False
    return start <= m < close - 30, m >= 585 and d in ind["or"], m >= close - 5


# ---------------------------------------------------------------- the setups
def setups(ind, i, d, ctl, used_orb):
    """Each setup for direction d at the close of candle i: fired / blocked by a filter / how close it is."""
    c, cp = ind["c"][i], ind["c"][i - 1]
    v, vp = ind["vw"][i], ind["vw"][i - 1]
    e9, e21, e50, e50p = ind["e9"][i], ind["e21"][i], ind["e50"][i], ind["e50"][i - 1]
    a = max(ind["atr"][i], c * 1e-5)
    r = ind["rsi"][i]
    x = lambda p, q: d * (p - q)
    mom = x(e9, e21) > 0
    out = []

    def near(level):
        gap = x(c, level)
        if gap > 0:
            return "on the right side", 100
        g = -gap / a
        return ("almost there" if g <= 0.5 else "close" if g <= 1.5 else "far"), max(0, int(100 - g * 33))

    def item(key, fired, lined, level=None, extra=None):
        st, pct = (near(level) if level is not None else ("", 0))
        if fired and not lined:
            st, pct = "not lined up yet", 70
        out.append({"setup": key, "fired": fired and lined, "state": extra or st, "pct": pct})

    item("vwap", x(cp, vp) <= 0 and x(c, v) > 0, mom, v)
    item("ema50", x(cp, e50p) <= 0 and x(c, e50) > 0, mom and x(c, v) > 0, e50)
    lvl = None
    if ind["date"][i] in ind["or"]:
        lvl = ind["or"][ind["date"][i]][0 if d > 0 else 1]
    if lvl is None or ind["m"][i] < 585:
        out.append({"setup": "orb", "fired": False, "state": "range forming", "pct": 0})
    elif used_orb:
        out.append({"setup": "orb", "fired": False, "state": "done for today", "pct": 0})
    else:
        item("orb", x(cp, lvl) <= 0 and x(c, lvl) > 0, x(c, v) > 0, lvl)
    stacked = x(e9, e21) > 0 and x(e21, e50) > 0 and x(c, v) > 0
    touch = (ind["lo"][i] <= e9) if d > 0 else (ind["hi"][i] >= e9)
    rsi_ok = (50 <= r < 70) if d > 0 else (30 < r <= 50)
    fired = stacked and touch and x(c, e9) > 0 and x(c, ind["op"][i]) > 0
    if not stacked:
        out.append({"setup": "trend", "fired": False, "state": "not lined up yet", "pct": 20})
    else:
        out.append({"setup": "trend", "fired": fired and rsi_ok, "state": "watching for a pullback" if not fired else
                    ("lined up" if rsi_ok else "RSI says no"), "pct": 100 if fired else 60})
    style = ctl["style"]
    for o in out:
        if not o["fired"]:
            continue
        o["state"] = "lined up"
        orig = o["setup"] != "trend"
        if (style == "scalp" and not orig) or (style == "trend" and orig):
            o["fired"], o["state"] = False, "style is off"
        elif orig and ctl["trend_filter"] and i >= 10 and x(e50, ind["e50"][i - 10]) < 0:
            o["fired"], o["state"] = False, "trend filter says no"
        elif ctl["chop_filter"] and crosses(ind, i) >= 4:
            o["fired"], o["state"] = False, "chop filter says no"
        elif (d > 0 and r > 75) or (d < 0 and r < 25):
            o["fired"], o["state"] = False, "too stretched (RSI %d)" % r
    return out


def why(ind, i):
    c, v = ind["c"][i], ind["vw"][i]
    st, _ = stack(ind, i)
    return {"e9": round(ind["e9"][i], 4), "e21": round(ind["e21"][i], 4), "e50": round(ind["e50"][i], 4),
            "vwap": round(v, 4), "gap": round(c - v, 4), "stack": st, "rsi": round(ind["rsi"][i]), "atr": round(ind["atr"][i], 4)}


# ---------------------------------------------------------------- option pricing (Black-Scholes)
def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, sig, right, r=0.04):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K) if right == "call" else max(0.0, K - S)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / v
    d2 = d1 - v
    call = S * _n(d1) - K * math.exp(-r * T) * _n(d2)
    return call if right == "call" else call - S + K * math.exp(-r * T)


def expiry_epoch(d):
    guess = dt.datetime.combine(d, dt.time(21, 0))
    utc = dt.datetime.combine(d, dt.time(16, 0)) - dt.timedelta(hours=ny_offset_hours(guess))
    return (utc - dt.datetime(1970, 1, 1)).total_seconds()


def years_left(exp, t):
    return max(60.0, expiry_epoch(exp) - t) / (365.0 * 86400)


def opt_value(p, S, t):
    return bs(S, p["strike"], years_left(dt.date.fromisoformat(p["expiry"]), t), p["iv"], p["right"])


# ---------------------------------------------------------------- the engine
class Engine:
    """Plays candles through the bots. `book` holds positions, trades and per-bot days; `acct` is the money."""

    def __init__(self, book, acct, ctl, opt=None, say=None, note=None):
        self.b, self.a, self.ctl = book, acct, ctl
        for k, v in (("positions", {}), ("trades", []), ("days", {}), ("last", {}), ("used", {}), ("cool", {})):
            book.setdefault(k, v)
        self.opt = opt or {}           # underlying -> {"iv", "hs", "expiry": date or None, "quotes": {(strike, right): (bid, ask, iv)}}
        self.say = say or (lambda *a, **k: None)
        self.note = note or (lambda *a, **k: None)
        self.skips = {}

    # -------------------------------------------------- days and brakes
    def day(self, bot, d):
        x = self.b["days"].get(bot)
        if not x or x.get("day") != d.isoformat():
            x = {"day": d.isoformat(), "pnl": 0.0, "n": 0, "w": 0, "l": 0, "brake": None, "spark": [], "start": round(self.a.day_start(), 2)}
            self.b["days"][bot] = x
        return x

    def braked(self, bot, d):
        x = self.day(bot, d)
        return x["brake"] or (x["n"] >= MAX_TRADES and "trade limit")

    # -------------------------------------------------- one bot over its new candles
    def run(self, bot, cfg, series, enter_from=None, now_t=None):
        """series: {symbol: (bars, ind)}. Candles after book["last"][symbol] are played in time order."""
        ev = []
        for sym, (bars, ind) in series.items():
            last = self.b["last"].get(sym, 0)
            for i in range(WARM, len(bars)):
                t = bars[i]["t"]
                if t <= last or (now_t is not None and t + 60 > now_t):
                    continue
                ev.append((t, sym, i))
        ev.sort()
        for t, sym, i in ev:
            bars, ind = series[sym]
            self.step(bot, cfg, sym, bars, ind, i, enter_from is None or t >= enter_from)
            self.b["last"][sym] = t

    def step(self, bot, cfg, sym, bars, ind, i, may_enter):
        b, hours, inst = bars[i], cfg["hours"], cfg["inst"]
        pid = "%s:%s" % (bot, sym)
        p = self.b["positions"].get(pid)
        enter_ok, _, flat = window(hours, ind, i)
        if p:
            self.manage(pid, p, b, ind, i, flat)
        if pid in self.b["positions"] or not may_enter or not enter_ok or flat or not self.ctl["auto"]:
            return
        d0 = ind["date"][i]
        if self.braked(bot, d0):
            return
        if sum(1 for q in self.b["positions"].values() if q["bot"] == bot) >= MAX_OPEN:
            return
        if b["t"] - self.b["cool"].get(sym, 0) < COOL * 60:
            return
        ok, reason = self.a.allowed(bot, inst, b["t"])
        if not ok:
            self.skips[bot] = reason
            return
        for d in (1, -1):
            if d < 0 and cfg.get("long_only"):
                continue
            used = self.b["used"].get("%s|%s|%s" % (sym, d, d0))
            fired = [s for s in setups(ind, i, d, self.ctl, used) if s["fired"]]
            if not fired:
                continue
            setup = min(fired, key=lambda s: SETUP_ORDER.index(s["setup"]))["setup"]
            if self.open(bot, cfg, sym, ind, i, b, d, setup):
                if setup == "orb":
                    self.b["used"]["%s|%s|%s" % (sym, d, d0)] = 1
                return

    # -------------------------------------------------- entering
    def open(self, bot, cfg, sym, ind, i, b, d, setup):
        inst, c = cfg["inst"], b["c"]
        a = ind["atr"][i]
        dist = max(STOP_ATR * a, MIN_STOP[stop_kind(inst, sym)] * c)
        stop = c - d * dist
        style = "trend" if setup == "trend" else "scalp"
        if self.ctl["cost_filter"]:
            cost = round_trip(inst, sym, c, self.opt.get(sym))
            if cost > COST_MAX * dist:
                self.skips[bot] = "%s: costs would eat the trade (%.0f%% of the risk)" % (sym, cost / dist * 100)
                return False
        eq, fx = self.a.equity(), self.a.fx
        risk_cad = min(eq * RISK[self.ctl["risk"]], max(0.0, self.a.risk_room()))
        if risk_cad <= 1:
            self.skips[bot] = "the account's open-risk cap is full"
            return False
        p = {"bot": bot, "sym": sym, "inst": inst, "dir": d, "setup": setup, "style": style, "inT": b["t"] + 60, "uIn": c,
             "stop": stop, "stop0": stop, "stopBy": "Stop loss", "R": dist, "best": c, "target": None,
             "why": why(ind, i), "lastT": b["t"], "uLast": c}
        if style == "scalp" and self.ctl["quick_target"]:
            p["target"] = c + d * QUICK_R * dist
        if inst in ("stock", "etf"):
            fill = c * (1 + d * SHARE_SLIP)
            per = dist + 2 * SHARE_SLIP * c
            units = int(risk_cad / fx / per)
            cap = min(SHARE_CAP * self.a.sleeve("usd") / fx, self.a.free_usd())
            units = min(units, int(cap / fill)) if cap > 0 else 0
            if units < 1:
                self.skips[bot] = "%s: not enough free cash for one share" % sym
                return False
            p.update(units=units, inPx=fill, mark=c, riskCad=round(units * per * fx, 2), costCad=round(units * fill * fx, 2))
            self.a.cash["usd"] += -units * fill if d > 0 else units * fill
        elif inst == "future":
            y, mult, mfrac, name = FUTURES[sym]
            tick = TICK.get(sym, 0)
            fill = c + d * tick
            per = (dist + 2 * tick) * mult + 2 * FUT_FEE
            n = int(risk_cad / fx / per)
            margin = mfrac * c * mult
            room = min(RULES["future_margin_cap_pct"] * self.a.sleeve("usd") / fx, self.a.free_usd())
            n = min(n, int(room / margin)) if margin > 0 else 0
            if n < 1:
                self.skips[bot] = "%s: one contract needs more than the risk budget or margin" % sym
                return False
            self.a.cash["usd"] -= FUT_FEE * n
            p.update(units=n, mult=mult, inPx=fill, mark=c, margin=round(margin * n, 2), feeIn=FUT_FEE * n,
                     riskCad=round(n * per * fx, 2), costCad=round(margin * n * fx, 2))
        elif inst == "crypto":
            fill = c * (1 + CRYPTO_COST)
            per = dist + 2 * CRYPTO_COST * c
            units = risk_cad / fx / per
            cap = min(CRYPTO_CAP * self.a.sleeve("cad"), self.a.cash["cad"]) / fx
            units = min(units, cap / fill)
            if units * fill * fx < 25:
                self.skips[bot] = "%s: not enough crypto cash" % sym
                return False
            self.a.cash["cad"] -= units * fill * fx
            p.update(units=units, inPx=fill, mark=c, riskCad=round(units * per * fx, 2), costCad=round(units * fill * fx, 2))
        else:
            o = self.pick_option(sym, c, stop, d, b["t"], risk_cad / fx, eq * OPT_CAP / fx)
            if not o:
                return False
            self.a.cash["usd"] -= o["n"] * 100 * o["ask"] + OPT_FEE * o["n"]
            p.update(units=o["n"], strike=o["strike"], expiry=o["expiry"], right=o["right"], iv=o["iv"], hs=o["hs"],
                     inPx=o["ask"], mark=o["mid"], riskCad=round(o["n"] * o["per"] * fx, 2),
                     costCad=round((o["n"] * 100 * o["ask"] + OPT_FEE * o["n"]) * fx, 2))
        pid = "%s:%s" % (bot, sym)
        p["id"] = "%s:%d" % (pid, b["t"])
        self.b["positions"][pid] = p
        self.say("open", p)
        return True

    def pick_option(self, sym, S, stop, d, t, risk_usd, cap_usd):
        ctx = self.opt.get(sym) or {}
        right = "call" if d > 0 else "put"
        today = to_ny(epoch_to_utc(t)).date()
        exp = ctx.get("expiry") or next_trading_day(today)
        if exp <= today:
            exp = next_trading_day(today)
        base_iv = ctx.get("iv") or 0.2
        quotes = ctx.get("quotes") or {}
        atm = round(S)
        tried = []
        for k in range(0, 5):
            strike = float(atm + k if d > 0 else atm - k)
            q = quotes.get((strike, right))
            iv = (q[2] if q and q[2] and 0.03 < q[2] < 3 else base_iv)
            hs = OPT_HALF_SPREAD
            if q and q[0] > 0 and q[1] > q[0]:
                hs = min(max((q[1] - q[0]) / 2, 0.005), 0.10)
            T = years_left(exp, t)
            mid = bs(S, strike, T, iv, right)
            if mid < 0.10:
                break
            ask, at_stop = mid + hs, max(0.01, bs(stop, strike, T, iv, right) - hs)
            per = (ask - at_stop) * 100 + 2 * OPT_FEE
            n = int(min(risk_usd / per, cap_usd / (ask * 100 + OPT_FEE), self.a.free_usd() / (ask * 100 + OPT_FEE)))
            tried.append(strike)
            if n >= 1:
                return {"n": n, "strike": strike, "expiry": exp.isoformat(), "right": right, "iv": round(iv, 4),
                        "hs": round(hs, 4), "ask": round(ask, 4), "mid": round(mid, 4), "per": round(per, 2)}
        self.skips["options"] = "%s: no %s fits the risk budget (strikes %s)" % (sym, right, ", ".join("%g" % s for s in tried[:3]))
        return None

    # -------------------------------------------------- managing and exiting
    def manage(self, pid, p, b, ind, i, flat):
        d, R = p["dir"], p["R"]
        hit = None
        if (d > 0 and b["o"] <= p["stop"]) or (d < 0 and b["o"] >= p["stop"]):
            hit = (b["o"], p["stopBy"])
        elif (d > 0 and b["l"] <= p["stop"]) or (d < 0 and b["h"] >= p["stop"]):
            hit = (p["stop"], p["stopBy"])
        elif p.get("target") and ((d > 0 and b["h"] >= p["target"]) or (d < 0 and b["l"] <= p["target"])):
            hit = (p["target"], "Quick target")
        if not hit:
            p["best"] = max(p["best"], b["h"]) if d > 0 else min(p["best"], b["l"])
            gain = (p["best"] - p["uIn"]) * d / R
            if self.ctl["profit_lock"] and gain >= LOCK_AT:
                lock = p["uIn"] + d * LOCK_KEEP * R
                if (lock - p["stop"]) * d > 0:
                    p["stop"], p["stopBy"] = lock, "Profit lock"
            if self.ctl["trailing_stop"] and gain >= TRAIL_AT:
                tr = p["best"] - d * TRAIL_GAP * R
                if (tr - p["stop"]) * d > 0:
                    p["stop"], p["stopBy"] = tr, "Trailing stop"
            held = (b["t"] - p["inT"]) / 60
            line = ind["vw"][i] if p["style"] == "scalp" else ind["e21"][i]
            if self.ctl["original_exit"] and held >= 3 and (b["c"] - line) * d < 0:
                hit = (b["c"], "Original exit")
            elif held >= MAX_HOLD[p["style"]]:
                hit = (b["c"], "Time stop")
            elif flat:
                hit = (b["c"], "End of session")
        p["lastT"], p["uLast"] = b["t"], b["c"]
        if p["inst"] == "option":
            p["mark"] = round(opt_value(p, b["c"], b["t"] + 60), 4)
        else:
            p["mark"] = b["c"]
        if hit:
            self.close(pid, p, hit[0], hit[1], b["t"] + 60, ind["date"][i])

    def close(self, pid, p, u, reason, t, d0):
        fx, d, inst = self.a.fx, p["dir"], p["inst"]
        if inst in ("stock", "etf"):
            px = u * (1 - d * SHARE_SLIP)
            self.a.cash["usd"] += p["units"] * px if d > 0 else -p["units"] * px
            pnl_usd = (px - p["inPx"]) * p["units"] * d
            pnl = pnl_usd * fx
            base = p["units"] * p["inPx"]
        elif inst == "future":
            px = u - d * TICK.get(p["sym"], 0)
            gain = (px - p["inPx"]) * p["mult"] * p["units"] * d - FUT_FEE * p["units"]
            self.a.cash["usd"] += gain
            pnl = (gain - p.get("feeIn", 0)) * fx
            base = p["margin"]
        elif inst == "crypto":
            px = u * (1 - CRYPTO_COST)
            self.a.cash["cad"] += p["units"] * px * fx
            pnl = p["units"] * px * fx - p["costCad"]
            base = p["units"] * p["inPx"]
        else:
            px = max(0.01, opt_value(p, u, t) - p["hs"])
            proceeds = p["units"] * 100 * px - OPT_FEE * p["units"]
            self.a.cash["usd"] += proceeds
            pnl = proceeds * fx - p["costCad"]
            base = p["units"] * 100 * p["inPx"]
        self.b["positions"].pop(pid, None)
        self.b["cool"][p["sym"]] = t
        pct = pnl / (base * fx) * 100 if base else 0
        tr = {"id": p["id"], "bot": p["bot"], "sym": p["sym"], "inst": inst, "side": side_word(inst, d),
              "setup": p["setup"], "style": p["style"], "units": round(p["units"], 6), "inT": p["inT"], "outT": int(t),
              "inPx": round(p["inPx"], 6), "outPx": round(px, 6), "uIn": round(p["uIn"], 6), "uOut": round(u, 6),
              "held": int(t - p["inT"]), "exit": reason, "pnlCad": round(pnl, 2), "pct": round(pct, 2),
              "riskCad": p.get("riskCad"), "r": round(pnl / p["riskCad"], 2) if p.get("riskCad") else 0, "why": p["why"]}
        if inst == "option":
            tr.update(strike=p["strike"], expiry=p["expiry"], right=p["right"])
        self.b["trades"].append(tr)
        x = self.day(p["bot"], d0)
        x["pnl"] = round(x["pnl"] + pnl, 2)
        x["n"] += 1
        x["w" if pnl > 0 else "l"] += 1
        x["spark"].append([int(t), x["pnl"]])
        tot = self.b.setdefault("totals", {}).setdefault(p["bot"], {"n": 0, "w": 0, "pnl": 0.0, "since": d0.isoformat()})
        tot["n"] += 1
        tot["w"] += 1 if pnl > 0 else 0
        tot["pnl"] = round(tot["pnl"] + pnl, 2)
        start = x.get("start") or self.a.day_start()
        if not x["brake"]:
            if self.ctl["loss_brake"] and x["pnl"] <= -LOSS_BRAKE * start:
                x["brake"] = "loss brake"
            elif self.ctl["profit_brake"] and x["pnl"] >= PROFIT_BRAKE * start:
                x["brake"] = "profit brake"
            if x["brake"]:
                self.say("brake", {"bot": p["bot"], "brake": x["brake"], "pnl": x["pnl"], "n": x["n"]})
        self.say("close", tr)
        return tr


def round_trip(inst, sym, price, octx=None):
    """Buying and selling once, in the underlying's price units."""
    if inst in ("stock", "etf"):
        return 2 * SHARE_SLIP * price
    if inst == "future":
        return 2 * TICK.get(sym, 0) + 2 * FUT_FEE / FUTURES[sym][1]
    if inst == "crypto":
        return 2 * CRYPTO_COST * price
    hs = (octx or {}).get("hs") or OPT_HALF_SPREAD
    return (2 * hs + 2 * OPT_FEE / 100) / 0.5     # an at-the-money option moves about half as much as the fund


def value_cad(p, fx):
    """What an open intraday position adds to the account's value (cash already moved at entry)."""
    inst, d = p["inst"], p["dir"]
    if inst in ("stock", "etf"):
        return p["units"] * p["mark"] * fx * (1 if d > 0 else -1)
    if inst == "future":
        return (p["mark"] - p["inPx"]) * p["mult"] * p["units"] * d * fx
    if inst == "crypto":
        return p["units"] * p["mark"] * fx
    return p["units"] * 100 * p["mark"] * fx


def open_pnl_cad(p, fx):
    inst, d = p["inst"], p["dir"]
    if inst in ("stock", "etf"):
        return (p["mark"] - p["inPx"]) * p["units"] * d * fx
    if inst == "future":
        return ((p["mark"] - p["inPx"]) * p["mult"] * p["units"] * d - p.get("feeIn", 0)) * fx
    return value_cad(p, fx) - p["costCad"]


def locked_usd(p):
    if p["inst"] == "future":
        return p.get("margin", 0)
    if p["inst"] in ("stock", "etf") and p["dir"] < 0:
        return p["units"] * p["mark"] * (1 + RULES["short_collateral"])
    return 0.0


def open_risk_cad(p):
    if (p["stop"] - p["uIn"]) * p["dir"] >= 0 and p["inst"] != "option":
        return 0.0
    return p.get("riskCad", 0)


# ---------------------------------------------------------------- the page's view of a bot
def checklist(ind, i, cfg, ctl, used):
    out = []
    for d in (1, -1):
        if d < 0 and cfg.get("long_only"):
            continue
        for s in setups(ind, i, d, ctl, used.get(d)):
            if s["setup"] == "orb" and cfg["hours"] == "24h":
                continue          # crypto has no opening bell
            out.append({"setup": SETUP_NAMES[s["setup"]], "side": side_word(cfg["inst"], d), "state": s["state"],
                        "pct": s["pct"], "fired": s["fired"]})
    return out


def nearest(items):
    """A score for how close a symbol is to a signal: higher is closer."""
    return max([it["pct"] for it in items if it["state"] not in ("done for today", "range forming", "on the right side")] or [0])


def chart(bars, ind, start_i, end_i, marks):
    """Compact 1-minute chart for the page: closes, VWAP and minute offsets; the page works out the EMAs."""
    start_i = max(start_i, end_i - 480)
    lo = max(0, start_i - 50)
    t0 = bars[lo]["t"]
    dec = 2 if bars[end_i]["c"] >= 10 else 4 if bars[end_i]["c"] >= 0.1 else 6
    rng = range(lo, end_i + 1)
    return {"t0": t0, "warm": start_i - lo, "dec": dec,
            "m": [(bars[j]["t"] - t0) // 60 for j in rng],
            "c": [round(bars[j]["c"], dec) for j in rng],
            "vw": [round(ind["vw"][j], dec) if j >= start_i else None for j in rng],
            "marks": marks}


def prepare(bars, hours):
    ind = indicators(bars, hours)
    ind["lo"] = [b["l"] for b in bars]
    ind["hi"] = [b["h"] for b in bars]
    ind["op"] = [b["o"] for b in bars]
    return ind


# ---------------------------------------------------------------- the backtest
class PaperAcct:
    """Money for the backtest: CAD 10,000 split like the real book (70% USD, 30% CAD)."""

    def __init__(self, start=10000.0, fx=1.40):
        self.fx = fx
        self.cash = {"usd": start * 0.7 / fx, "cad": start * 0.3}
        self.book = None
        self.starts = {}
        self.cur = None

    def equity(self):
        return self.cash["usd"] * self.fx + self.cash["cad"] + sum(value_cad(p, self.fx) for p in self.book["positions"].values())

    def sleeve(self, s):
        if s == "cad":
            return self.cash["cad"] + sum(value_cad(p, self.fx) for p in self.book["positions"].values() if p["inst"] == "crypto")
        return self.cash["usd"] * self.fx + sum(value_cad(p, self.fx) for p in self.book["positions"].values() if p["inst"] != "crypto")

    def free_usd(self):
        return self.cash["usd"] - sum(locked_usd(p) for p in self.book["positions"].values())

    def risk_room(self):
        return RULES["max_open_risk_pct"] * self.equity() - sum(open_risk_cad(p) for p in self.book["positions"].values())

    def day_start(self):
        return self.starts.get(self.cur, 10000.0)

    def allowed(self, bot, inst, t):
        d = to_ny(epoch_to_utc(t)).date()
        if d != self.cur:
            self.cur = d
            self.starts.setdefault(d, self.equity())
        if self.equity() <= self.day_start() * (1 - RULES["day_loss_pct"]):
            return False, "down 5% today"
        return True, ""


def fetch_month(ysym_, days=29, now=None):
    now = now or dt.datetime.utcnow()
    out, seen = [], set()
    start = now - dt.timedelta(days=days)
    while start < now:
        end = min(now, start + dt.timedelta(days=7))
        for b in minutes(ysym_, start=start, end=end):
            if b["t"] not in seen:
                seen.add(b["t"])
                out.append(b)
        start = end
    return sorted(out, key=lambda b: b["t"])


VARIANTS = {
    "reel rules": {},
    "no filters (like QQQ OG)": {"trend_filter": False, "chop_filter": False},
    "crossings only (scalp)": {"style": "scalp"},
    "trend only": {"style": "trend"},
    "with the original exit": {"original_exit": True},
    "no trailing stop or lock": {"trailing_stop": False, "profit_lock": False},
}


def backtest(cfg=None, days=29, data=None, variants=None, now=None):
    """Plays the last ~month of 1-minute candles through every bot, the same code as the live desk.
    Options are priced with Black-Scholes from the volatility index's previous close; spreads and fees are charged."""
    t0 = time.time()
    bset = bots(cfg)
    if data is None:
        data = {}
        for k, b in bset.items():
            for s in b["symbols"]:
                data[(k, s)] = fetch_month(ysymbol(b["inst"], s), days, now)
        for s, v in VOL_INDEX.items():
            data[("vol", s)] = daily_closes(v)
    if not data.get(("vol", "IWM")) and data.get(("vol", "SPY")):
        data[("vol", "IWM")] = {d: v * 1.3 for d, v in data[("vol", "SPY")].items()}   # small companies swing more
    prepared = {}
    for k, b in bset.items():
        for s in b["symbols"]:
            bars = data.get((k, s)) or []
            if len(bars) > WARM + 10:
                prepared[(k, s)] = (bars, prepare(bars, b["hours"]))
    results = {}
    for vname, over in (variants or VARIANTS).items():
        ctl = controls(cfg)
        ctl.update(over)
        acct = PaperAcct()
        book = {}
        acct.book = book
        eng = Engine(book, acct, ctl)
        # options: volatility from the previous day's close of the index
        ev = []
        for (k, s), (bars, ind) in prepared.items():
            for i in range(WARM, len(bars)):
                ev.append((bars[i]["t"], k, s, i))
        ev.sort()
        for t, k, s, i in ev:
            if k == "options":
                vol = data.get(("vol", s)) or {}
                d = to_ny(epoch_to_utc(t)).date()
                prev = [v for dd, v in vol.items() if dd < d]
                eng.opt[s] = {"iv": (prev[-1] if prev else 20.0) / 100.0}
            bars, ind = prepared[(k, s)]
            acct.allowed(k, bset[k]["inst"], t)
            eng.step(k, bset[k], s, bars, ind, i, True)
        for pid, p in list(book["positions"].items()):
            bars, ind = prepared[(p["bot"], p["sym"])]
            eng.close(pid, p, bars[-1]["c"], "End of test", bars[-1]["t"] + 60, ind["date"][-1])
        results[vname] = summarise(book["trades"], acct)
    first = min((b[0]["t"] for b, _ in prepared.values()), default=0)
    last = max((b[-1]["t"] for b, _ in prepared.values()), default=0)
    return {"ranOn": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "seconds": round(time.time() - t0),
            "from": first, "to": last, "symbols": len(prepared), "variants": results,
            "note": "Last ~month of 1-minute candles from Yahoo; options priced from the volatility index, not real quotes."}


def summarise(trades, acct):
    def block(ts):
        n = len(ts)
        if not n:
            return {"n": 0}
        pnl = sum(t["pnlCad"] for t in ts)
        w = sum(1 for t in ts if t["pnlCad"] > 0)
        eq, peak, dd = 0.0, 0.0, 0.0
        for t in sorted(ts, key=lambda t: t["outT"]):
            eq += t["pnlCad"]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return {"n": n, "win": round(w / n * 100), "pnl": round(pnl, 2), "avgR": round(sum(t["r"] for t in ts) / n, 3),
                "maxDD": round(dd, 2), "held": round(sum(t["held"] for t in ts) / n / 60, 1)}
    out = {"all": block(trades), "bots": {}, "setups": {}, "exits": {}}
    for k in BOTS:
        out["bots"][k] = block([t for t in trades if t["bot"] == k])
    for k in SETUP_NAMES:
        out["setups"][k] = block([t for t in trades if t["setup"] == k])
    for e in set(t["exit"] for t in trades):
        out["exits"][e] = block([t for t in trades if t["exit"] == e])
    days = {}
    for t in trades:
        dd = to_ny(epoch_to_utc(t["outT"])).date().isoformat()
        days[dd] = round(days.get(dd, 0) + t["pnlCad"], 2)
    out["days"] = sorted(days.items())
    out["endCad"] = round(acct.equity(), 2)
    return out


# ---------------------------------------------------------------- the live desk inside an agent run
def epoch(u):
    return int((u - dt.datetime(1970, 1, 1)).total_seconds())


def cboe_quotes(sym, today):
    """Next listed expiry after today from Cboe's free delayed quotes: {"expiry", "iv", "hs", "quotes"}."""
    d, err = get_json("https://cdn.cboe.com/api/global/delayed_quotes/options/%s.json" % sym, {"Accept": "application/json"})
    data = (d or {}).get("data") or {}
    rows = data.get("options") or []
    if not rows:
        return {}
    import re
    by = {}
    for o in rows:
        m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", str(o.get("option", "")))
        if not m or m.group(1) != sym:
            continue
        try:
            exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
        except ValueError:
            continue
        if exp <= today or (exp - today).days > 7:
            continue
        iv = float(o.get("iv") or 0)
        iv = iv / 100 if iv > 3 else iv
        by.setdefault(exp, {})[(int(m.group(4)) / 1000.0, "call" if m.group(3) == "C" else "put")] = (
            float(o.get("bid") or 0), float(o.get("ask") or 0), iv)
    if not by:
        return {}
    exp = min(by)
    q = by[exp]
    spot = float(data.get("current_price") or data.get("close") or 0)
    near = sorted(q.items(), key=lambda kv: abs(kv[0][0] - spot))[:6] if spot else []
    ivs = [v[2] for _, v in near if 0.03 < v[2] < 3]
    hss = [(v[1] - v[0]) / 2 for _, v in near if v[0] > 0 and v[1] > v[0]]
    return {"expiry": exp, "iv": sum(ivs) / len(ivs) if ivs else None, "hs": min(hss) if hss else None, "quotes": q,
            "source": "Cboe"}


class LiveAcct:
    """The agent's own paper book: the same cash, value, open-risk cap and pauses as every other trade."""

    def __init__(self, run):
        self.r = run
        self.cash = run.f
        self.fx = run.fx

    def equity(self):
        self.r.recompute()
        return self.r.f["equityCad"]

    def sleeve(self, s):
        return self.r.sleeve_value(s)

    def free_usd(self):
        return self.r.free_usd()

    def risk_room(self):
        return RULES["max_open_risk_pct"] * self.equity() - self.r.open_risk()

    def day_start(self):
        return self.r.f.get("dayStartEquityCad") or self.equity()

    def allowed(self, bot, inst, t):
        f, r = self.r.f, self.r
        if f.get("halted"):
            return False, "the agent is halted"
        eq = self.equity()
        if eq <= f["dayStartEquityCad"] * (1 - RULES["day_loss_pct"]):
            return False, "the account is down 5% today"
        if eq <= f["weekStartEquityCad"] * (1 - RULES["week_loss_pct"]):
            return False, "the account is down 10% this week"
        ny = to_ny(epoch_to_utc(t))
        m = ny.hour * 60 + ny.minute
        if ny.date().isoformat() in r.fomc_days and 13 * 60 + 30 <= m <= 15 * 60:
            return False, "Fed decision this afternoon"
        if ny.date() == r.today and r.econ:
            if inst in ("stock", "etf", "option") and m < 10 * 60 + 30:
                return False, "%s report morning: nothing before 10:30" % r.econ
            if inst == "future" and 8 * 60 <= m < 9 * 60 + 30:
                return False, "%s report morning: no futures 8:00 to 9:30" % r.econ
        return True, ""


class Desk:
    """Runs the six bots once per agent run and keeps what the page shows."""

    def __init__(self, run):
        self.r = run
        self.st = run.s.setdefault("intraday", {})
        self.book = self.st.setdefault("book", {})
        self.changed = False
        self.brakes = []

    def active(self, cfg):
        r = self.r
        if cfg["hours"] == "24h":
            return True
        if not r.market_day:
            return False
        if cfg["hours"] == "us":
            return 9 * 60 + 30 <= r.hm <= us_close(r.today) + 15
        return 7 * 60 + 30 <= r.hm <= us_close(r.today) + 15

    def idle_status(self, cfg):
        r = self.r
        if cfg["hours"] == "us":
            if r.market_day and r.hm < 9 * 60 + 30:
                return "Market opens 9:30 AM"
            return "Market closed · back next market day at 9:30 AM"
        if r.market_day and r.hm < 8 * 60:
            return "Starts at 8:00 AM New York"
        return "Trades 8:00 AM to %s New York on market days" % ("12:55 PM" if r.today.isoformat() in EARLY_CLOSE else "3:55 PM")

    def say(self, what, x):
        fl = self.r.floor
        self.changed = True
        if what == "close":
            name = BOTS[x["bot"]]["name"]
            if x["inst"] == "option":
                thing = "%s %g %s" % (x["sym"], x["strike"], x["right"])
            elif x["inst"] == "future":
                thing = "%d %s %s" % (x["units"], x["sym"], "long" if x["side"] == "BUY" else "short")
            elif x["inst"] == "crypto":
                thing = x["sym"]
            else:
                thing = "%s %s" % (x["sym"], "shares" if x["side"] == "BUY" else "short")
            mins, secs = divmod(x["held"], 60)
            fl.say("bots", "%s bot: %s (%s) %s%s, %+.1f%% in %dm %02ds, %s." % (
                name, thing, SETUP_NAMES[x["setup"]].lower(), "+" if x["pnlCad"] >= 0 else "-", money(abs(x["pnlCad"])),
                x["pct"], mins, secs, x["exit"].lower()), to="pm", sym=x["sym"], kind="fill" if x["pnlCad"] >= 0 else "block")
        elif what == "brake":
            name = BOTS[x["bot"]]["name"]
            txt = "%s bot hit its %s at %s%s after %d trades: done for today." % (
                name, x["brake"], "+" if x["pnl"] >= 0 else "-", money(abs(x["pnl"])), x["n"])
            self.brakes.append(txt)
            fl.say("risk", txt, to="pm", kind="halt" if x["brake"] == "loss brake" else "info")

    def options_context(self):
        out = {}
        for s in BOTS["options"]["symbols"] if "options" in self.bset else []:
            try:
                q = cboe_quotes(s, self.r.today)
            except Exception as e:
                log("Cboe quotes for %s failed: %s" % (s, e))
                q = {}
            if not q.get("iv"):
                vix = daily_closes(VOL_INDEX.get(s, "^VIX"), "5d")
                v = list(vix.values())[-1] if vix else 20.0
                q.update({"iv": v / 100.0 * (1.3 if s == "IWM" and not vix else 1.0), "source": q.get("source") or "volatility index"})
            out[s] = q
        return out

    def go(self):
        r = self.r
        ctl, self.bset = controls(r.cfg), bots(r.cfg)
        st, book = self.st, self.book
        r.fomc_days = __import__("core").FOMC_DAYS
        econ = r.econ_today() if r.market_day else []
        r.econ = " and ".join(econ)
        now_t = epoch(r.now)
        acct = LiveAcct(r)
        want_opt = "options" in self.bset and (self.active(self.bset["options"]) or
                                               any(p["inst"] == "option" for p in book.get("positions", {}).values()))
        eng = Engine(book, acct, ctl, opt=self.options_context() if want_opt else {}, say=self.say)
        st.update({"controls": ctl, "riskPct": RISK[ctl["risk"]] * 100, "lossBrakePct": LOSS_BRAKE * 100,
                   "profitBrakePct": PROFIT_BRAKE * 100, "maxTrades": MAX_TRADES, "checkedAt": now_t})
        st.setdefault("bots", {})
        for k in list(st["bots"]):
            if k not in self.bset:
                st["bots"].pop(k)
        n_before = len(book.get("trades", []))
        for k, cfg in self.bset.items():
            view = st["bots"].setdefault(k, {})
            view.update({"name": cfg["name"], "inst": cfg["inst"], "symbols": cfg["symbols"], "hours": cfg["hours"],
                         "longOnly": bool(cfg.get("long_only"))})
            holding = any(p["bot"] == k for p in book.get("positions", {}).values())
            if not (self.active(cfg) or holding):
                view.update({"status": self.idle_status(cfg), "live": False, "banner": None})
                continue
            series = {}
            for s in cfg["symbols"]:
                bars = minutes(ysymbol(cfg["inst"], s), "2d")
                if len(bars) > WARM + 5:
                    series[s] = (bars, prepare(bars, cfg["hours"]))
            if not series:
                view.update({"status": "No 1-minute prices from Yahoo this check", "live": True})
                continue
            eng.skips.pop(k, None)
            eng.run(k, cfg, series, enter_from=now_t - CATCHUP * 60, now_t=now_t)
            self.view(k, cfg, ctl, series, view, eng, now_t)
        book["trades"] = book.get("trades", [])[-200:]
        cut = (r.today - dt.timedelta(days=3)).isoformat()
        book["used"] = {k: v for k, v in book.get("used", {}).items() if k.rsplit("|", 1)[-1] >= cut}
        book["cool"] = {k: v for k, v in book.get("cool", {}).items() if v > now_t - 3600}
        new = len(book["trades"]) - n_before if len(book["trades"]) >= n_before else 0
        if self.brakes:
            r.events.extend(self.brakes)
        self.daily_summary()
        saved = st.get("savedAt", 0)
        if self.changed or new or book.get("positions") or now_t - saved >= 900:
            st["savedAt"] = now_t
            r.scanned = True

    def view(self, k, cfg, ctl, series, view, eng, now_t):
        r, book = self.r, self.book
        today = r.today.isoformat()
        best, focus = -1, None
        lists = {}
        for s, (bars, ind) in series.items():
            i = len(bars) - 1
            while i > WARM and bars[i]["t"] + 60 > now_t:
                i -= 1
            d0 = ind["date"][i]
            used = {d: book.get("used", {}).get("%s|%s|%s" % (s, d, d0)) for d in (1, -1)}
            items = checklist(ind, i, cfg, ctl, used)
            lists[s] = (i, items)
            score = nearest(items)
            if score > best:
                best, focus = score, s
        day = book.get("days", {}).get(k) or {}
        if day.get("day") != today:
            day = {"day": today, "pnl": 0.0, "n": 0, "w": 0, "l": 0, "brake": None, "spark": []}
        pos = [p for p in book.get("positions", {}).values() if p["bot"] == k]
        trades_today = [t for t in book.get("trades", []) if t["bot"] == k and
                        to_ny(epoch_to_utc(t["outT"])).date().isoformat() == today]
        chart_sym = (pos[0]["sym"] if pos else trades_today[-1]["sym"] if trades_today else focus)
        if chart_sym not in series:
            chart_sym = focus
        bars, ind = series[chart_sym]
        end_i = lists[chart_sym][0] if chart_sym in lists else len(bars) - 1
        if cfg["hours"] == "24h":
            start_i = max(WARM, end_i - 360)
        else:
            start_i = next((j for j in range(len(bars)) if ind["date"][j] == r.today and ind["key"][j] == r.today and
                            (cfg["hours"] == "us" or ind["m"][j] >= 7 * 60 + 30)), max(WARM, end_i - 390))
            start_i = min(start_i, end_i)
        t0 = bars[max(0, max(start_i, end_i - 480) - 50)]["t"]
        marks = []
        for t in trades_today:
            if t["sym"] == chart_sym:
                marks.append([(t["inT"] - 60 - t0) // 60, "in", 1 if t["side"] in ("BUY", "CALL") else -1, t["pnlCad"]])
                marks.append([(t["outT"] - 60 - t0) // 60, "out", 1 if t["side"] in ("BUY", "CALL") else -1, t["pnlCad"]])
        for p in pos:
            if p["sym"] == chart_sym:
                marks.append([(p["inT"] - 60 - t0) // 60, "in", p["dir"], None])
        i, items = lists[focus]
        fb = series[focus][1]
        banner = "Signal forming, but a filter says no" if any("says no" in it["state"] for it in items) else None
        if day.get("brake"):
            status = "Done for today · %s hit" % day["brake"]
        elif pos:
            p = pos[0]
            status = "Holding " + describe(p) + (" and %d more" % (len(pos) - 1) if len(pos) > 1 else "")
        elif eng.skips.get(k):
            status = "Sitting out · " + eng.skips[k]
        elif any(it["setup"] == "Trend continuation" and it["state"] == "watching for a pullback" for it in items):
            status = "Flat · watching for a pullback"
        else:
            status = "Flat · watching for the next setup"
        view.update({"status": status, "live": True, "focus": focus, "price": round(fb["c"][i], 6), "barT": series[focus][0][i]["t"],
                     "setups": items, "banner": banner, "day": day, "totals": book.get("totals", {}).get(k),
                     "open": [dict(describe=describe(p), sym=p["sym"], side=side_word(p["inst"], p["dir"]), setup=SETUP_NAMES[p["setup"]],
                                   inT=p["inT"], uIn=round(p["uIn"], 6), stop=round(p["stop"], 6), stopBy=p["stopBy"],
                                   target=round(p["target"], 6) if p.get("target") else None, units=round(p["units"], 6),
                                   pnlCad=round(open_pnl_cad(p, r.fx), 2))
                              for p in pos],
                     "chart": dict(chart(bars, ind, start_i, end_i, marks), sym=chart_sym)})

    def daily_summary(self):
        """Once a day after the US close: the bots' day in one alert, like the reel's Discord summary."""
        r, st = self.r, self.st
        if not r.market_day or r.hm < us_close(r.today) + 5 or st.get("summaryDay") == r.today.isoformat():
            return
        st["summaryDay"] = r.today.isoformat()
        parts, total, n = [], 0.0, 0
        for k, b in self.bset.items():
            d = (self.book.get("days") or {}).get(k) or {}
            if d.get("day") != r.today.isoformat() or not d.get("n"):
                continue
            total += d["pnl"]
            n += d["n"]
            parts.append("%s %d trades %dW/%dL %s%s%s" % (b["name"], d["n"], d["w"], d["l"], "+" if d["pnl"] >= 0 else "-",
                                                         money(abs(d["pnl"])), (" (%s)" % d["brake"]) if d.get("brake") else ""))
        if not n:
            return
        line = "Intraday bots today: " + "; ".join(parts) + ". Total %s%s." % ("+" if total >= 0 else "-", money(abs(total)))
        st["summary"] = {"day": r.today.isoformat(), "text": line, "total": round(total, 2), "n": n}
        r.events.append(line)
        r.floor.say("pm", line, kind="meeting")


def describe(p):
    if p["inst"] == "option":
        return "%s %g %s exp %s" % (p["sym"], p["strike"], p["right"], dt.date.fromisoformat(p["expiry"]).strftime("%-m/%-d"))
    if p["inst"] == "future":
        return "%d %s %s" % (p["units"], p["sym"], "long" if p["dir"] > 0 else "short")
    if p["inst"] == "crypto":
        return "%s %s" % (money(p["costCad"]), p["sym"])
    return "%d %s %s" % (p["units"], p["sym"], "shares" if p["dir"] > 0 else "short")
