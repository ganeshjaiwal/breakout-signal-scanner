"""Swing signals: trades held for days, from daily candles.

Once a day after the close, for every stock in a market's list:

  1. Setups on daily closes (the scanner's own rules): 20-day closing-high breakout,
     bull flag, reclaimed the 20-day average, oversold bounce.
  2. A plan from the signal day's close C and its 14-day average true range (ATR):
       buy range   C - 0.5 ATR  to  C + 0.5 ATR   (next session's open must be inside it)
       stop        C - 1.5 ATR
       target      C + K x 1.5 ATR, K tried at 1.5, 2 and 3
       time exit   the close of the 10th session after entry
  3. A backtest of the same setup and plan on that stock's own last 5 years, bar by bar:
     enter at the next open if it is inside the buy range, stop first when a day touches
     both, a gap through the stop fills at the open (a loss bigger than 1R).
  4. Gates (signal_config.json, "swing"): enough past trades, a positive average for this
     stock AND for the whole list, a minimum share of trades that reached the target.

"Confidence" is the share of this stock's past trades of the same setup that reached
the target before the stop or the time exit. It is in-sample history, not a promise.
"""
import datetime
import json
import os

import ml
import signals

SETUPS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold_bounce")
LABEL = {"breakout_20d": "20-day closing-high breakout", "bull_flag": "bull flag",
         "reclaim_ma20": "reclaimed the 20-day average", "oversold_bounce": "oversold bounce"}
WINDOW = 120   # closes handed to the setup rules, live and in the backtest alike

DEFAULTS = {
    "enabled": True,
    "range": "5y",
    "atr_days": 14,
    "stop_atr": 1.5,
    "buy_band_atr": 0.5,
    "targets_r": [1.5, 2.0, 3.0],
    "hold_days": 10,
    "min_trades": 10,            # past trades of this setup on this stock
    "min_avg_r": 0.15,           # their average result, in R
    "min_pool_avg_r": 0.0,       # the same setup and target across the whole list
    "min_confidence_pct": 30,
    "prior_trades": 30,          # pull each stock's numbers toward the whole-list numbers as if it had this
                                 # many extra average trades: a lucky handful of trades cannot top the ranking
    "max_signals_per_day": 1,    # per market
    "max_open": 3,               # per market: no new signal while this many are open
}


def config(cfg):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("swing") or {}).items() if k in DEFAULTS})
    return out


def daily_bars(mod, sym, rng, now_utc):
    """Completed daily bars, oldest first: [{'d': date, 'o','h','l','c'}]. Today's bar counts
    only once the session has closed."""
    series = mod.parse_chart(mod.yahoo_get(mod.chart_path(sym, rng, "1d")), sym)
    local_now = mod.to_eastern(now_utc)
    sess = mod.session(local_now.date())
    out = {}
    for i, ts in enumerate(series["t"]):
        vals = [series[k][i] if series.get(k) else None for k in ("o", "h", "l", "c")]
        if ts is None or None in vals:
            continue
        d = mod.ts_to_eastern(ts).date()
        if d > local_now.date() or (d == local_now.date() and (sess is None or local_now.time() < sess[1])):
            continue
        out[d] = {"d": d, "o": float(vals[0]), "h": float(vals[1]), "l": float(vals[2]), "c": float(vals[3])}
    # Yahoo can leave the latest daily bar without a close for hours after the session ends
    # (seen on NSE). Rebuild that one day from its 5-minute bars and the quoted last price.
    stamps = [t for t in series["t"] if t is not None]
    if stamps:
        d = mod.ts_to_eastern(stamps[-1]).date()
        over = d < local_now.date() or (sess is not None and local_now.time() >= sess[1])
        if d not in out and d <= local_now.date() and over:
            intra = mod.parse_chart(mod.yahoo_get(mod.chart_path(sym, "5d", "5m")), sym)
            rows = [(intra["o"][i], intra["h"][i], intra["l"][i], intra["c"][i]) for i, ts in enumerate(intra["t"])
                    if ts is not None and mod.ts_to_eastern(ts).date() == d
                    and None not in (intra["o"][i], intra["h"][i], intra["l"][i], intra["c"][i])]
            if len(rows) >= 30:
                meta = series.get("meta") or {}
                last = meta.get("regularMarketPrice")
                rmt = meta.get("regularMarketTime")
                close = float(last) if last and rmt and mod.ts_to_eastern(rmt).date() == d else float(rows[-1][3])
                out[d] = {"d": d, "o": float(rows[0][0]), "h": max(max(r[1] for r in rows), close),
                          "l": min(min(r[2] for r in rows), close), "c": close}
    return [out[k] for k in sorted(out)]


def atr_series(bars, n):
    """Simple n-day average true range for each bar (None until there are n true ranges)."""
    tr = [None]
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]["c"]
        tr.append(max(b["h"] - b["l"], abs(b["h"] - p), abs(b["l"] - p)))
    out = [None] * len(bars)
    for i in range(n, len(bars)):
        out[i] = sum(tr[i - n + 1:i + 1]) / n
    return out


def levels(close, atr, k, sc):
    risk = sc["stop_atr"] * atr
    return {"buy_lo": close - sc["buy_band_atr"] * atr, "buy_hi": close + sc["buy_band_atr"] * atr,
            "stop": close - risk, "target": close + k * risk}


def walk(bars, i, lv, hold):
    """The trade signalled at bar i's close. Returns None when it never entered (no next bar,
    or the next open was outside the buy range), else {'outcome', 'R', 'exit_i', 'entry'}.
    outcome is 'target', 'stop', 'time', or 'open' when the data ends first."""
    if i + 1 >= len(bars):
        return None
    entry = bars[i + 1]["o"]
    if not lv["buy_lo"] <= entry <= lv["buy_hi"]:
        return {"outcome": "no_entry", "R": 0.0, "exit_i": i + 1, "entry": entry}
    risk = entry - lv["stop"]
    last = min(len(bars) - 1, i + hold)
    for j in range(i + 1, last + 1):
        b = bars[j]
        if b["l"] <= lv["stop"]:
            fill = min(b["o"], lv["stop"])           # a gap under the stop fills at the open
            return {"outcome": "stop", "R": (fill - entry) / risk, "exit_i": j, "entry": entry}
        if b["h"] >= lv["target"]:
            return {"outcome": "target", "R": (lv["target"] - entry) / risk, "exit_i": j, "entry": entry}
    done = last == i + hold
    return {"outcome": "time" if done else "open", "R": (bars[last]["c"] - entry) / risk, "exit_i": last,
            "entry": entry}


def backtest(mod, bars, sc, rows=None, sym=None, market=None):
    """{(setup, k): [R, ...] with outcomes} over every past signal day of this stock, one trade
    at a time per setup and target (a new signal while one is open is ignored).
    rows: a list to append one learning row per trade to (ml.py), features as of the signal day."""
    closes = [b["c"] for b in bars]
    atr = atr_series(bars, sc["atr_days"])
    res = {(s, k): {"R": [], "target": 0, "stop": 0, "time": 0} for s in SETUPS for k in sc["targets_r"]}
    busy = {key: -1 for key in res}
    for i in range(max(40, sc["atr_days"] + 1), len(bars) - 1):
        if atr[i] is None or atr[i] <= 0:
            continue
        tags, _ = mod.find_setups(closes[max(0, i - WINDOW + 1):i + 1])
        for s in tags:
            if s not in SETUPS:
                continue
            for k in sc["targets_r"]:
                key = (s, k)
                if i <= busy[key]:
                    continue
                t = walk(bars, i, levels(closes[i], atr[i], k, sc), sc["hold_days"])
                if not t or t["outcome"] in ("no_entry", "open"):
                    continue
                if rows is not None:
                    r = res[key]
                    x = ml.swing_features(ml.swing_base(mod, bars, closes, atr, i), s, k, len(r["R"]), sum(r["R"]),
                                          r["target"], market)
                    rows.append({"id": "%s|%s|%s|%g" % (sym, bars[i]["d"].isoformat(), s, k), "market": market,
                                 "date": bars[i]["d"].isoformat(), "symbol": sym, "x": ml._round(x),
                                 "y": 1 if t["outcome"] == "target" else 0, "R": round(t["R"], 3),
                                 "outcome": t["outcome"]})
                busy[key] = t["exit_i"]
                res[key]["R"].append(t["R"])
                res[key][t["outcome"]] += 1
    return res


def stats(r):
    n = len(r["R"])
    return {"n": n, "avg_r": (sum(r["R"]) / n) if n else 0.0, "target_n": r["target"], "stop_n": r["stop"],
            "time_n": r["time"], "conf": (r["target"] / n) if n else 0.0,
            "green": (sum(1 for x in r["R"] if x > 0) / n) if n else 0.0}


def scan(mod, symbols, sc, now_utc, log=lambda t: None, market=None, rows=None):
    """Backtest every symbol, then list today's candidates. Returns (candidates, pool, as_of, failed).
    rows: a list to collect the backtest's learning rows into (see backtest)."""
    pool = {}
    live = []
    failed = 0
    dates = {}
    for sym in symbols:
        try:
            bars = daily_bars(mod, sym, sc["range"], now_utc)
        except Exception:
            failed += 1
            continue
        if len(bars) < 250:
            failed += 1
            continue
        bt = backtest(mod, bars, sc, rows, sym, market)
        for key, r in bt.items():
            p = pool.setdefault(key, {"R": [], "target": 0, "stop": 0, "time": 0})
            p["R"] += r["R"]
            for o in ("target", "stop", "time"):
                p[o] += r[o]
        closes = [b["c"] for b in bars]
        atrs = atr_series(bars, sc["atr_days"])
        atr = atrs[-1]
        dates[bars[-1]["d"]] = dates.get(bars[-1]["d"], 0) + 1
        tags, _ = mod.find_setups(closes[-WINDOW:])
        for s in tags:
            if s in SETUPS and atr:
                live.append({"symbol": sym, "setup": s, "close": closes[-1], "atr": atr, "date": bars[-1]["d"],
                             "ml_base": ml.swing_base(mod, bars, closes, atrs, len(bars) - 1),
                             "bt": {k: stats(bt[(s, k)]) for k in sc["targets_r"]}, "years": len(bars) / 252.0})
    as_of = max(dates, key=lambda d: (dates[d], d)) if dates else None
    live = [c for c in live if c["date"] == as_of]
    return live, {k: stats(v) for k, v in pool.items()}, as_of, failed


def choose(live, pool, sc, size, skip=(), log=lambda t: None, cfg=None, market=None):
    """The passing candidates, best first. Each is one (symbol, setup, target) plan.
    cfg and market turn on the learned check (ml.py)."""
    out = []
    for c in live:
        if c["symbol"] in skip:
            log("swing %s %s: already open" % (c["symbol"], c["setup"]))
            continue
        best, why = None, ""
        for k in sc["targets_r"]:
            raw, pl = c["bt"][k], pool[(c["setup"], k)]
            m = sc["prior_trades"]
            st = dict(raw, raw_avg_r=raw["avg_r"], raw_conf=raw["conf"],
                      avg_r=(raw["n"] * raw["avg_r"] + m * pl["avg_r"]) / (raw["n"] + m) if raw["n"] + m else 0.0,
                      conf=(raw["target_n"] + m * pl["conf"]) / (raw["n"] + m) if raw["n"] + m else 0.0)
            fails = []
            if st["n"] < sc["min_trades"]:
                fails.append("only %d past trades" % st["n"])
            else:
                if st["avg_r"] < sc["min_avg_r"]:
                    fails.append("adjusted average %+.2fR < %+.2fR" % (st["avg_r"], sc["min_avg_r"]))
                if st["conf"] * 100 < sc["min_confidence_pct"]:
                    fails.append("confidence %.0f%% < %s%%" % (st["conf"] * 100, sc["min_confidence_pct"]))
            if pl["avg_r"] < sc["min_pool_avg_r"]:
                fails.append("whole-list average %+.2fR" % pl["avg_r"])
            chk = None
            if cfg is not None and market and c.get("ml_base"):
                x = ml.swing_features(c["ml_base"], c["setup"], k, raw["n"], raw["avg_r"] * raw["n"], raw["target_n"],
                                      market)
                chk = ml.check("swing", x, cfg)
                if chk:
                    log("swing %s %s %gR: AI %.0f%% target first, %+.2fR expected (needs %+.2fR, %s)" % (
                        c["symbol"], c["setup"], k, chk["p"] * 100, chk["ev"], chk["threshold"],
                        "live" if ml.active(chk) else "watching only"))
                if not fails and chk and not chk["pass"]:
                    fails.append("AI model %+.2fR < %+.2fR" % (chk["ev"], chk["threshold"]))
            if fails:
                why = why or "%gR: %s" % (k, "; ".join(fails))
                continue
            if best is None or st["avg_r"] > best["avg_r"]:
                lv = levels(c["close"], c["atr"], k, sc)
                best = dict(st, k=k, pool_avg_r=pl["avg_r"], pool_n=pl["n"], ml=chk,
                            **{x: round(v, 2) for x, v in lv.items()})
        if best is None:
            log("swing %s %s: %s" % (c["symbol"], c["setup"], why))
            continue
        pos = None
        if size:
            pos, why = signals.position(size, best["buy_hi"], best["stop"], best["target"])
            if pos is None:
                log("swing %s %s: %s" % (c["symbol"], c["setup"], why))
                continue
            pos["charges"] = round(delivery_charges(pos["cost"], pos["qty"] * best["target"]), 2)
        log("swing %s %s: PASS %.0f%% %+.2fR at %gR over %d trades" % (
            c["symbol"], c["setup"], best["conf"] * 100, best["avg_r"], best["k"], best["n"]))
        out.append({"symbol": c["symbol"], "setup": c["setup"], "date": c["date"].isoformat(),
                    "close": round(c["close"], 2), "atr": round(c["atr"], 2), "years": round(c["years"], 1),
                    "position": pos, **best})
    def rank(x):
        if ml.active(x.get("ml")):   # the learned odds, as an expected result in R
            return x["ml"]["ev"]
        return x["avg_r"]
    out.sort(key=lambda x: -rank(x))
    return out


def delivery_charges(buy_value, sell_value):
    """Rough cost of an NSE delivery trade at a typical discount broker: no brokerage, STT 0.1%
    each side, exchange 0.00297%, SEBI 0.0001%, stamp duty 0.015% on the buy, 18% GST on
    exchange + SEBI, and a depository charge of about Rs.15.34 on the sell. An estimate only."""
    turn = buy_value + sell_value
    ex = 0.0000297 * turn + 0.000001 * turn
    return 0.001 * turn + ex * 1.18 + 0.00015 * buy_value + 15.34


PLAIN = {"breakout_20d": "the price closed at its highest in 20 days",
         "bull_flag": "a strong rise, then a calm pause (bull flag)",
         "reclaim_ma20": "the price climbed back above its 20-day average",
         "oversold_bounce": "the price bounced after a sharp fall"}


def sessions_after(mod, day, n):
    """The next n trading days after `day`, from the scanner's own calendar."""
    out, d = [], day
    while len(out) < n:
        d += datetime.timedelta(days=1)
        if mod.trading_day(d)[0]:
            out.append(d)
    return out


def message(market, label, shown, s, hold, mod=None):
    P = lambda x: signals.price(market, x)  # noqa: E731
    title = "🌱 SWING BUY: %s  %s %s · hold up to %d days" % (shown, signals.FLAG.get(market, ""), market, hold)
    risk = s["close"] - s["stop"]
    when, last = "the next market open", None
    if mod is not None:
        days = sessions_after(mod, datetime.date.fromisoformat(s["date"]), hold)
        when, last = days[0].strftime("%a %-d %b") + " open", days[-1].strftime("%a %-d %b")
    body = [
        "",
        "💰 Buy between: %s – %s" % (P(s["buy_lo"]), P(s["buy_hi"])),
        "      at %s. Skip it if it opens outside this range." % when,
        "🎯 Sell between: %s – %s" % (P(s["target"] - 0.25 * risk), P(s["target"])),
        "🛑 Stop loss: %s" % P(s["stop"]),
        "📊 Confidence: %.0f%%" % (s["conf"] * 100),
    ]
    body += signals.ai_lines(s.get("ml"))
    pos = signals.position_lines(market, s.get("position"), delivery=True)
    if pos:
        body += [""] + pos
    body += [
        "",
        "⏰ If neither price is hit, sell at the close on %s." % (last or "the %dth trading day" % hold),
        "💡 Why: %s." % PLAIN[s["setup"]],
        "📜 Last %.0f years, %d similar trades: ✅ %d hit the target, ❌ %d hit the stop, ⏳ %d ran out of time." % (
            s["years"], s["n"], s["target_n"], s["stop_n"], s["time_n"]),
        "🌙 The price can open below the stop overnight, so a loss can be bigger.",
        "",
        "⚠️ Not a promise or advice. This trade can lose.",
    ]
    return title, "\n".join(body)


def update_open(mod, positions, sc, now_utc):
    """Walk each open position forward on fresh daily bars. Returns (still_open, closed)."""
    still, closed = [], []
    for p in positions:
        try:
            bars = daily_bars(mod, p["symbol"], "3mo", now_utc)
        except Exception:
            still.append(p)
            continue
        idx = [i for i, b in enumerate(bars) if b["d"].isoformat() == p["date"]]
        if not idx:
            still.append(p)
            continue
        t = walk(bars, idx[0], p, sc["hold_days"])
        if t is None or t["outcome"] == "open":
            if t:
                p["entry"] = round(t["entry"], 2)
            still.append(p)
            continue
        p.update(outcome=t["outcome"], R=round(t["R"], 2), entry=round(t["entry"], 2),
                 closed_on=bars[t["exit_i"]]["d"].isoformat())
        closed.append(p)
    return still, closed


def closed_message(market, shown, p):
    P = lambda x: signals.price(market, x)  # noqa: E731
    o = p["outcome"]
    title = {"target": "✅ TARGET HIT", "stop": "❌ STOP LOSS HIT", "time": "⏳ TIME UP (10 days)",
             "no_entry": "🚫 CANCELLED"}[o] + ": %s  %s %s · swing" % (shown, signals.FLAG.get(market, ""), market)
    if o == "no_entry":
        return title, "\nIt opened at %s, outside the buy range %s – %s.\nNo trade. Nothing to do. 👍" % (
            P(p["entry"]), P(p["buy_lo"]), P(p["buy_hi"]))
    exit_px = p["entry"] + p["R"] * (p["entry"] - p["stop"])
    per = exit_px - p["entry"]
    body = ["", "🛒 Bought at the open: %s" % P(p["entry"]),
            "💵 Sold%s: %s on %s" % (" at the close" if o == "time" else "", P(exit_px), datetime.date.fromisoformat(p["closed_on"]).strftime("%a %-d %b"))]
    q = (p.get("position") or {}).get("qty")
    if q:
        body.append("%s Result: %s%s for %d share%s (before charges)" % (
            "📈" if per >= 0 else "📉", "+" if per >= 0 else "-", signals.money(market, per * q), q, "" if q == 1 else "s"))
    else:
        body.append("%s Result: %s%s per share" % ("📈" if per >= 0 else "📉", "+" if per >= 0 else "-",
                                                    signals.price(market, abs(per))))
    return title, "\n".join(body)


def load_book(path):
    try:
        with open(path) as fh:
            doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_book(path, doc):
    with open(path + ".tmp", "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(path + ".tmp", path)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
