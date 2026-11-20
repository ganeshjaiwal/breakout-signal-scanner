# Run the autoscan on a server

The scanner uses Python's standard library only, so any small Linux server can
run it all the time. On a server Telegram is the only way to hear from it,
since there is no desktop for pop-ups.

What runs there, every day, on each market's own clock:

| | NIFTY 50 (IST) | US (New York time) |
| --- | --- | --- |
| daily setups, one Telegram message | 08:45 | 09:00 |
| opening-range breakouts, one message per new hit | every 15 min, 09:45-14:45 | every 15 min, 10:00-15:30 |
| end-of-day report | 15:45 | 16:15 |

Weekends and exchange holidays are skipped. The server's own time zone does not matter.

## 1. Get a server

Any of these works. Pick one near you (an India or Singapore region is fine for both markets).

- **Oracle Cloud Always Free**: free forever; an "Always Free" Ubuntu virtual machine
  (VM.Standard.E2.1.Micro or an Ampere A1). Sign-up asks for a card for verification.
- **Hetzner, DigitalOcean, AWS Lightsail**: the smallest Ubuntu plan, about $4-6 a month.
- **A Raspberry Pi at home**: free after the hardware. Yahoo blocks home connections less often
  than data-centre ones.

Choose **Ubuntu 22.04 or 24.04**. 1 CPU and 512 MB of memory is plenty.

Basic safety: log in with an SSH key, not a password; keep the system updated
(`sudo apt update && sudo apt upgrade -y`, or `sudo apt install -y unattended-upgrades`).
Never put broker or Alpaca keys on this server: it only scans.

## 2. Make a Telegram bot (5 minutes, on your phone)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. It replies with a **token**
   like `123456789:AA...`. Keep it secret.
2. Open your new bot and send it any message (`hi`).
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (your token in place of
   `<TOKEN>`). Find `"chat":{"id":` and copy the number after it: that is your **chat id**.

## 3. Install (on the server)

```bash
ssh USER@SERVER
```

```bash
sudo apt update && sudo apt install -y python3 tzdata cron git
```

```bash
git clone https://github.com/ganeshjaiwal/breakout-signal-scanner.git ~/breakout-signal-scanner
```

```bash
bash ~/breakout-signal-scanner/autoscan/deploy/install.sh
```

The first run creates `~/.config/day-trader/telegram.env` and installs the cron jobs.
Put your token and chat id in it:

```bash
nano ~/.config/day-trader/telegram.env
```

Then run the installer again. It checks Python and cron, runs a test scan of
both markets, sends you a Telegram test message and prints when each market
scans next:

```bash
bash ~/breakout-signal-scanner/autoscan/deploy/install.sh
```

Optional: your own budget and settings, kept out of git:

```bash
cp ~/breakout-signal-scanner/autoscan/signal_config.local.example.json ~/breakout-signal-scanner/autoscan/signal_config.local.json
```

## 4. Updates

The installer adds a second cron job that runs `deploy/update.sh` every day at
07:00 IST, when both markets are closed. It pulls `main` from GitHub, checks the
new code compiles and loads, and rolls back by itself if it doesn't. Its output
goes to `autoscan/update.log`. To update now, or just see what's waiting:

```bash
bash ~/breakout-signal-scanner/autoscan/deploy/update.sh
bash ~/breakout-signal-scanner/autoscan/deploy/update.sh --check
```

Don't edit tracked files on the server: a local change blocks updates. Put your
settings in `signal_config.local.json` instead.

**No git on the server?** Run `bash autoscan/deploy/make-bundle.sh` on your computer,
copy the `.tar.gz` over, unpack it and run `install.sh` from it. That copy does not
update itself: repeat the steps to update.

## 5. If you ran it on your computer before

Once the server's test message arrives, remove the cron job on your computer,
or every alert comes twice:

```bash
crontab -l | grep -v 'autoscan' | crontab -
```

## Everyday use (on the server)

```bash
cd ~/breakout-signal-scanner/autoscan
python3 autoscan.py next                    # when each market scans next
python3 autoscan.py summary                 # today's hits
python3 autoscan.py report --market NSE --print
tail -n 50 cron.log                         # errors land here
tail -n 30 data/us/log/$(date +%F).log
```

**Update:** automatic every morning; see step 4.

**Lists:** `autoscan/lists/nifty50.txt` and `us.txt` (change them in the repository), one symbol a line.
NSE changes the NIFTY 50 in March and September.

**Holidays:** NSE has not published 2027 yet. When it does, add the dates to
`NSE_HOLIDAYS` in `day-trader-skill/skills/trader-scanner/scan.py` and the other skill scripts, then push.

## If something goes wrong

- **No Telegram messages**: `python3 autoscan.py test-notify`. "sent to: nowhere" means the
  token or chat id is wrong, or you never messaged the bot.
- **Many "failed" charts, or HTTP 429 in the log**: Yahoo is rate-limiting the server's IP.
  Wait a day; if it keeps happening, try another provider or region, or a Raspberry Pi at home.
- **Nothing in the logs at all**: `systemctl status cron`, and check `crontab -l` shows the
  autoscan line.
- **Turn it off**: `crontab -l | grep -v 'autoscan' | crontab -`
