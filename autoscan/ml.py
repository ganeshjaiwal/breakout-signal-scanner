"""Learn from how past signals ended, then check new signals with what was learned.

Two models, both "did the target come before the stop?" (yes = 1):

  day    one row per day-trade candidate the time machine scored (every target tried,
         sent or not, so the model does not only learn from trades the rules liked).
         The row is written when the candidate is checked (data/<market>/signals/<day>/
         tried.jsonl) and labelled after the close from that day's 5-minute bars.
  swing  one row per past swing trade the 5-year backtest walks, rebuilt on every swing
         run (data/ml/swing-<market>.jsonl.gz). Features use only bars up to the signal
         day, and the stock's own record counts only trades that closed before it.

Training (autoscan.py's "learn" step, after both markets' evening runs):

  1. Wait for enough rows, trading days and target hits (DEFAULTS "day" / "swing").
  2. Walk-forward check: fit on the older trading days and test on the newest
     test_share of days, with an embargo gap so a trade still open at the cut is not
     scored against itself.
  3. Each candidate gets an expected result, EV = p x (target in R) + (1 - p) x (the average
     R of past trades that missed the target: stops and time exits), so a
     near target that hits often does not win just for hitting often. The threshold on EV
     keeps the share of candidates with the best average R on the training days. On the test days, the model must keep at least min_test_kept rows,
     rank them with AUC >= min_auc, and the kept rows must beat ALL test rows by
     min_lift_r in average R and be positive.
  4. Pass: refit on every row and mark the model ready. Fail: it stays (or goes back to)
     not ready. That is re-checked on every training, so a model that stops working is
     taken out on its own.

The model is logistic regression in plain Python (no numpy needed on the server).

mode (signal_config.json "ml"):
  "auto"    score every candidate; block the ones under the threshold only once the
            model is ready (until then it only watches and learns)
  "shadow"  score and log, never block
  "off"     do nothing at all: no rows, no training, no scores

A model is a filter learned from past prices. It is not a promise and can be wrong.
"""
import datetime
import gzip
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("AUTOSCAN_DATA") or os.path.join(HERE, "data")
ML_DIR = os.path.join(DATA, "ml")

DEFAULTS = {
    "mode": "auto",
    "test_share": 0.25,          # newest share of trading days held out for the check
    "min_auc": 0.55,             # ranking skill on the held-out days (0.5 = a coin)
    "min_lift_r": 0.05,          # kept rows must beat all rows by this many R on average
    "min_test_kept": 30,         # and there must be at least this many kept rows to judge
    "keep_shares": [0.3, 0.4, 0.5, 0.6, 0.7],   # the threshold may keep this share of candidates
    "l2": 1.0,
    "max_rows": 100000,          # newest rows used, keeps training to about a minute
    "day": {"min_rows": 600, "min_days": 25, "min_positives": 80, "embargo_days": 1},
    "swing": {"min_rows": 3000, "min_days": 250, "min_positives": 400, "embargo_days": 15},
}

DAY_FEATURES = ["p_target", "p_stop", "sim_avg_r", "hist_n_log", "hist_hit", "hist_stop", "hist_green",
                "hist_avg_r", "target_r", "risk_pct", "chase_r", "age_min", "volume_x", "pct_over",
                "mins_open", "is_us"]
SWING_SETUPS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold_bounce")
SWING_FEATURES = ["setup_" + s for s in SWING_SETUPS] + [
    "k", "atr_pct", "ret5", "ret20", "dist_hi20", "dist_ma50", "ma20_slope", "rsi", "day_range_atr",
    "close_pos", "prior_n_log", "prior_avg_r", "prior_hit", "is_us"]
FEATURES = {"day": DAY_FEATURES, "swing": SWING_FEATURES}
REWARD = {"day": "target_r", "swing": "k"}   # the target, in R: the payoff when the model's "yes" comes true
OPEN = {"NSE": (9, 15), "US": (9, 30)}


def config(cfg):
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in ((cfg or {}).get("ml") or {}).items():
        if k in ("day", "swing") and isinstance(v, dict):
            out[k].update(v)
        elif k in out:
            out[k] = v
    return out


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---------------------------------------------------------------- features

def day_features(c, market):
    """Features of one scored day-trade candidate (the dict signals.evaluate builds)."""
    n = c.get("hist_trades") or 0
    rate = (lambda k: (c.get(k) or 0) / float(n)) if n else (lambda k: None)
    risk, entry = c.get("risk") or 0, c.get("entry") or 0
    mins = None
    try:
        t = datetime.datetime.strptime(c["entry_time"].strip(), "%I:%M %p")
        h, m = OPEN.get(market, (9, 30))
        mins = (t.hour * 60 + t.minute) - (h * 60 + m)
    except (KeyError, ValueError, AttributeError):
        pass
    return {"p_target": c.get("p_target"), "p_stop": c.get("p_stop"), "sim_avg_r": c.get("sim_avg_r"),
            "hist_n_log": math.log1p(n), "hist_hit": rate("hist_target"), "hist_stop": rate("hist_stop"),
            "hist_green": rate("hist_green"), "hist_avg_r": c.get("hist_avg_r"), "target_r": c.get("target_r"),
            "risk_pct": risk / entry * 100 if entry else None,
            "chase_r": ((c.get("last") or entry) - entry) / risk if risk else None,
            "age_min": c.get("age_min"), "volume_x": c.get("volume_x"), "pct_over": c.get("pct_over"),
            "mins_open": mins, "is_us": 1.0 if market == "US" else 0.0}


def swing_base(mod, bars, closes, atr, i):
    """Price features of daily bar i, from bars 0..i only."""
    c, b, a = closes[i], bars[i], atr[i]
    lo = max(0, i - 49)
    ma50 = sum(closes[lo:i + 1]) / (i + 1 - lo)
    ma20 = sum(closes[max(0, i - 19):i + 1]) / min(20, i + 1)
    ma20p = sum(closes[max(0, i - 24):i - 4]) / max(1, min(20, i - 4 - max(0, i - 24)))
    hi20 = max(x["h"] for x in bars[max(0, i - 19):i + 1])
    rng = b["h"] - b["l"]
    return {"atr_pct": a / c * 100, "ret5": (c / closes[max(0, i - 5)] - 1) * 100,
            "ret20": (c / closes[max(0, i - 20)] - 1) * 100, "dist_hi20": (c / hi20 - 1) * 100,
            "dist_ma50": (c / ma50 - 1) * 100, "ma20_slope": (ma20 / ma20p - 1) * 100 if ma20p else None,
            "rsi": mod.rsi(closes[max(0, i - 120):i + 1]), "day_range_atr": rng / a if a else None,
            "close_pos": (c - b["l"]) / rng if rng > 0 else 0.5}


def swing_features(base, setup, k, prior_n, prior_sum_r, prior_hits, market):
    """base from swing_base; prior_* = this stock's own earlier closed trades of this setup and k."""
    x = dict(base)
    for s in SWING_SETUPS:
        x["setup_" + s] = 1.0 if s == setup else 0.0
    x.update(k=k, prior_n_log=math.log1p(prior_n), prior_avg_r=prior_sum_r / (prior_n + 10.0),
             prior_hit=(prior_hits + 3.0) / (prior_n + 10.0), is_us=1.0 if market == "US" else 0.0)
    return x


# ---------------------------------------------------------------- rows on disk

def _open(path, mode):
    return gzip.open(path, mode + "t") if path.endswith(".gz") else open(path, mode)


def read_jsonl(path):
    out = []
    try:
        with _open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def write_jsonl(path, rows, append=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path if append else os.path.join(os.path.dirname(path), ".tmp-" + os.path.basename(path))
    with _open(tmp, "a" if append else "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    if not append:
        os.replace(tmp, path)


def _round(x):
    return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in x.items()}


def write_swing_rows(market, rows):
    write_jsonl(os.path.join(ML_DIR, "swing-%s.jsonl.gz" % market.lower()), rows)


def day_rows_path():
    return os.path.join(ML_DIR, "day.jsonl")


def load_rows(kind):
    if kind == "day":
        return read_jsonl(day_rows_path())
    rows = []
    if os.path.isdir(ML_DIR):
        for name in sorted(os.listdir(ML_DIR)):
            if name.startswith("swing-") and name.endswith(".jsonl.gz"):
                rows += read_jsonl(os.path.join(ML_DIR, name))
    return rows


def label_days(mod, market, signals_dir, outcome_fn, now_utc, max_age_days=6, log=lambda t: None):
    """Label every day-trade candidate in signals_dir/<day>/tried.jsonl that has not been
    labelled yet. A row still open (or with no data) is retried on later runs until it is
    max_age_days old. Returns how many rows were added to the day dataset."""
    if not os.path.isdir(signals_dir):
        return 0
    have = {r["id"] for r in read_jsonl(day_rows_path())}
    added = 0
    today = mod.to_eastern(now_utc).date()
    for name in sorted(os.listdir(signals_dir)):
        try:
            day = datetime.date.fromisoformat(name)
        except ValueError:
            continue
        ddir = os.path.join(signals_dir, name)
        done_path = os.path.join(ddir, "labelled.json")
        if os.path.exists(done_path) or day > today:
            continue
        tried = read_jsonl(os.path.join(ddir, "tried.jsonl"))
        bars_by_sym, new, waiting = {}, [], 0
        for t in tried:
            if t["id"] in have:
                continue
            sym = t["symbol"]
            if sym not in bars_by_sym:
                bars_by_sym[sym] = None
                try:
                    series = mod.parse_chart(mod.yahoo_get(mod.chart_path(sym, "5d", "5m")), sym)
                    bars_by_sym[sym] = mod.session_bars(series, day, mod.to_eastern(now_utc))
                except Exception:
                    pass
            res, r = outcome_fn(t, day, now_utc, bars_by_sym[sym]) if bars_by_sym[sym] is not None else ("unknown", None)
            if res in ("open", "unknown") or r is None:
                waiting += 1
                continue
            new.append({"id": t["id"], "market": market, "date": name, "symbol": sym, "x": t["x"],
                        "y": 1 if res == "target" else 0, "R": round(r, 3), "outcome": res,
                        "rules_ok": t.get("rules_ok"), "p_model": t.get("p_model")})
            have.add(t["id"])
        if new:
            write_jsonl(day_rows_path(), new, append=True)
            added += len(new)
        if not waiting or (today - day).days >= max_age_days:
            with open(done_path, "w") as fh:
                json.dump({"at": now_utc.isoformat(), "rows": len(tried), "unlabelled": waiting}, fh)
        log("learn: %s labelled %d candidate rows%s" % (name, len(new), (", %d waiting" % waiting) if waiting else ""))
    return added


# ---------------------------------------------------------------- the model

def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _scaler(X, names):
    mean, std = [], []
    for j in range(len(names)):
        v = [r[j] for r in X if r[j] is not None]
        m = sum(v) / len(v) if v else 0.0
        s = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) if v else 1.0
        mean.append(m)
        std.append(s if s > 1e-9 else 1.0)
    return mean, std


def _transform(row, mean, std):
    out = []
    for j, v in enumerate(row):
        z = 0.0 if v is None else (v - mean[j]) / std[j]
        out.append(max(-5.0, min(5.0, z)))
    out.append(1.0)   # intercept
    return out


def _solve(A, b):
    n = len(b)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        if abs(M[c][c]) < 1e-12:
            continue
        x[c] = (M[c][n] - sum(M[c][k] * x[k] for k in range(c + 1, n))) / M[c][c]
    return x


def fit(Z, y, l2, iters=25):
    """L2 logistic regression by Newton steps. Z rows end with the intercept 1."""
    d = len(Z[0])
    w = [0.0] * d
    for _ in range(iters):
        H = [[0.0] * d for _ in range(d)]
        g = [0.0] * d
        for z, t in zip(Z, y):
            p = _sigmoid(sum(a * b for a, b in zip(w, z)))
            r, s = p - t, max(p * (1 - p), 1e-6)
            for a in range(d):
                za = z[a]
                if za == 0.0:
                    continue
                g[a] += r * za
                row, sz = H[a], s * za
                for b in range(a, d):
                    row[b] += sz * z[b]
        for a in range(d):
            for b in range(a):
                H[a][b] = H[b][a]
            if a < d - 1:
                H[a][a] += l2
                g[a] += l2 * w[a]
        step = _solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def auc(p, y):
    pairs = sorted(zip(p, y))
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2.0
        rank_sum += avg * sum(t for _, t in pairs[i:j])
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _mean(v):
    return sum(v) / len(v) if v else None


def _threshold(p, R, shares):
    """The cut on p whose kept rows had the best average R, and that share."""
    order = sorted(range(len(p)), key=lambda i: -p[i])
    best = None
    for sh in shares:
        n = max(1, int(round(len(order) * sh)))
        kept = order[:n]
        m = _mean([R[i] for i in kept])
        if best is None or m > best[0]:
            best = (m, p[kept[-1]], sh)
    return best[1], best[2]


def model_path(kind):
    return os.path.join(ML_DIR, "model-%s.json" % kind)


def load_model(kind, _cache={}):
    path = model_path(kind)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(kind)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    _cache[kind] = (mt, doc)
    return doc


def predict(model, x):
    row = [_num(x.get(n)) for n in model["features"]]
    z = _transform(row, model["mean"], model["std"])
    return _sigmoid(sum(a * b for a, b in zip(model["w"], z)))


def ev(kind, x, p, miss_r=-1.0):
    """Expected result in R: the target's R when it hits, miss_r (the average of past misses) otherwise."""
    return p * (_num(x.get(REWARD[kind])) or 0.0) + (1 - p) * miss_r


def train(kind, cfg, log=lambda t: None):
    """Fit, check and save the model for kind. Returns the saved status dict."""
    mc = config(cfg)
    need = mc[kind]
    names = FEATURES[kind]
    rows = [r for r in load_rows(kind) if r.get("x") and r.get("R") is not None]
    rows.sort(key=lambda r: (r["date"], r["id"]))
    if len(rows) > mc["max_rows"]:
        rows = rows[-mc["max_rows"]:]
    days = sorted({r["date"] for r in rows})
    pos = sum(r["y"] for r in rows)
    prev = load_model(kind) or {}
    st = {"kind": kind, "trained_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
          "rows": len(rows), "days": len(days), "positives": pos, "ready": False,
          "version": prev.get("version", 0), "features": names}
    short = [w for w, have, want in (("rows", len(rows), need["min_rows"]), ("trading days", len(days), need["min_days"]),
                                     ("target hits", pos, need["min_positives"])) if have < want]
    if short:
        st["status"] = "collecting: " + ", ".join("%s %d/%d" % (w, {"rows": len(rows), "trading days": len(days),
                                                                   "target hits": pos}[w],
                                                               need[{"rows": "min_rows", "trading days": "min_days",
                                                                     "target hits": "min_positives"}[w]])
                                                  for w in short)
        _save(kind, st, prev, log)
        return st

    cut = days[int(len(days) * (1 - mc["test_share"]))]
    emb = (datetime.date.fromisoformat(cut) - datetime.timedelta(days=need["embargo_days"])).isoformat()
    tr = [r for r in rows if r["date"] < emb]
    te = [r for r in rows if r["date"] >= cut]
    X = lambda rs: [[_num(r["x"].get(n)) for n in names] for r in rs]  # noqa: E731
    Xtr, Xte = X(tr), X(te)
    mean, std = _scaler(Xtr, names)
    w = fit([_transform(r, mean, std) for r in Xtr], [r["y"] for r in tr], mc["l2"])
    score = lambda rs: [_sigmoid(sum(a * b for a, b in zip(w, _transform(r, mean, std)))) for r in rs]  # noqa: E731
    ptr, pte = score(Xtr), score(Xte)
    miss = _mean([r["R"] for r in tr if not r["y"]]) or -1.0
    etr = [ev(kind, r["x"], p, miss) for r, p in zip(tr, ptr)]
    ete = [ev(kind, r["x"], p, miss) for r, p in zip(te, pte)]
    thr, share = _threshold(etr, [r["R"] for r in tr], mc["keep_shares"])
    kept = [r for r, e in zip(te, ete) if e >= thr]
    a = auc(pte, [r["y"] for r in te])
    all_r, kept_r = _mean([r["R"] for r in te]), _mean([r["R"] for r in kept])
    m = {"train_rows": len(tr), "test_rows": len(te), "test_from": cut, "auc": a, "keep_share": share,
         "test_avg_r_all": all_r, "test_avg_r_kept": kept_r, "test_kept": len(kept),
         "test_hit_all": _mean([r["y"] for r in te]), "test_hit_kept": _mean([r["y"] for r in kept]),
         "test_auc_ev_vs_r": auc(ete, [1 if r["R"] > 0 else 0 for r in te])}
    ruled = [(r, e) for r, e in zip(te, ete) if r.get("rules_ok")]
    if ruled:
        m["test_rules_ok"] = len(ruled)
        m["test_avg_r_rules"] = _mean([r["R"] for r, _ in ruled])
        m["test_avg_r_rules_kept"] = _mean([r["R"] for r, p in ruled if p >= thr])
    fails = []
    if len(kept) < mc["min_test_kept"]:
        fails.append("only %d test rows kept (need %d)" % (len(kept), mc["min_test_kept"]))
    if a is None or a < mc["min_auc"]:
        fails.append("AUC %s < %.2f" % ("n/a" if a is None else "%.3f" % a, mc["min_auc"]))
    if kept_r is None or kept_r - all_r < mc["min_lift_r"]:
        fails.append("kept rows %s vs all %+.3fR (need +%.2fR)" % ("n/a" if kept_r is None else "%+.3fR" % kept_r,
                                                                   all_r, mc["min_lift_r"]))
    if kept_r is not None and kept_r <= 0:
        fails.append("kept rows lose on average")
    if len(ruled) >= mc["min_test_kept"] and m["test_avg_r_rules_kept"] is not None \
            and m["test_avg_r_rules_kept"] < m["test_avg_r_rules"]:
        fails.append("worse than the rules alone on the signals they pass")

    # Refit on everything for live scoring (in shadow too, so its scores can be compared later).
    Xall = X(rows)
    mean, std = _scaler(Xall, names)
    w = fit([_transform(r, mean, std) for r in Xall], [r["y"] for r in rows], mc["l2"])
    miss = _mean([r["R"] for r in rows if not r["y"]]) or -1.0
    pall = sorted((ev(kind, r["x"], _sigmoid(sum(a * b for a, b in zip(w, _transform(x, mean, std)))), miss)
                   for r, x in zip(rows, Xall)), reverse=True)
    thr_all = pall[max(0, min(len(pall) - 1, int(round(len(pall) * share)) - 1))]
    st.update(metrics=m, mean=mean, std=std, w=w, threshold=thr_all, miss_r=miss, ready=not fails,
              status="ready" if not fails else "not ready: " + "; ".join(fails))
    _save(kind, st, prev, log)
    return st


def _save(kind, st, prev, log):
    if st.get("w") is not None:
        st["version"] = prev.get("version", 0) + 1
    else:
        st["ready"] = False
    st["was_ready"] = bool(prev.get("ready"))
    os.makedirs(ML_DIR, exist_ok=True)
    tmp = model_path(kind) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, model_path(kind))
    with open(os.path.join(ML_DIR, "history.jsonl"), "a") as fh:
        fh.write(json.dumps({k: st.get(k) for k in ("kind", "trained_at", "version", "rows", "days", "positives",
                                                    "ready", "status", "metrics")}) + "\n")
    log("learn: %s model v%s, %d rows over %d days: %s" % (kind, st["version"], st["rows"], st["days"], st["status"]))


def dataset_stamp(kind):
    """Changes whenever the rows for kind change: training is skipped when it has not."""
    if kind == "day":
        paths = [day_rows_path()]
    else:
        paths = [os.path.join(ML_DIR, n) for n in sorted(os.listdir(ML_DIR))
                 if n.startswith("swing-") and n.endswith(".jsonl.gz")] if os.path.isdir(ML_DIR) else []
    out = []
    for p in paths:
        try:
            s = os.stat(p)
            out.append("%s:%d:%d" % (os.path.basename(p), s.st_size, int(s.st_mtime)))
        except OSError:
            pass
    return "|".join(out)


def learn(cfg, log=lambda t: None):
    """Retrain each model whose rows changed since its last training. Returns
    [(kind, status, changed_readiness)]."""
    if config(cfg)["mode"] == "off":
        return []
    out = []
    stamp_path = os.path.join(ML_DIR, "trained-stamps.json")
    try:
        with open(stamp_path) as fh:
            stamps = json.load(fh)
    except (OSError, ValueError):
        stamps = {}
    for kind in ("swing", "day"):
        s = dataset_stamp(kind)
        if not s or stamps.get(kind) == s:
            continue
        st = train(kind, cfg, log)
        stamps[kind] = s
        out.append((kind, st, st["ready"] != st["was_ready"]))
    os.makedirs(ML_DIR, exist_ok=True)
    with open(stamp_path, "w") as fh:
        json.dump(stamps, fh)
    return out


def check(kind, x, cfg):
    """Score one candidate. Returns None when the mode is off or no model is fitted yet,
    else {'p', 'ev', 'ready', 'threshold', 'version', 'pass', 'mode'}. 'pass' is False only
    when mode is auto, the model is ready and the expected R 'ev' is under its threshold."""
    mc = config(cfg)
    if mc["mode"] == "off":
        return None
    m = load_model(kind)
    if not m or m.get("w") is None:
        return None
    p = predict(m, x)
    e = ev(kind, x, p, m.get("miss_r", -1.0))
    ready = bool(m.get("ready"))
    return {"p": round(p, 4), "ev": round(e, 4), "ready": ready, "threshold": round(m["threshold"], 4),
            "version": m.get("version"), "mode": mc["mode"],
            "pass": not (mc["mode"] == "auto" and ready and e < m["threshold"])}


def active(chk):
    """True when this score is the live gate (shown in messages, used for ranking)."""
    return bool(chk and chk["ready"] and chk["mode"] == "auto")


def status_text(cfg):
    mc = config(cfg)
    lines = ["mode: %s" % mc["mode"]]
    for kind in ("day", "swing"):
        m = load_model(kind)
        if not m:
            lines.append("%-5s no training yet" % kind)
            continue
        lines.append("%-5s v%s %s · %d rows, %d days, %d target hits · trained %s" % (
            kind, m.get("version"), m.get("status"), m["rows"], m["days"], m["positives"], m["trained_at"]))
        mt = m.get("metrics")
        if mt:
            f = lambda v: "n/a" if v is None else "%+.3f" % v  # noqa: E731
            lines.append("      test from %s: AUC %s, avg R all %s -> kept %s (%d of %d kept), EV threshold %+.3fR" % (
                mt["test_from"], "n/a" if mt["auc"] is None else "%.3f" % mt["auc"], f(mt["test_avg_r_all"]),
                f(mt["test_avg_r_kept"]), mt["test_kept"], mt["test_rows"], m["threshold"]))
    return "\n".join(lines)
