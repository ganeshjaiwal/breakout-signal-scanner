# breakout-signal-scanner

Telegram signal bot for NIFTY 50 and US stocks: scans for breakouts every 5 minutes,
checks each against 10,000 test runs on past prices, and sends day-trade and swing
signals with buy range, sell range, stop loss and confidence.

**breakout-signal-scanner** runs on a small Linux server and watches the NIFTY 50 (NSE)
and about 167 US stocks (NASDAQ-100 + S&P 100) on each market's own clock, holidays
included.

- **Day trades:** every 5 minutes it looks for opening-range breakouts, plans a stop and
  target, replays each plan 10,000 times on the stock's own last 60 days, and checks the news.
- **Swing trades:** after each close it looks for daily setups and tests each one on
  5 years of the stock's own history.
- **What you get:** only trades that pass every check reach Telegram, with buy range,
  sell range, stop loss, confidence and a position size for a cash budget. A report after
  each close shows how every signal ended.

Signals come from past prices: they are not a promise or financial advice, and trades can lose.

## What's in this repository

| Folder | What it is |
| --- | --- |
| [`autoscan/`](autoscan/) | The scanner and signal bot: `autoscan.py` (cron runner), `signals.py` (day trades), `swing.py` (swing trades), `signal_config.json` (all thresholds, budget and max loss), `lists/` (the stocks it watches) |
| [`autoscan/deploy/`](autoscan/deploy/) | `make-bundle.sh` and `install.sh` for a server, and the step-by-step [server guide](autoscan/deploy/SERVER.md) |
| [`day-trader-skill/`](day-trader-skill/) | The Claude skill set the bot is built on (scanner, entry timer, exit planner, time machine, news and more), extended for NSE stocks |

## Quick start

Needs Python 3.9 or newer, cron and internet access. No extra packages.

```bash
python3 autoscan/autoscan.py next                             # when each market scans next
python3 autoscan/autoscan.py signal --market US --symbols TXN  # dry run of the day-trade checks
python3 autoscan/autoscan.py swing --market NSE                # dry run of the swing checks
```

To run it all the time on a server with Telegram alerts, follow
[autoscan/deploy/SERVER.md](autoscan/deploy/SERVER.md). How the signals are chosen, and
every setting, is in [autoscan/README.md](autoscan/README.md).

Telegram settings live outside the repository, in `~/.config/day-trader/telegram.env`.

## Credits and licence

`day-trader-skill/` is based on the upstream
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill); its own
[LICENSE](day-trader-skill/LICENSE) applies to that folder.
