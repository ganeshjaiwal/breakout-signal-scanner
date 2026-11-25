# breakout-signal-scanner

Telegram signal bot for NIFTY 50 and US stocks: scans for breakouts every 5 minutes,
checks each against 10,000 test runs on past prices, and sends day-trade and swing
signals with buy range, sell range, stop loss and confidence.

> ⚠️ **Not financial advice.** Signals come from past prices. They are not a promise,
> trades can and will lose, and nothing here knows your finances. Most people who day
> trade lose money. Use it to learn, test with small amounts, and decide for yourself.

**Contributions are welcome!** Found a bug, have an idea, or want to add a market?
Open an issue or raise a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Contents

- [What it does](#what-it-does)
- [What a signal looks like](#what-a-signal-looks-like)
- [How signals are chosen](#how-signals-are-chosen)
- [Requirements](#requirements)
- [Quick start (try it on your computer)](#quick-start-try-it-on-your-computer)
- [Set up Telegram](#set-up-telegram)
- [Run it on a server (always on)](#run-it-on-a-server-always-on)
- [Configuration](#configuration)
- [Commands](#commands)
- [Repository layout](#repository-layout)
- [Data sources and limits](#data-sources-and-limits)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Credits and licence](#credits-and-licence)

## What it does

It watches two markets, each on its own clock and holiday calendar:

| Market | Stocks | Hours (local) | Day-trade scans | Swing run |
| --- | --- | --- | --- | --- |
| 🇮🇳 NSE | NIFTY 50 | 09:15–15:30 IST | every 5 min, 09:35–14:50 IST | 16:30 IST |
| 🇺🇸 US | NASDAQ-100 + S&P 100 (about 167) | 09:30–16:00 New York | every 5 min, 09:50–15:30 New York | 17:00 New York |

- **Day-trade signals:** opening-range breakouts, closed the same day (NSE by 15:15 IST,
  US by 15:55 New York time).
- **Swing signals:** daily-chart setups held up to 10 trading days.
- **Results:** a message when each swing trade ends, and an end-of-day report per market
  showing how every day-trade signal did.
- **Position size (optional):** whole shares for a cash budget and a maximum loss per trade.
- Weekends and exchange holidays are skipped automatically.

Only trades that pass every check are sent. On most days that is zero to two per market.

## What a signal looks like

A day-trade signal on Telegram:

```
🟢 BUY NOW: KOTAKBANK  🇮🇳 NSE · day trade

💰 Buy between: ₹490.60 – ₹492.54
🎯 Sell between: ₹494.51 – ₹495.50
🛑 Stop loss: ₹487.60
📊 Confidence: 48%

📦 Buy 4 shares (about ₹1,970)
📉 If the stop hits: lose about ₹20
📈 If the target hits: gain about ₹12
💸 Charges: about ₹2

⏰ Sell by 3:15 PM IST, even if neither price is hit.
🚫 Too late if the price is above ₹492.54.
📰 No major news today.
🔁 Past prices, 10,000 test runs: target first 48%, stop first 33%, neither 19%.

⚠️ Not a promise or advice. This trade can lose.
```

A swing signal:

```
🌱 SWING BUY: HINDALCO  🇮🇳 NSE · hold up to 10 days

💰 Buy between: ₹914.91 – ₹944.09
      at Mon 23 Nov open. Skip it if it opens outside this range.
🎯 Sell between: ₹984.22 – ₹995.16
🛑 Stop loss: ₹885.73
📊 Confidence: 36%

📦 Buy 2 shares (about ₹1,888) - choose Delivery (CNC)
📉 If the stop hits: lose about ₹117
📈 If the target hits: gain about ₹102
💸 Charges: about ₹20

⏰ If neither price is hit, sell at the close on Mon 7 Dec.
💡 Why: the price climbed back above its 20-day average.
📜 Last 5 years, 43 similar trades: ✅ 17 hit the target, ❌ 14 hit the stop, ⏳ 12 ran out of time.
🌙 The price can open below the stop overnight, so a loss can be bigger.

⚠️ Not a promise or advice. This trade can lose.
```

When a swing trade ends you get ✅ TARGET HIT, ❌ STOP LOSS HIT, ⏳ TIME UP or 🚫 CANCELLED
(the stock opened outside the buy range, so there was no trade), with the result in money.

## How signals are chosen

### Day trades (opening-range breakout)

The **opening range** is the high and low of the first 15 minutes. A **breakout** is the
first 5-minute candle that closes above that high. Every 5 minutes, each new breakout goes
through these steps:

1. **Entry check:** the breakout candle is finished, at most 20 minutes old, and the price
   is still inside the buy range (no chasing).
2. **Exit plan:** stop at the middle of the opening range; targets at 1×, 1.5× and 2× the
   risk (the distance from entry to stop).
3. **Time machine:** each plan is replayed 10,000 times using the stock's own 5-minute
   moves from the last 60 days, and checked against every past day the same rule fired.
4. **News:** headlines from the last 24 hours: Yahoo Finance, plus Google News for NSE
   stocks (only headlines that name the company). Each headline is read as good, bad or
   neither from its wording (downgrade, cuts target, misses, probe... vs upgrade, beats,
   raises guidance, wins order...). The signal is blocked by a trading halt, a "risk"
   headline (recall, bankruptcy, default, breach...), or more bad headlines than good ones.
   Otherwise the message says what it found. Swing picks get the same check.

A signal is sent only if **all** of these hold (defaults, all adjustable):

- confidence (share of replays where the target came before the stop) ≥ 35%
- the replays average at least +0.10× the risk per trade
- the same rule was not a loser on this stock over the last 60 days, across at least 12 trades
- the stop is at least 0.15% below the entry, so normal price noise doesn't trigger it

At most one signal per scan and two per market per day.

### Swing trades (daily candles)

Once a day after the close, every stock is checked for four setups:

| Setup | Rule |
| --- | --- |
| 20-day breakout | closed above every close of the previous 20 days |
| Bull flag | up more than 10% over 10 days, then 5 calm days within a 5% range |
| 20-day average reclaim | closed back above its 20-day average |
| Oversold bounce | RSI(14) was under 30 and the stock closed up |

Each setup gets a plan from the close **C** and the 14-day **average true range (ATR)**:
buy range C ± 0.5 ATR at the next open, stop C − 1.5 ATR, target 1.5, 2 or 3 times the risk,
and a time exit after 10 trading days. The exact plan is then backtested on that stock's
last 5 years and on the whole list. Confidence is the past target hit rate, blended with the
whole-list rate so that a stock with a few lucky trades cannot rank first. At most one swing
signal per market per day, and none while three swing trades are open.

**About confidence:** it is measured on past data, the same data used to pick the trade, so
real results will usually be somewhat worse. Treat it as "how often this worked before",
not "the chance it works now".

### AI check (learns from how signals ended)

`autoscan/ml.py` learns which signals tend to reach the target before the stop and, once it
has proven itself, also has to pass a signal before it goes to Telegram. It runs on its own:

1. **Collect.** Every day-trade candidate the time machine scores is saved, sent or not,
   with each target it tried (`data/<market>/signals/<day>/tried.jsonl`). Every swing run
   also saves each trade of its 5-year backtest (`data/ml/swing-<market>.jsonl.gz`).
2. **Label.** The daily `learn` step (NSE 17:05 IST, US 17:35 New York time) checks each
   saved day-trade candidate against that day's 5-minute bars: target, stop or time limit,
   and the result in R (`data/ml/day.jsonl`).
3. **Train.** When the data changed, it retrains a logistic regression in plain Python
   (no extra packages). Day trades also use the news: how many headlines, how many read good
   or bad, and which tags. It waits for enough data first: day trades 600 rows over 25 trading
   days with 80 target hits; swing 3,000 rows over 250 days with 400 target hits.
4. **Test.** It holds out the newest 25% of trading days. The model scores each trade as an
   expected result in R. It is switched on only if, on those held-out days, the trades it
   would keep beat all of them by at least +0.05R on average, stay positive, rank well
   (AUC ≥ 0.55) and number at least 30. For day trades it must also not do worse than the
   rules alone on the signals the rules pass.
5. **Use.** Until it passes, it only watches (scores are logged, nothing is blocked). Once
   it passes you get a 🤖 message on Telegram, and from then on a signal must pass the usual
   rules **and** the model, ranked by the model's expected R, with a 🤖 line in the message.
   It is re-tested on every retrain: if it stops passing, it switches off and tells you.

`python3 autoscan/autoscan.py ml status` shows how much data each model has and its latest
test. `ml learn` labels and retrains now. `"ml": {"mode": "shadow"}` keeps it watching only,
`"off"` stops it. The other thresholds are in `DEFAULTS` in `autoscan/ml.py`.

**About the AI check:** it learns from past prices and is tested on past prices it did not
train on. That is better than in-sample, but markets change. It can still be wrong.

## Requirements

- **Python 3.9 or newer.** Standard library only: there is nothing to `pip install`.
- **Linux or macOS** with `cron` for automatic runs. Windows works for manual runs and in WSL.
- **Internet access** to Yahoo Finance.
- **A Telegram account** for alerts (optional; Linux desktops also get pop-ups via `notify-send`).

## Quick start (try it on your computer)

```bash
git clone https://github.com/ganeshjaiwal/breakout-signal-scanner.git
cd breakout-signal-scanner/autoscan
```

See when each market scans next:

```bash
python3 autoscan.py next
```

Run the day-trade checks on a few stocks without sending anything:

```bash
python3 autoscan.py signal --market US --symbols AAPL,MSFT,NVDA
```

Run the swing checks on the whole NIFTY 50 without sending anything:

```bash
python3 autoscan.py swing --market NSE
```

Outside market hours the day-trade check will say the breakout is too old or the market
is closed. That is expected; the swing check works at any time.

## Set up Telegram

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the **token** it gives you.
2. Open your new bot and press **Start** (or send it any message).
3. Find your **chat ID**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser
   and copy the number after `"chat":{"id":`. Or message **@userinfobot**, which replies
   with your ID. Use only the number. A group's ID starts with `-`.
4. Put both in `~/.config/day-trader/telegram.env` (never in the repository):

   ```
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=123456789
   ```

   ```bash
   chmod 600 ~/.config/day-trader/telegram.env
   ```

5. Send a test message:

   ```bash
   python3 autoscan/autoscan.py test-notify
   ```

   It should print `sent to: telegram`. If not, the error says why (wrong token, wrong
   chat ID, or you never pressed Start).

## Run it on a server (always on)

Any small Linux server works: 1 CPU and 512 MB of memory is plenty. Options include
Oracle Cloud's free tier, a $4–6/month VPS, or a Raspberry Pi at home.

```bash
sudo apt update && sudo apt install -y python3 tzdata cron git
```

```bash
git clone https://github.com/ganeshjaiwal/breakout-signal-scanner.git ~/breakout-signal-scanner
```

```bash
bash ~/breakout-signal-scanner/autoscan/deploy/install.sh
```

The installer:

1. checks Python and cron
2. creates `~/.config/day-trader/telegram.env` for your token and chat ID (fill it in, then run the installer again)
3. adds a cron job that runs every 5 minutes
4. adds a daily update at 07:00 IST that pulls the latest code from GitHub
5. runs a test scan and sends a Telegram test message

**Automatic updates:** `deploy/update.sh` runs every day at 07:00 IST, when both markets
are closed. It fetches `main`, checks that the new code compiles and loads, and rolls back
on its own if anything fails. Your logs, results and personal settings are never touched.
To update right away:

```bash
bash ~/breakout-signal-scanner/autoscan/deploy/update.sh
```

The full server guide, with provider options and troubleshooting, is in
[autoscan/deploy/SERVER.md](autoscan/deploy/SERVER.md).

## Configuration

Settings live in two files in `autoscan/`:

| File | In git? | Use it for |
| --- | --- | --- |
| `signal_config.json` | yes | Shared defaults. Change these in a pull request. |
| `signal_config.local.json` | **no** | Your own settings: budget, thresholds. Overrides the shared file key by key. |

Start your local file from the example:

```bash
cp autoscan/signal_config.local.example.json autoscan/signal_config.local.json
```

### Position size

```json
{ "sizing": { "NSE": { "budget": 2000, "max_loss": 250 } } }
```

Each signal then shows whole shares that fit the budget (cash only, no margin) and keep the
loss at the stop under `max_loss`, both measured at the top of the buy range. Stocks whose
single share costs more than the budget are skipped. Amounts are in the market's currency;
add `"US": {...}` in dollars for US signals. Charges shown are estimates at typical Indian
discount-broker rates.

### Main settings

| Key | Default | Meaning |
| --- | --- | --- |
| `alerts` | `"signals"` | `"all"` also sends every breakout and the morning setups list |
| `eod_report` | `true` | one end-of-day report per market |
| `fresh_minutes` | `20` | a breakout older than this is skipped |
| `max_chase_r` | `0.25` | top of the buy range: entry + this × risk |
| `min_confidence_pct` | `35` | day trades: minimum share of replays reaching the target first |
| `min_sim_avg_r` | `0.10` | day trades: minimum average replay result, in units of risk |
| `min_history_trades` | `12` | day trades: minimum past trades of the same rule on the stock |
| `max_signals_per_day` | `2` | day trades, per market |
| `block_news_tags` | `["halt", "risk"]` | news tags that block a signal (also: earnings, analyst, legal, deal, capital, people, regulatory, macro) |
| `block_negative_news` | `true` | block when the day's headlines read more bad than good |
| `swing.enabled` | `true` | turn swing signals on or off |
| `swing.hold_days` | `10` | swing time exit, in trading days |
| `swing.min_confidence_pct` | `30` | swing: minimum blended target hit rate |
| `swing.min_avg_r` | `0.15` | swing: minimum blended average result |
| `swing.max_open` | `3` | swing: no new signal while this many are open |
| `ml.mode` | `"auto"` | AI check: `"auto"` blocks only once the model passes its test, `"shadow"` never blocks, `"off"` |

Every key, with a comment, is in `DEFAULTS` at the top of
[autoscan/signals.py](autoscan/signals.py), [autoscan/swing.py](autoscan/swing.py) and
[autoscan/ml.py](autoscan/ml.py).

### Stock lists

`autoscan/lists/nifty50.txt` and `autoscan/lists/us.txt`, one symbol per line. NSE
symbols are written without `.NS`. NSE changes the NIFTY 50 in March and September;
the current list is at https://archives.nseindia.com/content/indices/ind_nifty50list.csv

## Commands

Run from `autoscan/`:

| Command | What it does |
| --- | --- |
| `python3 autoscan.py tick` | What cron runs every 5 minutes: does whatever each market is due for |
| `python3 autoscan.py next` | When each market's next scans run |
| `python3 autoscan.py signal --market US --symbols AAPL,MSFT` | Day-trade checks, prints only |
| `python3 autoscan.py swing --market NSE` | Swing checks, prints only (`--send` sends for real) |
| `python3 autoscan.py run --market NSE --mode orb` | One breakout scan now |
| `python3 autoscan.py report --market US --print` | Today's end-of-day report, printed |
| `python3 autoscan.py summary` | Today's scan hits, both markets |
| `python3 autoscan.py test-notify` | Send a test message |
| `python3 autoscan.py tick --dry-run --now "2026-11-23 20:30"` | What would run at that IST time |

To stop it: `crontab -l | grep -v 'autoscan' | crontab -`

## Repository layout

```
breakout-signal-scanner/
├── autoscan/                      the signal bot
│   ├── autoscan.py                cron runner: schedule, scans, Telegram, reports
│   ├── signals.py                 day-trade pipeline and messages
│   ├── swing.py                   swing pipeline, backtest and messages
│   ├── signal_config.json         shared defaults
│   ├── signal_config.local.example.json
│   ├── lists/                     the stocks it watches
│   └── deploy/                    install.sh, update.sh, make-bundle.sh, SERVER.md
├── day-trader-skill/              the Claude skills the bot is built on
│   └── skills/                    scanner, entry, exit, timemachine, news, ...
├── CONTRIBUTING.md
└── LICENSE
```

`day-trader-skill/` is also a Claude Code plugin: each skill has a `SKILL.md` and can be
used on its own in Claude ("scan the market", "run the time machine", ...). The bot runs
the same scripts directly, without Claude.

Runtime files are created next to the code and ignored by git: `autoscan/data/` (logs,
scan results, signal records), `autoscan/cron.log` and `autoscan/update.log`.

## Data sources and limits

- **Prices:** Yahoo Finance's free chart endpoints. Unofficial, can lag by a few minutes,
  and can rate-limit busy or cloud IP addresses. Not for high-frequency use.
- **News:** Yahoo Finance headlines, plus Google News for NSE stocks. The good/bad reading
  is a word list, not a full understanding of the article. SEC filings are not checked.
- **Calendars:** NYSE holidays and early closes for 2026–2027, NSE holidays for 2026.
  Add the NSE 2027 list to `NSE_HOLIDAYS` in the skill scripts when NSE publishes it.
- **Not modelled:** your broker's real fills, slippage and exact charges; circuit limits;
  short selling (signals are buy-only).
- **Small budgets:** with a few thousand rupees, charges can eat most of the expected edge.
  The message shows estimated charges so you can judge.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `sent to: nowhere` | Run `test-notify`; the error says whether the token or the chat ID is wrong. Press Start in the bot's chat. |
| `HTTP 400 chat not found` | The chat ID must be only the number (no `id:` in front). |
| Many charts `failed`, or HTTP 429 | Yahoo is rate-limiting the IP. Wait, or use another server or a home connection. |
| No scans in the logs | `systemctl status cron`, and check `crontab -l` shows the autoscan line. |
| No signals for days | Normal: most breakouts fail the checks. See why in `autoscan/data/<market>/log/`. |
| Update didn't apply | Read `autoscan/update.log`; local edits to tracked files block updates (`git status`). |

## Contributing

Contributions of every size are welcome: bug reports, documentation fixes, new setups,
more markets, better tests. **Raise a pull request** and it will be reviewed.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. It explains how to set up, test
your change and open a PR.

Ideas that would help:

- automated tests with saved price data
- NSE 2027 holidays as soon as NSE publishes them
- more markets (other NSE indices, BSE, LSE, ...)
- short-selling signals
- a paper-trading log that records every signal's real outcome over months

## Credits and licence

- Released under the [MIT License](LICENSE). © 2026 Ganesh Jaiwal.
- `day-trader-skill/` is based on [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
  by Jake Schincariol (MIT, see [day-trader-skill/LICENSE](day-trader-skill/LICENSE)),
  extended here for NSE stocks.
- Market data from Yahoo Finance. This project is not affiliated with Yahoo, NSE, NYSE,
  Nasdaq or Telegram.
