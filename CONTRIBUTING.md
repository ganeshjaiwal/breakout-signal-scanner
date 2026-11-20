# Contributing to breakout-signal-scanner

Thanks for wanting to help! Every contribution is welcome: a typo fix, a bug report, a
new setup, a new market. You don't need to be a trading expert or a Python expert.

**The short version:** fork the repository, make your change on a branch, check it with
the commands below, and open a pull request against `main`.

## Ways to help

- **Report a bug:** open an issue with what you ran, what you expected and what happened.
  Paste the error and the relevant lines of `autoscan/data/<market>/log/`.
- **Suggest an idea:** open an issue describing it before writing a lot of code, so we can
  agree on the approach.
- **Fix docs:** unclear sentence, missing step, wrong example: just open a PR.
- **Write code:** pick an open issue, or one of the ideas at the end of the README.

## Before you start

- **Never commit secrets:** no Telegram token, chat ID, broker key, server address or
  personal email. Telegram settings live in `~/.config/day-trader/telegram.env`, outside
  the repo. Your own settings go in `autoscan/signal_config.local.json`, which git ignores.
- **Standard library only:** the project runs on any server with plain Python 3.9+. Please
  don't add `pip` packages. If you think one is really needed, open an issue first.
- **Keep both markets working:** a change for NSE must not change US behaviour, and the
  other way round, unless that is the point of the change.
- **Be honest in messages:** signal messages must never promise profit. Keep the
  "not a promise or advice" line, and don't use words like "guaranteed" or "sure win".

## Set up

1. Fork the repository on GitHub, then clone your fork:

   ```bash
   git clone https://github.com/<your-username>/breakout-signal-scanner.git
   cd breakout-signal-scanner
   ```

2. Add the original repository as `upstream`, to stay up to date:

   ```bash
   git remote add upstream https://github.com/ganeshjaiwal/breakout-signal-scanner.git
   ```

3. Create a branch for your change:

   ```bash
   git checkout -b fix/short-description
   ```

   Branch names: `fix/...` for bugs, `feat/...` for new features, `docs/...` for docs.

There is nothing to install. For Telegram while testing, use your own bot (see the
README); without it, everything still prints to the terminal.

## Check your change

There is no automated test suite yet (adding one is a great first contribution). Until
then, please run these from the repository root and make sure none of them fail:

1. Everything compiles:

   ```bash
   python3 -m py_compile autoscan/*.py day-trader-skill/skills/*/*.py
   ```

2. The schedule still works for both markets:

   ```bash
   python3 autoscan/autoscan.py tick --dry-run --now "2026-11-23 10:00"
   python3 autoscan/autoscan.py tick --dry-run --now "2026-11-23 20:30"
   python3 autoscan/autoscan.py next
   ```

3. The day-trade and swing pipelines still run (they print, and send nothing):

   ```bash
   python3 autoscan/autoscan.py signal --market US --symbols AAPL,MSFT --fresh-minutes 600
   python3 autoscan/autoscan.py signal --market NSE --symbols RELIANCE,TCS --fresh-minutes 600
   python3 autoscan/autoscan.py swing --market NSE
   python3 autoscan/autoscan.py swing --market US
   ```

4. If you changed a message, paste a before and after in the pull request.

5. If you changed how signals are picked, explain the effect in the PR: for example, how
   many signals the swing run produces before and after on the same day.

## Code style

- Python 3.9+, standard library only, 4-space indents, lines under about 120 characters.
- Small functions with a short docstring saying what goes in and what comes out.
- Plain names (`budget`, `max_loss`, `stop`), no invented abbreviations.
- Settings belong in `DEFAULTS` (in `signals.py` or `swing.py`) with a comment, not as
  numbers buried in the code.
- User-facing text: short, plain words, a number with its unit or currency.

## Commit messages

One short line saying what changed, then a blank line and the why if it isn't obvious:

```
Fix NSE swing exit date when a holiday falls in the hold window

The exit date skipped weekends but not NSE holidays, so the message
named a day the market was closed.
```

## Open a pull request

1. Bring your branch up to date:

   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. Push your branch to your fork:

   ```bash
   git push -u origin fix/short-description
   ```

3. Open a pull request against `main` on GitHub and fill in the template.

Keep a PR to one topic: two unrelated fixes are easier to review as two PRs. A maintainer
will review it, may ask for changes, and will merge it once it's ready. Small PRs are
usually reviewed fastest.

## Data and markets

- Prices come from Yahoo Finance's free endpoints. Please keep requests polite: no
  tighter loops than the existing pauses, no parallel hammering.
- When NSE or NYSE publishes a new holiday list, update the holiday tables in every
  skill script that has one (search for `NSE_HOLIDAYS` or `NYSE_HOLIDAYS`) and say in the
  PR where the list came from.
- The NIFTY 50 list changes twice a year; update `autoscan/lists/nifty50.txt` from
  NSE's official CSV and mention the date in the PR.

## Be kind

Be respectful and patient in issues and reviews. Everyone was new once. Criticise code,
not people.

By contributing, you agree that your contribution is released under the project's
[MIT License](LICENSE).
