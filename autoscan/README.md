# Day trader autoscan

Automatic scans of the NIFTY 50 (NSE) and about 167 US stocks (NASDAQ-100 + S&P 100),
each inside its own market's trade window. Built on the day-trader-skill scanner
(`../day-trader-skill/skills/trader-scanner/scan.py`).

A scan is a list of charts that match a rule, not a list of stocks to buy.

## Schedule

cron runs `autoscan.py tick` every 5 minutes. Each tick checks both
markets on their own clock:

| market | daily setups (once) | opening-range breakout scan | same window in IST |
| --- | --- | --- | --- |
| NIFTY 50 | 08:45 IST | every 5 min, 09:35-14:50 IST | same |
| US | 09:00 ET | every 5 min, 09:50-15:30 ET | 19:30 / 20:30-02:00 (winter), 18:30 / 19:30-01:00 (summer) |

Weekends and exchange holidays (NSE 2026, NYSE 2026-2027) are skipped. NSE has not
published its 2027 holiday list yet: add it to the scanner's `NSE_HOLIDAYS` when it does.

After the close each market also runs `swing` (NSE 16:30 IST, US 17:00 ET) and `learn`
(NSE 17:05 IST, US 17:35 ET: labels the day's candidates and retrains the AI check, see `ml.py`).

By default only trade signals and one end-of-day report per market are sent.

## Trade signals (the only alerts by default)

Every 5 minutes in the entry window, each new breakout goes through the skills in order:
entry check -> exit plan (stop at the range midpoint; targets 1R, 1.5R, 2R) -> time machine
(10,000 replays + the last 60 days of the same rule) -> news. Only a breakout that passes
every check in `signal_config.json` is sent, at most one per scan and 2 per market per day:

- still fresh (bar closed at most 20 minutes ago) and the price is inside the buy range
- confidence (replays where the target came before the stop and the flat-by time) at least 35%
- replay average at least +0.10R, and the rule's real average over the last 60 days not negative
  over at least 12 trades
- no trading-halt or "risk" news, and not more bad headlines than good (Yahoo, plus Google
  News for NSE stocks); swing picks get the same check

The message gives the buy range, sell range, stop loss and confidence. Confidence is a
number from replays of past prices, not a promise: signals will lose sometimes. The
end-of-day report shows how each signal ended, which is the real track record.

Position size: `"sizing": {"NSE": {"budget": 2000, "max_loss": 250}}` in `signal_config.local.json` (your own file, not in git)
adds a quantity line to NSE signals: whole shares, cash only, sized at the top of the buy
range so that the cost fits the budget and the loss at the stop fits the max loss. A stock
whose single share costs more than the budget is skipped. Charges shown are an estimate at
typical discount-broker rates. US signals carry no quantity (add a `"US"` entry in dollars).

`"alerts": "all"` in `signal_config.json` brings back every breakout and the daily setups.
Dry run, sends nothing: `python3 autoscan.py signal --market US --symbols TXN,CVX`

## Swing signals (held for days)

Once a day after each close (NSE 16:30 IST, US 17:00 New York time) `swing.py` looks for
daily-candle setups (20-day closing-high breakout, bull flag, reclaimed the 20-day average,
oversold bounce), plans each from the close C and the 14-day average true range (ATR):
buy range C +/- 0.5 ATR at the next open, stop C - 1.5 ATR, target 1.5, 2 or 3 times the
risk, time exit after 10 sessions. It backtests that exact plan on the stock's own last
5 years and across the whole list, and sends at most one signal per market per day, and
none while 3 swing trades are open. A second message says how each trade ended (target,
stop, time exit, or cancelled because the open was outside the buy range).

Confidence is the past hit rate of the target, pulled toward the whole-list rate so a
lucky handful of trades cannot top the ranking. It is in-sample history, not a promise.
An overnight gap can jump past a stop. Settings: the `"swing"` block of
`signal_config.json` (`"enabled": false` turns it off).
Dry run: `python3 autoscan.py swing --market NSE` (add `--send` to really send).

## Notifications

- Desktop: `notify-send`, works while you are logged in.
- Telegram: fill in `~/.config/day-trader/telegram.env` (instructions inside the file),
  then run `python3 autoscan.py test-notify`.

## Commands

```bash
python3 autoscan.py tick                                  # what cron runs
python3 autoscan.py tick --dry-run --now "2026-11-13 20:30"  # what would run at an IST time
python3 autoscan.py run --market NSE --mode orb           # one scan now
python3 autoscan.py summary                               # today's hits
python3 autoscan.py test-notify
python3 autoscan.py ml status                             # AI check: data collected, latest test
python3 autoscan.py ml learn                              # label and retrain now
```

## Files

- `lists/nifty50.txt`, `lists/us.txt`: the symbols, one a line. Edit freely. The NIFTY 50
  list changes in March and September: refresh it from
  https://archives.nseindia.com/content/indices/ind_nifty50list.csv
- `data/<market>/log/DATE.log`: what each run did
- `data/<market>/state-DATE.json`: runs, hits, what was already notified
- `data/<market>/results/DATE/HHMM-mode.json`: full scanner output
- `data/<market>/signals/DATE/tried.jsonl`: every scored day-trade candidate, for learning
- `data/ml/`: learning rows (`day.jsonl`, `swing-<market>.jsonl.gz`), models
  (`model-day.json`, `model-swing.json`) and every training's test result (`history.jsonl`)
- `cron.log`: cron's own output (errors land here)


## Turn it off

```bash
crontab -l | grep -v 'autoscan' | crontab -
```
