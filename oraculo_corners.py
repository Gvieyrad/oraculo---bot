#!/usr/bin/env python3
# oraculo_corners.py - Corners Cantera Model (GBM)
# Predicts total corners using rolling team features + GBM regression.
# CANTERA ONLY: all picks shadow_only=True until n>=60 WR validated.
import os, json, math, logging, difflib, pickle
from collections import defaultdict

log = logging.getLogger("oraculo.corners")

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR      = os.path.join(SCRIPT_DIR, ".oraculo_cache", "csv")
MODEL_PICKLE   = os.path.join(SCRIPT_DIR, ".oraculo_cache", "corners_gbm.pkl")

MIN_TEAM_MATCHES = 5
MIN_EDGE         = 0.07
MIN_CONF         = 0.58
CANTERA_LIVE_N   = 60
_SEASONS         = ("2526", "2425")
N_ROLL           = 10


def _poisson_pmf(k, lam):
    if lam <= 0: return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_over(lam, line):
    k_max = int(math.floor(line))
    return 1.0 - sum(_poisson_pmf(k, lam) for k in range(0, k_max + 1))


def _roll_avg(history, field, n=N_ROLL):
    if not history: return None
    vals = [h[field] for h in history[-n:]]
    return sum(vals) / len(vals)


def _cache_mtime():
    try:
        return max(os.path.getmtime(os.path.join(CACHE_DIR, f))
                   for f in os.listdir(CACHE_DIR) if f.endswith(".json"))
    except Exception:
        return 0.0


class CornersGBM:

    FEAT_COLS = [
        "h_corners_avg", "a_corners_avg",
        "h_shots_avg",   "a_shots_avg",
        "h_sot_avg",     "a_sot_avg",
        "h_fouls_avg",   "a_fouls_avg",
        "lg_avg",        "ref_avg",      "has_ref",
    ]

    def __init__(self):
        self.gbm        = None
        self.home_feats = {}
        self.away_feats = {}
        self.league_avg = {}
        self.ref_avg    = {}
        self.global_avg = 9.7
        self.trained    = False

    def load(self):
        if self._try_load_pickle():
            return True
        return self._train_from_cache()

    def _try_load_pickle(self):
        if not os.path.exists(MODEL_PICKLE):
            return False
        try:
            if _cache_mtime() > os.path.getmtime(MODEL_PICKLE):
                log.info("[Corners] Cache newer than pickle - retraining")
                return False
            with open(MODEL_PICKLE, "rb") as f:
                data = pickle.load(f)
            self.gbm        = data["gbm"]
            self.home_feats = data["home_feats"]
            self.away_feats = data["away_feats"]
            self.league_avg = data["league_avg"]
            self.ref_avg    = data["ref_avg"]
            self.global_avg = data["global_avg"]
            self.trained    = True
            log.info("[Corners] GBM loaded from pickle (%d home / %d away teams)",
                     len(self.home_feats), len(self.away_feats))
            return True
        except Exception as e:
            log.warning("[Corners] Pickle load failed: %s", e)
            return False

    def _train_from_cache(self):
        try:
            import numpy as np
            from sklearn.ensemble import GradientBoostingRegressor
        except ImportError:
            log.error("[Corners] sklearn not available")
            return False

        matches = self._read_cache()
        if len(matches) < 200:
            log.warning("[Corners] Too few matches: %d", len(matches))
            return False

        home_hist = defaultdict(list)
        away_hist = defaultdict(list)
        lg_hist   = defaultdict(list)
        ref_hist  = defaultdict(list)
        X_rows, y_rows = [], []

        for m in matches:
            ht, at   = m["home"], m["away"]
            lg, ref  = m["league"], m["referee"]
            hh, ah   = home_hist[ht], away_hist[at]
            lg_list  = lg_hist[lg][-50:]  if lg_hist[lg]           else []
            ref_list = ref_hist[ref][-30:] if ref and ref_hist[ref] else []

            if len(hh) >= MIN_TEAM_MATCHES and len(ah) >= MIN_TEAM_MATCHES:
                feat = [
                    _roll_avg(hh, "c"),  _roll_avg(ah, "c"),
                    _roll_avg(hh, "s"),  _roll_avg(ah, "s"),
                    _roll_avg(hh, "st"), _roll_avg(ah, "st"),
                    _roll_avg(hh, "f"),  _roll_avg(ah, "f"),
                    sum(lg_list)  / len(lg_list)  if lg_list  else self.global_avg,
                    sum(ref_list) / len(ref_list) if ref_list else self.global_avg,
                    1.0 if ref and len(ref_hist[ref]) >= 5 else 0.0,
                ]
                X_rows.append(feat)
                y_rows.append(m["total"])

            home_hist[ht].append({"c": m["hc"], "s": m["hs"], "st": m["hst"], "f": m["hf"]})
            away_hist[at].append({"c": m["ac"], "s": m["aw"], "st": m["ast"], "f": m["af"]})
            lg_hist[lg].append(m["total"])
            if ref: ref_hist[ref].append(m["total"])

        if len(X_rows) < 500:
            log.warning("[Corners] Not enough training rows: %d", len(X_rows))
            return False

        import numpy as np
        from sklearn.ensemble import GradientBoostingRegressor

        X = np.array(X_rows, dtype=float)
        y = np.array(y_rows, dtype=float)
        gbm = GradientBoostingRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, random_state=42)
        gbm.fit(X, y)

        for team, hist in home_hist.items():
            if len(hist) >= MIN_TEAM_MATCHES:
                self.home_feats[team] = {
                    "c":  _roll_avg(hist, "c"),
                    "s":  _roll_avg(hist, "s"),
                    "st": _roll_avg(hist, "st"),
                    "f":  _roll_avg(hist, "f"),
                }
        for team, hist in away_hist.items():
            if len(hist) >= MIN_TEAM_MATCHES:
                self.away_feats[team] = {
                    "c":  _roll_avg(hist, "c"),
                    "s":  _roll_avg(hist, "s"),
                    "st": _roll_avg(hist, "st"),
                    "f":  _roll_avg(hist, "f"),
                }

        all_totals = [m["total"] for m in matches]
        self.global_avg = sum(all_totals) / len(all_totals)
        for lg, vals in lg_hist.items():
            if vals: self.league_avg[lg] = sum(vals) / len(vals)
        for ref, vals in ref_hist.items():
            if len(vals) >= 5: self.ref_avg[ref] = sum(vals) / len(vals)

        self.gbm     = gbm
        self.trained = True
        log.info("[Corners] GBM trained on %d rows (%d home / %d away teams)",
                 len(X_rows), len(self.home_feats), len(self.away_feats))

        try:
            os.makedirs(os.path.dirname(MODEL_PICKLE), exist_ok=True)
            with open(MODEL_PICKLE, "wb") as f:
                pickle.dump({
                    "gbm":        self.gbm,
                    "home_feats": self.home_feats,
                    "away_feats": self.away_feats,
                    "league_avg": self.league_avg,
                    "ref_avg":    self.ref_avg,
                    "global_avg": self.global_avg,
                }, f)
            log.info("[Corners] GBM pickled to %s", MODEL_PICKLE)
        except Exception as e:
            log.warning("[Corners] Pickle save failed: %s", e)

        return True

    def _read_cache(self):
        rows = []
        for fname in sorted(os.listdir(CACHE_DIR)):
            if not fname.endswith(".json"): continue
            if not any(s in fname for s in _SEASONS): continue
            league = fname.replace("_2526.json", "").replace("_2425.json", "")
            try: data = json.load(open(os.path.join(CACHE_DIR, fname)))
            except Exception: continue
            for m in data:
                hc = m.get("home_corners"); ac = m.get("away_corners")
                if hc is None or ac is None: continue
                try: hc, ac = int(hc), int(ac)
                except (ValueError, TypeError): continue
                hs  = m.get("home_shots");        aw  = m.get("away_shots")
                hst = m.get("home_shots_target"); ast = m.get("away_shots_target")
                hf  = m.get("home_fouls");        af  = m.get("away_fouls")
                if any(x is None for x in [hs, aw, hst, ast, hf, af]): continue
                try:
                    rows.append({
                        "hc": hc, "ac": ac, "total": hc + ac,
                        "hs": int(hs), "aw": int(aw),
                        "hst": int(hst), "ast": int(ast),
                        "hf": int(hf), "af": int(af),
                        "date":    (m.get("utc_date") or "")[:10],
                        "league":  league,
                        "home":    (m.get("home_team") or "").strip(),
                        "away":    (m.get("away_team") or "").strip(),
                        "referee": (m.get("referee")   or "").strip(),
                    })
                except (ValueError, TypeError):
                    continue
        rows.sort(key=lambda x: x["date"])
        return rows

    def _fuzzy(self, name, lookup):
        if not name: return None
        if name in lookup: return name
        key = name.lower().strip()
        for c in lookup:
            if c.lower() == key: return c
        m = difflib.get_close_matches(name, list(lookup.keys()), n=1, cutoff=0.72)
        return m[0] if m else None

    def predict(self, home_team, away_team, league=None, referee=None):
        if not self.trained or self.gbm is None:
            return None, {}
        import numpy as np

        lg_avg   = self.league_avg.get(league or "", self.global_avg)
        ref_key  = self._fuzzy(referee or "", self.ref_avg)
        ref_val  = self.ref_avg.get(ref_key, lg_avg) if ref_key else lg_avg
        has_ref  = 1.0 if ref_key else 0.0

        hk = self._fuzzy(home_team, self.home_feats)
        ak = self._fuzzy(away_team, self.away_feats)
        hf = self.home_feats.get(hk, {}) if hk else {}
        af = self.away_feats.get(ak, {}) if ak else {}

        feat = [
            hf.get("c",  lg_avg * 0.50), af.get("c",  lg_avg * 0.50),
            hf.get("s",  12.0),           af.get("s",  10.0),
            hf.get("st",  4.5),           af.get("st",  3.8),
            hf.get("f",  10.5),           af.get("f",  11.0),
            lg_avg, ref_val, has_ref,
        ]
        lam = float(self.gbm.predict(np.array([feat]))[0])
        lam = max(4.0, min(22.0, lam))
        p_over = {line: _poisson_over(lam, line)
                  for line in (8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5)}
        return lam, p_over


_COMP_MAP = {
    "E0":  "soccer-england-premier-league",
    "E1":  "soccer-england-championship",
    "SP1": "soccer-spain-primera-division",
    "D1":  "soccer-germany-bundesliga",
    "I1":  "soccer-italy-serie-a",
    "F1":  "soccer-france-ligue-1",
    "B1":  "soccer-belgium-jupiler-league",
    "N1":  "soccer-netherlands-eredivisie",
    "T1":  "soccer-turkey-super-lig",
    "P1":  "soccer-portugal-primeira-liga",
    "SE1": "soccer-scotland-premiership",
    "D2":  "soccer-germany-2-bundesliga",
    "I2":  "soccer-italy-serie-b",
    "F2":  "soccer-france-ligue-2",
}


def _parse_selections(market_data):
    out = []
    for _sm_key, sm in (market_data.get("submarkets") or {}).items():
        for sel in (sm.get("selections") or []):
            if sel.get("status") != "SELECTION_ENABLED": continue
            price = float(sel.get("price") or 0)
            if price <= 1.05: continue
            params = sel.get("params") or ""
            try:
                line = float(params.split("total=")[-1])
            except (IndexError, ValueError):
                continue
            out.append({
                "line":       line,
                "outcome":    sel.get("outcome") or "",
                "price":      price,
                "market_url": sel.get("marketUrl") or "",
            })
    return out


def scan_corners(api, state, dry_run=False):
    import requests

    model = CornersGBM()
    if not model.load():
        log.warning("[Corners] Model load failed")
        return []

    headers = {"X-API-Key": api.api_key, "accept": "application/json"}
    picks   = []

    for league_code, comp_key in _COMP_MAP.items():
        try:
            url = ("https://sports-api.cloudbet.com/pub/v2/odds/competitions/"
                   + comp_key + "?limit=30")
            resp = requests.get(url, headers=headers, timeout=12)
            if not resp.ok: continue
            events = resp.json().get("events") or []
        except Exception as e:
            log.debug("[Corners] %s: %s", comp_key, e)
            continue

        for ev in events:
            mkt = ev.get("markets") or {}
            tc  = mkt.get("soccer.total_corners")
            if not tc: continue
            sels = _parse_selections(tc)
            if not sels: continue

            home_obj = ev.get("home") or {}
            away_obj = ev.get("away") or {}
            hn = (home_obj.get("name") or "").strip()
            an = (away_obj.get("name") or "").strip()
            if not hn or not an: continue

            lam, p_over = model.predict(hn, an, league=league_code)
            if lam is None: continue

            lg_baseline = model.league_avg.get(league_code, model.global_avg)

            for sel in sels:
                line    = sel["line"]
                outcome = sel["outcome"]
                price   = sel["price"]
                book_p  = round(1.0 / price, 4)

                if outcome == "over":
                    model_p = p_over.get(line)
                elif outcome == "under":
                    model_p = 1.0 - p_over.get(line, 0.5)
                else:
                    continue
                if model_p is None: continue

                edge = round(model_p - book_p, 4)
                if edge < MIN_EDGE: continue
                if model_p < MIN_CONF: continue

                label     = "Corners {} {:.1f}".format(outcome.capitalize(), line)
                match_str = "{} vs {}".format(hn, an)
                log.info("[Corners Cantera] %s | %s | edge=%.1f%% p=%.1f%% @%.2f lam=%.1f",
                         match_str[:35], label, edge * 100, model_p * 100, price, lam)

                picks.append({
                    "sport":        "soccer",
                    "match":        match_str,
                    "league":       league_code,
                    "market_type":  "corners_total",
                    "label":        label,
                    "price":        price,
                    "odds":         price,
                    "model_prob":   round(model_p, 4),
                    "confidence":   round(model_p, 4),
                    "edge":         edge,
                    "market_url":   sel["market_url"],
                    "comp_key":     comp_key,
                    "event_key":    ev.get("key") or "",
                    "cutoff_time":  ev.get("cutoffTime") or "",
                    "_lambda":      round(lam, 2),
                    "_line":        line,
                    "_outcome":     outcome,
                    "_lg_baseline": round(lg_baseline, 2),
                    "_shadow_only": True,
                    "_source":      "corners_cantera",
                })

    if picks:
        log.info("[Corners Cantera] %d picks (all shadow, waiting n>=%d)",
                 len(picks), CANTERA_LIVE_N)
    return picks


def corners_cantera_status():
    try:
        import sqlite3
        from oraculo_sibila import SIBILA_DB
        conn = sqlite3.connect(SIBILA_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n,"
            " SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) as wins,"
            " SUM(CASE WHEN result IS NOT NULL AND result != 'VOID' THEN 1 ELSE 0 END) as resolved"
            " FROM sibila_picks WHERE market = 'corners_total' AND placed = 0"
        ).fetchone()
        conn.close()
        n        = row["n"]        or 0
        resolved = row["resolved"] or 0
        wins     = row["wins"]     or 0
        wr       = round(wins / resolved, 3) if resolved else None
        ready    = resolved >= CANTERA_LIVE_N and wr is not None and wr >= 0.57
        return {"n_shadow": n, "resolved": resolved, "wins": wins,
                "win_rate": wr, "ready_for_live": ready, "threshold_n": CANTERA_LIVE_N}
    except Exception as e:
        return {"error": str(e)}
