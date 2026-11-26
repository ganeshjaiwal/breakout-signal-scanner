#!/usr/bin/env python3
"""Automatic scans for NIFTY 50 (NSE), US stocks and forex, inside each market's trade window.

cron runs `autoscan.py tick` every 15 minutes. Each tick checks both markets on
their own clock and runs, per market:

  daily  once before the open    NSE 08:45-09:05 IST   US 09:00-09:15 New York
  orb    in the entry window     NSE 09:45-14:50 IST   US 10:00-15:30 New York
  report once after the close    NSE 15:45-16:15 IST   US 16:15-16:45 New York
  swing  once after the report   NSE 16:30-17:00 IST   US 17:00-17:30 New York
  learn  once after the swing    NSE 17:05-17:30 IST   US 17:35-18:00 New York
         (label today's candidates with how they ended, retrain the models: ml.py)

Forex (FX, the pairs in lists/forex.txt, London time; forex.py does the checks):
  orb    every 5 minutes 08:15-14:45 London   London-open breakouts, BUY or SELL
  report once 16:00-16:30 London
  swing  once 01:15-01:45 London, on the day's completed daily bar (UTC day)
  No pre-market scan and no learned (AI) check for forex.

Weekends and exchange holidays are skipped (the scanner's own calendars).
New hits are sent once each, as a desktop notification and/or a Telegram
message. Results and a log are kept under data/<market>/.

A scan is a list of charts that match a rule, not a list of stocks to buy.

Usage:
  autoscan.py tick                          # what cron runs
  autoscan.py run --market NSE --mode orb   # one scan now, whatever the clock says
  autoscan.py summary [--date YYYY-MM-DD]   # today's hits, both markets
  autoscan.py report --market NSE [--date]  # send the end-of-day report now
  autoscan.py test-notify                   # send a test notification
  autoscan.py ml status                     # the learned models: data collected, test results
  autoscan.py ml learn [--market NSE]       # label and retrain now
"""
import argparse
import datetime
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forex  # noqa: E402
import ml  # noqa: E402
import signals  # noqa: E402
import swing  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.environ.get("AUTOSCAN_SCANNER") or os.path.join(
    HERE, "..", "day-trader-skill", "skills", "trader-scanner", "scan.py")
TELEGRAM_ENV = os.path.expanduser("~/.config/day-trader/telegram.env")
DATA = os.environ.get("AUTOSCAN_DATA") or os.path.join(HERE, "data")
NO_NOTIFY = False

MARKETS = {
    "NSE": {"tz": ZoneInfo("Asia/Kolkata"), "label": "IST", "list": "lists/nifty50.txt", "suffix": ".NS",
            "daily": ("08:45", "09:05"), "orb": ("09:35", "14:50"), "report": ("15:45", "16:15"), "swing": ("16:30", "17:00"),
            "learn": ("17:05", "17:30"), "name": "NIFTY 50"},
    "US": {"tz": ZoneInfo("America/New_York"), "label": "ET", "list": "lists/us.txt", "suffix": "",
           "daily": ("09:00", "09:15"), "orb": ("09:50", "15:30"), "report": ("16:15", "16:45"), "swing": ("17:00", "17:30"),
           "learn": ("17:35", "18:00"), "name": "US (NDX 100 + S&P 100)"},
    # Forex: no daily or learn step. The swing run waits for UTC midnight (01:15 London is
    # 00:15 or 01:15 UTC), when Yahoo's daily forex bar for the day before is complete.
    # "history" downloads past sessions from Dukascopy a few minutes per tick in the evening.
    "FX": {"tz": forex.LONDON, "label": "London", "list": "lists/forex.txt", "suffix": "=X",
           "orb": ("08:15", "14:45"), "report": ("16:00", "16:30"), "history": ("17:00", "23:30"),
           "swing": ("01:15", "01:45"), "name": "Forex"},
}
MODES = ("daily", "orb", "report", "history", "swing", "learn")
FX_HISTORY_BUDGET_S = 200   # per tick, under cron's 5 minutes
FX_HISTORY_PAUSE_MIN = 30   # after the feed stops answering
SETUP_LABEL = {"breakout_20d": "20-day closing high", "bull_flag": "bull flag", "reclaim_ma20": "reclaimed 20-day avg",
               "oversold": "oversold (RSI<30)", "oversold_bounce": "oversold bounce", "orb": "opening-range breakout"}


def hhmm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def load_scanner():
    spec = importlib.util.spec_from_file_location("autoscan_scanmod", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def trading_day(market, day):
    """(ok, reason) from the scanner's own NYSE / NSE calendar, or forex's (weekdays)."""
    if market == "FX":
        return forex.trading_day(day)
    mod = load_scanner()
    mod.use_market("NSE" if market == "NSE" else "NYSE")
    return mod.trading_day(day)


def symbols(market):
    m = MARKETS[market]
    out = []
    with open(os.path.join(HERE, m["list"])) as fh:
        for line in fh:
            s = line.split("#", 1)[0].strip().upper()
            if s:
                out.append(s if s.endswith(m["suffix"]) or not m["suffix"] else s + m["suffix"])
    return out


def market_dir(market):
    d = os.path.join(DATA, market.lower())
    os.makedirs(os.path.join(d, "trader"), exist_ok=True)
    os.makedirs(os.path.join(d, "log"), exist_ok=True)
    return d


def log(market, day, text):
    stamp = datetime.datetime.now(MARKETS[market]["tz"]).strftime("%H:%M:%S")
    line = "%s %s %s" % (stamp, MARKETS[market]["label"], text)
    with open(os.path.join(market_dir(market), "log", "%s.log" % day.isoformat()), "a") as fh:
        fh.write(line + "\n")
    print("[%s] %s" % (market, line))


def state_path(market, day):
    return os.path.join(market_dir(market), "state-%s.json" % day.isoformat())


def load_state(market, day):
    try:
        with open(state_path(market, day)) as fh:
            doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(market, day, doc):
    tmp = state_path(market, day) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, state_path(market, day))


# ---------------------------------------------------------------- notifications
def telegram_conf():
    conf = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        with open(TELEGRAM_ENV) as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() in conf and not conf[k.strip()]:
                        conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf if all(conf.values()) else None


def telegram_reason(e):
    """Telegram's own reason (its JSON 'description'), never the URL, which holds the token."""
    code = getattr(e, "code", None)
    desc = ""
    try:
        desc = json.loads(e.read().decode("utf-8", "replace")).get("description", "")
    except Exception:
        pass
    hint = {401: "the bot token is wrong (copy it again from @BotFather, no spaces or quotes)",
            404: "the bot token is wrong or incomplete",
            400: "the chat id is wrong, or you never pressed Start in the bot's chat",
            403: "the bot cannot message this chat: press Start in the bot's chat, or unblock it"}.get(code, "")
    if code is None:
        return "%s (network problem?)" % type(e).__name__
    return "HTTP %s %s%s" % (code, desc, (" - " + hint) if hint else "")


def notify(title, body):
    sent = []
    if NO_NOTIFY:
        return sent
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % os.getuid())
    env.setdefault("DISPLAY", ":0")
    try:
        r = subprocess.run(["notify-send", "-a", "Day trader autoscan", title, body], env=env,
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            sent.append("desktop")
    except (OSError, subprocess.SubprocessError):
        pass
    tg = telegram_conf()
    if tg:
        data = urllib.parse.urlencode({"chat_id": tg["TELEGRAM_CHAT_ID"], "text": "%s\n%s" % (title, body)}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % tg["TELEGRAM_BOT_TOKEN"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as resp:
                if resp.status == 200:
                    sent.append("telegram")
        except Exception as e:  # never print the URL: it holds the token
            print("telegram failed: %s" % telegram_reason(e), file=sys.stderr)
    return sent


# ---------------------------------------------------------------- scans
def run_scan(market, mode, day):
    """Run scan.py once. Returns (doc or None, message)."""
    d = market_dir(market)
    cmd = [sys.executable, SCANNER, mode, "--symbols", ",".join(symbols(market)), "--json", "--quiet"]
    if mode == "orb":
        cmd += ["--max-confirm", "0"]
    if market == "NSE":
        cmd += ["--min-volume", "0"]   # every NIFTY 50 stock is liquid; 1,000,000 shares would drop MARUTI and other high-priced names
    try:
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "scan timed out after 10 minutes"
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        msg = (r.stdout.strip() or r.stderr.strip() or "no output").splitlines()
        return None, msg[-1] if msg else "no output"
    stamp = datetime.datetime.now(MARKETS[market]["tz"]).strftime("%H%M")
    rdir = os.path.join(d, "results", day.isoformat())
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "%s-%s.json" % (stamp, mode)), "w") as fh:
        json.dump(doc, fh, indent=2)
    return doc, ""


def show(sym):
    if sym.endswith("=X"):
        return forex.shown(sym)
    return sym[:-3] if sym.endswith(".NS") else sym


def do_scan(market, mode, day, force=False):
    m = MARKETS[market]
    st = load_state(market, day)
    if mode == "daily" and st.get("daily_done") and not force:
        return
    if mode == "swing":
        if not st.get("swing_done") or force:
            run_swing(market, day)
        return
    if mode == "learn":
        if not st.get("learn_done") or force:
            run_learn(market, day)
        return
    if mode == "report":
        if (not st.get("report_done") and signals.load_config()["eod_report"]) or force:
            send_report(market, day)
        return
    if market == "FX":
        if mode == "orb":
            fx_orb(day)
        elif mode == "history":
            fx_history(day)
        return
    doc, msg = run_scan(market, mode, day)
    if doc is None:
        log(market, day, "%s: %s" % (mode, msg))
        st = load_state(market, day)
        st.setdefault("notes", []).append({"mode": mode, "at": datetime.datetime.now(m["tz"]).strftime("%H:%M"),
                                           "msg": msg})
        save_state(market, day, st)
        return
    hits = doc.get("hits") or []
    c = doc.get("counts") or {}
    log(market, day, "%s: %d hits; %s scanned of %s, %s failed, %ss" % (
        mode, len(hits), doc.get("scanned"), doc.get("universe"), doc.get("failed"), c.get("seconds", doc.get("seconds"))))
    seen = set(st.get("notified", []))
    cfg = signals.load_config()
    loud = cfg["alerts"] == "all"
    if mode == "daily":
        st["daily_done"] = True
        if hits and loud:
            lines = ["%s %s - %s" % (show(h["symbol"]), h["price"], ", ".join(SETUP_LABEL.get(t, t) for t in h["setups"]))
                     for h in hits[:25]]
            more = "\n+%d more" % (len(hits) - 25) if len(hits) > 25 else ""
            sent = notify("%s pre-market setups (%d)" % (m["name"], len(hits)), "\n".join(lines) + more)
            log(market, day, "daily summary %s" % ("not sent (test run)" if NO_NOTIFY else
                                                   "sent: %s" % (", ".join(sent) or "nowhere")))
    else:
        new = [h for h in hits if "orb:" + h["symbol"] not in seen]
        for h in new:
            seen.add("orb:" + h["symbol"])
        sent_today = st.get("signals", [])
        if new and len(sent_today) < cfg["max_signals_per_day"]:
            sig = signals.pick(list(reversed(new)), market, os.path.join(market_dir(market), "signals", day.isoformat()),
                               cfg, log=lambda t: log(market, day, t))
            if sig:
                title, body = signals.message(market, m["label"], show(sig["symbol"]), sig, sig.get("note"))
                sent = notify(title, body)
                now_l = datetime.datetime.now(m["tz"])
                sig["sent_at"] = now_l.strftime("%H:%M")
                sig["bar_after"] = "%02d:%02d" % (now_l.hour, now_l.minute - now_l.minute % 5)
                sig.pop("tried", None)
                sent_today.append(sig)
                st["signals"] = sent_today
                log(market, day, "SIGNAL %s buy %s-%s sell %s-%s stop %s confidence %.0f%%, sent: %s" % (
                    show(sig["symbol"]), sig["buy_lo"], sig["buy_hi"], sig["sell_lo"], sig["sell_hi"], sig["stop"],
                    sig["p_target"] * 100, ", ".join(sent) or "nowhere"))
        for h in (new if loud else []):
            body = "%s closed %s at %s %s, %.2f%% over the range high %s (range low %s)." % (
                show(h["symbol"]), h["price"], h["at"], m["label"], h["pct_over"], h["or_high"], h["or_low"])
            sent = notify("%s ORB: %s" % (m["name"], show(h["symbol"])), body)
            log(market, day, "new ORB %s @ %s %s, sent: %s" % (show(h["symbol"]), h["price"], h["at"],
                                                             ", ".join(sent) or "nowhere"))
    st["notified"] = sorted(seen)
    st.setdefault("runs", []).append({"mode": mode, "at": datetime.datetime.now(m["tz"]).strftime("%H:%M"),
                                      "hits": [h["symbol"] for h in hits]})
    save_state(market, day, st)


def fx_history(day):
    """Download missing Dukascopy sessions for a few minutes; again on the next tick until
    nothing is missing. After the feed stops answering, wait FX_HISTORY_PAUSE_MIN minutes."""
    fx = forex.config(signals.load_config())
    st = load_state("FX", day)
    if st.get("history_done") or not fx["dukascopy"]:
        return
    now = datetime.datetime.now(forex.LONDON)
    wait = st.get("history_wait_until")
    if wait and now.strftime("%H:%M") < wait:
        return
    res = forex.fill_history(symbols("FX"), fx, budget_s=FX_HISTORY_BUDGET_S, log=lambda t: None)
    st = load_state("FX", day)
    log("FX", day, "history: %d sessions downloaded from Dukascopy, %d still missing%s" % (
        res["fetched"], res["missing"], (", stopped: " + res["stopped"]) if res["stopped"] else ""))
    if not res["missing"]:
        st["history_done"] = True
    elif res["stopped"] and res["stopped"] != "out of time":
        st["history_wait_until"] = (now + datetime.timedelta(minutes=FX_HISTORY_PAUSE_MIN)).strftime("%H:%M")
    save_state("FX", day, st)


def fx_orb(day, now=None, dry=False):
    """One forex day-trade scan: every pair's London-open breakout, at most one signal."""
    cfg = signals.load_config()
    fx = forex.config(cfg)
    now = now or datetime.datetime.now(forex.LONDON)
    say = print if dry else (lambda t: log("FX", day, t))
    st = {} if dry else load_state("FX", day)
    sent_today = st.get("signals", [])
    if len(sent_today) >= fx["max_signals_per_day"]:
        return None
    seen = set(st.get("notified", []))
    sig = forex.scan(symbols("FX"), cfg, now, taken=sent_today, log=say, seen=seen)
    if sig:
        sig["day"] = day.isoformat()
        sig["sent_at"] = now.strftime("%H:%M")
        sig["bar_after"] = "%02d:%02d" % (now.hour, now.minute - now.minute % 5)
        sig.pop("tried", None)
        title, body = forex.message(sig)
        if dry:
            print("\n" + title + "\n" + body)
            return sig
        sent = notify(title, body)
        sent_today.append(sig)
        st["signals"] = sent_today
        log("FX", day, "SIGNAL %s %s enter %s-%s stop %s target %s confidence %.0f%%, sent: %s" % (
            sig["side"].upper(), show(sig["symbol"]), forex.px(sig["symbol"], sig["enter_lo"]),
            forex.px(sig["symbol"], sig["enter_hi"]), forex.px(sig["symbol"], sig["stop"]),
            forex.px(sig["symbol"], sig["target"]), sig["p_target"] * 100, ", ".join(sent) or "nowhere"))
    if dry:
        return sig
    st["notified"] = sorted(seen)
    st.setdefault("runs", []).append({"mode": "orb", "at": now.strftime("%H:%M"),
                                      "hits": [k.split("@")[0] for k in sorted(seen)]})
    save_state("FX", day, st)
    return sig


def run_swing(market, day, dry=False):
    """The once-a-day swing run: close out open swing trades, then send at most
    max_signals_per_day new ones. dry=True prints and changes nothing."""
    m = MARKETS[market]
    cfg = signals.load_config()
    sc = swing.config(cfg)
    say = print if dry else (lambda t: log(market, day, t))
    if not sc["enabled"]:
        return
    fx_mkt = market == "FX"
    if fx_mkt:
        mod = forex.SwingClock()
    else:
        mod = load_scanner()
        mod.use_market("NSE" if market == "NSE" else "NYSE")
    now = swing.utcnow()
    path = os.path.join(market_dir(market), "swing.json")
    book = swing.load_book(path)
    still, closed = swing.update_open(mod, book.get("open", []), sc, now)
    for p in closed:
        title, body = (forex.swing_closed_message(p) if fx_mkt else
                       swing.closed_message(market, show(p["symbol"]), p))
        if dry:
            print(title + "\n" + body)
        else:
            sent = notify(title, body)
            log(market, day, "swing closed %s: %s %+.2fR, sent: %s" % (show(p["symbol"]), p["outcome"], p["R"],
                                                                      ", ".join(sent) or "nowhere"))
    rows = [] if ml.config(cfg)["mode"] != "off" and not fx_mkt else None   # no learned check for forex
    live, pool, as_of, failed = swing.scan(mod, symbols(market), sc, now, say, market=market, rows=rows)
    if rows:
        ml.write_swing_rows(market, rows)
        say("learn: %d past swing trades saved for training" % len(rows))
    say("swing: %d setups on the %s close; %d of %d charts failed or too short" % (
        len(live), as_of, failed, len(symbols(market))))
    picks = []
    if len(still) < sc["max_open"]:
        size = forex.config(cfg)["sizing"] if fx_mkt else (cfg.get("sizing") or {}).get(market)
        ranked = swing.choose(live, pool, sc, size, skip={p["symbol"] for p in still}, log=say,
                              cfg=None if fx_mkt else cfg, market=market)
        room = min(sc["max_signals_per_day"], max(0, sc["max_open"] - len(still)))
        ndir = os.path.join(market_dir(market), "swing-news")
        os.makedirs(ndir, exist_ok=True)
        for s in ranked:   # best first; the news check runs only until there are enough picks
            if len(picks) >= room:
                break
            if fx_mkt:   # forex: no company news; name the big releases coming up
                blocked, note = "", forex.calendar_soon(s["symbol"], datetime.datetime.now(forex.UTC), 2,
                                                        forex.config(cfg))
            else:
                blocked, note = signals.news_check(s["symbol"], ndir, cfg, market)
            if blocked:
                say("swing %s %s dropped: %s" % (s["symbol"], s["setup"], blocked))
                continue
            s["note"] = note
            picks.append(s)
    else:
        say("swing: %d trades already open (limit %d), no new signal" % (len(still), sc["max_open"]))
    for s in picks:
        title, body = (forex.swing_message(s, sc["hold_days"], s.get("note")) if fx_mkt else
                       swing.message(market, m["label"], show(s["symbol"]), s, sc["hold_days"], mod, s.get("note")))
        if dry:
            print("\n" + title + "\n" + body)
            continue
        sent = notify(title, body)
        log(market, day, "SWING SIGNAL %s %s buy %s-%s stop %s target %s confidence %.0f%%, sent: %s" % (
            show(s["symbol"]), s["setup"], s["buy_lo"], s["buy_hi"], s["stop"], s["target"], s["conf"] * 100,
            ", ".join(sent) or "nowhere"))
        still.append(s)
    if not picks:
        say("swing: no signal today")
    if dry:
        return
    book["open"] = still
    book["closed"] = (book.get("closed", []) + closed)[-200:]
    swing.save_book(path, book)
    st = load_state(market, day)
    st["swing_done"] = True
    save_state(market, day, st)


def run_learn(market, day, send=True, say=None):
    """Label this market's day-trade candidates with how they ended, then retrain every model
    whose data changed. Tells you on Telegram when a model starts or stops checking signals."""
    cfg = signals.load_config()
    say = say or (lambda t: log(market, day, t))
    if ml.config(cfg)["mode"] != "off":
        mod = load_scanner()
        mod.use_market("NSE" if market == "NSE" else "NYSE")
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        os.makedirs(ml.ML_DIR, exist_ok=True)
        with open(os.path.join(ml.ML_DIR, ".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            ml.label_days(mod, market, os.path.join(market_dir(market), "signals"),
                          lambda t, d, n, bars: signals.outcome(mod, t, d, n, bars), now, log=say)
            for kind, st, changed in ml.learn(cfg, log=say):
                if not changed or not send:
                    continue
                name = {"day": "day-trade", "swing": "swing"}[kind]
                if st["ready"]:
                    mt = st["metrics"]
                    title = "🤖 AI check is now ON for %s signals" % name
                    body = ("It learned from %d past trades and passed its test on the newest %d.\n"
                            "On those, the trades it would keep averaged %+.2fR vs %+.2fR for all of them.\n"
                            "From now on a %s signal is sent only if the model also expects %+.2fR or more.\n\n"
                            "⚠️ Learned from past prices. Not a promise or advice." % (
                                mt["train_rows"], mt["test_rows"], mt["test_avg_r_kept"], mt["test_avg_r_all"], name,
                                st["threshold"]))
                else:
                    title = "🤖 AI check is now OFF for %s signals" % name
                    body = ("Its latest test on the newest trades did not pass (%s).\n"
                            "Signals go out on the usual rules alone while it keeps learning." % st["status"])
                sent = notify(title, body)
                say("learn: %s, sent: %s" % (title, ", ".join(sent) or "nowhere"))
    if send:
        st = load_state(market, day)
        st["learn_done"] = True
        save_state(market, day, st)


def fx_report_text(day):
    st = load_state("FX", day)
    lines = ["📋 %s Forex · end of day %s" % (forex.FLAG, day.strftime("%a %-d %b")), ""]
    sigs = st.get("signals", [])
    if sigs:
        lines.append("🟢 Day-trade signals today: %d" % len(sigs))
        lines += forex.report_lines(sigs, datetime.datetime.now(forex.LONDON))
    else:
        lines.append("🟢 Day-trade signals today: none (nothing passed every check)")
    seen = st.get("notified", [])
    lines.append("🔎 Breakouts checked: %s" % (", ".join("%s at %s" % (show(k.split("@")[0]), k.split("@")[1])
                                                         for k in seen) if seen else "none"))
    runs = st.get("runs", [])
    lines.append("⚙️ Scanner: %d scans ran" % len(runs))
    lines += ["", "⚠️ Signals come from past prices. Not a promise or advice."]
    return "\n".join(lines)


def report_text(market, day):
    if market == "FX":
        return fx_report_text(day)
    m = MARKETS[market]
    st = load_state(market, day)
    lines = ["📋 %s %s · end of day %s" % (signals.FLAG.get(market, ""), market, day.strftime("%a %-d %b")), ""]
    sigs = st.get("signals", [])
    if sigs:
        mod = load_scanner()
        mod.use_market("NSE" if market == "NSE" else "NYSE")
        total = 0.0
        for g in sigs:
            res, r = signals.outcome(mod, g, day, datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
            total += r or 0.0
            word = {"target": "✅ target hit", "stop": "❌ stop hit", "flat": "⏰ sold at the time limit",
                    "open": "⏳ still open", "unknown": "❔ result unknown"}[res]
            money_txt = ""
            q = (g.get("position") or {}).get("qty")
            if r is not None:
                per = r * g["risk"]
                money_txt = " (%s%s)" % ("+" if per >= 0 else "-", signals.money(market, per * q) if q
                                         else signals.price(market, abs(per)) + " a share")
            lines.append("• %s at %s: %s%s" % (show(g["symbol"]), g["sent_at"], word, money_txt))
        lines.insert(2, "🟢 Day-trade signals today: %d" % len(sigs))
    else:
        lines.append("🟢 Day-trade signals today: none (nothing passed every check)")
    runs = st.get("runs", [])
    daily = [r for r in runs if r["mode"] == "daily"]
    orbs = [r for r in runs if r["mode"] == "orb"]
    d = None
    rdir = os.path.join(market_dir(market), "results", day.isoformat())
    if os.path.isdir(rdir):
        for name in sorted(os.listdir(rdir)):
            if name.endswith("-daily.json"):
                try:
                    with open(os.path.join(rdir, name)) as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    pass
    if d and d.get("hits"):
        per = {}
        for h in d["hits"]:
            for t in h["setups"]:
                per[t] = per.get(t, 0) + 1
        lines.append("Daily setups: %d (%s)" % (len(d["hits"]), ", ".join(
            "%s %d" % (SETUP_LABEL.get(k, k), v) for k, v in sorted(per.items(), key=lambda x: -x[1]))))
        lines.append("  top: " + ", ".join(show(h["symbol"]) for h in d["hits"][:10]))
    else:
        lines.append("Daily setups: %s" % ("none" if daily else "no daily scan ran"))
    first = {}
    if os.path.isdir(rdir):
        for name in sorted(os.listdir(rdir)):
            if name.endswith("-orb.json"):
                try:
                    with open(os.path.join(rdir, name)) as fh:
                        for h in json.load(fh).get("hits") or []:
                            first.setdefault(h["symbol"], h)
                except (OSError, ValueError):
                    pass
    if first:
        lines.append("🔎 Breakouts checked: %d" % len(first))
        for h in list(first.values())[:20]:
            lines.append("  %s %s at %s %s (+%.2f%% over %s)" % (show(h["symbol"]), h["price"], h["at"], m["label"],
                                                               h["pct_over"], h["or_high"]))
        if len(first) > 20:
            lines.append("  +%d more" % (len(first) - 20))
    else:
        lines.append("🔎 Breakouts checked: none")
    notes = st.get("notes", [])
    lines.append("%s Scanner: %d scans ran%s" % (
        "⚙️" if not notes else "⚠️", len(daily) + len(orbs), (", %d gave no result (last: %s %s)" % (
            len(notes), notes[-1]["at"], notes[-1]["msg"][:120])) if notes else ", no problems"))
    if signals.load_config()["alerts"] != "all":   # signals-only mode: keep the report short
        lines = [x for x in lines if not x.startswith("  ") and not x.startswith("Daily setups")]
    lines.append("")
    lines.append("⚠️ Signals come from past prices. Not a promise or advice.")
    return "\n".join(lines)


def send_report(market, day):
    text = report_text(market, day)
    title, body = text.split("\n", 1)
    sent = notify(title, body)
    log(market, day, "end-of-day report sent: %s" % (", ".join(sent) or "nowhere"))
    st = load_state(market, day)
    st["report_done"] = True
    save_state(market, day, st)
    return text


def due(market, now_local):
    """The mode this market should run at now_local, or None."""
    m = MARKETS[market]
    t = now_local.time()
    for mode in MODES:
        if mode not in m:   # forex has no daily or learn step
            continue
        a, b = m[mode]
        if hhmm(a) <= t <= hhmm(b):
            return mode
    return None


def cmd_tick(a):
    for market in MARKETS:
        now = datetime.datetime.now(MARKETS[market]["tz"])
        if a.now:
            now = datetime.datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=MARKETS["NSE"]["tz"]).astimezone(
                MARKETS[market]["tz"])
        mode = due(market, now)
        if not mode:
            continue
        ok, why = trading_day(market, now.date())
        if not ok:
            if a.dry_run:
                print("[%s] closed %s: %s" % (market, now.date().isoformat(), why))
                continue
            st = load_state(market, now.date())
            if not st.get("closed_logged"):
                log(market, now.date(), "closed today: %s" % why)
                st["closed_logged"] = True
                save_state(market, now.date(), st)
            continue
        with open(os.path.join(market_dir(market), ".lock"), "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log(market, now.date(), "%s skipped: the previous scan is still running" % mode)
                continue
            if a.dry_run:
                print("[%s] would run %s at %s" % (market, mode, now.strftime("%Y-%m-%d %H:%M %Z")))
                continue
            do_scan(market, mode, now.date())
    return 0


def cmd_run(a):
    global NO_NOTIFY
    NO_NOTIFY = a.no_notify
    market = a.market.upper()
    day = datetime.datetime.now(MARKETS[market]["tz"]).date()
    do_scan(market, a.mode, day, force=True)
    return 0


def cmd_report(a):
    market = a.market.upper()
    day = datetime.date.fromisoformat(a.date) if a.date else datetime.datetime.now(MARKETS[market]["tz"]).date()
    if a.print:
        print(report_text(market, day))
        return 0
    print(send_report(market, day))
    return 0


def cmd_next(a):
    """Walk forward in 5-minute steps (cron's grid) to each market's next daily, orb and report run."""
    start = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    start += datetime.timedelta(minutes=(5 - start.minute % 5) % 5)
    for market, m in MARKETS.items():
        found, closed = {}, {}
        t = start
        for _ in range(12 * 24 * 10):
            local = t.astimezone(m["tz"])
            mode = due(market, local)
            if mode and mode not in found:
                if local.date() not in closed:
                    closed[local.date()] = trading_day(market, local.date())[0]
                if closed[local.date()]:
                    found[mode] = local
            if len(found) == len([x for x in MODES if x in m]):
                break
            t += datetime.timedelta(minutes=5)
        print("%s:" % m["name"])
        for mode in MODES:
            if mode in found:
                print("  next %-6s %s (%s here)" % (mode, found[mode].strftime("%a %Y-%m-%d %H:%M ") + m["label"],
                                                   found[mode].astimezone().strftime("%H:%M %Z")))
    return 0


def cmd_signal(a):
    """Dry run of the signal pipeline on given symbols (or the latest scan's hits). Sends nothing."""
    market = a.market.upper()
    m = MARKETS[market]
    if market == "FX":
        now = forex.now_london(a.now)
        sig = fx_orb(now.date(), now=now, dry=True)
        if not sig:
            print("No signal: nothing passed every check.")
            return 1
        return 0
    cfg = signals.load_config()
    if a.fresh_minutes is not None:
        cfg["fresh_minutes"] = a.fresh_minutes
    if a.symbols:
        syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
        syms = [x if x.endswith(m["suffix"]) or not m["suffix"] else x + m["suffix"] for x in syms]
    else:
        day = datetime.datetime.now(m["tz"]).date()
        runs = [r for r in load_state(market, day).get("runs", []) if r["mode"] == "orb"]
        syms = list(reversed(runs[-1]["hits"])) if runs else []
    if not syms:
        print("no symbols: give --symbols, or run an orb scan first")
        return 1
    cfg["max_candidates_per_scan"] = len(syms)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sig = signals.pick([{"symbol": x} for x in syms], market, tmp, cfg, now=a.now, log=print)
    if not sig:
        print("No signal: nothing passed every check.")
        return 1
    title, body = signals.message(market, m["label"], show(sig["symbol"]), sig, sig.get("note"))
    print("\n" + title + "\n" + body)
    return 0


def cmd_swing(a):
    market = a.market.upper()
    day = datetime.datetime.now(MARKETS[market]["tz"]).date()
    run_swing(market, day, dry=not a.send)
    return 0


def cmd_summary(a):
    for market, m in MARKETS.items():
        day = datetime.date.fromisoformat(a.date) if a.date else datetime.datetime.now(m["tz"]).date()
        st = load_state(market, day)
        print("== %s, %s (%s)" % (m["name"], day.isoformat(), m["label"]))
        if not st:
            print("  no scans")
            continue
        for r in st.get("runs", []):
            print("  %s %-5s %d hits: %s" % (r["at"], r["mode"], len(r["hits"]),
                                             ", ".join(show(s) for s in r["hits"][:20])))
    return 0


def cmd_ml(a):
    cfg = signals.load_config()
    if (a.market or "").upper() == "FX":
        print("Forex has no learned (AI) check: its signals use the rules alone.")
        return 0
    if a.action == "learn":
        markets = [a.market.upper()] if a.market else [x for x in MARKETS if x != "FX"]   # no AI check for forex
        for market in markets:
            day = datetime.datetime.now(MARKETS[market]["tz"]).date()
            run_learn(market, day, send=False, say=print)
    print(ml.status_text(cfg))
    return 0


def cmd_test(a):
    sent = notify("Day trader autoscan", "Test notification. Telegram is %s." %
                  ("set up" if telegram_conf() else "not set up (no %s)" % TELEGRAM_ENV))
    print("sent to: %s" % (", ".join(sent) or "nowhere"))
    return 0 if sent else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick")
    p.add_argument("--now", help='pretend it is this IST time, "YYYY-MM-DD HH:MM" (with --dry-run)')
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("run")
    p.add_argument("--market", required=True, choices=["NSE", "US", "FX", "nse", "us", "fx"])
    p.add_argument("--mode", required=True, choices=["daily", "orb"])
    p.add_argument("--no-notify", action="store_true", help="scan and log, send nothing")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("next", help="when each market's next scans run, in that market's time and this machine's")
    p.set_defaults(fn=cmd_next)
    p = sub.add_parser("report")
    p.add_argument("--market", required=True, choices=["NSE", "US", "FX", "nse", "us", "fx"])
    p.add_argument("--date")
    p.add_argument("--print", action="store_true", help="print it, send nothing")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("signal", help="dry run of the signal checks; prints, sends nothing")
    p.add_argument("--market", required=True, choices=["NSE", "US", "FX", "nse", "us", "fx"])
    p.add_argument("--symbols", help="comma-separated; default: the latest orb scan's hits")
    p.add_argument("--now", help='pretend it is this market-local time, "YYYY-MM-DD HH:MM"')
    p.add_argument("--fresh-minutes", type=int, help="override the freshness limit for this run")
    p.set_defaults(fn=cmd_signal)
    p = sub.add_parser("swing", help="the daily swing run; prints only unless --send")
    p.add_argument("--market", required=True, choices=["NSE", "US", "FX", "nse", "us", "fx"])
    p.add_argument("--send", action="store_true", help="really send and record the signals")
    p.set_defaults(fn=cmd_swing)
    p = sub.add_parser("summary")
    p.add_argument("--date")
    p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("ml", help="the learned signal check: status, or label and retrain now")
    p.add_argument("action", choices=["status", "learn"])
    p.add_argument("--market", choices=["NSE", "US", "FX", "nse", "us", "fx"])
    p.set_defaults(fn=cmd_ml)
    p = sub.add_parser("test-notify")
    p.set_defaults(fn=cmd_test)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
