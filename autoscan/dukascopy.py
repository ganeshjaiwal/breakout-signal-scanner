#!/usr/bin/env python3
"""Forex history from Dukascopy's free datafeed: real bid and ask quotes, tick by tick.

Only past London sessions are downloaded (one file per pair per UTC hour, about 8 a day),
turned into 5-minute bars of the mid price plus the average spread, and kept for good in
data/fx/dukascopy/<PAIR>/<date>.json: a past day never changes, so it is fetched once.

The feed is unofficial and often slow or down (503 or timeouts). So the download is slow on
purpose, stops after a few failures in a row, and simply carries on at the next run; anything
missing is taken from Yahoo instead. Dukascopy is never used for today's live bars: its files
appear only after each hour ends.
"""
import datetime
import json
import lzma
import os
import struct
import time
import urllib.error
import urllib.request

BASE = "https://datafeed.dukascopy.com/datafeed"
UTC = datetime.timezone.utc
TIMEOUT = 30
PAUSE = 0.3            # seconds between requests
MAX_FAILS = 3          # failures in a row before a run gives up for now


class Unavailable(Exception):
    """The feed did not answer (503, timeout, network). Try again later."""


def scale(pair):
    return 1e3 if "JPY" in pair else 1e5


def pip(pair):
    return 0.01 if "JPY" in pair else 0.0001


def day_path(root, pair, d):
    return os.path.join(root, pair, d.isoformat() + ".json")


def hours(d, open_t, close_t, tz):
    """The UTC hours (aware datetimes) covering the session on local date d."""
    a = datetime.datetime.combine(d, open_t, tzinfo=tz).astimezone(UTC)
    b = datetime.datetime.combine(d, close_t, tzinfo=tz).astimezone(UTC)
    h = a.replace(minute=0, second=0, microsecond=0)
    out = []
    while h < b:
        out.append(h)
        h += datetime.timedelta(hours=1)
    return out


def fetch_hour(pair, h):
    """[(epoch seconds, ask, bid)] for one UTC hour of a weekday London session.
    The majors always tick in those hours, so an empty or missing file is the feed failing
    (it sometimes answers 200 with nothing), not a quiet hour: it raises, and is retried later."""
    url = "%s/%s/%04d/%02d/%02d/%02dh_ticks.bi5" % (BASE, pair, h.year, h.month - 1, h.day, h.hour)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise Unavailable("HTTP %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise Unavailable(str(getattr(e, "reason", e)))
    if not raw:
        raise Unavailable("an empty file")
    try:
        data = lzma.decompress(raw)
    except lzma.LZMAError:
        raise Unavailable("a damaged file")
    f, t0 = scale(pair), h.timestamp()
    out = []
    for i in range(len(data) // 20):
        ms, ask, bid, _, _ = struct.unpack(">3i2f", data[i * 20:i * 20 + 20])
        out.append((t0 + ms / 1000.0, ask / f, bid / f))
    if not out:
        raise Unavailable("a file with no ticks")
    return out


def bars_of(pair, ticks):
    """5-minute bars of the mid price: [[start epoch, o, h, l, c, avg spread in pips, ticks]]."""
    buckets = {}
    for t, ask, bid in ticks:
        buckets.setdefault(int(t // 300) * 300, []).append(((ask + bid) / 2, ask - bid))
    out = []
    for start in sorted(buckets):
        q = buckets[start]
        mids = [m for m, _ in q]
        sp = sum(s for _, s in q) / len(q) / pip(pair)
        out.append([start, mids[0], max(mids), min(mids), mids[-1], round(sp, 2), len(q)])
    return out


def fetch_day(root, pair, d, open_t, close_t, tz):
    """Download one past session and save it. Raises Unavailable (nothing saved) on failure."""
    ticks = []
    for i, h in enumerate(hours(d, open_t, close_t, tz)):
        if i:
            time.sleep(PAUSE)
        ticks += fetch_hour(pair, h)
    doc = {"pair": pair, "date": d.isoformat(), "source": "dukascopy", "bars": bars_of(pair, ticks)}
    path = day_path(root, pair, d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)
    return doc


def load_day(root, pair, d):
    try:
        with open(day_path(root, pair, d)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def series(docs):
    """Saved days as one Yahoo-shaped series {'t', 'o', 'h', 'l', 'c'} (plus 'spread')."""
    out = {k: [] for k in ("t", "o", "h", "l", "c", "spread")}
    for doc in docs:
        for b in doc["bars"]:
            for k, v in zip(("t", "o", "h", "l", "c", "spread"), b[:6]):
                out[k].append(v)
    return out


def wanted(today, days, is_open):
    """The last `days` open dates before today, newest first."""
    out, d = [], today
    while len(out) < days:
        d -= datetime.timedelta(days=1)
        if is_open(d):
            out.append(d)
    return out


def fill(root, pairs, dates, open_t, close_t, tz, budget_s=240, log=print):
    """Download the missing (pair, date) sessions, newest date first, until done, out of time
    or the feed fails MAX_FAILS times in a row. {'fetched', 'missing', 'stopped'}."""
    t0, fails, fetched = time.time(), 0, 0
    todo = [(d, p) for d in dates for p in pairs if not os.path.exists(day_path(root, p, d))]
    stopped = ""
    for i, (d, p) in enumerate(todo):
        if time.time() - t0 > budget_s:
            stopped = "out of time"
            break
        try:
            doc = fetch_day(root, p, d, open_t, close_t, tz)
            fetched += 1
            fails = 0
            log("dukascopy %s %s: %d bars" % (p, d, len(doc["bars"])))
        except Unavailable as e:
            fails += 1
            log("dukascopy %s %s: %s" % (p, d, e))
            if fails >= MAX_FAILS:
                stopped = "the feed is not answering (%s)" % e
                break
        time.sleep(PAUSE)
    missing = sum(1 for d in dates for p in pairs if not os.path.exists(day_path(root, p, d)))
    return {"fetched": fetched, "missing": missing, "stopped": stopped}
