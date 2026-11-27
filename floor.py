"""The trading floor: the agent's departments and what they say to each other.

Every message is written by the code at the moment a desk does its real job in a run
(a scan, a news read, a score, a risk check, a fill, an exit). Nothing is invented and no AI chat is used.
Messages are one line. Repeats are held back, so a check every five minutes doesn't say the same thing twice.
"""
import datetime as dt

DESKS = {
    "research": "Research",      # scans the market, finds setups, sets watch alerts
    "news": "News desk",         # reads the headlines with the finance-news model
    "analyst": "Analyst",        # company numbers, SEC filings, earnings, Congress trades
    "strategy": "Strategy",      # market mood, the NYSE clock, the Fed calendar
    "quant": "Quant",            # the time machine: 10,000 replays per trade
    "risk": "Risk",              # sizing, the 17 rule checks, limits, halts
    "trader": "Trader",          # tickets, fills, stops, exits, big-move alerts
    "pm": "Portfolio manager",   # decides, runs the morning meeting and the closing bell
    "bots": "Intraday bots",     # the reel's 1-minute EMA/VWAP bots, one per market
}

KEEP = 400   # messages kept in the records

# The risk check's rule names, said the way a risk manager would say them
FAILS = {
    "Buying allowed now": "trading is paused",
    "Allowed market": "that market is closed to us",
    "Not already held": "we already hold it",
    "Score high enough": "the score is too low",
    "Setup valid": "there's no valid setup",
    "No earnings block": "earnings are too close",
    "Not chasing": "it has moved too far today",
    "Stop on the right side": "the stop is on the wrong side",
    "Reward is 2x risk": "reward is under 2x risk",
    "Loss at stop fits budget": "the loss at the stop is too big",
    "Size within limit": "it's too big for its limit",
    "Cash or margin available": "there isn't enough cash",
    "Position count": "we hold the most of that kind already",
    "Trades today and this week": "the trade limit is reached",
    "Daily loss limit": "a stop-out would break today's loss limit",
    "Total open risk": "it would push open risk past the 15% cap",
    "Time machine favourable": "the replays favour the stop",
    "Pack risk check": "the pack's risk check said no",
    "No shares available to borrow": "there are no shares to borrow",
    "Setup switched off by the backtest": "that setup is switched off after the backtest",
}


def fail_words(failed):
    out = []
    for f in failed:
        f = str(f)
        if f.startswith("Sizing: "):
            out.append("sizing says " + f[8:].rstrip(".").lower())
        else:
            out.append(FAILS.get(f, f[:1].lower() + f[1:]))
    return out


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


class Floor:
    def __init__(self, state, now):
        self.fl = state.setdefault("floor", {})
        self.fl.setdefault("messages", [])
        self.fl.setdefault("said", {})
        self.now = now
        self.out = []

    def recently(self, key, hours):
        t = _parse(self.fl["said"].get(key))
        return bool(t and (self.now - t).total_seconds() < hours * 3600)

    def say(self, desk, text, to="all", sym="", kind="info", key=None, hours=24):
        """One line from one desk. With a key, the same thing isn't said again for `hours`."""
        if desk not in DESKS or not text:
            return False
        if key:
            if self.recently(key, hours):
                return False
            self.fl["said"][key] = _iso(self.now)
        self.out.append({"t": _iso(self.now), "n": len(self.out), "d": desk, "to": to if to in DESKS else "all",
                         "x": " ".join(str(text).split())[:150], "s": str(sym or "")[:20], "k": kind})
        return True

    def flush(self):
        self.fl["messages"] = (self.fl["messages"] + self.out)[-KEEP:]
        cut = self.now - dt.timedelta(days=3)
        self.fl["said"] = {k: v for k, v in self.fl["said"].items() if (_parse(v) or cut) > cut}
        if self.out:
            self.fl["lastRun"] = {"at": _iso(self.now), "count": len(self.out)}
        self.out = []
