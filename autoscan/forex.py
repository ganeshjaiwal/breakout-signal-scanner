#!/usr/bin/env python3
"""Forex signals: the seven major pairs, on the London session.

Pairs come from lists/forex.txt (EUR/USD, USD/JPY, GBP/USD, USD/CHF, AUD/USD, USD/CAD,
NZD/USD by default). Prices:
  - past sessions (replays, the rule's record, study): Dukascopy's free tick history (real bid
    and ask, see dukascopy.py), downloaded slowly in the evening and kept; any session it does
    not have yet comes from Yahoo's 5-minute bars (about the last 60 days).
  - today's live bars and the daily bars for swing trades: Yahoo (EURUSD=X ...). Dukascopy's
    files appear only after each hour ends, too late for a live signal.
Forex has no real volume on any free feed, so nothing here uses it.

Day trades (every 5 minutes, 08:15-14:45 London time):

  1. Opening range: the high and low of the first 15 minutes after the London open (08:00).
  2. Breakout: the first 5-minute bar to CLOSE above the range high (a BUY) or below the
     range low (a SELL). Only the day's first break counts, and only while it is fresh.
  3. Plan: stop at the other side of the range (or its middle, see "stop"), rounded away from
     the entry to the pair's last digit; targets at 1R, 1.5R and 2R; flat by 15:45 London.
  4. Time machine: 10,000 replays of the rest of the session built from the pair's own
     5-minute bars on its past sessions (up to "history_days", the same code
     trader-timemachine uses), plus the same rule on every one of those past sessions.
  5. Spread: every result is counted AFTER the spread (lists the typical retail spread per
     pair, in pips; change them in "spreads"). A stop must be at least min_risk_spreads
     spreads away, or the spread eats the trade.
  6. Economic calendar (Forex Factory's free weekly feed): a high-impact release for either
     currency within 30 minutes of now blocks the signal; one later in the trade is named
     in the message.
  7. Two signals a day at most, and never two that make the same bet on the US dollar
     (BUY EUR/USD and BUY GBP/USD both sell dollars).

Swing trades (once a day, 01:15 London, on the day's completed daily bar): the stock swing
setups and backtest (swing.py) on 5 years of daily bars, long only.

Every number is measured on past prices. It is not a promise: a signal can and will lose.

Usage (prints only, sends nothing):
  forex.py scan [--now "2026-11-25 09:30"]   # the day-trade check at a London time
  forex.py calendar                          # this week's high-impact releases
  forex.py study [--pairs EURUSD,GBPUSD]     # the rule's record on the past sessions, by setting
  forex.py history                           # how many past sessions come from Dukascopy / Yahoo
  forex.py fill [--minutes 30]               # download missing Dukascopy sessions now
"""
import argparse
import datetime
import json
import math
import os
import statistics
import sys
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dukascopy  # noqa: E402
import signals  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("AUTOSCAN_DATA") or os.path.join(HERE, "data")
FX_DIR = os.path.join(DATA, "fx")
TIMEMACHINE = signals.TIMEMACHINE
LONDON = ZoneInfo("Europe/London")
INDIA = ZoneInfo("Asia/Kolkata")
UTC = datetime.timezone.utc
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FLAG = "💱"

# Typical retail spread in pips on a standard account at a quiet London hour. Your app's may
# be wider (news, the Asian session, cent accounts): set yours in signal_config "forex" "spreads".
SPREAD_PIPS = {"EURUSD": 1.0, "USDJPY": 1.2, "GBPUSD": 1.5, "USDCHF": 1.8, "AUDUSD": 1.3, "USDCAD": 2.0,
               "NZDUSD": 2.0}

DEFAULTS = {
    "session_open": "08:00",      # London time: the opening range starts here
    "session_close": "16:00",
    "or_minutes": 15,
    "stop": "range_far",          # "range_far": the other side of the opening range; "range_mid": its middle
                                  # (on the 60 days to Nov 2026 the tighter middle stop lost more to the spread)
    "targets": [1.0, 1.5, 2.0],   # in R
    "no_entries_after": "14:30",
    "flat_by": "15:45",
    "sides": ["buy", "sell"],
    "fresh_minutes": 20,
    "max_chase_r": 0.25,
    "min_confidence_pct": 35,
    "min_sim_avg_r": 0.10,        # after the spread
    "min_history_trades": 10,
    "min_history_avg_r": 0.0,     # after the spread
    "min_risk_spreads": 5,        # the stop at least this many spreads from the entry
    "spreads": {},                # {"EURUSD": 0.8}: your app's spread in pips
    "replays": 10000,
    "max_signals_per_day": 2,
    "one_per_usd_side": True,
    "calendar": True,
    "calendar_impact": ["High"],
    "calendar_block_minutes": 30, # a release this close to now (before or after) blocks the signal
    "sizing": {},                 # {"max_loss": 10}: US dollars lost at the stop, spread included
    "history_days": 120,          # past sessions to replay (Dukascopy, Yahoo for the days it lacks)
    "dukascopy": True,            # False: Yahoo only (about 60 days)
}


def config(cfg):
    out = json.loads(json.dumps(DEFAULTS))
    out.update({k: v for k, v in ((cfg or {}).get("forex") or {}).items() if k in DEFAULTS})
    return out


# ---------------------------------------------------------------- pairs and prices

def pair_of(sym):
    """'EURUSD=X' -> 'EURUSD'."""
    return sym.upper().replace("=X", "").replace("/", "")


def shown(sym):
    p = pair_of(sym)
    return "%s/%s" % (p[:3], p[3:])


def pip(sym):
    return 0.01 if "JPY" in pair_of(sym) else 0.0001


def dp(sym):
    return 3 if "JPY" in pair_of(sym) else 5


def spread(sym, fx):
    """The spread in price units."""
    p = pair_of(sym)
    return float((fx.get("spreads") or {}).get(p, SPREAD_PIPS.get(p, 2.0))) * pip(sym)


def px(sym, x):
    return ("%%.%df" % dp(sym)) % x


def pips(sym, d):
    return abs(d) / pip(sym)


def away(x, sym, long):
    """Round a stop to the pair's last digit, away from the entry: down for a buy, up for a sell."""
    f = 10 ** dp(sym)
    c = round(x * f, 4)
    return (math.floor(c) if long else math.ceil(c)) / f


def usd_side(sym, side):
    """'long' or 'short' US dollars for this trade, or None when the pair has no USD."""
    p = pair_of(sym)
    long_base = side == "buy"
    if p[3:] == "USD":
        return "short" if long_base else "long"
    if p[:3] == "USD":
        return "long" if long_base else "short"
    return None


def usd_per_unit(sym, move, price):
    """US dollars made or lost per unit of the base currency for a price move."""
    p = pair_of(sym)
    if p[3:] == "USD":
        return move
    if p[:3] == "USD":
        return move / price
    return None


# ---------------------------------------------------------------- clock

def hm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def trading_day(d):
    """(open, why) for the forex market on London date d."""
    if d.weekday() >= 5:
        return False, "weekend"
    if (d.month, d.day) in ((12, 25), (1, 1)):
        return False, "forex holiday (%s)" % ("Christmas" if d.month == 12 else "New Year's Day")
    return True, ""


def session_start(d, fx):
    return datetime.datetime.combine(d, hm(fx["session_open"]), tzinfo=LONDON)


def n_bars(fx):
    a, b = hm(fx["session_open"]), hm(fx["session_close"])
    return ((b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)) // 5


def slot_of(fx, text):
    """The bar index at a London time: '15:45' -> the first bar that STARTS at or after it."""
    a, t = hm(fx["session_open"]), hm(text)
    return ((t.hour * 60 + t.minute) - (a.hour * 60 + a.minute)) // 5


def clock12(dt):
    return "%d:%02d %s" % ((dt.hour - 1) % 12 + 1, dt.minute, "AM" if dt.hour < 12 else "PM")


def both_clocks(dt):
    """'3:45 PM London (9:15 PM IST)'."""
    return "%s London (%s IST)" % (clock12(dt.astimezone(LONDON)), clock12(dt.astimezone(INDIA)))


# ---------------------------------------------------------------- data

def scanner():
    return signals._module("scan", signals.SCANNER)


def chart(sym, rng, interval="5m"):
    mod = scanner()
    return mod.parse_chart(mod.yahoo_get(mod.chart_path(sym, rng, interval)), sym)


def sessions(series, fx, now):
    """{London date: [bar or None] * n_bars} of COMPLETE 5-minute bars in the London session.
    bar = {'start': aware London datetime, 'o', 'h', 'l', 'c'}."""
    n = n_bars(fx)
    out = {}
    for i, ts in enumerate(series["t"]):
        if ts is None:
            continue
        vals = [series[k][i] if series.get(k) else None for k in ("o", "h", "l", "c")]
        if None in vals:
            continue
        start = datetime.datetime.fromtimestamp(ts, UTC).astimezone(LONDON)
        if start.second or start.minute % 5 or start + datetime.timedelta(minutes=5) > now:
            continue
        k = int((start - session_start(start.date(), fx)).total_seconds() // 300)
        if not 0 <= k < n:
            continue
        out.setdefault(start.date(), [None] * n)[k] = {"start": start, "o": float(vals[0]), "h": float(vals[1]),
                                                       "l": float(vals[2]), "c": float(vals[3])}
    return out


def filled(bars, upto, max_gaps=3):
    """bars[0..upto] with up to max_gaps missing bars carried forward flat, or None."""
    if not bars or bars[0] is None:
        return None
    out, gaps = [], 0
    for k in range(upto + 1):
        b = bars[k]
        if b is None:
            gaps += 1
            if gaps > max_gaps:
                return None
            p = out[-1]
            b = {"start": p["start"] + datetime.timedelta(minutes=5), "o": p["c"], "h": p["c"], "l": p["c"],
                 "c": p["c"]}
        out.append(b)
    return out


def yahoo_series(sym, day):
    """Yahoo's last ~60 days of 5-minute bars, kept for the day in data/fx/cache."""
    path = os.path.join(FX_DIR, "cache", day.isoformat(), pair_of(sym) + ".json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass
    series = chart(sym, "60d")
    series = {k: series.get(k) for k in ("t", "o", "h", "l", "c")}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(series, fh)
    return series


DUKA_DIR = os.path.join(FX_DIR, "dukascopy")


def history_dates(day, fx):
    """The past open London dates the replays use, newest first."""
    return dukascopy.wanted(day, int(fx["history_days"]), lambda d: trading_day(d)[0])


def past_sessions(sym, day, fx, now, sources=None):
    """Full past London sessions before `day`, oldest first: [(date, [bar] * n_bars)].
    Each date comes from Dukascopy when it was downloaded (data/fx/dukascopy), else from Yahoo.
    sources: a dict that gets {'dukascopy': n, 'yahoo': n}."""
    n = n_bars(fx)
    dates = history_dates(day, fx)
    got = {}
    if fx.get("dukascopy", True):
        docs = [doc for doc in (dukascopy.load_day(DUKA_DIR, pair_of(sym), d) for d in dates) if doc]
        for d, bars in sessions(dukascopy.series(docs), fx, now).items():
            full = filled(bars, n - 1)
            if full:
                got[d] = (full, "dukascopy")
    if len(got) < len(dates):
        try:
            ys = yahoo_series(sym, day)
        except Exception:
            if not got:
                raise
            ys = {"t": []}
        for d, bars in sessions(ys, fx, now).items():
            if d in got or d >= day or not trading_day(d)[0]:
                continue
            full = filled(bars, n - 1)
            if full:
                got[d] = (full, "yahoo")
    keep = sorted(d for d in got if d < day and trading_day(d)[0])[-int(fx["history_days"]):]
    if sources is not None:
        for d in keep:
            sources[got[d][1]] = sources.get(got[d][1], 0) + 1
    return [(d, got[d][0]) for d in keep]


def fill_history(symbols, fx, budget_s=240, log=print, today=None):
    """Download the missing Dukascopy sessions (newest first). See dukascopy.fill."""
    today = today or datetime.datetime.now(LONDON).date()
    return dukascopy.fill(DUKA_DIR, [pair_of(s) for s in symbols], history_dates(today, fx),
                          hm(fx["session_open"]), hm(fx["session_close"]), LONDON, budget_s, log)


# ---------------------------------------------------------------- the rule

def breakout(bars, fx, upto, sides):
    """The day's first close beyond the opening range among bars[n..upto]:
    {'side', 'k', 'oh', 'ol'} or None. bars is a full list (no None) up to upto."""
    n = fx["or_minutes"] // 5
    if upto < n or len(bars) <= upto:
        return None
    oh = max(b["h"] for b in bars[:n])
    ol = min(b["l"] for b in bars[:n])
    for k in range(n, upto + 1):
        c = bars[k]["c"]
        side = "buy" if c > oh else "sell" if c < ol else None
        if side:
            return {"side": side, "k": k, "oh": oh, "ol": ol} if side in sides else None
    return None


def plan(sym, entry, oh, ol, side, target_r, fx):
    long = side == "buy"
    raw = (oh + ol) / 2 if fx["stop"] == "range_mid" else (ol if long else oh)
    stop = away(raw, sym, long)
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        return None
    target = round(entry + target_r * risk if long else entry - target_r * risk, dp(sym))
    return {"entry": entry, "stop": stop, "target": target, "risk": risk, "target_r": target_r}


def walk(bars, k, last, p, side):
    """('target'|'stop'|'close', R) for a trade entered at bar k's close, walked to bar last.
    The stop is checked first in every bar (a bar that touches both is a loss)."""
    long = side == "buy"
    for b in bars[k + 1:last + 1]:
        if (b["l"] <= p["stop"]) if long else (b["h"] >= p["stop"]):
            return "stop", -1.0
        if (b["h"] >= p["target"]) if long else (b["l"] <= p["target"]):
            return "target", p["target_r"]
    cl = bars[last]["c"]
    return "close", ((cl - p["entry"]) if long else (p["entry"] - cl)) / p["risk"]


def history(sym, past, fx, side, target_r, last, last_entry):
    """The same rule on every past session, same side only. R after the spread."""
    sp = spread(sym, fx)
    trades = []
    for d, bars in past:
        b = breakout(bars, fx, last_entry, [side])
        if not b:
            continue
        p = plan(sym, bars[b["k"]]["c"], b["oh"], b["ol"], side, target_r, fx)
        if not p:
            continue
        oc, r = walk(bars, b["k"], last, p, side)
        trades.append({"date": d.isoformat(), "outcome": oc, "R": r - sp / p["risk"]})
    n = len(trades)
    return {"sessions": len(past), "triggered": n,
            "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "green": sum(1 for t in trades if t["R"] > 0),
            "avg_R": statistics.mean(t["R"] for t in trades) if n else None, "trades": trades}


def time_machine():
    return signals._module("timemachine", TIMEMACHINE)


def replays(past, p, side, k, last, fx, sym):
    """trader-timemachine's own Monte Carlo on this pair's sessions. avg_R after the spread."""
    tm = time_machine()
    tm.SESSION_BARS = n_bars(fx)
    pool = tm.build_pool([(d, bars) for d, bars in past])
    mc = tm.simulate(pool, p["entry"], p["stop"], p["target"], k, last, replays=fx["replays"], side=side)
    mc["avg_R"] = mc["avg_R"] - spread(sym, fx) / p["risk"]
    return mc


# ---------------------------------------------------------------- economic calendar

def calendar(force=False):
    """This week's releases from Forex Factory's free feed, cached for 2 hours in data/fx.
    [{'title', 'country', 'when' (aware), 'impact'}], or None when it cannot be fetched."""
    path = os.path.join(FX_DIR, "calendar.json")
    doc = None
    try:
        if not force and datetime.datetime.now().timestamp() - os.path.getmtime(path) < 7200:
            with open(path) as fh:
                doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if doc is None:
        try:
            req = urllib.request.Request(CALENDAR_URL, headers={"User-Agent": "breakout-signal-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
            os.makedirs(FX_DIR, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(doc, fh)
        except Exception:
            try:   # an old copy beats nothing; it is still this week's list most of the time
                with open(path) as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                return None
    out = []
    for e in doc if isinstance(doc, list) else []:
        try:
            when = datetime.datetime.fromisoformat(e["date"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"title": e.get("title", ""), "country": e.get("country", ""), "when": when,
                    "impact": e.get("impact", "")})
    return out


def calendar_check(sym, now, until, fx):
    """(blocked_reason or '', note) for a trade in sym from now to `until` (aware datetimes)."""
    if not fx["calendar"]:
        return "", ""
    events = calendar()
    if events is None:
        return "", "❔ Economic calendar could not be checked."
    p = pair_of(sym)
    mine = [e for e in events if e["country"] in (p[:3], p[3:]) and e["impact"] in fx["calendar_impact"]]
    gap = datetime.timedelta(minutes=fx["calendar_block_minutes"])
    for e in mine:
        if abs(e["when"] - now) <= gap:
            return "%s %s at %s" % (e["country"], e["title"], both_clocks(e["when"])), ""
    later = sorted((e for e in mine if now + gap < e["when"] <= until), key=lambda e: e["when"])
    if later:
        e = later[0]
        more = " (+%d more)" % (len(later) - 1) if len(later) > 1 else ""
        return "", "📅 %s %s at %s%s: the price can jump then." % (e["country"], e["title"], both_clocks(e["when"]),
                                                                     more)
    return "", "📅 No big releases for %s before the close." % shown(sym)


def calendar_soon(sym, now, days, fx):
    """The note for a swing trade: high-impact releases for the pair in the next `days` days."""
    events = calendar()
    if events is None:
        return "❔ Economic calendar could not be checked."
    p = pair_of(sym)
    soon = sorted((e for e in events if e["country"] in (p[:3], p[3:]) and e["impact"] in fx["calendar_impact"]
                   and now <= e["when"] <= now + datetime.timedelta(days=days)), key=lambda e: e["when"])
    if not soon:
        return "📅 No big releases for %s in the next %d days." % (shown(sym), days)
    names = ", ".join("%s %s (%s)" % (e["country"], e["title"], e["when"].astimezone(INDIA).strftime("%a"))
                      for e in soon[:3])
    return "📅 Coming up: %s%s." % (names, " +%d more" % (len(soon) - 3) if len(soon) > 3 else "")


# ---------------------------------------------------------------- size

def position(size, sym, worst_entry, stop, target, side, fx):
    """Lots (steps of 0.01 = 1,000 units) that lose at most max_loss US dollars at the stop,
    spread included. Returns (dict, '') or (None, why)."""
    max_loss = float((size or {}).get("max_loss") or 0)
    if max_loss <= 0:
        return None, "no max_loss set"
    sp = spread(sym, fx)
    loss1 = usd_per_unit(sym, abs(worst_entry - stop) + sp, stop)
    gain1 = usd_per_unit(sym, abs(target - worst_entry) - sp, target)
    if loss1 is None:
        return None, "sizing needs a pair with US dollars in it"
    units = int(max_loss / loss1 / 1000) * 1000
    if units < 1000:
        return None, "0.01 lot loses about $%.2f at the stop, over the max loss $%g" % (loss1 * 1000, max_loss)
    notional = units if pair_of(sym)[:3] == "USD" else units * worst_entry
    return {"qty": units, "lots": units / 100000.0, "max_loss": round(units * loss1, 2),
            "gain_at_target": round(units * gain1, 2), "spread_cost": round(usd_per_unit(sym, sp, worst_entry) * units, 2),
            "notional": round(notional, 0)}, ""


def position_lines(p):
    if not p:
        return []
    return ["📦 Size: %.2f lots (%s units, about $%s)" % (p["lots"], "{:,}".format(p["qty"]),
                                                         "{:,.0f}".format(p["notional"])),
            "📉 If the stop hits: lose about $%s (spread included)" % "{:,.2f}".format(p["max_loss"]),
            "📈 If the target hits: gain about $%s (after the spread)" % "{:,.2f}".format(p["gain_at_target"])]


# ---------------------------------------------------------------- one scan

def now_london(text=None):
    if text:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
    return datetime.datetime.now(LONDON)


def evaluate(sym, cfg, now, log=lambda t: None, seen=None):
    """Today's breakout on sym, checked end to end. {'symbol', 'ok', 'why', ...the signal when ok}.
    seen: breakouts ('EURUSD=X@08:35') already checked on an earlier scan today, skipped; this
    one is added once it is checked."""
    fx = config(cfg)
    out = {"symbol": sym, "ok": False}
    day = now.date()
    n_or = fx["or_minutes"] // 5
    last_entry = slot_of(fx, fx["no_entries_after"]) - 1
    last = slot_of(fx, fx["flat_by"]) - 1
    try:
        series = chart(sym, "1d" if day == datetime.datetime.now(LONDON).date() else "5d")
    except Exception as e:
        out["why"] = "no prices: %s" % e
        return out
    today = sessions(series, fx, now).get(day)
    done = max([k for k, b in enumerate(today or []) if b is not None], default=-1)
    bars = filled(today, done) if done >= 0 else None
    if not bars or done < n_or:
        out["why"] = "the opening range is not complete yet"
        return out
    b = breakout(bars, fx, min(done, last_entry), fx["sides"])
    if not b:
        out["why"] = "no breakout"
        return out
    trig = bars[b["k"]]
    closed_at = trig["start"] + datetime.timedelta(minutes=5)
    age = int((now - closed_at).total_seconds() // 60)
    out.update(side=b["side"], at=closed_at.strftime("%H:%M"), or_high=b["oh"], or_low=b["ol"])
    if age > fx["fresh_minutes"]:
        out["why"] = "the %s breakout is %d minutes old (limit %d)" % (b["side"], age, fx["fresh_minutes"])
        return out
    key = "%s@%s" % (sym, out["at"])
    if seen is not None:
        if key in seen:
            out["why"] = "the %s breakout at %s was checked already" % (b["side"], out["at"])
            return out
        seen.add(key)
    long = b["side"] == "buy"
    entry, lastpx = trig["c"], bars[-1]["c"]
    meta = series.get("meta") or {}
    if meta.get("regularMarketPrice") and day == datetime.datetime.now(LONDON).date():
        lastpx = float(meta["regularMarketPrice"])
    try:
        past = past_sessions(sym, day, fx, now)
    except Exception as e:
        out["why"] = "no history: %s" % e
        return out
    if len(past) < 20:
        out["why"] = "only %d full past sessions" % len(past)
        return out
    sp = spread(sym, fx)
    tried, best = [], None
    for tr in fx["targets"]:
        p = plan(sym, entry, b["oh"], b["ol"], b["side"], float(tr), fx)
        if not p:
            out["why"] = "the stop is on the wrong side of the entry"
            return out
        if p["risk"] < fx["min_risk_spreads"] * sp:
            out["why"] = "stop only %.1f pips away, under %d spreads (%.1f pips)" % (
                pips(sym, p["risk"]), fx["min_risk_spreads"], pips(sym, fx["min_risk_spreads"] * sp))
            return out
        chase = p["entry"] + fx["max_chase_r"] * p["risk"] if long else p["entry"] - fx["max_chase_r"] * p["risk"]
        if (lastpx > chase) if long else (lastpx < chase):
            out["why"] = "price %s already past the entry range limit %s" % (px(sym, lastpx), px(sym, chase))
            return out
        if (lastpx <= p["stop"]) if long else (lastpx >= p["stop"]):
            out["why"] = "price %s already at the stop %s" % (px(sym, lastpx), px(sym, p["stop"]))
            return out
        mc = replays(past, p, b["side"], b["k"], last, fx, sym)
        hist = history(sym, past, fx, b["side"], float(tr), last, last_entry)
        cand = dict(p, side=b["side"], p_target=mc["p_target"], p_stop=mc["p_stop"], p_neither=mc["p_neither"],
                    sim_avg_r=mc["avg_R"], hist_trades=hist["triggered"], hist_target=hist["target"],
                    hist_stop=hist["stop"], hist_avg_r=hist["avg_R"], sessions=hist["sessions"],
                    enter_lo=round(min(p["entry"], chase), dp(sym)), enter_hi=round(max(p["entry"], chase), dp(sym)),
                    exit_lo=round(min(p["target"], p["target"] - (0.25 if long else -0.25) * p["risk"]), dp(sym)),
                    exit_hi=round(max(p["target"], p["target"] - (0.25 if long else -0.25) * p["risk"]), dp(sym)),
                    last=lastpx, age_min=age, spread_pips=round(pips(sym, sp), 1), entry_k=b["k"])
        fails = []
        if cand["p_target"] * 100 < fx["min_confidence_pct"]:
            fails.append("confidence %.0f%% < %s%%" % (cand["p_target"] * 100, fx["min_confidence_pct"]))
        if cand["sim_avg_r"] < fx["min_sim_avg_r"]:
            fails.append("replay average %+.2fR < %+.2fR after the spread" % (cand["sim_avg_r"], fx["min_sim_avg_r"]))
        if cand["hist_trades"] < fx["min_history_trades"]:
            fails.append("only %d past trades" % cand["hist_trades"])
        elif cand["hist_avg_r"] < fx["min_history_avg_r"]:
            fails.append("past trades averaged %+.2fR after the spread" % cand["hist_avg_r"])
        cand["fails"] = fails
        tried.append(cand)
        if not fails and (best is None or cand["sim_avg_r"] > best["sim_avg_r"]):
            best = cand
    out["tried"] = tried
    if best is None:
        closest = min(tried, key=lambda c: len(c["fails"]))
        out["why"] = "%gR: %s" % (closest["target_r"], "; ".join(closest["fails"]))
        return out
    size = fx.get("sizing") or {}
    if size:
        worst = best["enter_hi"] if long else best["enter_lo"]
        pos, why = position(size, sym, worst, best["stop"], best["target"], b["side"], fx)
        if pos is None:
            out["why"] = why
            return out
        best["position"] = pos
    flat = datetime.datetime.combine(day, hm(fx["flat_by"]), tzinfo=LONDON)
    blocked, note = calendar_check(sym, now, flat, fx)
    if blocked:
        out["why"] = "big release: %s" % blocked
        return out
    out.update(best, ok=True, why="", note=note, flat_by=fx["flat_by"])
    return out


def scan(symbols, cfg, now, taken=(), log=lambda t: None, seen=None):
    """Every pair, then the best passing signal (or None). taken: the day's sent signals,
    so a second signal never doubles the same bet on the US dollar. seen: see evaluate."""
    fx = config(cfg)
    passing = []
    for sym in symbols:
        res = evaluate(sym, cfg, now, log, seen)
        log("fx %s: %s" % (shown(sym), ("PASS %s %.0f%% %+.2fR at %gR" % (
            res["side"].upper(), res["p_target"] * 100, res["sim_avg_r"], res["target_r"])) if res["ok"] else res["why"]))
        if res["ok"]:
            passing.append(res)
    passing.sort(key=lambda r: -r["sim_avg_r"])
    sides = {usd_side(s["symbol"], s["side"]) for s in taken} - {None}
    for res in passing:
        if any(s["symbol"] == res["symbol"] for s in taken):
            log("fx %s dropped: already signalled today" % shown(res["symbol"]))
            continue
        if fx["one_per_usd_side"] and usd_side(res["symbol"], res["side"]) in sides:
            log("fx %s dropped: same US dollar bet as an earlier signal today" % shown(res["symbol"]))
            continue
        return res
    return None


def message(s):
    """(title, body) for one forex day-trade signal."""
    sym, long = s["symbol"], s["side"] == "buy"
    day = datetime.datetime.strptime(s.get("day") or datetime.date.today().isoformat(), "%Y-%m-%d").date()
    flat = datetime.datetime.combine(day, hm(s["flat_by"]), tzinfo=LONDON)
    title = "%s %s NOW: %s  %s forex · day trade" % ("🟢" if long else "🔴", "BUY" if long else "SELL", shown(sym), FLAG)
    P = lambda x: px(sym, x)  # noqa: E731
    body = [
        "",
        "💰 %s between: %s – %s" % ("Buy" if long else "Sell", P(s["enter_lo"]), P(s["enter_hi"])),
        "🎯 Take profit between: %s – %s" % (P(s["exit_lo"]), P(s["exit_hi"])),
        "🛑 Stop loss: %s (%.0f pips)" % (P(s["stop"]), pips(sym, s["risk"])),
        "📊 Confidence: %.0f%%" % (s["p_target"] * 100),
    ]
    pos = position_lines(s.get("position"))
    if pos:
        body += [""] + pos
    body += [
        "",
        "⏰ Close it by %s, even if neither price is hit." % both_clocks(flat),
        "🚫 Too late if the price is %s %s." % ("above" if long else "below", P(s["enter_hi"] if long else s["enter_lo"])),
        "💡 Why: it closed %s the first 15 minutes' range after the London open." % ("above" if long else "below"),
    ]
    if s.get("note"):
        body.append(s["note"])
    body += [
        "🔁 Past prices, 10,000 test runs: target first %.0f%%, stop first %.0f%%, neither %.0f%%." % (
            s["p_target"] * 100, s["p_stop"] * 100, s["p_neither"] * 100),
        "💸 Counted after a %.1f-pip spread. A wider spread in your app means worse results." % s["spread_pips"],
        "",
        "⚠️ Not a promise or advice. This trade can lose, and leverage makes losses bigger.",
    ]
    return title, "\n".join(body)


def outcome(sig, now):
    """How a sent signal ended: ('target'|'stop'|'flat'|'open'|'unknown', R before the spread)."""
    fx = config(signals.load_config())
    day = datetime.date.fromisoformat(sig["day"])
    try:
        today = sessions(chart(sig["symbol"], "5d"), fx, now).get(day) or []
    except Exception:
        return "unknown", None
    after = [b for b in today if b is not None and b["start"].strftime("%H:%M") >= sig["bar_after"]]
    flat = hm(sig["flat_by"])
    long = sig["side"] == "buy"
    last = None
    for b in after:
        if b["start"].time() >= flat:
            break
        last = b
        if (b["l"] <= sig["stop"]) if long else (b["h"] >= sig["stop"]):
            return "stop", -1.0
        if (b["h"] >= sig["target"]) if long else (b["l"] <= sig["target"]):
            return "target", sig["target_r"]
    if last is None:
        return "unknown", None
    r = ((last["c"] - sig["entry"]) if long else (sig["entry"] - last["c"])) / sig["risk"]
    done = now.time() >= flat or now.date() > day
    return ("flat" if done else "open"), round(r, 2)


def report_lines(sigs, now):
    lines = []
    for g in sigs:
        res, r = outcome(g, now)
        word = {"target": "✅ target hit", "stop": "❌ stop hit", "flat": "⏰ closed at the time limit",
                "open": "⏳ still open", "unknown": "❔ result unknown"}[res]
        extra = ""
        if r is not None:
            extra = " (%+.0f pips" % (r * pips(g["symbol"], g["risk"]))
            q = (g.get("position") or {}).get("qty")
            if q:
                usd = usd_per_unit(g["symbol"], r * g["risk"] * q, g["entry"])
                if usd is not None:
                    extra += ", %s$%s" % ("+" if usd >= 0 else "-", "{:,.2f}".format(abs(usd)))
            extra += ", before the spread)"
        lines.append("• %s %s at %s: %s%s" % (g["side"].upper(), shown(g["symbol"]), g["sent_at"], word, extra))
    return lines


# ---------------------------------------------------------------- swing (daily bars)

class SwingClock:
    """What swing.py needs from the scanner, on the forex clock: a day is a UTC day (Yahoo's
    daily forex bar runs 00:00 to 24:00 UTC), and it is complete once UTC midnight passes."""

    def __init__(self):
        self.mod = scanner()
        self.find_setups, self.rsi = self.mod.find_setups, self.mod.rsi
        self.parse_chart, self.yahoo_get, self.chart_path = self.mod.parse_chart, self.mod.yahoo_get, self.mod.chart_path

    @staticmethod
    def to_eastern(utc):
        return utc.replace(tzinfo=None)

    @staticmethod
    def ts_to_eastern(ts):
        return datetime.datetime.fromtimestamp(int(ts), UTC).replace(tzinfo=None)

    @staticmethod
    def session(d):
        return (datetime.time(0, 0), datetime.time(23, 59, 59)) if trading_day(d)[0] else None

    @staticmethod
    def trading_day(d):
        return trading_day(d)


def swing_position(size, s, fx):
    return position(size, s["symbol"], s["buy_hi"], s["stop"], s["target"], "buy", fx)


def swing_message(s, hold, note=None):
    sym = s["symbol"]
    P = lambda x: px(sym, x)  # noqa: E731
    risk = s["close"] - s["stop"]
    import swing
    title = "🌱 SWING BUY: %s  %s forex · hold up to %d days" % (shown(sym), FLAG, hold)
    body = [
        "",
        "💰 Buy now if the price is between: %s – %s" % (P(s["buy_lo"]), P(s["buy_hi"])),
        "🎯 Take profit between: %s – %s" % (P(s["target"] - 0.25 * risk), P(s["target"])),
        "🛑 Stop loss: %s (%.0f pips)" % (P(s["stop"]), pips(sym, risk)),
        "📊 Confidence: %.0f%%" % (s["conf"] * 100),
    ]
    pos = position_lines(s.get("position"))
    if pos:
        body += [""] + pos
    body += [
        "",
        "⏰ If neither price is hit, close it after %d trading days." % hold,
        "💡 Why: %s." % swing.PLAIN[s["setup"]],
    ] + ([note] if note else []) + [
        "📜 Last %.0f years, %d similar trades: ✅ %d hit the target, ❌ %d hit the stop, ⏳ %d ran out of time." % (
            s["years"], s["n"], s["target_n"], s["stop_n"], s["time_n"]),
        "🌙 Holding overnight costs (or pays) a daily swap, and the price can gap over the weekend.",
        "",
        "⚠️ Not a promise or advice. This trade can lose, and leverage makes losses bigger.",
    ]
    return title, "\n".join(body)


def swing_closed_message(p):
    sym, o = p["symbol"], p["outcome"]
    title = {"target": "✅ TARGET HIT", "stop": "❌ STOP LOSS HIT", "time": "⏳ TIME UP",
             "no_entry": "🚫 CANCELLED"}[o] + ": %s  %s forex · swing" % (shown(sym), FLAG)
    if o == "no_entry":
        return title, "\nThe price was %s, outside the buy range %s – %s.\nNo trade. Nothing to do. 👍" % (
            px(sym, p["entry"]), px(sym, p["buy_lo"]), px(sym, p["buy_hi"]))
    exit_px = p["entry"] + p["R"] * (p["entry"] - p["stop"])
    move = exit_px - p["entry"]
    body = ["", "🛒 Bought at: %s" % px(sym, p["entry"]),
            "💵 Closed%s: %s on %s" % (" at the day's close" if o == "time" else "", px(sym, exit_px),
                                       datetime.date.fromisoformat(p["closed_on"]).strftime("%a %-d %b")),
            "%s Result: %+.0f pips (before the spread and swap)" % ("📈" if move >= 0 else "📉",
                                                                 move / pip(sym))]
    q = (p.get("position") or {}).get("qty")
    usd = usd_per_unit(sym, move * q, exit_px) if q else None
    if usd is not None:
        body.append("💵 About %s$%s for %.2f lots" % ("+" if usd >= 0 else "-", "{:,.2f}".format(abs(usd)), q / 1e5))
    return title, "\n".join(body)


# ---------------------------------------------------------------- command line

def read_pairs(path=None):
    path = path or os.path.join(HERE, "lists", "forex.txt")
    out = []
    with open(path) as fh:
        for line in fh:
            s = line.split("#", 1)[0].strip().upper().replace("/", "")
            if s:
                out.append(s if s.endswith("=X") else s + "=X")
    return out


def cmd_scan(a):
    cfg = signals.load_config()
    now = now_london(a.now)
    ok, why = trading_day(now.date())
    if not ok:
        print("Closed on %s: %s" % (now.date(), why))
        return 1
    syms = [s if s.endswith("=X") else s + "=X" for s in a.pairs.upper().replace("/", "").split(",")] if a.pairs \
        else read_pairs()
    sig = scan(syms, cfg, now, log=print)
    if not sig:
        print("No signal: nothing passed every check.")
        return 1
    sig["day"] = now.date().isoformat()
    title, body = message(sig)
    print("\n" + title + "\n" + body)
    return 0


def cmd_calendar(a):
    fx = config(signals.load_config())
    ev = calendar(force=True)
    if ev is None:
        print("The calendar could not be fetched.")
        return 1
    for e in sorted(ev, key=lambda e: e["when"]):
        if e["impact"] in fx["calendar_impact"]:
            print("%s  %s  %s" % (e["when"].astimezone(INDIA).strftime("%a %d %b %H:%M IST"), e["country"], e["title"]))
    return 0


def cmd_study(a):
    """The rule on every past session (history_days), for a grid of settings, after the
    spread. In-sample history to choose sensible settings, not a forecast."""
    cfg = signals.load_config()
    base = config(cfg)
    syms = [s if s.endswith("=X") else s + "=X" for s in a.pairs.upper().replace("/", "").split(",")] if a.pairs \
        else read_pairs()
    now = datetime.datetime.now(LONDON)
    data, src = {}, {}
    for sym in syms:
        data[sym] = past_sessions(sym, now.date(), base, now, src)
    print("%-10s %-9s %-4s %6s %6s %7s" % ("range", "stop", "R", "trades", "hit%", "avgR"))
    for orm in (15, 30, 60):
        for stop in ("range_mid", "range_far"):
            for tr in (1.0, 1.5, 2.0):
                fx = dict(base, or_minutes=orm, stop=stop)
                last_entry = slot_of(fx, fx["no_entries_after"]) - 1
                last = slot_of(fx, fx["flat_by"]) - 1
                rs, hits, n = [], 0, 0
                for sym in syms:
                    for side in fx["sides"]:
                        h = history(sym, data[sym], fx, side, tr, last, last_entry)
                        for t in h["trades"]:
                            rs.append(t["R"])
                        hits += h["target"]
                        n += h["triggered"]
                print("%-10s %-9s %-4g %6d %5.0f%% %+7.3f" % ("%d min" % orm, stop, tr, n, 100.0 * hits / n if n else 0,
                                                             statistics.mean(rs) if rs else 0))
    print("\n%d pairs, %s past sessions each (%s). R is after each pair's spread." % (
        len(syms), "/".join(str(len(v)) for v in data.values()),
        ", ".join("%d from %s" % (v, k.capitalize()) for k, v in sorted(src.items()))))
    measured = spreads_measured(syms, now.date(), base)
    if measured:
        print("\nSpread in the London session, Dukascopy (median of 5-minute averages) vs the setting:")
        for sym in syms:
            if sym in measured:
                print("  %s  %.1f pips measured, %.1f set" % (shown(sym), measured[sym], pips(sym, spread(sym, base))))
    return 0


def spreads_measured(syms, day, fx):
    """{sym: median Dukascopy spread in pips over the saved sessions}. Dukascopy is an ECN
    feed: a retail app's spread is usually wider, so this is a floor, not your spread."""
    out = {}
    for sym in syms:
        vals = []
        for d in history_dates(day, fx):
            doc = dukascopy.load_day(DUKA_DIR, pair_of(sym), d)
            vals += [b[5] for b in (doc or {}).get("bars", [])]
        if vals:
            out[sym] = statistics.median(vals)
    return out


def cmd_history(a):
    fx = config(signals.load_config())
    now = datetime.datetime.now(LONDON)
    dates = history_dates(now.date(), fx)
    print("Past sessions for the replays (history_days %d, %s to %s):" % (len(dates), dates[-1], dates[0]))
    for sym in read_pairs():
        src = {}
        try:
            past = past_sessions(sym, now.date(), fx, now, src)
        except Exception as e:
            print("  %s  no history: %s" % (shown(sym), e))
            continue
        print("  %s  %3d sessions: %3d Dukascopy, %3d Yahoo" % (shown(sym), len(past), src.get("dukascopy", 0),
                                                              src.get("yahoo", 0)))
    return 0


def cmd_fill(a):
    fx = config(signals.load_config())
    res = fill_history(read_pairs(), fx, budget_s=a.minutes * 60)
    print("Downloaded %d sessions, %d still missing.%s" % (res["fetched"], res["missing"],
                                                          (" Stopped: " + res["stopped"]) if res["stopped"] else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="the day-trade check now (or --now, London time); prints, sends nothing")
    p.add_argument("--now", help='London time "YYYY-MM-DD HH:MM" (within the last 5 days)')
    p.add_argument("--pairs", help="comma-separated, e.g. EURUSD,GBPUSD (default lists/forex.txt)")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("calendar", help="this week's high-impact releases")
    p.set_defaults(fn=cmd_calendar)
    p = sub.add_parser("study", help="the rule's record on the past sessions, by setting")
    p.add_argument("--pairs")
    p.set_defaults(fn=cmd_study)
    p = sub.add_parser("history", help="how many past sessions come from Dukascopy and from Yahoo")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("fill", help="download missing Dukascopy sessions now")
    p.add_argument("--minutes", type=float, default=30)
    p.set_defaults(fn=cmd_fill)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
