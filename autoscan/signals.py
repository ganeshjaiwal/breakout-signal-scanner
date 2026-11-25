"""Signal pipeline: turn fresh opening-range breakouts into at most one trade signal a scan.

For each fresh breakout the scanner found, run the day-trader skills in order:

  trader-entry        is the breakout real, on a completed bar, and how old is it
  trader-exit         stop at the opening-range midpoint, targets at 1R / 1.5R / 2R
  trader-timemachine  10,000 replays of each plan from the stock's own last 60 days,
                      plus every past session the same rule fired
  trader-news         headlines on the one candidate that is left (a halt blocks it)

A candidate becomes a signal only if every gate in signal_config.json passes. The
best one (highest simulated average R) is sent; the rest are dropped silently.

"Confidence" is the share of the 10,000 replays in which the target came before
the stop and before the flat-by time. It is a number from a simulation of past
price moves, not a promise: a signal can and will lose.
"""
import datetime
import email.utils
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import ml

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "..", "day-trader-skill", "skills")
ENTRY = os.path.join(SKILLS, "trader-entry", "entry.py")
EXIT = os.path.join(SKILLS, "trader-exit", "exit.py")
TIMEMACHINE = os.path.join(SKILLS, "trader-timemachine", "timemachine.py")
NEWS = os.path.join(SKILLS, "trader-news", "news.py")
CONFIG = os.path.join(HERE, "signal_config.json")              # shared defaults, in git
LOCAL_CONFIG = os.path.join(HERE, "signal_config.local.json")  # your own settings, not in git

DEFAULTS = {
    "alerts": "signals",          # "signals": only trade signals. "all": also every breakout and the daily setups
    "eod_report": True,           # one end-of-day message per market with the signals and how they ended
    "targets": ["1R", "1.5R", "2R"],
    "fresh_minutes": 20,          # the breakout bar must have closed at most this long ago
    "max_chase_r": 0.25,          # top of the buy range: entry + this many R; a price above it is too late
    "min_confidence_pct": 35,     # replays where the target came first
    "min_sim_avg_r": 0.10,        # average result of the 10,000 replays, in R (must be a positive bet)
    "min_history_trades": 12,     # times the same rule fired on this stock in the last 60 days
    "min_history_avg_r": 0.0,     # and its real average result then
    "min_risk_pct": 0.15,         # stop at least this far under the entry (percent), or the spread eats it
    "max_candidates_per_scan": 12,
    "max_signals_per_day": 2,     # per market
    "replays": 10000,
    "block_news_tags": ["halt", "risk"],   # any headline with one of these tags blocks the signal
    "block_negative_news": True,  # block when the day's headlines read more bad than good (see NEG_WORDS)
    # Position size per market, cash only, whole shares: {"NSE": {"budget": 2000, "max_loss": 250}}.
    # A market with no entry gets no quantity line. Amounts are in the stock's own currency.
    "sizing": {},
    "swing": {},                  # swing-signal settings, see swing.py DEFAULTS
    "ml": {},                     # the learned signal check, see ml.py DEFAULTS
}


def charges(buy_value, sell_value):
    """Rough round-trip cost of an NSE intraday equity trade at a typical discount broker:
    brokerage 0.03% a side (capped at 20), STT 0.025% on the sell, exchange 0.00297%,
    SEBI 0.0001%, stamp duty 0.003% on the buy, 18% GST on brokerage + exchange + SEBI.
    An estimate only: brokers differ."""
    br = min(0.0003 * buy_value, 20) + min(0.0003 * sell_value, 20)
    ex = 0.0000297 * (buy_value + sell_value)
    sebi = 0.000001 * (buy_value + sell_value)
    return br + ex + sebi + 0.18 * (br + ex + sebi) + 0.00025 * sell_value + 0.00003 * buy_value


def position(size, buy_hi, stop, target):
    """Whole shares that fit the budget and the max loss, both measured at the top of the
    buy range (the worst price you would pay). Returns (dict, '') or (None, why)."""
    budget, max_loss = float(size.get("budget") or 0), float(size.get("max_loss") or 0)
    if budget <= 0:
        return None, "no budget set"
    by_budget = int(budget // buy_hi)
    if by_budget < 1:
        return None, "one share costs %.2f, over the budget %.0f" % (buy_hi, budget)
    risk = buy_hi - stop
    by_loss = int(max_loss // risk) if max_loss > 0 else by_budget
    if by_loss < 1:
        return None, "one share risks %.2f, over the max loss %.0f" % (risk, max_loss)
    qty = min(by_budget, by_loss)
    return {"qty": qty, "cost": round(qty * buy_hi, 2), "max_loss": round(qty * risk, 2),
            "gain_at_target": round(qty * (target - buy_hi), 2),
            "charges": round(charges(qty * buy_hi, qty * target), 2),
            "limit": "budget" if by_budget <= by_loss else "max loss"}, ""


def load_config():
    """DEFAULTS, then signal_config.json, then signal_config.local.json. A dict setting
    (like "swing" or "sizing") is merged key by key, so a local file can change one value."""
    cfg = json.loads(json.dumps(DEFAULTS))
    for path in (CONFIG, LOCAL_CONFIG):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        for k, v in doc.items():
            if k not in DEFAULTS:
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = dict(cfg[k], **v)
            else:
                cfg[k] = v
    return cfg


def _run(script, args, cwd, timeout=120):
    """Run one skill script with --json. Returns (doc or None, last line of output)."""
    try:
        r = subprocess.run([sys.executable, script] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        lines = (r.stdout.strip() or r.stderr.strip() or "no output").splitlines()
        return None, lines[-1][:200]


TZ = {"NSE": ZoneInfo("Asia/Kolkata"), "US": ZoneInfo("America/New_York")}


def evaluate(sym, workdir, cfg, now=None, size=None, market=None):
    """Run entry -> exit -> time machine for one symbol.
    Returns {"symbol", "ok": bool, "why": str, ...plan and odds when ok}."""
    os.makedirs(os.path.join(workdir, "trader"), exist_ok=True)
    out = {"symbol": sym, "ok": False}
    pin = ["--now", now] if now else []

    entry, err = _run(ENTRY, ["check", sym, "--json", "--write"] + pin, workdir)
    if entry is None:
        out["why"] = "entry check failed: %s" % err
        return out
    if entry.get("status") != "triggered":
        out["why"] = "entry is %s" % entry.get("status")
        return out
    age = entry.get("minutes_since")
    if age is None or age > cfg["fresh_minutes"]:
        out["why"] = "breakout is %s minutes old (limit %s)" % (age, cfg["fresh_minutes"])
        return out
    trig = entry["trigger"]
    last = ((entry.get("forming") or {}).get("price")) or ((entry.get("last") or {}).get("close")) or trig["price"]

    best = None
    tried = []
    news = None
    for target in cfg["targets"]:
        name = "plan-%s.json" % target
        plan, err = _run(EXIT, ["plan", "--target", target, "--json", "--write", os.path.join("trader", name)] + pin,
                         workdir)
        if plan is None:
            out["why"] = "no exit plan: %s" % err
            return out
        risk = plan["risk_per_share"]
        if risk / plan["entry"] * 100 < cfg["min_risk_pct"]:
            out["why"] = "stop only %.2f%% under the entry (limit %s%%)" % (risk / plan["entry"] * 100,
                                                                            cfg["min_risk_pct"])
            return out
        buy_hi = round(plan["entry"] + cfg["max_chase_r"] * risk, 2)
        if last > buy_hi:
            out["why"] = "price %.2f already above the buy range top %.2f" % (last, buy_hi)
            return out
        if last <= plan["stop"]:
            out["why"] = "price %.2f already at or under the stop %.2f" % (last, plan["stop"])
            return out
        pos = None
        if size:
            pos, why = position(size, buy_hi, plan["stop"], plan["target"])
            if pos is None:
                out["why"] = why
                return out
        tm, err = _run(TIMEMACHINE, ["run", "--plan", os.path.join("trader", name), "--until", plan["flat_by"],
                                     "--replays", str(cfg["replays"]), "--json"] + pin, workdir, timeout=180)
        if tm is None:
            out["why"] = "time machine failed: %s" % err
            return out
        mc, hist = tm["mc"], tm["history"]
        cand = {"target_r": plan["target_r"], "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"],
                "risk": risk, "buy_lo": round(min(plan["opening_range"]["high"], plan["entry"]), 2), "buy_hi": buy_hi,
                "sell_lo": round(plan["target"] - 0.25 * risk, 2), "sell_hi": plan["target"],
                "flat_by": plan["flat_by"], "entry_time": plan["entry_time"],
                "p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
                "sim_avg_r": mc["avg_R"], "hist_trades": hist["triggered"], "hist_target": hist["target"],
                "hist_stop": hist["stop"], "hist_green": hist["green"], "hist_avg_r": hist["avg_R"],
                "sessions": hist["sessions"], "last": last, "age_min": age,
                "volume_x": trig.get("volume_x"), "pct_over": trig.get("pct_over"), "position": pos}
        tried.append(cand)
        fails = []
        if cand["p_target"] * 100 < cfg["min_confidence_pct"]:
            fails.append("confidence %.0f%% < %s%%" % (cand["p_target"] * 100, cfg["min_confidence_pct"]))
        if cand["sim_avg_r"] < cfg["min_sim_avg_r"]:
            fails.append("replay average %+.2fR < %+.2fR" % (cand["sim_avg_r"], cfg["min_sim_avg_r"]))
        if cand["hist_trades"] < cfg["min_history_trades"]:
            fails.append("only %d past trades" % cand["hist_trades"])
        elif cand["hist_avg_r"] < cfg["min_history_avg_r"]:
            fails.append("past trades averaged %+.2fR" % cand["hist_avg_r"])
        cand["rules_ok"] = not fails
        if market:
            if news is None:
                news = news_info(sym, workdir, market)
                out["news"] = news
            cand["ml_x"] = dict(ml.day_features(cand, market), **news_features(news))
            chk = ml.check("day", cand["ml_x"], cfg)
            cand["ml"] = chk
            if chk and not chk["pass"]:
                fails.append("AI model %+.2fR < %+.2fR" % (chk["ev"], chk["threshold"]))
        cand["fails"] = fails
        if not fails and (best is None or cand["sim_avg_r"] > best["sim_avg_r"]):
            best = cand
    out["tried"] = tried
    if best is None:
        closest = min(tried, key=lambda c: len(c["fails"]))
        out["why"] = "%sR: %s" % (("%g" % closest["target_r"]), "; ".join(closest["fails"]))
        return out
    out.update(best, ok=True, why="")
    return out


# Headline wording that reads bad or good for the stock. Checked on titles only, case-insensitive.
NEG_WORDS = re.compile(
    r"\bdowngrade[sd]?\b|\bcuts?\b.{0,30}\b(?:target|rating|guidance|forecast|outlook|stake)\b|"
    r"\b(?:lowers?|slashes|trims?)\b.{0,30}\b(?:target|guidance|forecast|outlook)\b|\bmiss(?:es|ed)?\b|"
    r"\bplunge[sd]?\b|\bslump(?:s|ed)?\b|\btumble[sd]?\b|\bs[ai]nk(?:s)?\b|\bcrash(?:es|ed)?\b|\bslid(?:es?)?\b|"
    r"\bprobe\b|\binvestigation\b|\blawsuit\b|\bsued\b|\bfraud\b|\braid(?:s|ed)?\b|\bpenalty\b|\bfined?\b|"
    r"\brecall\b|\bbankrupt|\bdefault(?:s|ed)?\b|\binsolvency\b|\bwarns?\b|\bwarning\b|\blayoffs?\b|"
    r"\bresign(?:s|ed)?\b|\bsteps? down\b|\bdelay(?:s|ed)?\b|\bshort[- ]seller\b|\bsell rating\b|"
    r"\bunderperform(?:s|ed|ing)?\b|\bloss widens\b|\bprofit (?:falls|drops|declines)\b|\bweak(?:er)?\b|\bhalt(?:s|ed)?\b|"
    r"\bpledge\b|\bblock deal\b|\bstake sale\b", re.I)
POS_WORDS = re.compile(
    r"\bupgrade[sd]?\b|\bbeats?\b|\btops? (?:estimates|expectations)\b|"
    r"\b(?:raises?|hikes?|lifts?|boosts?)\b.{0,30}\b(?:target|guidance|forecast|outlook|dividend|stake)\b|"
    r"\brecord (?:revenue|profit|sales|high|order)|\bsurge[sd]?\b|\bjump(?:s|ed)?\b|\bsoar(?:s|ed)?\b|"
    r"\brall(?:y|ies|ied)\b|\bclimb(?:s|ed)?\b|\bgain(?:s|ed)\b|"
    r"\bwins?\b.{0,30}\b(?:contract|order|deal|approval)\b|\bbags?\b.{0,30}\border|\bapproval\b|\bapproves?\b|"
    r"\bbuyback\b|\boutperform(?:s|ed|ing)?\b|\bbuy rating\b|\bprofit (?:rises|jumps|grows|up)\b|\bbonus (?:issue|shares)\b|"
    r"\bstrong(?:er)?\b", re.I)
NEWS_TAGS_ML = ("earnings", "analyst", "legal", "deal", "risk", "people")
SCANNER = os.path.join(SKILLS, "trader-scanner", "scan.py")
_mods = {}


def _module(name, path):
    if name not in _mods:
        spec = importlib.util.spec_from_file_location("autoscan_" + name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mods[name] = mod
    return _mods[name]


def tone(title):
    """-1 bad, +1 good, 0 neither or both."""
    neg, pos = bool(NEG_WORDS.search(title)), bool(POS_WORDS.search(title))
    return (1 if pos else 0) - (1 if neg else 0)


def company_name(sym):
    """The company's name from Yahoo's quote data, cached in data/names.json ('' when unknown)."""
    path = os.path.join(ml.DATA, "names.json")
    try:
        with open(path) as fh:
            names = json.load(fh)
    except (OSError, ValueError):
        names = {}
    if sym in names:
        return names[sym]
    name = ""
    try:
        mod = _module("scan", SCANNER)
        meta = mod.parse_chart(mod.yahoo_get(mod.chart_path(sym, "1d", "1d")), sym).get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or ""
        name = re.sub(r"\s*(?:Limited|Ltd\.?|Inc\.?|Corp(?:oration)?\.?|plc)$", "", name, flags=re.I).strip(" .,")
    except Exception:
        return ""
    names[sym] = name
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(names, fh, indent=1)
    return name


def google_news(name, hours=24, now=None):
    """[(title, published_utc)] from Google News search for the exact company name, or None
    when it cannot be fetched. Used for NSE stocks, where Yahoo carries few headlines."""
    # Indian market words keep out other companies with the same name ("Eternal" vs "Eternal Beauty").
    q = urllib.parse.quote('"%s" (share OR shares OR stock OR NSE OR BSE OR Sensex OR Nifty) when:%dd' % (
        name, max(1, math.ceil(hours / 24.0))))
    url = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en" % q
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "breakout-signal-scanner/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            when = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if title and when and now - when <= datetime.timedelta(hours=hours):
            out.append((re.sub(r"\s+-\s+[^-]+$", "", title), when))   # drop " - Publisher"
    return out


def news_info(sym, workdir, market=None):
    """The last 24 hours of headlines for sym: Yahoo Finance, plus Google News for NSE stocks.
    {'checked': bool, 'n', 'tags': {tag: first title}, 'neg': [titles], 'pos': [titles], 'sources'}."""
    heads, sources = [], []
    doc, _ = _run(NEWS, ["--tickers", sym, "--no-sec", "--no-moves", "--dry-run", "--hours", "24", "--json"],
                  workdir, timeout=60)
    if doc is not None:
        sources.append("Yahoo")
        heads += [(it.get("title", ""), it.get("tags") or []) for it in doc.get("items") or []]
    if market == "NSE" or sym.endswith(".NS"):
        name = company_name(sym)
        g = google_news(name) if len(name) >= 3 else None
        if g is not None:
            sources.append("Google News")
            kw = _module("news", NEWS).KEYWORDS
            # Keep headlines that name the company: the search also returns market wraps that only
            # mention it in the text ("Sensex jumps 2,000 points...").
            first = name.split()[0]
            rx = re.compile(r"\b%s\b" % re.escape(first if len(first) >= 4 else name), re.I)
            heads += [(t, [label for label, r in kw if re.search(r, t, re.I)]) for t, _ in g if rx.search(t)]
    if not sources:
        return {"checked": False}
    seen, tags, neg, pos = set(), {}, [], []
    for title, tg in heads:
        key = re.sub(r"\W+", " ", title.lower())[:60]
        if not title or key in seen:
            continue
        seen.add(key)
        for t in tg:
            tags.setdefault(t, title)
        tn = tone(title)
        (neg if tn < 0 else pos if tn > 0 else []).append(title)
    return {"checked": True, "n": len(seen), "tags": tags, "neg": neg, "pos": pos, "sources": sources}


def news_features(info):
    """The news as numbers for the day model (None when the news was not checked)."""
    if not info or not info.get("checked"):
        return {k: None for k in ["news_n_log", "news_neg", "news_pos"] + ["news_" + t for t in NEWS_TAGS_ML]}
    x = {"news_n_log": math.log1p(info["n"]), "news_neg": float(len(info["neg"])), "news_pos": float(len(info["pos"]))}
    x.update({"news_" + t: 1.0 if t in info["tags"] else 0.0 for t in NEWS_TAGS_ML})
    return x


def news_verdict(info, cfg):
    """(blocked_reason or '', note for the message)."""
    if not info or not info.get("checked"):
        return "", "❔ News could not be checked."
    for t in cfg["block_news_tags"]:
        if t in info["tags"]:
            return "news tagged %s: %s" % (t, info["tags"][t][:100]), ""
    neg, pos = info["neg"], info["pos"]
    if cfg.get("block_negative_news") and len(neg) > len(pos):
        return "bad news today: %s" % neg[0][:100], ""
    parts = []
    if "earnings" in info["tags"]:
        parts.append("📰 Earnings news today: the price can move more than usual.")
    if pos and not neg:
        parts.append("📰 Good news today: %s" % pos[0][:120])
    elif pos or neg:
        parts.append("📰 Mixed news today (%d good, %d bad): %s" % (len(pos), len(neg), (neg or pos)[0][:120]))
    elif info["tags"] and not parts:
        parts.append("📰 In the news today: %s." % ", ".join(sorted(info["tags"])))
    return "", "\n".join(parts) if parts else "📰 No major news today."


def news_check(sym, workdir, cfg, market=None, info=None):
    """(blocked_reason or '', note) from the last 24 hours of headlines."""
    return news_verdict(info if info is not None else news_info(sym, workdir, market), cfg)


def fmt(x):
    return ("%.2f" % x)


FLAG = {"NSE": "🇮🇳", "US": "🇺🇸"}


def cur(market):
    return "₹" if market == "NSE" else "$"


def price(market, x):
    return "%s%s" % (cur(market), "{:,.2f}".format(x))


def money(market, x):
    return "%s%s" % (cur(market), "{:,.0f}".format(abs(x)))


def clock12(hhmm, label):
    """'15:15', 'IST' -> '3:15 PM IST'."""
    t = datetime.datetime.strptime(hhmm, "%H:%M")
    return "%d:%02d %s %s" % ((t.hour - 1) % 12 + 1, t.minute, "AM" if t.hour < 12 else "PM", label)


def position_lines(market, p, delivery=False):
    if not p:
        return []
    out = ["📦 Buy %d share%s (about %s)%s" % (p["qty"], "" if p["qty"] == 1 else "s", money(market, p["cost"]),
                                              " - choose Delivery (CNC)" if delivery and market == "NSE" else ""),
           "📉 If the stop hits: lose about %s" % money(market, p["max_loss"]),
           "📈 If the target hits: gain about %s" % money(market, p["gain_at_target"]),
           "💸 Charges: about %s" % money(market, p["charges"])]
    return out


def ai_lines(chk):
    if not ml.active(chk):
        return []
    return ["🤖 AI check: %.0f%% target first, expected %+.2fR (passes at %+.2fR, model v%s)" % (
        chk["p"] * 100, chk["ev"], chk["threshold"], chk["version"])]


def message(market, label, sym_shown, s, note):
    """(title, body) for one day-trade signal: short, plain words, emojis for scanning."""
    title = "🟢 BUY NOW: %s  %s %s · day trade" % (sym_shown, FLAG.get(market, ""), market)
    body = [
        "",
        "💰 Buy between: %s – %s" % (price(market, s["buy_lo"]), price(market, s["buy_hi"])),
        "🎯 Sell between: %s – %s" % (price(market, s["sell_lo"]), price(market, s["sell_hi"])),
        "🛑 Stop loss: %s" % price(market, s["stop"]),
        "📊 Confidence: %.0f%%" % (s["p_target"] * 100),
    ]
    body += ai_lines(s.get("ml"))
    pos = position_lines(market, s.get("position"))
    if pos:
        body += [""] + pos
    body += [
        "",
        "⏰ Sell by %s, even if neither price is hit." % clock12(s["flat_by"], label),
        "🚫 Too late if the price is above %s." % price(market, s["buy_hi"]),
    ]
    if note:
        body.append(note)
    body += [
        "🔁 Past prices, 10,000 test runs: target first %.0f%%, stop first %.0f%%, neither %.0f%%." % (
            s["p_target"] * 100, s["p_stop"] * 100, s["p_neither"] * 100),
        "",
        "⚠️ Not a promise or advice. This trade can lose.",
    ]
    return title, "\n".join(body)


def pick(hits, market, day_dir, cfg, now=None, log=lambda t: None):
    """hits: the scanner's ORB hits (dicts with 'symbol'). Returns the best passing signal or None,
    and writes every candidate's result to day_dir/candidates.jsonl."""
    passing = []
    os.makedirs(day_dir, exist_ok=True)
    learn = ml.config(cfg)["mode"] != "off"
    for h in hits[:cfg["max_candidates_per_scan"]]:
        sym = h["symbol"]
        res = evaluate(sym, os.path.join(day_dir, sym.replace("&", "_")), cfg, now, (cfg.get("sizing") or {}).get(market),
                       market=market if learn else None)
        local = datetime.datetime.strptime(now, "%Y-%m-%d %H:%M") if now else \
            datetime.datetime.now(TZ.get(market, TZ["US"])).replace(tzinfo=None)
        res["checked_at"] = local.strftime("%Y-%m-%d %H:%M")
        bar_after = "%02d:%02d" % (local.hour, local.minute - local.minute % 5)
        with open(os.path.join(day_dir, "candidates.jsonl"), "a") as fh:
            fh.write(json.dumps({k: v for k, v in res.items() if k not in ("tried", "ml_x")}) + "\n")
        if learn:
            # Every target the time machine scored, passed or not: the rows the day model learns from.
            with open(os.path.join(day_dir, "tried.jsonl"), "a") as fh:
                for c in res.get("tried", []):
                    if "ml_x" not in c:
                        continue
                    fh.write(json.dumps({
                        "id": "%s|%s|%s|%g" % (market, res["checked_at"], sym, c["target_r"]), "symbol": sym,
                        "checked_at": res["checked_at"], "bar_after": bar_after, "x": c["ml_x"],
                        "rules_ok": c["rules_ok"], "p_model": (c.get("ml") or {}).get("p"),
                        **{k: c[k] for k in ("entry", "stop", "target", "target_r", "risk", "flat_by")}}) + "\n")
        log("candidate %s: %s" % (sym, "PASS %.0f%% %+.2fR at %gR" % (res["p_target"] * 100, res["sim_avg_r"],
                                                                       res["target_r"]) if res["ok"] else res["why"]))
        if res["ok"]:
            passing.append(res)
    def rank(r):
        chk = r.get("ml")
        if ml.active(chk):   # the learned odds, as an expected result in R
            return chk["ev"]
        return r["sim_avg_r"]
    passing.sort(key=lambda r: -rank(r))
    for res in passing:
        blocked, note = news_check(res["symbol"], os.path.join(day_dir, res["symbol"].replace("&", "_")), cfg,
                                   market, res.get("news"))
        if blocked:
            log("candidate %s dropped: %s" % (res["symbol"], blocked))
            continue
        res["note"] = note
        return res
    return None


def outcome(mod, sig, day, now_utc, bars=None):
    """How a sent signal ended, from the day's 5-minute bars: ('target'|'stop'|'flat'|'open'|'unknown', R).
    bars: that day's session bars when the caller already has them."""
    cutoff = mod.to_eastern(now_utc)
    if bars is None:
        try:
            series = mod.parse_chart(mod.yahoo_get(mod.chart_path(sig["symbol"], "1d", "5m")), sig["symbol"])
            bars = mod.session_bars(series, day, cutoff)
        except Exception:
            return "unknown", None
    hh, mm = [int(x) for x in sig["flat_by"].split(":")]
    flat = datetime.time(hh, mm)
    after = [b for b in bars if b["start"].strftime("%H:%M") >= sig["bar_after"]]
    last = None
    for b in after:
        if b["start"].time() >= flat:
            break
        last = b
        if b["l"] is not None and b["l"] <= sig["stop"]:
            return "stop", -1.0
        if b["h"] is not None and b["h"] >= sig["target"]:
            return "target", sig["target_r"]
    if last is None:
        return "unknown", None
    r = round((last["c"] - sig["entry"]) / sig["risk"], 2)
    done = cutoff.time() >= flat or cutoff.date() > day
    return ("flat" if done else "open"), r
