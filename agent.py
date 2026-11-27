#!/usr/bin/env python3
"""Trading Agent - paper trading with $10,000 of pretend money, running itself on GitHub.

Every run studies what it can of the whole market and trades what passes its rules:
- every US-listed stock (about 5,900) and the liquid ETFs, screened each morning before the open,
  then rescanned about every 10 minutes from 9:50 AM to 2:55 PM New York time, long or short
- options: strong stock signals may buy a call (up) or a put (down) instead of shares
- micro futures on the S&P 500, Nasdaq, Dow, Russell, gold, silver, oil, bitcoin, ether and the euro
- the 100 largest coins (without stablecoins, wrapped copies and meme coins), long only

It reads headlines with a free finance-news AI model, companies' numbers from Nasdaq, their own
earnings press releases from the SEC (when a contact email is set), and Congress trade reports.
It never places a real order: there is no broker code here.

    python3 agent.py run                  # one run; picks OPEN, CLOSE or WATCH from the clock
    python3 agent.py run --dry-run        # do everything, save nothing, send nothing
    python3 agent.py selftest             # check every data source and send a test alert
    python3 agent.py run --state FILE     # keep the records in a local file instead of GitHub
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import tempfile
import traceback

from core import *  # noqa: F401,F403  (clock, data sources, judgement, maths, storage)
from core import OPTION_ERRORS, LOG, CALLS, RULES, FUTURES, INDEX_FUTURES, CRYPTO_FUTURES, LONG_SETUPS, SHORT_SETUPS, SETUPS
from live import LiveDesk
from floor import Floor, fail_words
import intraday


def kind_group(kind):
    return "equity" if kind in ("stock", "etf") else kind


def addon(default):
    """A Day Trader pack add-on must never stop a run. On any error: log it and carry on with the safe answer."""
    def wrap(fn):
        def inner(self, *args, **kw):
            try:
                return fn(self, *args, **kw)
            except Exception as e:
                log("%s skipped after an error: %s: %s" % (fn.__name__, type(e).__name__, str(e)[:120]))
                return default() if callable(default) else default
        inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
        return inner
    return wrap


def day_label(d):
    """'2026-11-21' -> 'Nov 21'."""
    try:
        return dt.date.fromisoformat(str(d)[:10]).strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return str(d or "")


def t12(hm):
    """'16:00' -> '4:00 PM'."""
    try:
        h, m = [int(x) for x in str(hm)[:5].split(":")]
        return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")
    except ValueError:
        return str(hm or "")


class Run:
    """One run of the agent."""

    def __init__(self, state, cfg, now, dry, mode=None):
        self.s, self.cfg, self.now, self.dry = state, cfg, now, dry
        self.f = state["fund"]
        self.ny = to_ny(now)
        self.today = self.ny.date()
        self.market_day = is_trading_day(self.today)
        self.type = mode or pick_mode(state, self.ny)
        self.hm = self.ny.hour * 60 + self.ny.minute
        mins = lambda key: (now - (parse_iso(self.f.get(key)) or dt.datetime(2000, 1, 1))).total_seconds() / 60
        etf_age = (now - (parse_iso(self.f.get("lastEtfScan")) or dt.datetime(2000, 1, 1))).days
        self.universe_due = self.market_day and 8 * 60 <= self.hm < 14 * 60 + 55 and \
            (self.f.get("focus") or {}).get("date") != self.today.isoformat()
        self.etf_due = (etf_age >= 7 or not self.s.get("etfUniverse")) and \
            not (self.market_day and 9 * 60 + 30 <= self.hm <= 16 * 60)
        self.scan_stocks = self.market_day and (self.type == "OPEN" or (
            9 * 60 + 50 <= self.hm <= 14 * 60 + 55 and mins("lastStockScan") >= 9))
        self.scan_crypto = self.type in ("OPEN", "CLOSE") or mins("lastCryptoScan") >= 28
        self.scan_futures = mins("lastFuturesScan") >= 28
        self.replay_now = None
        self.events, self.blocked, self.cands, self.notes = [], [], [], []
        self.work = tempfile.mkdtemp(prefix="agent-")
        os.makedirs(os.path.join(self.work, "trader"), exist_ok=True)
        self.packmode = os.path.exists(os.path.join(PACK, "scan.py"))
        self.daily, self.news, self.names = {}, {}, {}
        self.scanned = False
        self.earn_known = bool(self.f.get("earningsCheckedOn") == self.today.isoformat() and self.f.get("earningsKnown"))
        self.sec_contact = os.environ.get("AGENT_SEC_CONTACT", "").strip()
        self.facts_budget = 4   # SEC company-facts lookups per run (each file can be several MB)
        self.orb = {}
        self.stock_regime = self.crypto_regime = "cautious"
        self.btc24 = 0.0
        self.live = None
        self.floor = Floor(state, now)

    # ---------------------------------------------------------- prices
    def ny_arg(self):
        return self.ny.strftime("%Y-%m-%d %H:%M") if self.replay_now else None

    def session_frac(self):
        o = dt.datetime.combine(self.today, dt.time(9, 30))
        c = dt.datetime.combine(self.today, dt.time(13 if self.today.isoformat() in EARLY_CLOSE else 16, 0))
        return max(0.0, min(1.0, (self.ny - o).total_seconds() / (c - o).total_seconds()))

    def quote(self, sym):
        ch = yahoo_chart(sym, "5d", "5m", self.replay_now)
        if not ch or not ch["price"]:
            return None, None
        return ch["price"], ch

    def dview(self, sym, utc_days=False):
        if sym not in self.daily:
            ch = yahoo_chart(sym, "1y", "1d", self.replay_now)
            frac = 1.0 if utc_days else (self.session_frac() if self.market_day else 1.0)
            self.daily[sym] = daily_view(ch, self.now.date() if utc_days else self.today, utc_days, frac)
        return self.daily[sym]

    # ---------------------------------------------------------- money
    def bookkeeping(self):
        f = self.f
        if f.get("dayDate") != self.today.isoformat():
            f["dayDate"], f["dayStartEquityCad"], f["entriesToday"] = self.today.isoformat(), f["equityCad"], 0
        monday = (self.today - dt.timedelta(days=self.today.weekday())).isoformat()
        if f.get("weekStart") != monday:
            f["weekStart"], f["weekStartEquityCad"], f["entriesThisWeek"] = monday, f["equityCad"], 0

    def mark(self):
        f = self.f
        self.fx, _ = self.quote("CAD=X")
        self.spy, _ = self.quote("SPY")
        self.btc, _ = self.quote("BTC-USD")
        if not self.fx or not self.spy or not self.btc:
            raise RuntimeError("prices could not be fetched (USD/CAD %s, SPY %s, BTC %s)" % (self.fx, self.spy, self.btc))
        f["usdCad"] = round(self.fx, 5)
        if not f.get("benchmarkSet"):
            start = f["startEquityCad"]
            f["usd"] = start * 0.70 * (1 - RULES["fx_cost"]) / self.fx
            f["cad"] = start * 0.30
            f["benchmarkSpyUnits"] = start * 0.70 * (1 - RULES["fx_cost"]) / self.fx * 0.999 / self.spy
            f["benchmarkBtcUnits"] = start * 0.30 * 0.992 / (self.btc * self.fx)
            f["benchmarkSet"] = True
            f["startDate"] = self.today.isoformat()
        for pid, p in self.s["positions"].items():
            if p["kind"] == "option":
                self.mark_option(p)
            else:
                px, _ = self.quote(p["yahoo"])
                if px:
                    p["lastPrice"] = px
            if p["side"] == "short" and p["kind"] in ("stock", "etf"):
                last = parse_iso(p.get("lastFeeAt")) or self.now
                days = max(0.0, (self.now - last).total_seconds() / 86400)
                fee = p["units"] * p["lastPrice"] * p.get("borrowRate", RULES["borrow_rate"]) * days / 365
                f["usd"] -= fee
                p["borrowFeeCad"] = r2(p.get("borrowFeeCad", 0) + fee * self.fx)
                p["lastFeeAt"] = iso(self.now)
            self.value_position(p)
        self.recompute()

    def mark_option(self, p):
        exp = dt.date.fromisoformat(p["expiry"])
        chain = nasdaq_option_chain(p["underlying"], exp, exp, money="all")
        row = next((r for r in chain if r["expiry"] == exp and abs((r["strike"] or 0) - p["strike"]) < 1e-6), None)
        if row:
            leg = row[p["right"]]
            bid, ask = leg["bid"], leg["ask"]
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (leg["last"] or p["lastPrice"])
            p.update({"bid": bid, "ask": ask, "lastPrice": mid, "markedAt": iso(self.now)})
        upx, _ = self.quote(p["underlying"])
        if upx:
            p["underlyingPrice"] = upx

    def value_position(self, p):
        fx = self.fx if p.get("currency", "USD") == "USD" else 1.0
        k, px = p["kind"], p["lastPrice"]
        if k == "option":
            val = p["units"] * 100 * px * fx
            pnl = val - p["costCad"]
        elif k == "future":
            dirn = 1 if p["side"] == "long" else -1
            val = (px - p["entryPrice"]) * p["mult"] * p["units"] * dirn * fx
            pnl = val - p.get("feesCad", 0)
        elif p["side"] == "short":
            val = -p["units"] * px * fx
            pnl = p["costCad"] + val - p.get("borrowFeeCad", 0)
        else:
            val = p["units"] * px * fx
            pnl = val - p["costCad"]
        p["valueCad"] = r2(val)
        p["pnlCad"] = r2(pnl)
        p["pnlPct"] = round(pnl / abs(p["costCad"]) * 100, 2) if p.get("costCad") else 0.0

    def ib_positions(self):
        """Open positions of the intraday bots (intraday.py). They share this book's cash, value and limits."""
        return list(((self.s.get("intraday") or {}).get("book") or {}).get("positions", {}).values())

    def ib_value(self, sleeve=None):
        return sum(intraday.value_cad(p, self.fx) for p in self.ib_positions()
                   if sleeve is None or (p["inst"] == "crypto") == (sleeve == "cad"))

    def recompute(self):
        f = self.f
        f["equityCad"] = r2(f.get("usd", 0) * self.fx + f.get("cad", 0) + sum(p["valueCad"] for p in self.s["positions"].values())
                            + self.ib_value())
        f["benchmarkCad"] = r2(f["benchmarkSpyUnits"] * self.spy * self.fx + f["benchmarkBtcUnits"] * self.btc * self.fx)

    def free_usd(self):
        locked = 0.0
        for p in self.s["positions"].values():
            if p["kind"] in ("stock", "etf") and p["side"] == "short":
                locked += p["units"] * p["lastPrice"] * (1 + RULES["short_collateral"])
            elif p["kind"] == "future":
                locked += p.get("marginUsd", 0)
        locked += sum(intraday.locked_usd(p) for p in self.ib_positions())
        return self.f.get("usd", 0) - locked

    def sleeve_value(self, sleeve):
        f = self.f
        if sleeve == "cad":
            return f.get("cad", 0) + sum(p["valueCad"] for p in self.s["positions"].values() if p["sleeve"] == "cad") + self.ib_value("cad")
        return f.get("usd", 0) * self.fx + sum(p["valueCad"] for p in self.s["positions"].values() if p["sleeve"] == "usd") \
            + self.ib_value("usd")

    def open_risk(self):
        tot = 0.0
        for p in self.s["positions"].values():
            at_entry = (p["side"] == "long" and p["stopPrice"] >= p["entryPrice"]) or \
                       (p["side"] == "short" and p["stopPrice"] <= p["entryPrice"])
            tot += 0 if at_entry and p["kind"] != "option" else p.get("riskCad", 0)
        return tot + sum(intraday.open_risk_cad(p) for p in self.ib_positions())

    def count(self, group):
        return sum(1 for p in self.s["positions"].values() if kind_group(p["kind"]) == group)

    # ---------------------------------------------------------- closing
    def close(self, pid, price, reason, when=None, bid=None):
        p = self.s["positions"].pop(pid)
        if self.live and p["kind"] in ("stock", "etf") and p["side"] == "long" and p.get("market") != "TSX":
            self.live.stock_exit(p["underlying"], reason)
        f, k, fx = self.f, p["kind"], self.fx
        when = when or self.ny
        fees = 0.0
        if k == "option":
            px = bid if bid is not None else p.get("bid") or price
            fees = RULES["option_fee"] * p["units"]
            proceeds = p["units"] * 100 * px - fees
            f["usd"] += proceeds
            pnl = proceeds * fx - p["costCad"]
            value, side_lbl = proceeds * fx, "SELL"
        elif k == "future":
            slip = RULES["slip"]["future"]
            dirn = 1 if p["side"] == "long" else -1
            px = price * (1 - slip * dirn)
            fees = RULES["future_fee"] * p["units"]
            gain = (px - p["entryPrice"]) * p["mult"] * p["units"] * dirn
            f["usd"] += gain - fees
            pnl = (gain - fees) * fx - p.get("feesCad", 0)
            value, side_lbl = abs(px * p["mult"] * p["units"]) * fx, "SELL" if dirn > 0 else "COVER"
        elif k == "crypto":
            px = price * (1 - RULES["slip"]["crypto"])
            proceeds = p["units"] * px * fx
            f["cad"] += proceeds
            pnl = proceeds - p["costCad"]
            value, side_lbl = proceeds, "SELL"
            fees = p["units"] * price * RULES["slip"]["crypto"]
        elif p["side"] == "short":
            px = price * (1 + RULES["slip"][k])
            cost = p["units"] * px
            f["usd"] -= cost
            pnl = p["costCad"] - cost * fx - p.get("borrowFeeCad", 0)
            value, side_lbl = cost * fx, "COVER"
            fees = p["units"] * price * RULES["slip"][k]
        else:
            px = price * (1 - RULES["slip"][k])
            proceeds = p["units"] * px
            pfx = 1.0 if p.get("currency") == "CAD" else fx
            if p.get("sleeve") == "cad":
                f["cad"] += proceeds
            else:
                f["usd"] += proceeds
            pnl = proceeds * pfx - p["costCad"]
            value, side_lbl = proceeds * pfx, "SELL"
            fees = p["units"] * price * RULES["slip"][k]
        r = pnl / p["riskCad"] if p.get("riskCad") else 0.0
        tid = "%s-%s-%s" % (when.strftime("%Y%m%d-%H%M"), pid.replace(" ", "_"), side_lbl)
        self.s["trades"].append({
            "id": tid, "date": when.strftime("%Y-%m-%d %H:%M"), "symbol": p["symbol"], "kind": k, "side": side_lbl,
            "units": p["units"], "price": round(px, 6), "currency": p.get("currency"), "valueCad": r2(value),
            "costsCad": r2(fees * (fx if p.get("currency") == "USD" else 1)), "setup": p.get("setup"),
            "reason": reason, "pnlCad": r2(pnl), "r": round(r, 2), "closedOn": self.today.isoformat()})
        self.floor_exit(p, side_lbl, px, reason, pnl, r)
        self.events.append("%s %s (%s): %s%s" % ("Covered" if side_lbl == "COVER" else "Sold", p["symbol"], reason,
                                                 "+" if pnl >= 0 else "", money(pnl)))
        log("%s %s at %.4f: %s, P&L %s (%.2fR)" % (side_lbl, p["symbol"], px, reason, money(pnl), r))

    # ---------------------------------------------------------- step 2: exits
    def exits(self):
        for pid in list(self.s["positions"]):
            p = self.s["positions"][pid]
            k, side = p["kind"], p["side"]
            stock_like = k in ("stock", "etf", "option")
            since = parse_iso(p.get("lastChecked") or p.get("entryTime")) or self.now
            done = False
            if not (stock_like and not self.market_day and p.get("lastChecked")):
                ysym_ = p["underlying"] if k == "option" else p["yahoo"]
                _, ch = self.quote(ysym_)
                if ch:
                    cut = utc_to_epoch(since)
                    if k == "option":
                        stop, target, dirn = p["uStop"], p["uTarget"], (1 if p["right"] == "call" else -1)
                    else:
                        stop, target, dirn = p["stopPrice"], p["targetPrice"], (1 if side == "long" else -1)
                    for b in ch["bars"]:
                        if b["t"] + 300 <= cut:
                            continue
                        when = to_ny(epoch_to_utc(b["t"] + 300))
                        hit = None
                        if dirn > 0:
                            if b["o"] <= stop:
                                hit = (b["o"], "stop-loss hit")
                            elif b["l"] <= stop:
                                hit = (stop, "stop-loss hit")
                            elif b["h"] >= target:
                                hit = (target, "take-profit hit")
                        else:
                            if b["o"] >= stop:
                                hit = (b["o"], "stop-loss hit")
                            elif b["h"] >= stop:
                                hit = (stop, "stop-loss hit")
                            elif b["l"] <= target:
                                hit = (target, "take-profit hit")
                        if hit:
                            if k == "option":
                                self.close(pid, p["lastPrice"], "the stock hit its %s" % ("stop" if "stop" in hit[1] else "target"))
                            else:
                                self.close(pid, hit[0], hit[1], when)
                            done = True
                            break
            if done:
                continue
            reason = self.exit_reason(p)
            if reason:
                desk = "news" if reason.startswith("news") else "analyst" if "report" in reason or "earnings" in reason else "risk"
                self.floor.say(desk, "Close %s: %s." % (p["symbol"], reason.replace("day trade closed: ", "day trade ")), to="trader",
                               sym=p["underlying"], kind="block")
                self._reason_said = pid
                self.close(pid, p["lastPrice"], reason)
                continue
            if k != "option":
                init = p.get("initialStop", p["stopPrice"])
                gain = (p["lastPrice"] - p["entryPrice"]) if side == "long" else (p["entryPrice"] - p["lastPrice"])
                if gain >= abs(p["entryPrice"] - init) and ((side == "long" and p["stopPrice"] < p["entryPrice"]) or
                                                             (side == "short" and p["stopPrice"] > p["entryPrice"])):
                    p["stopPrice"] = p["entryPrice"]
                    if self.live and k in ("stock", "etf") and side == "long":
                        self.live.stock_stop(p["underlying"], p["entryPrice"])
                    log("%s: up 1R, stop moved to the entry price" % p["symbol"])
                    self.floor.say("trader", "%s is up 1R: stop moved to the entry, %s. This one can't lose now." % (
                        p["symbol"], self.px(p["entryPrice"])), to="risk", sym=p["underlying"], kind="info")
            p["lastChecked"] = iso(self.now)
            self.value_position(p)
        self.recompute()

    def exit_reason(self, p):
        k, side = p["kind"], p["side"]
        if k == "option":
            exp = dt.date.fromisoformat(p["expiry"])
            if p.get("bid") is not None and p["bid"] <= p["stopPrice"]:
                return "option lost half its value"
            if p.get("bid") is not None and p["bid"] >= p["targetPrice"]:
                return "option doubled: take-profit"
            if (exp - self.today).days <= RULES["option_exit_dte"]:
                return "close to expiry"
        if k == "crypto":
            held = (self.now.date() - dt.date.fromisoformat(p["entryDate"])).days
        else:
            held = trading_days_between(dt.date.fromisoformat(p["entryDate"]), self.today)
        if held > RULES["hold_days"][k]:
            return "held the maximum %d days" % RULES["hold_days"][k]
        n = self.news.get(p["underlying"])
        bullish = (side == "long") if k != "option" else (p["right"] == "call")
        if n and (n.get("brokenLong") if bullish else n.get("brokenShort")):
            return "news broke the reason for the trade: %s" % (n["headlines"] or [""])[0][:60]
        if self.type == "CLOSE" and self.market_day and k in ("stock", "etf", "option"):
            if k != "option" and p["entryDate"] == self.today.isoformat():
                dv = self.dview(p["underlying"])
                if side == "long":
                    ok = p["lastPrice"] > p["entryPrice"] and dv and dv["sma20"] and p["lastPrice"] > dv["sma20"]
                else:
                    ok = p["lastPrice"] < p["entryPrice"] and dv and dv["sma20"] and p["lastPrice"] < dv["sma20"]
                if not ok:
                    return "day trade closed: not in profit with the trend intact"
            if p.get("assetKind", k) in ("stock",) or (k == "option"):
                e = self.s["earnings"].get(p["underlying"])
                nxt = next_trading_day(self.today).isoformat()
                if not self.earn_known and not e:
                    return "earnings date unknown: no overnight hold"
                if e and e.get("status") == "upcoming" and (
                        (e.get("reportDate") == self.today.isoformat() and e.get("timing") != "before-open")
                        or (e.get("reportDate") == nxt and e.get("timing") != "after-close")):
                    return "company reports before the next open"
        return None

    # ---------------------------------------------------------- the trading floor: what each desk says
    @staticmethod
    def px(v):
        v = float(v)
        return "$%s" % ("{:,.2f}".format(v) if v >= 1 else "%.6f" % v)

    @addon(None)
    def floor_mood(self):
        spy, btc = self.daily.get("SPY"), self.daily.get("BTC-USD")
        if self.market_day and spy and spy.get("sma200"):
            where = ("down %.1f%% today" % abs(spy["movePct"]) if spy["movePct"] < -2 else
                     "below its 200-day average" if spy["price"] < spy["sma200"] else
                     "above its 50- and 200-day averages" if spy["price"] > spy["sma50"] else "between its averages")
            self.floor.say("strategy", "Stocks are %s: the S&P 500 fund is %s." % (self.stock_regime.replace("-", " "), where),
                           to="pm", kind="mood", key="mood:stocks:%s:%s" % (self.stock_regime, self.today), hours=24)
        if btc and btc.get("sma50"):
            self.floor.say("strategy", "Crypto is %s: Bitcoin %+.1f%% in 24 hours, %s its 50-day average." % (
                self.crypto_regime.replace("-", " "), self.btc24, "above" if btc["price"] > btc["sma50"] else "below"),
                to="pm", kind="mood", key="mood:crypto:%s:%s" % (self.crypto_regime, self.today), hours=24)

    @addon(None)
    def floor_gate(self, fomc_late):
        if self.block_reason:
            self.floor.say("risk", "No new trades: %s." % self.block_reason, to="pm", kind="block",
                           key="gate:%s:%s" % (self.block_reason, self.today), hours=24)
        if fomc_late:
            self.floor.say("strategy", "Fed decision this afternoon: we stand aside until it's out.", to="pm", kind="mood",
                           key="fomc:%s" % self.today, hours=24)

    @addon(None)
    def floor_meeting(self, which):
        f = self.f
        held = len(self.s["positions"])
        if which == "open":
            self.floor.say("pm", "Morning meeting: value %s, %d position%s held, stocks %s, crypto %s. Let's find setups." % (
                money(f["equityCad"]), held, "" if held == 1 else "s", self.stock_regime.replace("-", " "),
                self.crypto_regime.replace("-", " ")), kind="meeting", key="meeting:open:%s" % self.today, hours=20)
        else:
            day = f["equityCad"] - f["dayStartEquityCad"]
            today = [t for t in self.s["trades"] if str(t.get("date", "")).startswith(self.today.isoformat())]
            ins = sum(1 for t in today if t.get("side") in ("BUY", "SHORT"))
            self.floor.say("pm", "Closing bell: value %s (%s%s today), %d new trade%s, %d exit%s, %d held overnight." % (
                money(f["equityCad"]), "+" if day >= 0 else "", money(day), ins, "" if ins == 1 else "s",
                len(today) - ins, "" if len(today) - ins == 1 else "s", held),
                kind="meeting", key="meeting:close:%s" % self.today, hours=20)

    @addon(None)
    def floor_idea(self, c, need, setup, why):
        """Research, News, Analyst and Strategy brief the PM on one idea; the PM decides."""
        sym, side, p = c["symbol"], c["side"], c.get("parts") or {}
        label = "%s %s" % ("Long" if side == "long" else "Short", sym)
        c["talk"] = False
        if c["score"] < need - 6 or getattr(self, "_floor_limit", False):
            return
        key = "idea:%s:%s:%d:%s" % (sym, side, c["score"] // 4, setup or "none")
        if not self.floor.say("research", "%s: %s, chart %d/30." % (label, why, p.get("technical", 0)),
                              to="pm", sym=sym, kind="idea", key=key, hours=3):
            return
        c["talk"] = True
        n = c.get("news") or {}
        heads = n.get("headlines") or []
        if heads:
            tone = "mostly good" if n.get("pos", 0) > n.get("neg", 0) else "mostly bad" if n.get("neg", 0) > n.get("pos", 0) else "mixed"
            top = heads[0] if len(heads[0]) <= 64 else heads[0][:64].rsplit(" ", 1)[0] + "..."
            self.floor.say("news", "%s: %d fresh headline%s, %s; %d/25. Top: \"%s\"" % (
                sym, len(heads), "" if len(heads) == 1 else "s", tone, p.get("news", 12), top), to="pm", sym=sym, kind="vote")
        else:
            self.floor.say("news", "%s: no fresh headlines, neutral %d/25." % (sym, p.get("news", 12)), to="pm", sym=sym, kind="vote")
        if c["kind"] == "stock":
            fw = c.get("fundWhy") or "not available"
            self.floor.say("analyst", ("%s: company numbers unavailable today, neutral %d/15." if fw == "not available" else "%s: %s; %d/15.") % (
                (sym, p.get("fundamental", 9)) if fw == "not available" else (sym, fw[:90], p.get("fundamental", 9))), to="pm", sym=sym, kind="vote")
        market = "crypto" if c["kind"] == "crypto" else "futures" if c["kind"] == "future" else "stocks"
        self.floor.say("strategy", "%s are %s: %d/15 for a %s." % (market.title(), c["regime"].replace("-", " "), p.get("macro", 0), side),
                       to="pm", kind="vote", key="macro:%s:%s:%s:%s" % (market, side, c["regime"], self.now.isoformat()), hours=1)
        if c["score"] >= need and setup:
            self.floor.say("pm", "%s scores %d, needs %d. Quant and Risk, check it." % (label, c["score"], need), sym=sym, kind="decide")
        elif c["score"] >= need:
            self.floor.say("pm", "%s scores %d but %s. Waiting." % (label, c["score"], why), sym=sym, kind="decide")
        else:
            self.floor.say("pm", "%s scores %d, needs %d. Pass." % (label, c["score"], need), sym=sym, kind="decide")

    @addon(None)
    def floor_plan(self, c, price, stop, target, rep, opt):
        if not c.get("talk"):
            return
        sym = c["symbol"]
        pt, ps = rep.get("pTarget"), rep.get("pStop")
        if pt is not None and ps is not None:
            how = "5-minute" if rep.get("source") == "pack-5m" else "daily"
            timed = " Most run out of time first." if pt + ps < 0.5 else ""
            self.floor.say("quant", "%s: in 10,000 %s replays the target comes first %.0f%%, the stop %.0f%%.%s %s" % (
                sym, how, pt * 100, ps * 100, timed, "Odds are with us." if rep.get("favourable") else "Odds are against us."),
                to="pm", sym=sym, kind="vote")
        if opt:
            self.floor.say("trader", "%s: %d %s%s at strike %g, %s expiry, for %s." % (
                sym, opt["contracts"], opt["right"], "" if opt["contracts"] == 1 else "s", opt["strike"], day_label(opt["expiry"]),
                money(opt["costUsd"])), to="pm", sym=sym, kind="vote")

    @addon(None)
    def floor_check(self, c, inst, failed, passed):
        if not c.get("talk"):
            return
        sym, side = c["symbol"], c["side"]
        sz = c.get("sizing") or {}
        if c.get("shares"):
            self.floor.say("risk", "%s: %d whole shares, limited by the %s; max loss %s." % (
                sym, c["shares"], str(sz.get("bound_by") or "budget").replace("_", " "), "$%.2f" % float(sz.get("max_loss") or 0)),
                to="trader", sym=sym, kind="check")
        if failed:
            words = fail_words(failed)
            self.floor.say("risk", "Blocked %s: %s." % (sym, "; ".join(words[:3])), to="pm", sym=sym, kind="block")
            if set(failed) <= {"Trades today and this week", "Daily loss limit", "Total open risk", "Buying allowed now"}:
                self._floor_limit = True
        else:
            self.floor.say("risk", "%s passes all %d checks." % (sym, passed), to="pm", sym=sym, kind="check")
            if inst == "option":
                what = "buy the %s" % ("calls" if side == "long" else "puts")
            elif inst == "future":
                what = "go %s %s" % (side, sym)
            elif c.get("shares"):
                what = "buy %d %s" % (c["shares"], sym)
            else:
                what = "%s %s" % ("buy" if side == "long" else "short", sym)
            self.floor.say("pm", "Approved: %s." % what, to="trader", sym=sym, kind="order")
            t = c.get("ticket") or {}
            if t.get("plain"):
                parts = str(t["plain"]).split(" / ")
                keep = [parts[0].replace(", market, good for today", "")] + [x for x in parts[1:] if x.startswith(("stop-loss", "take-profit"))]
                self.floor.say("trader", "Ticket: %s." % ", ".join(keep), to="pm", sym=sym, kind="order")

    @addon(None)
    def floor_fill(self, p, side_lbl, units):
        sym = p["symbol"]
        what = ("%d %s" % (units, sym)) if p["kind"] in ("stock", "etf", "option", "future") and float(units).is_integer() else sym
        verb = {"BUY": "bought", "SHORT": "shorted"}.get(side_lbl, side_lbl.lower())
        self.floor.say("trader", "Filled: %s %s at %s. Stop %s, target %s." % (
            verb, what, self.px(p["entryPrice"]), self.px(p["stopPrice"]), self.px(p["targetPrice"])),
            to="pm", sym=p.get("underlying", sym), kind="fill")

    @addon(None)
    def floor_exit(self, p, side_lbl, px, reason, pnl, r):
        said = getattr(self, "_reason_said", None) in (p["symbol"], p["symbol"] + " short") or \
            any(m["s"] == p.get("underlying") and m["x"].startswith("Close ") for m in self.floor.out[-1:])
        self.floor.say("trader", "%s %s at %s%s: %s%s (%+.2fR)." % (
            "Covered" if side_lbl == "COVER" else "Sold", p["symbol"], self.px(px), "" if said else " (%s)" % reason,
            "+" if pnl >= 0 else "", money(pnl), r), to="pm", sym=p.get("underlying", p["symbol"]), kind="exit")

    # ---------------------------------------------------------- step 3: the market's mood and the risk gate
    def regimes(self):
        spy = self.dview("SPY")
        if spy and spy["sma200"]:
            if spy["price"] < spy["sma200"] or spy["movePct"] < -2:
                self.stock_regime = "risk-off"
            elif spy["price"] > spy["sma50"] and spy["price"] > spy["sma200"]:
                self.stock_regime = "risk-on"
            else:
                self.stock_regime = "cautious"
        btc = self.dview("BTC-USD", True)
        _, bch = self.quote("BTC-USD")
        if bch and bch["bars"]:
            t_end = bch["bars"][-1]["t"]
            old = [b for b in bch["bars"] if b["t"] <= t_end - 86400]
            if old:
                self.btc24 = (bch["price"] / old[-1]["c"] - 1) * 100
        if not btc or not btc["sma200"] or self.btc24 < -6 or btc["price"] < btc["sma200"]:
            self.crypto_regime = "risk-off"
        elif btc["price"] > btc["sma50"]:
            self.crypto_regime = "risk-on"
        else:
            self.crypto_regime = "cautious"
        self.floor_mood()

    def gate(self):
        f = self.f
        why = []
        halt_at = f["startEquityCad"] * RULES["halt_pct"]
        if f["equityCad"] <= halt_at and not f.get("halted"):
            f["halted"], f["haltReason"] = True, "value fell to %s, at or below the %s limit" % (money(f["equityCad"]), money(halt_at))
            self.events.append("HALTED: " + f["haltReason"])
            self.floor.say("risk", "Halting all new trades: %s." % f["haltReason"], kind="halt")
        if f.get("halted"):
            why.append("halted")
        if f["equityCad"] <= f["dayStartEquityCad"] * (1 - RULES["day_loss_pct"]):
            why.append("down %d%% today" % round(RULES["day_loss_pct"] * 100))
        if f["equityCad"] <= f["weekStartEquityCad"] * (1 - RULES["week_loss_pct"]):
            why.append("down %d%% this week" % round(RULES["week_loss_pct"] * 100))
        if f["entriesToday"] >= RULES["entries_per_day"]:
            why.append("%d trades already today" % RULES["entries_per_day"])
        if f["entriesThisWeek"] >= RULES["entries_per_week"]:
            why.append("%d trades already this week" % RULES["entries_per_week"])
        if self.open_risk() >= RULES["max_open_risk_pct"] * f["equityCad"]:
            why.append("%d%% of the account already at risk" % round(RULES["max_open_risk_pct"] * 100))
        self.block_reason = ", ".join(why)
        self.stocks_ok = not why and self.scan_stocks and self.type != "CLOSE"
        fomc_late = self.today.isoformat() in FOMC_DAYS and 13 * 60 + 30 <= self.hm <= 15 * 60
        if fomc_late:
            self.notes.append("Fed decision this afternoon: no new trades around it")
        self.stocks_ok = self.stocks_ok and not fomc_late
        self.crypto_ok = not why and self.scan_crypto and self.crypto_regime != "risk-off" and not fomc_late
        self.futures_ok = not why and self.scan_futures and not fomc_late
        econ = self.econ_today() if self.market_day else []
        if econ:
            what = " and ".join(econ)
            if self.hm < 10 * 60 + 30 and self.stocks_ok:
                self.stocks_ok = False
                self.notes.append("%s report at 8:30 today: no stock trades before 10:30" % what.capitalize())
            if 8 * 60 <= self.hm < 9 * 60 + 30:
                self.futures_ok = False
            self.floor.say("strategy", "The %s report comes out at 8:30 this morning. No stock trades before 10:30, "
                           "no futures trades 8:00 to 9:30." % what, to="pm", kind="mood", key="econ:%s" % self.today, hours=20)
        self.floor_gate(fomc_late)

    # ---------------------------------------------------------- the whole market, once a day
    def universe_scan(self):
        t0 = time.time()
        stocks = stock_universe(self.work)
        etfs = self.s.get("etfUniverse") or []
        names = dict(stocks)
        syms = [s for s, _ in stocks] + [e for e in etfs if e not in names]
        if not syms:
            return
        spark = yahoo_spark_daily(syms, self.replay_now)
        rows = []
        for sym, ch in spark.items():
            dv = daily_view(ch, self.today, False, 1.0)
            if not dv or dv["price"] < 5 or dv["dollarVol"] < 20e6:
                continue
            rows.append((sym, tech_score(dv, "long"), tech_score(dv, "short"), dv["dollarVol"], dv["movePct"]))
        rows.sort(key=lambda r: -r[3])
        active = [r[0] for r in rows[:30]]
        longs = [r[0] for r in sorted(rows, key=lambda r: (-r[1], -r[3])) if r[1] >= 15][:60]
        shorts = [r[0] for r in sorted(rows, key=lambda r: (-r[2], -r[3])) if r[2] >= 15][:40]
        keep = set(active) | set(longs) | set(shorts)
        self.f["focus"] = {"date": self.today.isoformat(), "long": longs, "short": shorts, "active": active,
                           "screened": len(spark), "liquid": len(rows), "universe": len(syms),
                           "names": {s: names.get(s, s)[:60] for s in keep}}
        self.f["lastUniverseScan"] = iso(self.now)
        self.scanned = True
        log("Market screen: %d of %d charts read, %d liquid, focus %d long / %d short ideas (%.0fs)" % (
            len(spark), len(syms), len(rows), len(longs), len(shorts), time.time() - t0))
        self.floor.say("research", "Morning screen: %s charts read, %s liquid enough. %d long and %d short ideas on today's list." % (
            "{:,}".format(len(spark)), "{:,}".format(len(rows)), len(longs), len(shorts)), to="pm", kind="info")

    def etf_refresh(self):
        t0 = time.time()
        etfs = etf_universe()
        if not etfs:
            return
        spark = yahoo_spark_daily([e for e, _ in etfs], None)
        rows = []
        for sym, ch in spark.items():
            dv = daily_view(ch, self.today, False, 1.0)
            if dv and dv["price"] >= 5 and dv["dollarVol"] >= 20e6:
                rows.append((dv["dollarVol"], sym))
        rows.sort(reverse=True)
        self.s["etfUniverse"] = [s for _, s in rows[:300]]
        self.f["lastEtfScan"] = iso(self.now)
        self.scanned = True
        log("ETF list: %d of %d ETFs read, %d liquid kept (%.0fs)" % (len(spark), len(etfs), len(self.s["etfUniverse"]), time.time() - t0))
        self.floor.say("research", "Weekly ETF list: %d liquid ETFs kept out of %s." % (len(self.s["etfUniverse"]), "{:,}".format(len(etfs))),
                       to="pm", kind="info")

    # ---------------------------------------------------------- Congress
    def congress(self):
        f = self.f
        if not self.market_day or self.ny.hour < 9 or f.get("lastCongressCheck") == self.today.isoformat():
            return
        since = (self.today - dt.timedelta(days=7)).isoformat()
        code, data, tail = pack("ptr.py", ["index", "--since", since, "--json"], self.work)
        if not isinstance(data, list):
            log("Congress check unavailable: %s" % tail[-120:])
            return
        readable = ensure_pdftotext()
        seen = set(self.s.get("congressSeen", []))
        new = [r for r in data if r.get("doc_id") not in seen][:8]
        read = 0
        for r in new:
            member = r.get("name", "").replace("Hon. ", "")
            report = None
            if readable:
                argv = ["fetch", str(r["doc_id"]), "--year", str(r.get("year") or self.today.year),
                        "--filed", r.get("filed") or "", "--json"]
                code, report, tail = pack("ptr.py", argv, self.work)
            rows = (report or {}).get("rows") or []
            stock_rows = [x for x in rows if x.get("ticker") and x.get("asset_type") in (None, "ST", "OP")]
            if report is not None:
                read += 1
            for i, x in enumerate(stock_rows[:12]):
                cid = "%s-%s-%s-%d" % (r.get("filed"), x["ticker"], r.get("last", ""), i)
                kind = {"P": "Purchase", "S": "Sale", "S (partial)": "Sale"}.get(x.get("type"), str(x.get("type_name", "")).title())
                self.s["congress"][cid] = {
                    "member": member, "chamber": "House", "symbol": x["ticker"], "type": kind,
                    "amount": x.get("amount") or "", "tradeDate": x.get("date") or "", "filedDate": r.get("filed"),
                    "url": r.get("url"), "note": ("options, " if x.get("asset_type") == "OP" else "") +
                    ("%s days to report" % x["days_to_disclose"] if x.get("days_to_disclose") is not None else "")}
            if not stock_rows:
                self.s["congress"]["%s-%s" % (r.get("filed"), r.get("doc_id"))] = {
                    "member": member, "chamber": "House", "symbol": "", "type": "Filed", "amount": "",
                    "tradeDate": "", "filedDate": r.get("filed"), "url": r.get("url"),
                    "note": "no stock trades read from this report" if report is not None else "open the link to read it"}
            seen.add(r.get("doc_id"))
        self.s["congressSeen"] = sorted(seen)[-300:]
        keep = sorted(self.s["congress"], key=lambda k: self.s["congress"][k].get("filedDate") or "", reverse=True)[:40]
        self.s["congress"] = {k: self.s["congress"][k] for k in keep}
        f["lastCongressCheck"] = self.today.isoformat()
        log("Congress tracker: %d new House reports, %d read" % (len(new), read))
        fresh = [c for c in self.s["congress"].values() if c.get("symbol") and c.get("filedDate") and
                 any(str(c.get("url") or "").endswith("%s.pdf" % r.get("doc_id")) for r in new)]
        for c in fresh[:3]:
            self.floor.say("analyst", "House filing: %s %s %s (%s), traded %s." % (
                c["member"], "bought" if c["type"] == "Purchase" else "sold", c["symbol"], c.get("amount") or "amount not given",
                day_label(c.get("tradeDate"))), to="research", sym=c["symbol"], kind="info")
        if len(new) and not fresh:
            self.floor.say("analyst", "%d new House filing%s read, no stock trades in them." % (len(new), "" if len(new) == 1 else "s"),
                           to="research", kind="info")

    def congress_bonus(self, sym, side):
        cut = (self.today - dt.timedelta(days=45)).isoformat()
        want = "Purchase" if side == "long" else "Sale"
        members = {c.get("member") for c in self.s.get("congress", {}).values()
                   if c.get("symbol") == sym and c.get("type") == want and (c.get("tradeDate") or "") >= cut}
        return min(3, len(members))

    # ---------------------------------------------------------- price alerts
    def check_alerts(self):
        hits = []
        for aid, a in self.s["alerts"].items():
            if a.get("status") != "waiting":
                continue
            if a.get("expiresOn") and a["expiresOn"] < self.today.isoformat():
                a["status"] = "expired"
                continue
            ysym_ = crypto_yahoo(a["symbol"]) if a.get("kind") == "crypto" else a["symbol"]
            px, _ = self.quote(ysym_)
            if px and ((a["direction"] == "above" and px >= a["level"]) or (a["direction"] == "below" and px <= a["level"])):
                a["status"], a["hitOn"] = "hit", self.today.isoformat()
                hits.append((a["symbol"], a.get("side", "long")))
                self.events.append("Alert hit: %s %s %s" % (a["symbol"], a["direction"], a["level"]))
                self.floor.say("research", "%s just went %s %s. Bringing it back to the desk." % (a["symbol"], a["direction"], self.px(a["level"])),
                               to="pm", sym=a["symbol"], kind="alert")
        return hits

    def set_alerts(self, cands):
        waiting = [a for a in self.s["alerts"].values() if a.get("status") == "waiting"]
        added = 0
        for c in cands:
            if added >= RULES["alerts_per_run"] or len(waiting) + added >= RULES["alert_max"]:
                break
            sym, dv, side = c["symbol"], c["dv"], c["side"]
            if c["kind"] in ("future",) or c["score"] < 60 or c.get("setup"):
                continue
            if any(p["underlying"] == sym for p in self.s["positions"].values()) or any(a["symbol"] == sym for a in waiting):
                continue
            if side == "long":
                if dv["price"] < dv["high20"]:
                    level, direction, reason = dv["high20"], "above", "a breakout would start above here"
                elif dv["sma20"] and dv["price"] > dv["sma20"] * 1.01:
                    level, direction, reason = dv["sma20"] * 1.01, "below", "a pullback entry would start here"
                else:
                    continue
            else:
                if dv["price"] > dv["low20"]:
                    level, direction, reason = dv["low20"], "below", "a breakdown (short) would start below here"
                elif dv["sma20"] and dv["price"] < dv["sma20"] * 0.99:
                    level, direction, reason = dv["sma20"] * 0.99, "above", "a rally to short would start here"
                else:
                    continue
            aid = "%s-%s-%s" % (sym, side, self.today.strftime("%Y%m%d"))
            self.s["alerts"][aid] = {"symbol": sym, "kind": c["kind"], "side": side, "level": round(level, 4),
                                     "direction": direction, "currency": "USD", "reason": reason,
                                     "createdOn": self.today.isoformat(),
                                     "expiresOn": add_trading_days(self.today, RULES["alert_days"]).isoformat(),
                                     "status": "waiting", "hitOn": ""}
            added += 1
            self.floor.say("research", "Watching %s: %s, at %s." % (sym, reason, self.px(level)), to="pm", sym=sym, kind="info")
        cut = (self.today - dt.timedelta(days=10)).isoformat()
        for aid in [k for k, a in self.s["alerts"].items() if a.get("status") != "waiting"
                    and (a.get("hitOn") or a.get("expiresOn") or "") < cut]:
            del self.s["alerts"][aid]

    # ---------------------------------------------------------- earnings: dates, results and the company's own report
    def earnings(self, symbols):
        if not self.market_day or not symbols:
            return
        f = self.f
        if f.get("earningsCheckedOn") != self.today.isoformat():
            days = [prev_trading_day(prev_trading_day(self.today)), prev_trading_day(self.today), self.today]
            d = self.today
            for _ in range(3):
                d = next_trading_day(d)
                days.append(d)
            cal, ok = {}, 0
            for day in days:
                rows = nasdaq_earnings(day)
                if rows is None:
                    continue
                ok += 1
                for sym, r in rows.items():
                    cal.setdefault(sym, []).append([day.isoformat(), r["timing"], r.get("eps"), r.get("epsForecast")])
            self.earn_known = ok >= 4
            f["earningsCheckedOn"], f["earningsKnown"] = self.today.isoformat(), self.earn_known
            focus = self.f.get("focus") or {}
            watch = set(focus.get("long", [])) | set(focus.get("short", [])) | set(focus.get("active", [])) | \
                set(self.cfg.get("stocks", [])) | {p["underlying"] for p in self.s["positions"].values()}
            self.s["earnCal"] = {s: v for s, v in cal.items() if s in watch}
            if not self.earn_known:
                log("Earnings calendar unavailable: stocks will not be held overnight today")
        cal = self.s.get("earnCal") or {}
        for sym in symbols:
            for day, timing, eps, est in cal.get(sym, []):
                e = self.s["earnings"].get(sym, {})
                if day >= self.today.isoformat():
                    if e.get("reportDate") != day or e.get("status") != "upcoming":
                        e = {"symbol": sym, "reportDate": day, "timing": timing, "status": "upcoming", "verdict": "",
                             "note": "reports %s" % timing.replace("-", " ") if timing != "unknown" else "report date from Nasdaq",
                             "updatedOn": self.today.isoformat()}
                elif e.get("reportDate") != day or e.get("status") != "reported":
                    e = self.read_earnings(sym, day, timing)
                self.s["earnings"][sym] = e
        old = (self.today - dt.timedelta(days=10)).isoformat()
        held = {p["underlying"] for p in self.s["positions"].values()}
        for sym in list(self.s["earnings"]):
            if (self.s["earnings"][sym].get("reportDate") or "") < old and sym not in held:
                del self.s["earnings"][sym]

    def read_earnings(self, sym, day, timing):
        """Judge a fresh report from its numbers, its tone and its outlook."""
        sur = nasdaq_surprise(sym) or {}
        fund = self.s.get("fundamentals", {}).get(sym) or {}
        pts, parts = 0, []
        if sur.get("eps") is not None and sur.get("consensus") is not None:
            sp = sur.get("surprisePct")
            if sp is None and sur["consensus"]:
                sp = (sur["eps"] / sur["consensus"] - 1) * 100
            sp = sp or 0
            pts += 1 if sp >= 2 else -1 if sp <= -2 else 0
            parts.append("profit per share %.2f vs %.2f expected (%+.1f%%)" % (sur["eps"], sur["consensus"], sp))
        g = fund.get("revGrowthPct")
        if g is not None:
            pts += 1 if g >= 5 else -1 if g < 0 else 0
            parts.append("sales %+.0f%% on a year ago" % g)
        rel, src = None, "headlines"
        if self.sec_contact:
            try:
                rel = sec_earnings_release(sym, (dt.date.fromisoformat(day) - dt.timedelta(days=2)).isoformat(), self.sec_contact)
            except (ConnectionError, ValueError, KeyError) as e:
                log("SEC report for %s unavailable: %s" % (sym, str(e)[:80]))
        if rel:
            rd = read_release(rel["text"])
            src = "the company's report"
            tone, guide = rd["tone"], rd["guidance"]
        else:
            n = self.news.get(sym) or {}
            tone = ((n.get("pos", 0) - n.get("neg", 0)) / max(1, n.get("pos", 0) + n.get("neg", 0))) if n else 0.0
            guide = ""
        pts += 1 if tone >= 0.2 else -1 if tone <= -0.2 else 0
        parts.append("tone %s (from %s)" % ("positive" if tone >= 0.2 else "negative" if tone <= -0.2 else "neutral", src))
        if guide:
            pts += 1 if guide == "raised" else -2
            parts.append("outlook %s" % guide)
        verdict = "beat" if pts >= 2 else "miss" if pts <= -1 else "mixed"
        until = add_trading_days(dt.date.fromisoformat(day), 5).isoformat()
        e = {"symbol": sym, "reportDate": day, "timing": timing, "status": "reported", "verdict": verdict,
             "note": "; ".join(parts)[:200], "updatedOn": self.today.isoformat(),
             "blockLongUntil": until if verdict == "miss" else "",
             "blockShortUntil": until if verdict == "beat" else "",
             "source": rel["url"] if rel else ""}
        if sym in {p["underlying"] for p in self.s["positions"].values()}:
            self.events.append("Earnings read: %s %s (%s)" % (sym, verdict, e["note"][:90]))
        log("Earnings %s: %s - %s" % (sym, verdict, e["note"]))
        self.floor.say("analyst", "%s reported: %s. %s." % (sym, {"beat": "a beat", "miss": "a miss"}.get(verdict, "mixed"), e["note"][:90]),
                       to="pm", sym=sym, kind="info")
        return e

    def earnings_block(self, sym, side):
        e = self.s["earnings"].get(sym)
        if not e:
            return False
        t = self.today.isoformat()
        if e.get("status") == "upcoming":
            return trading_days_between(self.today, dt.date.fromisoformat(e["reportDate"])) <= 2
        key = "blockLongUntil" if side == "long" else "blockShortUntil"
        return bool(e.get(key) and e[key] >= t)

    # ---------------------------------------------------------- scoring
    def fundamental(self, sym, price, facts=None):
        cache = self.s.setdefault("fundamentals", {})
        f = cache.get(sym)
        fresh = f and f.get("fetchedOn") and (utcnow().date() - dt.date.fromisoformat(f["fetchedOn"])).days < 7
        if not fresh:
            got = nasdaq_fundamentals(sym)
            if got:
                cache[sym] = f = got
        if facts and not facts.get("missing"):
            # Filed numbers first: the SEC figures replace the website's growth and profit numbers
            f = dict(f or {})
            if facts.get("revGrowth") is not None:
                f["revGrowthPct"] = facts["revGrowth"]
            if facts.get("epsGrowth") is not None:
                f["epsGrowthPct"] = facts["epsGrowth"]
            if facts.get("epsTtm") is not None:
                f["lastEps"] = facts["epsTtm"]
                if not f.get("ttmEps"):
                    f["ttmEps"] = facts["epsTtm"]
            sc, why = fundamental_score(f, price)
            if sc is not None and facts.get("opMargin") is not None:
                why = "filed: operating margin %.0f%%, " % facts["opMargin"] + why
            return sc, why
        if not f:
            return None, "not available"
        return fundamental_score(f, price)

    def regime_for(self, kind, sym):
        if kind == "crypto" or (kind == "future" and sym in CRYPTO_FUTURES):
            return self.crypto_regime
        if kind in ("stock", "etf", "option") or (kind == "future" and sym in INDEX_FUTURES):
            return self.stock_regime
        return "neutral"

    def score(self, c):
        sym, kind, side, dv, news = c["symbol"], c["kind"], c["side"], c["dv"], c["news"]
        tech = tech_score(dv, side)
        news_pts = news["score"] if side == "long" else 25 - news["score"]
        fund, fwhy = 9, ""
        regime = self.regime_for(kind, sym)
        favour = {"long": "risk-on", "short": "risk-off"}[side]
        macro = 15 if regime == favour else 8 if regime in ("cautious", "neutral") else 0
        micro = 5 + (5 if dv["volPace"] >= 1.5 else 0)
        if kind == "stock":
            if self.earn_known:
                e = self.s["earnings"].get(sym)
                soon = e and e.get("status") == "upcoming" and \
                    trading_days_between(self.today, dt.date.fromisoformat(e["reportDate"])) <= 5
                micro += 0 if soon else 5
            else:
                micro += 2
            micro += self.congress_bonus(sym, side)
        else:
            micro += 5
        if kind == "stock":
            need = RULES["score_min"] if regime == favour else 75 if regime == "neutral" else RULES["score_min_against"]
            close_call = tech + news_pts + macro + min(15, micro) + 15 >= need
            fs, fwhy = self.fundamental(sym, dv["price"], self.sec_facts(sym) if close_call else None)
            if fs is not None:
                fund = fs if side == "long" else 15 - fs
        total = tech + news_pts + fund + macro + min(15, micro)
        c.update({"score": total, "regime": regime, "fundWhy": fwhy,
                  "parts": {"technical": tech, "news": news_pts, "fundamental": fund, "macro": macro, "micro": min(15, micro)}})
        return total

    def need(self, c):
        favour = {"long": "risk-on", "short": "risk-off"}[c["side"]]
        if c["regime"] == favour:
            return RULES["score_min"]
        return 75 if c["regime"] == "neutral" else RULES["score_min_against"]

    def setup_for(self, c):
        dv, news, side, kind = c["dv"], c["news"], c["side"], c["kind"]
        mv = dv["movePct"]
        e = self.s["earnings"].get(c["symbol"]) if kind == "stock" else None
        fresh_e = e and e.get("status") == "reported" and \
            trading_days_between(dt.date.fromisoformat(e["reportDate"]), self.today) <= 3
        if side == "long":
            if mv > RULES["max_move_today_pct"]:
                return None, "up more than 10% today"
            if c.get("orb"):
                return "breakout", "opening-range breakout"
            if dv["newHigh20"] and dv["volPace"] >= 1.5 and dv["price"] <= dv["high20"] * 1.03:
                return "breakout", "new 20-day high on heavy volume"
            if fresh_e and e["verdict"] == "beat" and 2 <= mv <= 12 and dv["upperHalf"]:
                return "earnings", "strong earnings and holding the gain"
            if news.get("catalyst") and 2 <= mv <= 10 and dv["upperHalf"]:
                return "news", "real good news and holding the gain"
            if (dv["sma50"] and dv["sma50_5ago"] and dv["price"] > dv["sma50"] and dv["sma50"] > dv["sma50_5ago"]
                    and dv["sma20"] and abs(dv["price"] / dv["sma20"] - 1) <= 0.02 and dv["rsi"] and 40 <= dv["rsi"] <= 60
                    and mv > 0):
                return "pullback", "pullback in an uptrend"
        else:
            if mv < -RULES["max_move_today_pct"]:
                return None, "down more than 10% today"
            if c.get("orb"):
                return "breakdown", "fell below its opening range"
            if dv["newLow20"] and dv["volPace"] >= 1.5 and dv["price"] >= dv["low20"] * 0.97:
                return "breakdown", "new 20-day low on heavy volume"
            if fresh_e and e["verdict"] == "miss" and -12 <= mv <= -2 and not dv["upperHalf"]:
                return "earnings_short", "weak earnings and still falling"
            if news.get("badCatalyst") and -10 <= mv <= -2 and not dv["upperHalf"]:
                return "news_short", "real bad news and still weak"
            if (dv["sma50"] and dv["sma50_5ago"] and dv["price"] < dv["sma50"] and dv["sma50"] < dv["sma50_5ago"]
                    and dv["sma20"] and abs(dv["price"] / dv["sma20"] - 1) <= 0.02 and dv["rsi"] and 40 <= dv["rsi"] <= 60
                    and mv < 0):
                return "rally_short", "a rally in a downtrend fading"
        return None, "no clean setup yet"

    # ---------------------------------------------------------- news
    def fetch_news(self, feeds):
        """feeds: [(symbol, yahoo feed ticker)]"""
        feeds = [(s, t) for s, t in feeds if s not in self.news]
        if not feeds:
            return
        argv = ["--tickers"] + [t for _, t in feeds] + ["--hours", "12", "--no-sec", "--no-moves", "--all", "--dry-run", "--json"]
        if self.replay_now:
            argv += ["--now", iso(self.now)]
        code, data, tail = pack("news.py", argv, self.work)
        if data is None:
            log("News check unavailable: %s" % tail[-120:].strip())
            return
        items = data.get("items", [])
        aliases = self.cfg.get("names", {})
        names = (self.f.get("focus") or {}).get("names", {})
        texts = []
        for s, t in feeds:
            for it in items:
                if t in (it.get("touches") or [it.get("ticker")]) and about(str(it.get("title", "")), s, aliases, names.get(s)):
                    texts.append(news_text(it))
        scores = classify_texts(sorted(set(texts)))
        for s, t in feeds:
            self.news[s] = read_news(items, s, aliases, feed=t, scores=scores, name=names.get(s))

    # ---------------------------------------------------------- finding ideas
    def orb_short(self, sym):
        """The first completed 5-minute bar to close below the opening range (the mirror of the pack's breakout)."""
        ch = yahoo_chart(sym, "1d", "5m", self.replay_now)
        if not ch:
            return None
        bars = [b for b in ch["bars"] if bar_day(b["t"]) == self.today]
        if len(bars) < 4:
            return None
        rng = [b for b in bars if to_ny(epoch_to_utc(b["t"])).time() < dt.time(9, 45)]
        if len(rng) < 3:
            return None
        lo, hi = min(b["l"] for b in rng), max(b["h"] for b in rng)
        now_e = utc_to_epoch(self.now)
        for b in bars[len(rng):]:
            if b["t"] + 300 > now_e:
                break
            if b["c"] < lo:
                return {"price": b["c"], "or_low": lo, "or_high": hi, "time": to_ny(epoch_to_utc(b["t"] + 300)).strftime("%H:%M"),
                        "entry_time": to_ny(epoch_to_utc(b["t"] + 300)).strftime("%I:%M %p").lstrip("0")}
        return None

    def stock_candidates(self, alert_hits):
        focus = self.f.get("focus") or {}
        if focus.get("date") != self.today.isoformat():
            focus = {"long": list(self.cfg["stocks"]) + list(self.cfg["etfs"]), "short": list(self.cfg["stocks"]) + list(self.cfg["etfs"]),
                     "active": [], "names": {}}
        names = focus.get("names", {})
        etfset = set(self.s.get("etfUniverse") or []) | set(self.cfg.get("etfs", []))
        hit_long = {s for s, sd in alert_hits if sd == "long"}
        hit_short = {s for s, sd in alert_hits if sd == "short"}
        long_pool = list(dict.fromkeys(list(hit_long) + focus.get("long", []) + focus.get("active", [])))
        short_pool = list(dict.fromkeys(list(hit_short) + focus.get("short", []) + focus.get("active", [])))
        spark = yahoo_spark_daily(list(dict.fromkeys(long_pool + short_pool)), self.replay_now)
        pre_l, pre_s = [], []
        for sym in long_pool:
            dv = daily_view(spark.get(sym), self.today, False, 1.0)
            if dv and dv["price"] >= 5:
                pre_l.append((tech_score(dv, "long") + (5 if sym in hit_long else 0), sym))
        for sym in short_pool:
            dv = daily_view(spark.get(sym), self.today, False, 1.0)
            if dv and dv["price"] >= 5:
                pre_s.append((tech_score(dv, "short") + (5 if sym in hit_short else 0), sym))
        def quota(pre, n_stock, n_etf):
            """Individual stocks get their own places, so smooth fund charts can't crowd them out."""
            ranked = [s for _, s in sorted(pre, reverse=True)]
            st = [s for s in ranked if s not in etfset][:n_stock]
            et = [s for s in ranked if s in etfset][:n_etf]
            return [s for s in ranked if s in st or s in et]
        top_l = quota(pre_l, 14, 6)
        top_s = quota(pre_s, 5, 3)
        if self.packmode and top_l and self.hm >= 9 * 60 + 50:
            argv = ["orb", "--symbols", ",".join(top_l), "--json", "--no-write", "--max-confirm", "20", "--quiet"]
            if self.ny_arg():
                argv += ["--now", self.ny_arg()]
            code, data, tail = pack("scan.py", argv, self.work)
            if data:
                self.orb = {h["symbol"]: h for h in data.get("hits", [])}
        orb_s = {}
        if self.hm >= 9 * 60 + 50:
            for sym in top_s:
                hit = self.orb_short(sym)
                if hit:
                    orb_s[sym] = hit
        log("Stock scan: %d long and %d short ideas pre-screened; %d breakouts, %d breakdowns" % (
            len(pre_l), len(pre_s), len([s for s in top_l if s in self.orb]), len(orb_s)))
        nb = len([s for s in top_l if s in self.orb])
        self.floor.say("research", "Stock scan: %d long and %d short charts checked, %d breakout%s, %d breakdown%s. Strongest: %s." % (
            len(pre_l), len(pre_s), nb, "" if nb == 1 else "s", len(orb_s), "" if len(orb_s) == 1 else "s",
            ", ".join(top_l[:3] + top_s[:1]) or "none"), to="pm", kind="info",
            key="scan:stocks:%s:%d:%d" % (",".join(top_l[:3] + top_s[:1]), nb, len(orb_s)), hours=1)
        def pick(top, hits, n_stock, n_etf):
            order = [s for s in top if s in hits] + [s for s in top if s not in hits]
            st = [s for s in order if s not in etfset][:n_stock]
            et = [s for s in order if s in etfset][:n_etf]
            return [s for s in order if s in st or s in et]
        short_l = pick(top_l, self.orb, 3, 2)
        short_s = pick(top_s, orb_s, 2, 1)
        for s in short_l + short_s:
            self.names.setdefault(s, names.get(s, s))
        self.fetch_news([(s, s) for s in dict.fromkeys(short_l + short_s)])
        self.earnings([s for s in dict.fromkeys(short_l + short_s) if s not in etfset])
        cands = []
        for side, syms in (("long", short_l), ("short", short_s)):
            for sym in syms:
                dv = self.dview(sym)
                if not dv:
                    continue
                c = {"symbol": sym, "kind": "etf" if sym in etfset else "stock", "side": side, "dv": dv,
                     "news": self.news.get(sym, {"score": 12}), "yahoo": sym,
                     "orb": self.orb.get(sym) if side == "long" else orb_s.get(sym)}
                self.score(c)
                cands.append(c)
        return cands

    @addon(list)
    def tsx_candidates(self):
        """Canadian stocks and ETFs on the TSX: bought in Canadian dollars, so no currency conversion. Buy-only for now."""
        if self.today.isoformat() in TSX_HOLIDAYS:
            self.floor.say("strategy", "The TSX is closed today for a Canadian holiday.", to="pm", kind="mood",
                           key="tsx-closed:%s" % self.today, hours=20)
            return []
        etfs = {s + ".TO" for s in self.cfg.get("tsx_etfs", [])}
        syms = [s + ".TO" for s in self.cfg.get("tsx", []) + self.cfg.get("tsx_etfs", [])]
        held = {p["underlying"] for p in self.s["positions"].values()}
        spark = yahoo_spark_daily([s for s in syms if s not in held], self.replay_now)
        pre = []
        for sym, ch in spark.items():
            dv = daily_view(ch, self.today, False, 1.0)
            if dv and dv["price"] >= 5 and dv["dollarVol"] >= 5e6:
                pre.append((tech_score(dv, "long"), sym))
        top = [s for _, s in sorted(pre, reverse=True)[:3]]
        self.floor.say("research", "TSX scan: %d Canadian charts checked. Strongest: %s." % (len(pre), ", ".join(top) or "none"),
                       to="pm", kind="info", key="scan:tsx:%s" % ",".join(top), hours=2)
        self.fetch_news([(s, s) for s in top])
        cands = []
        for sym in top:
            dv = self.dview(sym)
            if not dv:
                continue
            c = {"symbol": sym, "kind": "etf" if sym in etfs else "stock", "market": "TSX", "side": "long", "dv": dv,
                 "news": self.news.get(sym, {"score": 12}), "yahoo": sym}
            self.names.setdefault(sym, sym)
            self.score(c)
            cands.append(c)
        return cands

    def crypto_candidates(self):
        syms, src = crypto_universe()
        held = {p["underlying"] for p in self.s["positions"].values() if p["kind"] == "crypto"}
        ymap = {crypto_yahoo(s): s for s in syms if s not in held}
        spark = yahoo_spark_daily(list(ymap), self.replay_now)
        pre = []
        for y, s in ymap.items():
            dv = daily_view(spark.get(y), self.now.date(), True, 1.0)
            if dv and dv["dollarVol"] >= 5e6:
                pre.append((tech_score(dv, "long"), s))
        top = [s for _, s in sorted(pre, reverse=True)[:4]]
        log("Crypto scan: %d coins from %s, %d with data" % (len(syms), src, len(pre)))
        self.floor.say("research", "Crypto scan: %d coins checked. Strongest charts: %s." % (len(pre), ", ".join(top[:3]) or "none"),
                       to="pm", kind="info", key="scan:crypto:%s" % ",".join(top[:3]), hours=3)
        self.fetch_news([(s, crypto_yahoo(s)) for s in top[:2]])
        cands = []
        for s in top:
            dv = self.dview(crypto_yahoo(s), True)
            if not dv:
                continue
            c = {"symbol": s, "kind": "crypto", "side": "long", "dv": dv, "news": self.news.get(s, {"score": 12}),
                 "yahoo": crypto_yahoo(s)}
            self.score(c)
            cands.append(c)
        return cands

    def futures_candidates(self):
        _, es = self.quote("ES=F")
        if not es or not es["bars"] or utc_to_epoch(self.now) - es["bars"][-1]["t"] > 40 * 60:
            log("Futures market closed now: no futures trades")
            self.floor.say("strategy", "Futures market is closed right now. No futures trades.", to="pm", kind="mood",
                           key="futures-closed:%s" % self.today, hours=12)
            return []
        spark = yahoo_spark_daily([v[0] for v in FUTURES.values()], self.replay_now)
        pre, fits, too_big = [], [], []
        risk_usd = RULES["risk_pct"] * self.f["equityCad"] / self.fx
        margin_cap = RULES["future_margin_cap_pct"] * self.sleeve_value("usd") / self.fx
        for code, (y, mult, mfrac, name) in FUTURES.items():
            dv = daily_view(spark.get(y), self.today, False, 1.0)
            if not dv:
                continue
            one = stop_distance("future", dv["atrPct"]) * dv["price"] * mult
            if one > risk_usd or dv["price"] * mult * mfrac > margin_cap:
                too_big.append(code)
                continue
            fits.append(code)
            tl, ts = tech_score(dv, "long"), tech_score(dv, "short")
            pre.append((max(tl, ts), code, "long" if tl >= ts else "short"))
        self.floor.say("risk", "Futures that fit the account today: %s. Too big for one contract: %s." % (
            ", ".join(fits) or "none", ", ".join(too_big) or "none"), to="research", kind="check",
            key="futures-fit:%s:%s" % (self.today, ",".join(fits)), hours=20)
        pre.sort(reverse=True)
        cands = []
        for _, code, side in pre[:2]:
            dv = self.dview(FUTURES[code][0])
            if not dv:
                continue
            c = {"symbol": code, "kind": "future", "side": side, "dv": dv, "news": {"score": 12}, "yahoo": FUTURES[code][0]}
            self.score(c)
            cands.append(c)
        return cands

    def hunt(self, alert_hits):
        cands = []
        if self.stocks_ok:
            self.f["lastStockScan"] = iso(self.now)
            self.scanned = True
            cands += self.stock_candidates(alert_hits)
            cands += self.tsx_candidates()
        if self.crypto_ok:
            self.f["lastCryptoScan"] = iso(self.now)
            self.scanned = True
            cands += self.crypto_candidates()
        if self.futures_ok:
            self.f["lastFuturesScan"] = iso(self.now)
            self.scanned = True
            cands += self.futures_candidates()
        cands.sort(key=lambda c: -c["score"])
        before = self.f["entriesToday"]
        for c in cands:
            self.try_enter(c)
        if cands and self.f["entriesToday"] == before:
            b = cands[0]
            if b["score"] >= self.need(b) and not b.get("setup"):
                line = "Nothing to trade this scan. %s %s scores enough but has no clean setup yet." % (
                    "Long" if b["side"] == "long" else "Short", b["symbol"])
            else:
                line = "Nothing good enough this scan. Closest: %s %s at %d, needs %d." % (
                    "long" if b["side"] == "long" else "short", b["symbol"], b["score"], self.need(b))
            self.floor.say("pm", line, kind="decide", key="nothing:%s:%d" % (b["symbol"], b["score"] // 4), hours=2)
        self.set_alerts(cands)

    # ---------------------------------------------------------- deciding and sizing one trade
    def plan(self, c):
        """Entry, stop, target and the time machine's verdict."""
        sym, kind, side, dv = c["symbol"], c["kind"], c["side"], c["dv"]
        price = dv["price"]
        stop = target = rep = None
        nowarg = ["--now", self.ny_arg()] if self.ny_arg() else []
        bar = self.ny - dt.timedelta(minutes=1)
        bar = bar.replace(minute=bar.minute - bar.minute % 5, second=0, microsecond=0)
        etime = bar.strftime("%I:%M %p").lstrip("0")
        if kind in ("stock", "etf") and c.get("orb") and self.packmode:
            if side == "long":
                code, data, tail = pack("entry.py", ["check", sym, "--write", "--json"] + nowarg, self.work)
                if data and data.get("status") == "triggered" and price <= data["trigger"]["price"] * 1.01:
                    code, pl, tail = pack("exit.py", ["plan", "--target", "2R", "--entry", "%.2f" % price, "--write", "--json"] + nowarg, self.work)
                    if pl and pl.get("stop") and pl.get("target"):
                        stop, target = pl["stop"], pl["target"]
            else:
                o = c["orb"]
                if price >= o["price"] * 0.99:
                    stop = max((o["or_low"] + o["or_high"]) / 2, price * 1.005)
                    target = price - 2 * (stop - price)
            if stop is not None:
                argv = ["run", "--symbol", sym, "--side", "buy" if side == "long" else "sell", "--date", self.today.isoformat(),
                        "--entry", "%.2f" % price, "--stop", "%.2f" % stop, "--target", "%.2f" % target, "--entry-time", etime,
                        "--setup", "orb", "--until", "15:55", "--json"] + nowarg
                code, tm, tail = pack("timemachine.py", argv, self.work)
                if tm and tm.get("mc"):
                    pt, ps = tm["mc"]["p_target"], tm["mc"]["p_stop"]
                    v = str(tm.get("verdict", ""))
                    rep = {"pTarget": pt, "pStop": ps, "meanR": tm["mc"].get("avg_R"), "source": "pack-5m",
                           "favourable": (pt / (pt + ps) >= 0.40 if pt + ps > 0 else False) and "toward the stop" not in v}
        if stop is None:
            d = stop_distance(kind, dv["atrPct"])   # 2 average daily ranges (backtest, Nov 18 2026)
            if side == "long":
                stop, target = price * (1 - d), price * (1 + 2 * d)
            else:
                stop, target = price * (1 + d), price * (1 - 2 * d)
        if rep is None:
            res = replay({"bars": dv["ohlc"], "entry": price, "stop": stop, "target": target, "side": side,
                          "maxDays": {"crypto": 10, "future": 3}.get(kind, 2),
                          "costPct": {"crypto": 0.016, "future": 0.001}.get(kind, 0.002)})
            rep = {"pTarget": res.get("pTarget"), "pStop": res.get("pStop"), "meanR": res.get("meanR"),
                   "favourable": bool(res.get("favourable")), "source": "daily"}
        return price, stop, target, rep

    def pick_option(self, sym, side, price, risk_usd, cap_usd):
        d0 = self.today + dt.timedelta(days=RULES["option_min_dte"])
        d1 = self.today + dt.timedelta(days=RULES["option_max_dte"])
        chain = [r for r in nasdaq_option_chain(sym, d0, d1, "at") if d0 <= r["expiry"] <= d1 and r["strike"]]
        if not chain:
            chain = [r for r in cboe_option_chain(sym, d0, d1) if r["strike"] and abs(r["strike"] / price - 1) <= 0.2]
            if chain:
                log("Options for %s from Cboe (Nasdaq had none)" % sym)
        if not chain:
            why = OPTION_ERRORS.get(sym)
            return None, "no options data from Nasdaq or Cboe (%s)" % why if why else "no options expiring in 2-6 weeks"
        exp = min({r["expiry"] for r in chain}, key=lambda e: abs((e - self.today).days - 30))
        rows = sorted([r for r in chain if r["expiry"] == exp], key=lambda r: r["strike"])
        right = "call" if side == "long" else "put"
        if right == "call":
            itm = [r for r in rows if r["strike"] <= price]
            row = itm[-1] if itm else rows[0]
        else:
            itm = [r for r in rows if r["strike"] >= price]
            row = itm[0] if itm else rows[-1]
        leg = row[right]
        bid, ask, oi = leg["bid"], leg["ask"], leg["oi"]
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        if not mid or oi < 100 or ask < 0.30 or (ask - bid) / mid > 0.15:
            return None, "option not liquid enough (open interest %d, spread %s)" % (oi, "%.0f%%" % ((ask - bid) / mid * 100) if mid else "n/a")
        per = ask * 100 * RULES["option_stop"] + 2 * RULES["option_fee"]
        n = int(risk_usd // per)
        while n > 0 and n * (ask * 100 + RULES["option_fee"]) > cap_usd:
            n -= 1
        if n < 1:
            return None, "one contract costs too much for the risk budget (%s)" % money(ask * 100)
        return {"expiry": exp.isoformat(), "strike": row["strike"], "right": right, "ask": ask, "bid": bid,
                "contracts": n, "costUsd": n * (ask * 100 + RULES["option_fee"]), "dte": (exp - self.today).days}, ""

    def try_enter(self, c):
        f, sym, kind, side, dv = self.f, c["symbol"], c["kind"], c["side"], c["dv"]
        need = self.need(c)
        setup, why = self.setup_for(c)
        c["setup"] = setup
        label = "%s %s" % ("LONG" if side == "long" else "SHORT", sym)
        verdict = "%d/100, %s" % (c["score"], why) if c["score"] >= need else "%d/100, below the %d needed" % (c["score"], need)
        self.cands.append({"symbol": label, "score": c["score"], "verdict": verdict[:90]})
        self.floor_idea(c, need, setup, why)
        if c["score"] < need or not setup:
            return
        if setup in RULES["setups_off"]:
            self.blocked.append({"symbol": label, "failed": ["Setup switched off by the backtest"]})
            if c.get("talk"):
                self.floor.say("pm", "Pass on %s: the %s setup is switched off. It lost money in both years of the backtest." % (
                    sym, setup.replace("_", " ")), sym=sym, kind="block")
            return
        if f["stats"]["bySetup"].setdefault(setup, {"n": 0, "wins": 0, "r": 0.0, "disabled": False})["disabled"]:
            self.blocked.append({"symbol": label, "failed": ["Setup switched off"]})
            if c.get("talk"):
                self.floor.say("pm", "Pass on %s: %s is switched off after losses." % (sym, SETUP_NAMES.get(setup, setup)), sym=sym, kind="block")
            return
        allowed = {"stock": self.stocks_ok, "etf": self.stocks_ok, "crypto": self.crypto_ok, "future": self.futures_ok}[kind]
        price, stop, target, rep = self.plan(c)
        risk_cad = RULES["risk_pct"] * f["equityCad"]
        fx = self.fx
        inst, opt = kind, None
        if kind == "stock" and not c.get("market") and c["score"] >= RULES["option_score"] and self.count("option") < RULES["max_pos"]["option"]:
            cap = RULES["option_cap_pct"] * f["equityCad"] / fx
            opt, owhy = self.pick_option(sym, side, price, risk_cad / fx, min(cap, max(0.0, self.free_usd())))
            if opt:
                inst = "option"
            else:
                log("%s: options not used (%s); shares instead" % (sym, owhy))
        self.floor_plan(c, price, stop, target, rep, opt)
        group = kind_group(inst)
        held = any(p["underlying"] == sym and (p["side"] == side or p["kind"] == "option") for p in self.s["positions"].values())
        o = {"symbol": label, "allowed": allowed, "blockReason": self.block_reason, "alreadyHeld": held,
             "score": c["score"], "scoreMin": need, "setup": setup,
             "earningsBlocked": kind == "stock" and self.earnings_block(sym, side),
             "moveWithTradePct": dv["movePct"] if side == "long" else -dv["movePct"],
             "stopOk": (stop < price < target) if side == "long" else (target < price < stop),
             "stop": round(stop, 4), "entry": round(price, 4), "riskCad": risk_cad, "replay": rep,
             "equityCad": f["equityCad"], "dayStartEquityCad": f["dayStartEquityCad"], "openRiskCad": self.open_risk(),
             "positionsOfKind": self.count(group), "maxPositionsOfKind": RULES["max_pos"][group],
             "entriesToday": f["entriesToday"], "entriesThisWeek": f["entriesThisWeek"],
             "rewardRisk": abs(target - price) / abs(price - stop) if price != stop else 0}
        d = abs(price - stop) / price
        if inst == "option":
            loss = (opt["contracts"] * opt["ask"] * 100 * RULES["option_stop"] + 2 * RULES["option_fee"] * opt["contracts"]) * fx
            o.update({"sizeCad": opt["costUsd"] * fx, "sizeCapCad": RULES["option_cap_pct"] * f["equityCad"],
                      "cashNeededCad": opt["costUsd"] * fx, "cashFreeCad": self.free_usd() * fx,
                      "lossAtStopCad": loss, "rewardRisk": RULES["option_target"] / RULES["option_stop"]})
        elif kind == "future":
            code = sym
            y, mult, mfrac, name = FUTURES[code]
            n = int((risk_cad / fx) // (abs(price - stop) * mult)) if price != stop else 0
            margin = max(1, n) * price * mult * mfrac
            loss = (max(1, n) * abs(price - stop) * mult + 2 * RULES["future_fee"] * max(1, n)) * fx
            o.update({"sizeCad": margin * fx if n >= 1 else 0, "sizeCapCad": RULES["future_margin_cap_pct"] * self.sleeve_value("usd"),
                      "cashNeededCad": margin * fx, "cashFreeCad": self.free_usd() * fx, "lossAtStopCad": loss})
            c["contracts"], c["margin"] = n, margin
            if n < 1:
                o["sizeCad"] = 0
        elif kind == "crypto":
            sleeve = self.sleeve_value("cad")
            value = min(risk_cad / d, RULES["crypto_cap_pct"] * sleeve, f["cad"] / (1 + RULES["slip"]["crypto"]))
            o.update({"sizeCad": value, "sizeCapCad": RULES["crypto_cap_pct"] * sleeve, "cashNeededCad": value,
                      "cashFreeCad": f["cad"], "lossAtStopCad": value * d + value * 0.016})
            c["value"] = value
        elif c.get("market") == "TSX":
            sleeve = self.sleeve_value("cad")
            cap = RULES["equity_cap_pct"] * sleeve
            slip = RULES["slip"][kind]
            value = min(risk_cad / d, cap, f["cad"] / (1 + slip))
            shares = int(value // (price * (1 + slip))) if price else 0
            c["shares"] = shares
            value = shares * price * (1 + slip)
            o.update({"sizeCad": value, "sizeCapCad": cap, "cashNeededCad": value, "cashFreeCad": f["cad"],
                      "lossAtStopCad": value * d + value * 0.002})
            c["value"] = value
        else:
            sleeve = self.sleeve_value("usd")
            cap = RULES["equity_cap_pct"] * sleeve
            free = self.free_usd() * fx
            need_mult = RULES["short_collateral"] if side == "short" else 1.0
            value = min(risk_cad / d, cap, free / need_mult / (1 + RULES["slip"][kind]))
            if side == "long" and self.packmode:
                # Whole shares, sized by the Day Trader pack's own four limits (trader-budget)
                shares, sizing = self.pack_size(sym, price, stop, target, setup, risk_cad, cap, free / (1 + RULES["slip"][kind]))
                c["sizing"] = sizing
                if shares >= 1:
                    c["shares"] = shares
                    value = shares * price * (1 + RULES["slip"][kind]) * fx
                else:
                    value = 0.0
            o.update({"sizeCad": value, "sizeCapCad": cap, "cashNeededCad": value * need_mult, "cashFreeCad": free,
                      "lossAtStopCad": value * d + value * 0.002})
            c["value"] = value
        chk = order_check(o)
        failed = list(chk["failed"])
        if kind == "future" and c.get("contracts", 0) < 1:
            failed.append("One contract risks more than %d%% of the account" % round(RULES["risk_pct"] * 100))
        if c.get("sizing") is not None and not c.get("shares"):
            failed.append("Sizing: %s" % str(c["sizing"].get("refusal") or "not even one whole share fits")[:120])
        passed = sum(1 for r in chk["rules"] if r["ok"])
        if side == "short" and inst in ("stock", "etf"):
            ok, why_b = self.borrow_check(sym, c["value"] / fx / price if price else 0, c)
            if ok:
                passed += 1
            else:
                failed.append(why_b)
        same = self.same_bet(c, inst)
        if same:
            failed.append("Same bet as %s (moves together %.0f%% of the time)" % same)
        else:
            passed += 1
        if inst in ("stock", "etf") and self.packmode and not failed and not c.get("market"):
            if self.pack_risk(sym, side, c["value"] / fx, price, stop, target, setup, c.get("shares")):
                passed += 1
                if c.get("shares"):
                    c["ticket"] = self.pack_ticket()
            else:
                failed.append("Pack risk check")
        self.floor_check(c, inst, failed, passed)
        if failed:
            self.blocked.append({"symbol": label, "failed": failed})
            log("BLOCK %s: %s" % (label, ", ".join(failed)))
            return
        # Record the real loss if the stop is hit (not the whole budget), so open risk and R are measured honestly
        actual = o.get("lossAtStopCad") or risk_cad
        self.enter(c, inst, setup, price, stop, target, min(actual, risk_cad * 1.5), rep, passed, opt)

    @addon(None)
    def weekly_backtest(self, force=False):
        """The agent's own rules replayed over two years of past prices: every Saturday, and once soon after it's added.
        Runs only while US markets are closed. Takes about ten minutes on GitHub."""
        b = self.s.get("backtest") or {}
        if self.replay_now or self.dry:
            return
        in_hours = self.market_day and 9 * 60 <= self.hm <= 16 * 60 + 30
        weekly = lambda x: not x.get("ranOn") or (self.ny.weekday() == 5 and (self.today - dt.date.fromisoformat(x["ranOn"])).days >= 6)
        if force or (not in_hours and weekly((self.s.get("intraday") or {}).get("backtest") or {})):
            self.intraday_backtest()
        if not force:
            due = weekly(b)
            if in_hours or not due:
                return
        import backtest
        res = backtest.run(self.s, self.cfg, years=2)
        if res.get("error"):
            log("Backtest skipped: %s" % res["error"])
            return
        keep = ("variant", "trades", "winRate", "avgR", "totalR", "returnPct", "maxDrawdownPct", "halted", "firstHalfAvgR",
                "secondHalfAvgR", "byKind", "bySetup", "exits")
        self.s["backtest"] = {"ranOn": self.today.isoformat(), "asOf": res["asOf"], "from": res["from"], "symbols": res["symbols"],
                              "kinds": res["kinds"], "seconds": res["seconds"], "riskPct": RULES["risk_pct"] * 100,
                              "summary": [{k: x.get(k) for k in keep} for x in res["summary"]], "curves": res["curves"]}
        self.scanned = True   # make sure this run is saved
        live = next((x for x in res["summary"] if x["variant"] == "live rules"), None)
        one = next((x for x in res["summary"] if x["variant"] == "live rules at 1% risk"), None)
        if live and one:
            self.floor.say("quant", "Backtest, last 2 years: our rules made %d trades, %+.2fR a trade, %+.1f%% overall, worst drop %.0f%%%s. "
                           "At 1%% risk: %+.2fR, %+.1f%%." % (live["trades"], live["avgR"], live["returnPct"], live["maxDrawdownPct"],
                                                           ", and hit the -15% stop" if live["halted"] else "", one["avgR"], one["returnPct"]),
                           to="pm", kind="meeting")

    @addon(None)
    def intraday_backtest(self):
        """The bots over the last month of 1-minute candles (Yahoo keeps 30 days): every Saturday, and once after they're added."""
        res = intraday.backtest(self.cfg)
        st = self.s.setdefault("intraday", {})
        res["ranOn"] = self.today.isoformat()
        st["backtest"] = res
        self.scanned = True
        v = (res.get("variants") or {}).get("reel rules") or {}
        a = v.get("all") or {}
        if a.get("n"):
            best = max(((k, x.get("pnl", 0)) for k, x in v["bots"].items() if x.get("n")), key=lambda kv: kv[1], default=None)
            self.floor.say("quant", "Bots backtest, last month of 1-minute candles: %d trades, %d%% winners, %s%s, %+.2fR a trade%s." % (
                a["n"], a["win"], "+" if a["pnl"] >= 0 else "-", money(abs(a["pnl"])), a["avgR"],
                (". Best market: %s" % intraday.BOTS[best[0]]["name"]) if best else ""), to="pm", kind="meeting")
        log("Bots backtest: %s trades, %s in %ss" % (a.get("n"), money(a.get("pnl", 0)), res.get("seconds")))

    @addon(None)
    def intraday_bots(self):
        """The reel's 1-minute EMA/VWAP bots, one per market (intraday.py). Paper only, like everything here."""
        if self.replay_now:
            return
        intraday.Desk(self).go()

    @addon(None)
    def econ_refresh(self):
        """Inflation (CPI) and jobs report dates from the BLS schedule, refreshed weekly."""
        e = self.s.setdefault("econ", {})
        if self.replay_now or (e.get("fetched") and (self.today - dt.date.fromisoformat(e["fetched"])).days < 7):
            return
        lo, hi = self.today - dt.timedelta(days=1), self.today + dt.timedelta(days=400)
        cpi = [d.isoformat() for d in bls_release_dates("cpi", self.sec_contact) if lo <= d <= hi]
        jobs = [d.isoformat() for d in bls_release_dates("empsit", self.sec_contact) if lo <= d <= hi]
        fetched = self.today if (cpi or jobs) else self.today - dt.timedelta(days=6)   # no answer: try again tomorrow
        e.update({"fetched": fetched.isoformat(), "cpi": cpi or e.get("cpi") or [], "jobs": jobs or e.get("jobs") or [],
                  "source": "BLS" if cpi or jobs else "rule of thumb (jobs report on the first Friday)"})

    def econ_today(self):
        e = self.s.get("econ") or {}
        t = self.today.isoformat()
        out = []
        if t in (e.get("cpi") or []):
            out.append("inflation (CPI)")
        jobs = e.get("jobs") or []
        if t in jobs or (not jobs and self.today == first_friday(self.today.year, self.today.month)):
            out.append("jobs")
        return out

    @addon(None)
    def source_check(self):
        """Once a day: do the backup and extra data sources answer? Saved, logged and said on the floor."""
        src = self.s.setdefault("sources", {})
        if self.replay_now:
            return
        if src.get("date") == self.today.isoformat():
            if not src.get("secContact") and "@" in self.sec_contact:
                src["secContact"] = True
                log("SEC contact now set: filed numbers and earnings releases are on")
                self.floor.say("analyst", "The SEC contact is set. I'll read companies' own filings and earnings releases from now on.",
                               to="pm", kind="info", key="sec-on", hours=24 * 30)
            return
        d0, d1 = self.today + dt.timedelta(days=14), self.today + dt.timedelta(days=45)
        cb = len(cboe_option_chain("SPY", d0, d1))
        bt = borrow_table()
        one = borrow_one("SPY") if bt is None else None
        e = self.s.get("econ") or {}
        src.update({"date": self.today.isoformat(), "cboeOptions": cb, "borrowNames": len(bt) if bt else 0, "borrowBackup": one is not None,
                    "cpiDates": len(e.get("cpi") or []), "jobsDates": len(e.get("jobs") or []), "secContact": "@" in self.sec_contact})
        log("Data sources: Cboe options %d rows, borrow list %d names, CPI dates %d, jobs dates %d, SEC contact %s" % (
            cb, src["borrowNames"], src["cpiDates"], src["jobsDates"], "set" if src["secContact"] else "missing"))
        ok = lambda v: "ok" if v else "not answering"
        self.floor.say("research", "Data check: options backup %s, borrow list %s, economic calendar %s, SEC filings %s." % (
            ok(cb), ("%s names" % "{:,}".format(src["borrowNames"])) if bt else ("backup answering" if one else "not answering"),
            ok(src["cpiDates"] or src["jobsDates"]), "on" if src["secContact"] else "off (no SEC_CONTACT secret)"),
            to="pm", kind="info", key="sources:%s" % self.today, hours=20)

    def borrow_check(self, sym, shares, c):
        """Shorts need shares to borrow and a fee under 20% a year (Interactive Brokers' public list)."""
        bt = borrow_table()
        if bt is None:
            b = borrow_one(sym)
            if b is None:
                return True, ""      # neither source answered: allowed, with the flat 5% fee
        else:
            b = bt.get(sym.upper())
        if not b or b["available"] <= 0:
            return False, "No shares available to borrow"
        if b["available"] < shares:
            return False, "Only %s shares available to borrow" % "{:,}".format(b["available"])
        if b["feePct"] > 20:
            return False, "Borrowing costs %.0f%% a year" % b["feePct"]
        c["borrowRate"] = max(0.0025, b["feePct"] / 100)
        return True, ""

    def same_bet(self, c, inst):
        """A new trade can't repeat a bet already held: same direction and daily moves 80%+ alike."""
        mine = [r[3] for r in c["dv"]["ohlc"]] + [c["dv"]["price"]]
        long_me = c["side"] == "long"
        crypto_me = c["kind"] == "crypto"
        for p in self.s["positions"].values():
            if (p["kind"] == "crypto") != crypto_me:
                continue
            long_p = (p["right"] == "call") if p["kind"] == "option" else p["side"] == "long"
            if long_p != long_me or p["underlying"] == c["symbol"]:
                continue
            dv = self.dview(p["yahoo"] if p["kind"] in ("crypto", "future") else p["underlying"], p["kind"] == "crypto")
            if not dv:
                continue
            r = returns_corr(mine, [x[3] for x in dv["ohlc"]] + [dv["price"]])
            if r is not None and r >= 0.8:
                return p["underlying"], r * 100
        return None

    def pack_cfg(self, max_loss_usd):
        """The agent's limits written in the Day Trader pack's config format."""
        f = self.f
        sleeve = self.sleeve_value("usd") / self.fx
        return {"budget": round(sleeve, 2), "cash_buffer_pct": 0, "max_position_pct": round(RULES["equity_cap_pct"] * 100, 2),
                "max_order_usd": round(RULES["equity_cap_pct"] * sleeve + 1, 2), "min_order_usd": 5,
                "max_loss_per_trade_usd": round(max_loss_usd, 2),
                "max_daily_loss_usd": round(RULES["day_loss_pct"] * f["dayStartEquityCad"] / self.fx, 2),
                "max_trades_per_day": RULES["entries_per_day"], "max_orders_per_day": 12, "min_reward_risk": 1.9,
                "require_stop": True, "no_entries_before": "09:45", "no_entries_after": "15:00", "flat_by": "15:55",
                "fractional": False, "broker": "alpaca", "paper": True, "blocked": [], "allow_options": False,
                "allow_margin": False, "allow_short": True, "pdt_guard": False, "sec_user_agent": ""}

    def pack_nowarg(self):
        return ["--now", self.ny_arg()] if self.ny_arg() else []

    @addon(lambda: (0, {"refusal": "the sizing tool had an error, so no trade"}))
    def pack_size(self, sym, price, stop, target, setup, risk_cad, cap_cad, cash_cad):
        """Day Trader pack's Budget Manager (trader-budget): whole shares inside four limits, written as one bracket order."""
        fx = self.fx
        cfg = self.pack_cfg(risk_cad / fx)
        cfg["max_order_usd"] = round(cap_cad / fx, 2)
        plan = {"symbol": sym, "side": "buy", "date": self.today.isoformat(), "entry": round(price, 2), "stop": round(stop, 2),
                "target": round(target, 2), "setup": setup, "entry_time": self.ny.strftime("%I:%M %p").lstrip("0"), "flat_by": "15:55"}
        tdir = os.path.join(self.work, "trader")
        for name, doc in (("size-config.json", cfg), ("plan.json", plan), ("size-cash.json", {"cash": round(max(0.0, cash_cad) / fx, 2)})):
            with open(os.path.join(tdir, name), "w") as fh:
                json.dump(doc, fh)
        code, data, tail = pack("size.py", ["--config", "trader/size-config.json", "--portfolio", "trader/size-cash.json", "--json", "--write"]
                                + self.pack_nowarg(), self.work)
        sz = (data or {}).get("sizing") if isinstance(data, dict) else None
        if not sz:
            lines = [x for x in tail.strip().splitlines() if x.strip()]
            return 0, {"refusal": (lines[-1] if lines else "no answer from the sizing tool")[:160]}
        keep = {k: sz.get(k) for k in ("qty", "bound_by", "cost", "max_loss", "risk_per_share", "reward_risk", "refusal")}
        keep["limits"] = [{"name": x.get("name"), "shares": x.get("shares")} for x in sz.get("limits") or []]
        if sz.get("refusal") or not sz.get("qty"):
            return 0, keep
        log("pack sizing %s: %s shares, bound by %s" % (sym, sz["qty"], sz.get("bound_by")))
        return int(sz["qty"]), keep

    @addon(False)
    def pack_risk(self, sym, side, usd, price, stop, target, setup, shares=None):
        cfg = self.pack_cfg(1.5 * RULES["risk_pct"] * self.f["equityCad"] / self.fx)
        with open(os.path.join(self.work, "trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)
        if not shares:
            # Shorts: the sizing tool never sizes them, so the agent writes the order itself
            order = {"symbol": sym, "side": "buy" if side == "long" else "sell", "notional": "%.2f" % usd, "type": "market",
                     "time_in_force": "day", "why": "paper",
                     "plan": {"entry": round(price, 2), "stop": round(stop, 2), "target": round(target, 2), "setup": setup, "flat_by": "15:55"}}
            with open(os.path.join(self.work, "trader", "orders.json"), "w") as fh:
                json.dump({"created_by": "trading-agent", "status": "proposed", "paper": True, "orders": [order]}, fh)
        code, data, tail = pack("check.py", ["--json"] + self.pack_nowarg(), self.work)
        res = (data or {}).get("results") or []
        if res and res[0].get("verdict") == "PASS":
            return True
        reasons = (res[0].get("reasons") if res else None) or [tail[-100:].strip() or "no answer"]
        log("pack risk check (%s %s): %s" % (side, sym, "; ".join(str(r) for r in reasons)[:200]))
        return False

    @addon(None)
    def pack_ticket(self):
        """Day Trader pack's Order Executor (trader-broker), used only to write the plain ticket for the record.
        It never approves, never records a yes and never sends anything to a broker: this is the paper book."""
        code, data, tail = pack("ticket.py", ["show", "--json"] + self.pack_nowarg(), self.work)
        t = data[0] if isinstance(data, list) and data else None
        if not t:
            log("pack ticket: no ticket (%s)" % tail[-100:].strip())
            return None
        return {"plain": str(t.get("plain") or "")[:300], "why": str(t.get("why") or "")[:200], "maxLoss": t.get("max_loss"),
                "riskPassed": bool(t.get("ready")), "sent": False}

    @addon(None)
    def pack_clock(self):
        """Day Trader pack's NYSE clock (trader-alerts) double-checks the agent's own calendar before any stock trade."""
        if not self.packmode:
            return
        code, clk, tail = pack("alerts.py", ["clock", "--json"] + self.pack_nowarg(), self.work)
        if not isinstance(clk, dict):
            log("pack clock: no answer (%s)" % tail[-80:].strip())
            return
        keep = {k: clk.get(k) for k in ("date", "open", "reason", "early_close", "opens", "closes", "next_open", "calendar_known")}
        keep["checkedAt"] = iso(self.now)
        self.s.setdefault("pack", {})["clock"] = keep
        if clk.get("open"):
            self.floor.say("strategy", "NYSE is open until %s." % t12(clk.get("closes") or "16:00"), to="pm", kind="mood",
                           key="clock:open:%s" % self.today, hours=12)
        elif self.market_day and clk.get("next_open"):
            nd = str(clk["next_open"])[:10]
            when = ("today" if nd == self.today.isoformat() else day_label(nd)) + " at " + t12(str(clk["next_open"])[11:16])
            self.floor.say("strategy", "NYSE is closed (%s). Opens %s." % (clk.get("reason"), when), to="pm", kind="mood",
                           key="clock:closed:%s:%s" % (self.today, clk.get("reason")), hours=12)
        if clk.get("calendar_known") and self.stocks_ok and not clk.get("open"):
            self.stocks_ok = False
            self.block_reason = "the pack's NYSE clock says the market is closed (%s)" % clk.get("reason")
            log("No stock trades: " + self.block_reason)

    @addon(None)
    def pack_alerts(self):
        """Day Trader pack's Price Alerts (trader-alerts): a phone alert when something held moves a lot in a day.
        An alert is information only: the agent's own stops and targets still do the selling."""
        if not self.packmode or self.replay_now:
            return
        pk = self.s.setdefault("pack", {})
        rules = {}
        for p in self.s["positions"].values():
            if p["kind"] == "option" or not p.get("yahoo"):
                continue
            ysym = str(p["yahoo"]).upper()
            rid = re.sub(r"[^a-z0-9]+", "-", ysym.lower()).strip("-") + "-move"
            rules[rid] = {"id": rid, "symbol": ysym, "type": "pct_move", "pct": {"crypto": 6, "future": 2.5}.get(p["kind"], 4),
                          "note": p["symbol"]}
        pk["alertRules"] = len(rules)
        if not rules:
            pk["alertState"] = {}
            return
        tdir = os.path.join(self.work, "trader")
        os.makedirs(os.path.join(tdir, "state"), exist_ok=True)
        spath = os.path.join(tdir, "state", "alerts.json")
        with open(os.path.join(tdir, "alerts.json"), "w") as fh:
            json.dump({"rules": list(rules.values())}, fh)
        with open(spath, "w") as fh:
            json.dump(pk.get("alertState") or {}, fh)
        code, res, tail = pack("alerts.py", ["check", "--json"], self.work)
        try:
            with open(spath) as fh:
                st = json.load(fh)
            if isinstance(st.get("rung"), dict):
                st["rung"] = {k: v for k, v in st["rung"].items() if k in rules}
            pk["alertState"] = st
        except (OSError, ValueError, AttributeError):
            pass
        if not isinstance(res, dict):
            log("pack alerts: no answer (%s)" % tail[-100:].strip())
            return
        names = {r["symbol"]: r["note"] for r in rules.values()}
        for r in res.get("rings") or []:
            msg = str(r.get("message") or "")
            self.events.append("Alert: " + msg)
            self.floor.say("trader", "Heads up: " + msg, to="pm", sym=names.get(r.get("symbol"), r.get("symbol")), kind="alert")
            pk.setdefault("rings", []).append({"at": iso(self.now), "symbol": names.get(r.get("symbol"), r.get("symbol")), "message": msg[:200]})
        pk["rings"] = pk.get("rings", [])[-20:]
        if res.get("errors"):
            log("pack alerts: " + "; ".join(str(e) for e in res["errors"])[:200])

    @addon(None)
    def sec_facts(self, sym):
        """Day Trader pack's Earnings Analyst (trader-analyst): the numbers the company filed with the SEC."""
        cache = self.s.setdefault("facts", {})
        got = cache.get(sym)
        e = self.s["earnings"].get(sym) or {}
        stale = not got or (self.today - dt.date.fromisoformat(got["fetchedOn"])).days >= 7 or \
            (e.get("status") == "reported" and (e.get("reportDate") or "") >= got.get("fetchedOn", ""))
        if not stale or not self.packmode or self.replay_now or self.facts_budget <= 0:
            return got if got and not got.get("missing") else None
        if "@" not in self.sec_contact:
            if not getattr(self, "_facts_said", False):
                log("SEC filed numbers skipped: add the SEC_CONTACT secret (your email) to turn them on")
                self._facts_said = True
            return got if got and not got.get("missing") else None
        self.facts_budget -= 1
        cik = sec_cik(sym, self.sec_contact)
        if not cik:
            cache[sym] = {"fetchedOn": self.today.isoformat(), "missing": True}
            return None
        rel = "trader/metrics-%s.json" % re.sub(r"[^A-Za-z0-9]", "_", sym)
        code, data, tail = pack("facts.py", ["--cik", str(cik), "--quarters", "5", "--json", "--metrics", rel,
                                             "--user-agent", "trading-agent research %s" % self.sec_contact], self.work)
        row = None
        try:
            with open(os.path.join(self.work, rel)) as fh:
                row = (json.load(fh).get("companies") or [None])[0]
        except (OSError, ValueError):
            row = None
        res = data[0] if isinstance(data, list) and data else {}
        if not row:
            log("SEC filed numbers for %s: none (%s)" % (sym, tail[-100:].strip()))
            cache[sym] = {"fetchedOn": self.today.isoformat(), "missing": True}
            return None
        changes = res.get("changes") or []
        cache[sym] = {"fetchedOn": self.today.isoformat(), "period": row.get("period_end"),
                      "revGrowth": row.get("revenue_growth_yoy"), "epsGrowth": row.get("eps_growth_yoy"), "epsTtm": row.get("eps_ttm"),
                      "grossMargin": row.get("gross_margin"), "opMargin": row.get("operating_margin"), "netMargin": row.get("net_margin"),
                      "line": str(changes[0])[:220] if changes else "", "note": str(res.get("note") or "")[:220],
                      "oneOff": any("operating income" in str(x) for x in changes)}
        log("SEC filed numbers for %s: sales %s%% y/y, operating margin %s%%" % (sym, row.get("revenue_growth_yoy"), row.get("operating_margin")))
        return cache[sym]

    def enter(self, c, inst, setup, price, stop, target, risk_cad, rep, passed, opt):
        f, fx, sym, side = self.f, self.fx, c["symbol"], c["side"]
        dv = c["dv"]
        base = {"underlying": sym, "kind": inst, "side": side, "currency": "USD", "entryDate": self.today.isoformat(),
                "entryTime": iso(self.now), "setup": setup, "score": c["score"], "riskCad": r2(risk_cad),
                "lastChecked": iso(self.now), "dayLowAtEntry": dv["dayLow"], "dayHighAtEntry": dv["dayHigh"],
                "name": self.names.get(sym) or (FUTURES[sym][3] if inst == "future" else sym),
                "thesis": "%s, score %d%s" % (SETUP_NAMES.get(setup, setup), c["score"],
                                              (" (%s)" % c["fundWhy"]) if c.get("fundWhy") else "")}
        if inst == "option":
            pid = "%s %s %g%s" % (sym, opt["expiry"], opt["strike"], "C" if opt["right"] == "call" else "P")
            f["usd"] -= opt["costUsd"]
            p = dict(base, symbol="%s %s %g %s" % (sym, opt["expiry"], opt["strike"], opt["right"].upper()), units=opt["contracts"],
                     mult=100, sleeve="usd", entryPrice=opt["ask"], expiry=opt["expiry"], strike=opt["strike"], right=opt["right"],
                     side="long", stopPrice=opt["ask"] * (1 - RULES["option_stop"]), targetPrice=opt["ask"] * (1 + RULES["option_target"]),
                     initialStop=opt["ask"] * (1 - RULES["option_stop"]), uStop=stop, uTarget=target, costCad=r2(opt["costUsd"] * fx),
                     lastPrice=(opt["ask"] + opt["bid"]) / 2, bid=opt["bid"], ask=opt["ask"], yahoo=sym,
                     assetKind="option", optionSide=side)
            label, value = "%s %s %s" % ("Bought", opt["right"].upper(), p["symbol"]), opt["costUsd"] * fx
            units, fillpx, cost_fee = opt["contracts"], opt["ask"], RULES["option_fee"] * opt["contracts"] * fx
            trade_side = "BUY"
        elif inst == "future":
            y, mult, mfrac, name = FUTURES[sym]
            dirn = 1 if side == "long" else -1
            fill = price * (1 + RULES["slip"]["future"] * dirn)
            n = c["contracts"]
            fee = RULES["future_fee"] * n
            f["usd"] -= fee
            pid = sym
            p = dict(base, symbol=sym, units=n, mult=mult, sleeve="usd", entryPrice=fill, yahoo=y, marginUsd=c["margin"],
                     stopPrice=fill + (stop - price), targetPrice=fill + (target - price), initialStop=fill + (stop - price),
                     costCad=r2(c["margin"] * fx), feesCad=r2(fee * fx), lastPrice=price)
            label, value = "%s %d %s (%s)" % ("Bought" if side == "long" else "Shorted", n, sym, name), c["margin"] * fx
            units, fillpx, cost_fee = n, fill, fee * fx
            trade_side = "BUY" if side == "long" else "SHORT"
        elif inst == "crypto":
            fill = price * (1 + RULES["slip"]["crypto"])
            value = c["value"]
            units = value / (fill * fx)
            f["cad"] -= value
            pid = sym
            p = dict(base, symbol=sym, units=units, mult=1, sleeve="cad", entryPrice=fill, yahoo=c["yahoo"],
                     stopPrice=fill * stop / price, targetPrice=fill * target / price, initialStop=fill * stop / price,
                     costCad=r2(value), lastPrice=price)
            label, fillpx, cost_fee, trade_side = "Bought %s %s" % (money(value), sym), fill, value * 0.008 / 1.008, "BUY"
        else:
            slip = RULES["slip"][inst]
            value = c["value"]
            usd = value / fx
            tsx = c.get("market") == "TSX"
            if side == "long":
                fill = price * (1 + slip)
                units = c.get("shares") or usd / fill
                if tsx:
                    f["cad"] -= units * fill
                    costcad = units * fill
                else:
                    f["usd"] -= units * fill
                    costcad = units * fill * fx
                trade_side, verb = "BUY", "Bought"
            else:
                fill = price * (1 - slip)
                units = usd / fill
                f["usd"] += units * fill
                costcad = units * fill * fx
                trade_side, verb = "SHORT", "Shorted"
            pid = sym if side == "long" else sym + " short"
            p = dict(base, symbol=sym, units=units, mult=1, sleeve="usd", entryPrice=fill, yahoo=sym,
                     stopPrice=fill * stop / price, targetPrice=fill * target / price, initialStop=fill * stop / price,
                     costCad=r2(costcad), lastPrice=price, lastFeeAt=iso(self.now), borrowFeeCad=0.0)
            if side == "short" and c.get("borrowRate"):
                p["borrowRate"] = round(c["borrowRate"], 4)
            if tsx:
                p.update({"sleeve": "cad", "currency": "CAD", "market": "TSX"})
            label, fillpx, cost_fee = "%s %s %s" % (verb, money(value), sym), fill, value * slip
        self.s["positions"][pid] = p
        if self.live:
            if inst == "crypto":
                self.live.enter_crypto(sym, price, stop, target, setup, c["score"])
            elif inst in ("stock", "etf") and side == "long" and not c.get("market"):
                self.live.stock_entry(sym, price, stop, target, setup, c["score"], p.get("name", sym))
        self.value_position(p)
        f["entriesToday"] += 1
        f["entriesThisWeek"] += 1
        self.s["trades"].append({
            "id": "%s-%s-%s" % (self.ny.strftime("%Y%m%d-%H%M"), pid.replace(" ", "_"), trade_side),
            "date": self.ny.strftime("%Y-%m-%d %H:%M"), "symbol": p["symbol"], "kind": inst, "side": trade_side,
            "units": units, "price": round(fillpx, 6), "currency": p["currency"], "valueCad": r2(value),
            "costsCad": r2(cost_fee), "setup": setup, "reason": "%s, score %d" % (SETUP_NAMES.get(setup, setup), c["score"]),
            "check": {"verdict": "PASS", "passed": passed, "failed": []},
            "ticket": c.get("ticket"), "sizedBy": (c.get("sizing") or {}).get("bound_by"),
            "replay": {"pTarget": rep.get("pTarget"), "pStop": rep.get("pStop"), "meanR": rep.get("meanR"), "source": rep.get("source")}})
        self.floor_fill(p, trade_side, units)
        self.events.append("%s (%s, score %d)" % (label, SETUP_NAMES.get(setup, setup), c["score"]))
        log("%s %s: entry %.4f, stop %.4f, target %.4f" % (trade_side, p["symbol"], p["entryPrice"], p["stopPrice"], p["targetPrice"]))
        self.recompute()

    # ---------------------------------------------------------- review
    def review(self):
        st = self.f["stats"]
        for t in self.s["trades"]:
            if t.get("side") not in ("SELL", "COVER") or t.get("reviewed"):
                continue
            t["reviewed"] = True
            r, setup = float(t.get("r", 0)), t.get("setup") or "breakout"
            st["closed"] += 1
            st["wins"] += 1 if t.get("pnlCad", 0) > 0 else 0
            st["totalR"] = round(st["totalR"] + r, 3)
            b = st["bySetup"].setdefault(setup, {"n": 0, "wins": 0, "r": 0.0, "disabled": False})
            b["n"] += 1
            b["wins"] += 1 if t.get("pnlCad", 0) > 0 else 0
            b["r"] = round(b["r"] + r, 3)
            if b["n"] >= 8 and b["r"] < 0 and not b["disabled"]:
                b["disabled"] = True
                self.events.append("Setup switched off: %s lost money over %d trades" % (SETUP_NAMES.get(setup, setup), b["n"]))
                self.floor.say("pm", "Switching off %s: it lost money over %d trades." % (SETUP_NAMES.get(setup, setup), b["n"]), kind="block")
            reason = t.get("reason", "")
            if "stop" in reason or "half its value" in reason:
                lesson = "%s %s: stopped out (%+.2fR). Wait for a cleaner entry before this setup again." % (t["symbol"], SETUP_NAMES.get(setup, setup), r)
            elif "take-profit" in reason or "doubled" in reason:
                lesson = "%s %s: target reached (%+.2fR). The plan worked as written." % (t["symbol"], SETUP_NAMES.get(setup, setup), r)
            else:
                lesson = "%s %s: closed by rule (%s), %+.2fR." % (t["symbol"], SETUP_NAMES.get(setup, setup), reason[:40], r)
            self.s["lessons"] = ([lesson[:150]] + self.s["lessons"])[:12]
            if "stop" in reason or "half its value" in reason:
                note = "stopped out (%+.2fR). Wait for a cleaner entry next time." % r
            elif "take-profit" in reason or "doubled" in reason:
                note = "hit its target (%+.2fR). The plan worked as written." % r
            else:
                note = "closed by rule (%+.2fR)." % r
            self.floor.say("pm", "Review: %s %s %s" % (t["symbol"], SETUP_NAMES.get(setup, setup).lower(), note), sym=t.get("symbol", ""), kind="info")

    # ---------------------------------------------------------- the whole run
    def go(self):
        log("Run %s %s (New York %s), stock market %s" % (self.today, self.type, self.ny.strftime("%H:%M"),
                                                          "open day" if self.market_day else "closed today"))
        self.bookkeeping()
        self.mark()
        self.live = LiveDesk(self)
        self.live.start()
        if self.market_day and not self.replay_now:
            spy = yahoo_chart("SPY", "5d", "5m")
            if spy and spy["bars"] and bar_day(spy["bars"][-1]["t"]) != self.today and self.hm >= 9 * 60 + 45:
                self.market_day = False
                log("SPY has no bars today: treating the stock market as closed")
        held = [(p["underlying"], crypto_yahoo(p["underlying"]) if p["kind"] == "crypto" else p["underlying"])
                for p in self.s["positions"].values() if p["kind"] != "future"]
        self.fetch_news(list(dict.fromkeys(held)))
        stocks_held = [p["underlying"] for p in self.s["positions"].values() if p["kind"] in ("stock", "option")]
        if stocks_held and self.market_day:
            self.earnings(stocks_held)
        self.exits()
        self.live.manage()
        self.regimes()
        self.econ_refresh()
        self.source_check()
        self.gate()
        self.pack_clock()
        self.intraday_bots()
        if self.type == "OPEN":
            self.floor_meeting("open")
        if self.etf_due:
            self.etf_refresh()
        if self.universe_due:
            self.universe_scan()
        self.congress()
        hits = self.check_alerts()
        if self.stocks_ok or self.crypto_ok or self.futures_ok:
            self.hunt(hits)
        elif self.type != "WATCH" or self.scan_stocks:
            log("No new trades this run: %s" % (self.block_reason or "markets closed or nothing due"))
        self.pack_alerts()
        self.weekly_backtest()
        self.review()
        self.live.finish()
        self.record()

    def record(self):
        f, s = self.f, self.s
        for p in s["positions"].values():
            self.value_position(p)
        self.recompute()
        cash = f.get("usd", 0) * self.fx + f.get("cad", 0)
        problems = []
        if f.get("cad", 0) < -0.01:
            problems.append("crypto cash is negative")
        if self.free_usd() < -1:
            self.notes.append("USD cash is fully committed (shorts or futures moved against it)")
        if abs(cash + sum(p["valueCad"] for p in s["positions"].values()) + self.ib_value() - f["equityCad"]) > 0.05:
            problems.append("value does not add up")
        if f["entriesToday"] > RULES["entries_per_day"] or f["entriesThisWeek"] > RULES["entries_per_week"]:
            problems.append("too many trades")
        if problems:
            f["ruleBreaks"] = f.get("ruleBreaks", 0) + 1
            self.events.append("Bookkeeping problem: " + ", ".join(problems))
            self.floor.say("risk", "Bookkeeping problem: %s. Checking the books." % ", ".join(problems), to="pm", kind="halt")
        f["peakEquityCad"] = max(f.get("peakEquityCad", f["equityCad"]), f["equityCad"])
        dd = (1 - f["equityCad"] / f["peakEquityCad"]) * 100 if f["peakEquityCad"] else 0
        f["maxDrawdownPct"] = round(max(f.get("maxDrawdownPct", 0), dd), 2)
        if self.type == "CLOSE" and self.market_day and f.get("lastTradingDay") != self.today.isoformat():
            f["tradingDays"] = f.get("tradingDays", 0) + 1
            f["lastTradingDay"] = self.today.isoformat()
            if f["tradingDays"] == RULES["paper_days_goal"]:
                self.events.append("Paper test complete: 20 trading days. Open the page and compare with buy-and-hold.")
        nav = [n for n in s["nav"] if n.get("date") != self.today.isoformat()]
        nav.append({"date": self.today.isoformat(), "equityCad": f["equityCad"], "benchmarkCad": f["benchmarkCad"], "cashCad": r2(cash)})
        s["nav"] = sorted(nav, key=lambda n: n.get("date") or "")[-400:]
        s["trades"] = s["trades"][-400:]
        summary = self.summary()
        regime = self.stock_regime if self.market_day else self.crypto_regime
        j = next((x for x in s["journal"] if x.get("date") == self.today.isoformat()), None)
        if not j:
            j = {"date": self.today.isoformat(), "summary": ""}
            s["journal"].append(j)
        label = {"OPEN": "Morning", "CLOSE": "Close", "WATCH": self.ny.strftime("%I:%M %p").lstrip("0")}[self.type]
        if self.type != "WATCH" or any(e.startswith(("Bought", "Sold", "Shorted", "Covered", "HALTED")) for e in self.events):
            text = (j["summary"] + " " if j.get("summary") else "") + "%s: %s" % (label, summary)
            if len(text) > 1400:
                text = "..." + text[-1400:].split(". ", 1)[-1]
            j["summary"] = text
        if self.cands or self.blocked:
            j.update({"topCandidates": self.cands[:8], "blocked": self.blocked[:8]})
        focus = f.get("focus") or {}
        j.update({"regime": regime, "cryptoRegime": self.crypto_regime, "dataCalls": CALLS["n"],
                  "screened": focus.get("screened"), "liquid": focus.get("liquid"), "universe": focus.get("universe")})
        s["journal"] = s["journal"][-30:]
        f["lastRun"] = iso(self.now)
        f["lastRunNote"] = summary[:240]
        f["lastRunBy"] = os.environ.get("AGENT_RUNNER", "manual")
        if self.type != "WATCH":
            s["runs"]["%s %s" % (self.today, self.type)] = {"at": iso(self.now), "by": f["lastRunBy"]}
            s["runs"] = dict(sorted(s["runs"].items())[-20:])
        keep_f = {p["underlying"] for p in s["positions"].values()} | set(focus.get("long", [])[:20])
        fu = s.get("fundamentals", {})
        cut = (self.today - dt.timedelta(days=14)).isoformat()
        s["fundamentals"] = {k: v for k, v in fu.items() if k in keep_f or (v.get("fetchedOn") or "") >= cut}
        s["facts"] = {k: v for k, v in s.get("facts", {}).items() if k in keep_f or (v.get("fetchedOn") or "") >= cut}
        if self.type == "CLOSE" and self.market_day:
            self.floor_meeting("close")
        try:
            self.floor.flush()
        except Exception as e:
            log("floor not saved: %s" % e)
        s["log"] = LOG[-100:]
        shutil.rmtree(self.work, ignore_errors=True)

    def summary(self):
        f = self.f
        parts = []
        spy = self.daily.get("SPY")
        if self.market_day and spy:
            parts.append("The S&P 500 fund was %s %.1f%% today; market mood %s." % (
                "up" if spy["movePct"] >= 0 else "down", abs(spy["movePct"]), self.stock_regime))
        elif not self.market_day:
            parts.append("US stock market closed today.")
        parts.append("Bitcoin %s %.1f%% in 24 hours (%s)." % ("up" if self.btc24 >= 0 else "down", abs(self.btc24), self.crypto_regime))
        trades = [e for e in self.events if e.startswith(("Bought", "Sold", "Shorted", "Covered"))]
        if trades:
            parts.append(" ".join(e + "." for e in trades[:6]))
        elif self.blocked:
            parts.append("No trades: %s came closest but failed %s." % (
                self.blocked[0]["symbol"], ", ".join(self.blocked[0]["failed"][:2]).lower()))
        elif not (self.stocks_ok or self.crypto_ok or self.futures_ok):
            parts.append("No new trades this run (%s)." % (self.block_reason or "nothing due"))
        else:
            parts.append("No trades: nothing scored high enough with a clean setup.")
        parts.extend(self.notes)
        held = len(self.s["positions"])
        parts.append("Holding %d position%s. Value %s vs buy-and-hold %s." % (held, "" if held == 1 else "s",
                                                                            money(f["equityCad"]), money(f["benchmarkCad"])))
        return " ".join(" ".join(parts).split()[:110])


# ---------------------------------------------------------------- commands
def make_store(a):
    if getattr(a, "state", None):
        return LocalStore(a.state)
    repo = os.environ.get("AGENT_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("AGENT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        sys.exit("No state store: pass --state FILE, or set AGENT_GITHUB_REPO and AGENT_GITHUB_TOKEN.")
    return GitHubStore(repo, token)


def pick_mode(state, ny):
    """OPEN once each market morning, CLOSE once each market afternoon, WATCH otherwise."""
    d, hm, runs = ny.date(), ny.hour * 60 + ny.minute, state.get("runs", {})
    if is_trading_day(d):
        if 10 * 60 + 15 <= hm < 12 * 60 and "%s OPEN" % d not in runs:
            return "OPEN"
        if 15 * 60 + 15 <= hm < 16 * 60 + 30 and "%s CLOSE" % d not in runs:
            return "CLOSE"
    return "WATCH"


def migrate(state, now):
    """Version 1 ($100, long only) becomes version 2 ($10,000, every market)."""
    if state.get("schema", 1) >= 2:
        return state
    old = state.get("fund", {})
    new = empty_state()
    new["previous"] = {"startEquityCad": old.get("startEquityCad"), "endEquityCad": old.get("equityCad"),
                       "startDate": old.get("startDate"), "endDate": to_ny(now).date().isoformat(),
                       "note": "the $100 long-only paper test, replaced by the $10,000 version"}
    new["journal"] = [{"date": to_ny(now).date().isoformat(), "regime": "",
                       "summary": "Restarted the paper test with $10,000: every US stock and liquid ETF, long and short, "
                                  "options, micro futures and the top 100 coins. The $100 test ended at %s." % money(old.get("equityCad", 100))}]
    new["congress"], new["congressSeen"] = state.get("congress", {}), state.get("congressSeen", [])
    new["fundamentals"] = state.get("fundamentals", {})
    log("Records upgraded to the $10,000 version")
    return new


def cmd_run(a):
    now = parse_iso(a.now) if a.now else utcnow()
    cfg = load_config()
    topic = os.environ.get("AGENT_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC") or ""
    store = make_store(a)
    try:
        state, version = store.load()
    except Exception as e:
        log("Could not read the agent's records: %s" % e)
        notify(topic, "Trading agent: run failed", "Could not read its records: %s" % str(e)[:150], a.dry_run)
        return 1
    state = migrate(state, now)
    run = Run(state, cfg, now, a.dry_run, a.mode)
    if a.now:
        run.replay_now = now
    slot = "%s %s" % (run.today, run.type)
    try:
        run.go()
    except Exception as e:
        log("Run failed, nothing changed: %s" % e)
        log(traceback.format_exc()[-1200:])
        notify(topic, "Trading agent: run failed", "%s run failed and changed nothing: %s" % (run.type, str(e)[:150]), a.dry_run)
        return 1
    f = state["fund"]
    if a.dry_run:
        log("Dry run: nothing saved. Value %s vs buy-and-hold %s." % (money(f["equityCad"]), money(f["benchmarkCad"])))
        return 0
    last_saved = parse_iso(f.get("lastSaved")) or dt.datetime(2000, 1, 1)
    if run.type == "WATCH" and not run.events and not run.scanned and (now - last_saved).total_seconds() < 1800 \
            and state.get("schema") == 2 and f.get("benchmarkSet") and version:
        log("Quiet check: nothing changed worth saving. Value %s." % money(f["equityCad"]))
        return 0
    f["lastSaved"] = iso(now)
    ok, msg = store.save(state, version, "Agent %s %s" % (slot, now.strftime("%H:%MZ")))
    log(msg)
    if not ok:
        notify(topic, "Trading agent: not saved", msg, False)
        return 1
    tail = " | Value %s vs buy-and-hold %s" % (money(f["equityCad"]), money(f["benchmarkCad"]))
    if run.events:
        notify(topic, "Trading agent", " | ".join(run.events)[:600] + tail, False)
    elif run.type == "CLOSE" and cfg.get("daily_summary", True):
        notify(topic, "Trading agent: day done", run.summary()[:500], False)
    return 0


def cmd_selftest(a):
    topic = os.environ.get("AGENT_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC") or ""
    rows = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)[:80]
        rows.append((name, ok, detail))
        print("%-28s %s  %s" % (name, "OK  " if ok else "FAIL", detail), flush=True)

    def yahoo(sym):
        def f():
            ch = yahoo_chart(sym, "1d", "5m")
            return bool(ch and ch["price"]), ("price %.4f" % ch["price"]) if ch and ch["price"] else "no data"
        return f
    check("Yahoo stocks (SPY)", yahoo("SPY"))
    check("Yahoo crypto (BTC-USD)", yahoo("BTC-USD"))
    check("Yahoo futures (ES=F)", yahoo("ES=F"))
    check("Yahoo USD/CAD", yahoo("CAD=X"))
    check("Yahoo news feed", lambda: (lambda s: (s[0] == 200, "HTTP %s" % s[0]))(
        http("https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US")))
    check("Stock list (Nasdaq Trader)", lambda: (lambda u: (len(u) > 3000, "%d stocks" % len(u)))(stock_universe(tempfile.mkdtemp())))
    check("CoinGecko top 100", lambda: (lambda r: (r[1] != "fixed list", "%d coins" % len(r[0])))(crypto_universe()))
    check("Nasdaq earnings", lambda: (lambda r: (r is not None, "%d companies" % len(r or {})))(
        nasdaq_earnings(next_trading_day(to_ny(utcnow()).date()))))
    check("Nasdaq fundamentals", lambda: (lambda r: (bool(r), "AAPL sales %s%%" % (r or {}).get("revGrowthPct")))(nasdaq_fundamentals("AAPL")))
    d0 = to_ny(utcnow()).date() + dt.timedelta(days=14)
    check("Nasdaq options", lambda: (lambda r: (len(r) > 0, "%d strikes, expiries %s" % (len(r), sorted({str(x["expiry"]) for x in r})[:3])))(
        nasdaq_option_chain("AAPL", d0, d0 + dt.timedelta(days=31))))
    contact = os.environ.get("AGENT_SEC_CONTACT", "")
    if contact:
        check("SEC earnings reports", lambda: (lambda c: (bool(c), "contact set, AAPL CIK %s" % c))(sec_cik("AAPL", contact)))
    else:
        rows.append(("SEC earnings reports", True, "no contact set yet (optional)"))
        print("%-28s OK    no SEC_CONTACT secret yet: reports read from headlines" % "SEC earnings reports")
    check("House Clerk (Congress)", lambda: (lambda s: (s[0] == 200, "HTTP %s" % s[0]))(
        http("https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%dFD.zip" % to_ny(utcnow()).year, method="HEAD")))
    check("Yahoo 1-minute (QQQ)", lambda: (lambda b: (len(b) > 100, "%d candles" % len(b)))(intraday.minutes("QQQ", "5d")))
    check("Cboe options (QQQ)", lambda: (lambda q: (bool(q.get("quotes")), "next expiry %s, %d quotes, IV %s" % (
        q.get("expiry"), len(q.get("quotes") or {}), round(q["iv"], 3) if q.get("iv") else "none")))(intraday.cboe_quotes("QQQ", to_ny(utcnow()).date())))
    check("News model", lambda: (lambda r: (bool(news_model()), "loaded" if news_model() else "word rules"))(None))
    if getattr(a, "state", None) or os.environ.get("AGENT_GITHUB_TOKEN"):
        def gh():
            st, v = make_store(a).load()
            return True, "records read, value %s" % money(st["fund"]["equityCad"])
        check("Agent records (GitHub)", gh)
    tools = ["scan.py", "news.py", "entry.py", "exit.py", "timemachine.py", "size.py", "check.py", "ticket.py", "alerts.py", "facts.py", "ptr.py"]
    check("Day Trader pack", lambda: (lambda bad: (not bad, "all 11 tools load" if not bad else "failed: " + ", ".join(bad)))(
        [t for t in tools if pack(t, ["--help"], tempfile.gettempdir())[0] not in (0, None)]))
    check("Pack NYSE clock", lambda: (lambda d: (isinstance(d, dict) and "open" in d, "open now" if (d or {}).get("open") else "closed now: %s" % (d or {}).get("reason")))(
        pack("alerts.py", ["clock", "--json"], tempfile.gettempdir())[1]))
    if "@" in contact:
        def facts_check():
            w = tempfile.mkdtemp()
            code, data, tail = pack("facts.py", ["--cik", "320193", "--quarters", "4", "--json", "--no-filings",
                                                 "--user-agent", "trading-agent research %s" % contact], w)
            ok = isinstance(data, list) and bool(data)
            return ok, ((data[0].get("changes") or ["read"])[0][:70] if ok else tail[-80:])
        check("SEC filed numbers (AAPL)", facts_check)
    good = all(r[1] for r in rows)
    msg = "Self-test %s: %d of %d checks passed." % ("passed" if good else "had problems", sum(r[1] for r in rows), len(rows))
    print(msg)
    notify(topic, "Trading agent self-test", msg + " " + "; ".join("%s %s" % (r[0], "ok" if r[1] else "FAIL") for r in rows), a.dry_run)
    return 0 if good else 1


def cmd_probe(a):
    """Developer check on GitHub: options chains, picking a contract and marking it."""
    state = empty_state()
    r = Run(state, load_config(), utcnow(), True, "WATCH")
    r.fx, _ = r.quote("CAD=X")
    for sym in ("AAPL", "MSFT", "AMD", "F"):
        px, _ = r.quote(sym)
        for side in ("long", "short"):
            opt, why = r.pick_option(sym, side, px, 70.0, 400.0)
            print(sym, side, "price %.2f" % px, opt or why, flush=True)
            if opt:
                p = {"underlying": sym, "expiry": opt["expiry"], "strike": opt["strike"], "right": opt["right"], "lastPrice": opt["ask"]}
                r.mark_option(p)
                print("   marked:", {k: p.get(k) for k in ("bid", "ask", "lastPrice", "underlyingPrice")}, flush=True)
    try:
        pairs = kraken_cad_pairs()
        print("kraken CAD pairs:", len(pairs), sorted(pairs)[:40], flush=True)
        print("kraken BTC:", pairs.get("BTC"), kraken_public("Ticker", {"pair": pairs["BTC"]["pair"]}) if "BTC" in pairs else "", flush=True)
    except Exception as e:
        print("kraken public failed:", e, flush=True)
    print("surprise AAPL:", nasdaq_surprise("AAPL"))
    print("fundamentals MSFT:", nasdaq_fundamentals("MSFT"))
    print("release read (word rules if no model):", read_release("Revenue grew 18% to a record $50 billion. We are raising our full-year guidance. Gross margin expanded."))
    return 0


def cmd_backtest(a):
    """Run the two-year backtest now and save it with the records."""
    cfg = load_config()
    now = utcnow()
    store = make_store(a)
    state, version = store.load()
    state = migrate(state, now)
    run = Run(state, cfg, now, a.dry_run)
    run.weekly_backtest(force=True)
    if not a.dry_run and state.get("backtest", {}).get("ranOn"):
        ok, msg = store.save(state, version, "Agent backtest %s" % now.strftime("%Y-%m-%d %H:%MZ"))
        log(msg)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Trading agent, paper trading only")
    sub = p.add_subparsers(dest="cmd")
    for name in ("run", "selftest"):
        sp = sub.add_parser(name)
        sp.add_argument("--state", help="keep the records in this local file instead of GitHub")
        sp.add_argument("--dry-run", action="store_true", help="save nothing and send no alert")
        if name == "run":
            sp.add_argument("--now", help="replay a past moment, UTC ISO time (for testing)")
            sp.add_argument("--mode", choices=["OPEN", "CLOSE", "WATCH"], help="force the run type (testing)")
    sub.add_parser("probe")
    sp = sub.add_parser("backtest")
    sp.add_argument("--state", help="keep the records in this local file instead of GitHub")
    sp.add_argument("--dry-run", action="store_true", help="save nothing")
    a = p.parse_args(argv)
    if a.cmd == "backtest":
        return cmd_backtest(a)
    if a.cmd == "probe":
        return cmd_probe(a)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "selftest":
        return cmd_selftest(a)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
