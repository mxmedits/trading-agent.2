# Trading Agent

An automatic **paper-trading** agent (pretend money: CAD 10,000) that studies the whole US market
and trades whatever passes its rules: long or short, shares or options, plus micro futures and crypto.
It runs by itself on GitHub's free computers at no cost:

- every 5 minutes while US markets can be open, Monday to Friday
- every 30 minutes, all week, for crypto, futures and news

**Page:** https://mxmedits.github.io/trading-agent.2/ (add it to your iPhone home screen).
**Alerts:** every trade, stop, target, earnings read and halt is posted to the issue
"Trading agent alerts", which GitHub sends to you in its app and by email.

It never places a real order. There is no broker code in this repository.

## What it studies

| market | how |
| --- | --- |
| every US-listed stock (about 6,000) | read each market morning before the open; the liquid ones (over $20M a day, $5 or more) ranked for buys and shorts; the best 100 rescanned every 10 minutes from 9:50 AM to 2:55 PM New York |
| ETFs | every US-listed ETF read weekly; the 300 most traded, without leveraged, inverse or volatility funds, join the daily screen |
| options | strong stock ideas (80+ points) may buy a call or a put instead of shares, 2 to 6 weeks out, near the money, liquid only |
| Canadian stocks and ETFs | about 70 large TSX companies and 12 main TSX ETFs, bought in Canadian dollars (no currency cost), buy-only for now, Canadian holidays respected |
| futures | micro S&P 500, Nasdaq, Dow, Russell, gold, silver, oil, bitcoin, ether, euro, long or short, only when one contract fits the 5% risk and the margin cap |
| crypto | the 100 largest coins without stablecoins, wrapped copies, meme coins or exchange tokens, long only |

## Judgement

- **News:** each headline about the company or coin is read by the open-source finance-news model
  `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` (Apache-2.0), on GitHub's computer.
- **Fundamentals:** sales and profit growth, price/earnings, debt, free cash flow and analysts' targets (Nasdaq);
  for close calls, the numbers the company filed with the SEC take over (the pack's earnings analyst).
- **Earnings:** report dates (Nasdaq); after a report, profit against forecast, sales growth, and the tone and
  outlook of the company's own press release from the SEC.
- **Congress:** every new House trade report, read from the PDF.
- **Time machine:** 10,000 replays before every trade (the Day Trader pack's 5-minute replay for breakouts and breakdowns).
- **Sizing:** every stock or ETF buy is whole shares, sized by the pack's Budget Manager inside four limits
  (max loss, position cap, max order, cash); a stop too far away means no trade, not a smaller one.
- **Tickets:** the pack's Order Executor writes the plain ticket for each paper buy (shares, stop, target,
  max loss). It never records a yes and never contacts a broker.
- **Market clock and alerts:** the pack's NYSE clock double-checks the market is open before a stock trade,
  and a phone alert goes out when anything held moves a lot in a day (4% stocks, 6% crypto, 2.5% futures).
- **Same bet:** a new trade is blocked when it moves with something already held, in the same direction, 80% of the time.
- **Calendar:** on inflation (CPI) and jobs report mornings (BLS schedule), no stock trades before 10:30 and no futures 8:00 to 9:30.
- **Shorts:** only when Interactive Brokers' public list shows shares to borrow, at under 20% a year; the real fee is charged.
- **Options data:** Nasdaq first, Cboe's free delayed quotes when Nasdaq has none; bought at the ask, sold at the bid.

## Intraday bots

Six bots run the 1-minute rules from Nick Fischer's "QQQ bots" reel, one per market:

| bot | trades | how |
| --- | --- | --- |
| Options | QQQ, SPY, IWM | calls and puts expiring the next trading day, priced from Cboe's delayed quotes and implied volatility |
| Stocks | NVDA, TSLA, AAPL, MSFT, AMZN, META, GOOGL, AMD, AVGO, PLTR, NFLX, MU | shares, long or short |
| ETFs | DIA, SMH, XLK, XLF, XLE, XLV, TLT, EEM | shares, long or short |
| Futures | micro S&P 500, Nasdaq, Dow, Russell, euro | 8:00 AM to 3:55 PM New York, long or short |
| Commodities | micro gold, silver, crude oil | same hours as futures |
| Crypto | BTC, ETH, SOL, XRP, DOGE | around the clock, buys only |

- **Signals**, on each closed 1-minute candle with the 9, 21 and 50 EMAs and VWAP: VWAP cross, EMA50 cross,
  opening-range break (first 15 minutes, once each way a day) and trend continuation (EMAs stacked, a pullback to the 9 EMA).
- **Filters**: trend (no buys while the 50 EMA falls, no puts or shorts while it rises), chop (no trades after 4 VWAP
  crossings in 30 minutes), RSI past 75 / 25, and costs (no trade when buying and selling costs over a quarter of the risk).
- **Exits**: a stop on every trade (1.5 one-minute ranges), a profit lock (+0.25R once up 1R), a trailing stop (1R behind
  the best price from +1.5R), a quick target at 2R on crossing trades, 30/90-minute time stops, flat 5 minutes before the close.
- **Brakes**: 1% of the account at risk a trade ("Medium"); each bot stops for the day at -2% or +3%, or after 15 trades.
  The account's own limits still apply: the 15% open-risk cap, the -5% day and -10% week pauses, CPI/jobs mornings and Fed afternoons.
- **Every trade logs** why it bought: the setup, the EMAs, VWAP and the gap to it, RSI, the price in and out, and how long it held.
- The agent checks in every 5 minutes; each check plays through the candles that closed since the last one, using only what was
  known at each close. New trades only on candles from the last 35 minutes.
- **Last month's test** (Nov 27 2026, 20 trading days, the same code): about 1,000 trades, 51% winners, between +$59 and -$401
  on CAD 10,000 depending on the start hour: break-even at best. Options did best, futures and commodities lost, crypto never traded
  (Kraken's 0.4% fee each way is bigger than a 1-minute move). Turning the filters off (like the reel's "QQQ OG") made about +$2,000,
  almost all from options; small rule changes swung results by thousands, so none of this is a proven edge.
  It reruns every Saturday on the latest 30 days and shows on the page.

Change the controls in `config.json` under `"intraday"`, e.g. `{"controls": {"risk": "low", "style": "trend"}, "bots": {"crypto": {"on": false}}}`.

## Risk and the backtest

Each trade risks up to 5% of the account (your choice, Nov 18 2026), with at most 15% at risk across all open trades.
The agent pauses after losing 5% in a day or 10% in a week, and stops for good at -15%. A stock or ETF position is
also capped at 25% of its currency's share of the account, so most stock trades risk well under 5%.

`backtest.py` replays the agent's own daily rules over the last two years of prices for about 400 symbols, every
Saturday (and once soon after it was added), and the results show on the page. What it found on Nov 18 2026:

- the old rules lost money: stops half a day's range away were hit by normal noise
- stops two daily ranges away roughly broke even, so the agent now uses them
- pullback trades lost money in both years, so that setup is switched off
- easing the score bar on cautious days and selling half at +1R both made results worse, so neither is used
- none of the versions shows a reliable edge: at 1% risk the best was about break-even; at 5% risk the agent hit
  the -15% stop in one of the two universes tested

The backtest can't see past headlines or company numbers, can't replay opening-range day trades or options, and uses
today's list of companies. Run it by hand from Actions with the command `backtest`.

**Trading floor:** nine desks (Research, News, Analyst, Strategy, Quant, Risk, Trader, the Portfolio manager and the intraday bots)
report each real step of a run to each other, one line at a time: scans, headlines, scores, replays, risk checks, fills
and exits. The code writes every line from what it just did; no AI chat, no cost. Tap Replay on the page to watch the
latest check play out.

The rules are listed in full on the page under "How it decides".

## Real money

Set in `config.json` under `live`: at most **CAD 1,000** in total, about **$10 risked per trade**.
Switch every new real trade off at once with `"enabled": false` (edit the file in the GitHub app).

**Crypto on Kraken, fully automatic (budget $300).** Buys only (Kraken offers Canadians no margin or
derivatives). Every buy is protected at once by a stop-loss order resting at Kraken, so it works even
between runs. Real buying stops if real losses reach 15%. Every order carries the agent's own tag, so it
never sells coins you bought yourself. Only coins Kraken sells for Canadian dollars can be bought for
real (in November 2026: BTC, ETH, SOL, XRP, DOGE, PEPE, XDC); the paper book still trades the top 100.

To switch it on: in Kraken, Settings → API → create a key with only *Query Funds*, *Query Open Orders &
Trades*, *Query Closed Orders & Trades*, *Create & Modify Orders* and *Cancel/Close Orders* (never
*Withdraw*), then add two repository secrets on github.com: `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`.
The agent checks the key each day and refuses to trade if it could withdraw money.

**Stocks and ETFs on Questrade, approved by you (budget $700).** Questrade lets only partner companies
place orders by program. The agent writes each long trade as a signal in `docs/state.json`
(`live.stocks`); Claude's Questrade connection sends it to your Questrade app, and nothing happens
unless you approve it there. Needs an active self-directed Questrade account with push-to-approve set up.

## One optional setting: reading companies' own earnings reports

The SEC asks every program to give a contact email. To let the agent read press releases from the SEC,
add a repository secret on github.com: **Settings → Secrets and variables → Actions → New repository secret**,
name `SEC_CONTACT`, value: your email. It is only sent to the SEC and never appears in the code.
It also turns on the SEC's filed numbers (sales, margins, profit per share) for close calls.
Without it, earnings are judged from Nasdaq's numbers and the earnings headlines.

## Running it by hand, or stopping it

GitHub app or github.com → this repository → **Actions → agent → Run workflow** (`run`, `selftest`, `probe` or `backtest`).
To stop it: **Actions → agent → ⋯ → Disable workflow**.

## Files

| file | what |
| --- | --- |
| `agent.py` | the trading logic: one run per call |
| `backtest.py` | the two-year backtest of the agent's own daily rules |
| `intraday.py` | the six intraday bots (the reel's 1-minute rules) and their one-month backtest |
| `floor.py` | the trading floor: the nine desks and what they say to each other |
| `core.py` | clock, free data sources, judgement, paper-trading maths, storage, alerts |
| `config.json` | fallback lists and the names used to match headlines |
| `docs/state.json` | everything it knows: cash, holdings, trades, history (written by each run) |
| `docs/index.html` | the page (published from the `gh-pages` branch) |
| `.github/workflows/agent.yml` | the schedule |
| `requirements.txt` | the news model's libraries (installed on GitHub's computer) |
| `pack/` | all eleven [Day Trader skill pack](https://github.com/Jakeschincariol/day-trader-skill) tools (MIT, commit `3d0f4ef`) |

Data: Yahoo Finance (prices, headlines), Nasdaq (earnings, fundamentals, options), CoinGecko (coin list),
SEC (press releases), US House Clerk (Congress). All free, unofficial where noted, and delayed.
Paper results use real market prices and modelled costs. Not financial advice.
