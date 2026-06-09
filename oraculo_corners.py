#!/usr/bin/env python3
# oraculo_corners.py - Corners Cantera Model (GBM + API-Football reference)
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

# API-Football reference odds (Pinnacle = sharp benchmark)
_AF_BASE          = "https://v3.football.api-sports.io"
_AF_BET_CORNERS   = 45   # BET 45: Corners Over Under
_AF_BOOK_PINNACLE = 4
_AF_BOOK_BET365   = 8

_LEAGUE_TO_AF_ID = {
    "E0":  39,  "E1":  40,  "SP1": 140, "D1":  78,  "I1":  135,
    "F1":  61,  "B1":  144, "N1":  88,  "T1":  203, "P1":  94,
    "SE1": 179, "D2":  79,  "I2":  136, "F2":  62,
}
_AF_VALID_IDS = set(_LEAGUE_TO_AF_ID.values())

# In-process caches (reset each runner invocation)
_af_fix_cache  = {}   # {date_str: [(af_league_id, fix_id, home, away), ...]}
_af_odds_cache = {}   # {fix_id: {(line, outcome): p_ref}}


# ---------------------------------------------------------------------------
# Poisson helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# API-Football helpers (reference odds)
# ---------------------------------------------------------------------------

def _af_key():
    try:
        cfg = json.load(open(os.path.join(SCRIPT_DIR, "oraculo_config.json")))
        return cfg.get("api_football_key") or cfg.get("rapidapi_key")
    except Exception:
        return None


def _af_fetch_today(date_str):
    import requests
    if date_str in _af_fix_cache:
        return _af_fix_cache[date_str]
    api_key = _af_key()
    if not api_key:
        _af_fix_cache[date_str] = []
        return []
    try:
        r = requests.get(
            _AF_BASE + "/fixtures",
            headers={"x-apisports-key": api_key},
            params={"date": date_str},
            timeout=15,
        )
        all_fix = r.json().get("response") or []
        result  = []
        for f in all_fix:
            lg_id = (f.get("league") or {}).get("id")
            if lg_id not in _AF_VALID_IDS:
                continue
            fid  = f["fixture"]["id"]
            home = (f["teams"]["home"]["name"] or "").strip()
            away = (f["teams"]["away"]["name"] or "").strip()
            result.append((lg_id, fid, home, away))
        _af_fix_cache[date_str] = result
        log.debug("[Corners AF] %d relevant fixtures for %s (%d total)",
                  len(result), date_str, len(all_fix))
        return result
    except Exception as e:
        log.debug("[Corners AF] fetch_today(%s) error: %s", date_str, e)
        _af_fix_cache[date_str] = []
        return []


def _af_find_fixture(home_cb, away_cb, af_league_id, date_str):
    candidates = [(fid, h, a)
                  for (lg, fid, h, a) in _af_fetch_today(date_str)
                  if lg == af_league_id]
    best_score, best_id = 0.0, None
    for fid, h, a in candidates:
        sh = difflib.SequenceMatcher(None, home_cb.lower(), h.lower()).ratio()
        sa = difflib.SequenceMatcher(None, away_cb.lower(), a.lower()).ratio()
        score = sh * sa
        if score > best_score:
            best_score, best_id = score, fid
    return best_id if best_score >= 0.40 else None


def _af_corners_p(fixture_id, line, outcome):
    import requests
    if fixture_id not in _af_odds_cache:
        api_key = _af_key()
        cache   = {}
        if api_key:
            try:
                r = requests.get(
                    _AF_BASE + "/odds",
                    headers={"x-apisports-key": api_key},
                    params={"fixture": fixture_id, "bet": _AF_BET_CORNERS},
                    timeout=12,
                )
                for entry in (r.json().get("response") or []):
                    for bk in (entry.get("bookmakers") or []):
                        if bk.get("id") not in (_AF_BOOK_PINNACLE, _AF_BOOK_BET365):
                            continue
                        for bet in (bk.get("bets") or []):
                            for val in (bet.get("values") or []):
                                vstr = (val.get("value") or "").lower()
                                odd  = float(val.get("odd") or 0)
                                if odd < 1.02:
                                    continue
                                try:
                                    parts = vstr.split()
                                    oc = parts[0]        # "over" / "under"
                                    ln = float(parts[1]) # 9.5
                                    p  = round(1.0 / odd, 4)
                                    ck = (ln, oc)
                                    if ck not in cache or p > cache[ck]:
                                        cache[ck] = p
                                except (IndexError, ValueError):
                                    continue
            except Exception as e:
                log.debug("[Corners AF] odds(%s) error: %s", fixture_id, e)
        _af_odds_cache[fixture_id] = cache
    return _af_odds_cache[fixture_id].get((line, outcome))


# ---------------------------------------------------------------------------
# GBM model
# ---------------------------------------------------------------------------

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
            lg_list  = lg_hist[lg][-50:]   if lg_hist[lg]           else []
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

        lg_avg  = self.league_avg.get(league or "", self.global_avg)
        ref_key = self._fuzzy(referee or "", self.ref_avg)
        ref_val = self.ref_avg.get(ref_key, lg_avg) if ref_key else lg_avg
        has_ref = 1.0 if ref_key else 0.0

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


# ---------------------------------------------------------------------------
# Cloudbet scanner
# ---------------------------------------------------------------------------

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

        af_lid = _LEAGUE_TO_AF_ID.get(league_code)

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

            # Pre-fetch AF fixture once per event (shared across all selections)
            fix_date  = (ev.get("cutoffTime") or "")[:10]
            af_fix_id = None
            if af_lid and fix_date:
                af_fix_id = _af_find_fixture(hn, an, af_lid, fix_date)

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

                # Try API-Football reference odds (Pinnacle/Bet365)
                ref_p = _af_corners_p(af_fix_id, line, outcome) if af_fix_id else None

                if ref_p is not None:
                    use_edge = round(ref_p - book_p, 4)
                    use_conf = ref_p
                    edge_src = "pinnacle"
                else:
                    use_edge = round(model_p - book_p, 4)
                    use_conf = model_p
                    edge_src = "gbm"

                if use_edge < MIN_EDGE: continue
                if use_conf < MIN_CONF: continue

                label     = "Corners {} {:.1f}".format(outcome.capitalize(), line)
                match_str = "{} vs {}".format(hn, an)
                log.info("[Corners Cantera] %s | %s | edge=%.1f%% p=%.1f%% @%.2f lam=%.1f [%s]",
                         match_str[:35], label, use_edge * 100, use_conf * 100,
                         price, lam, edge_src)

                picks.append({
                    "sport":        "soccer",
                    "match":        match_str,
                    "league":       league_code,
                    "market_type":  "corners_total",
                    "label":        label,
                    "price":        price,
                    "odds":         price,
                    "model_prob":   round(model_p, 4),
                    "confidence":   round(use_conf, 4),
                    "edge":         use_edge,
                    "market_url":   sel["market_url"],
                    "comp_key":     comp_key,
                    "event_key":    ev.get("key") or "",
                    "cutoff_time":  ev.get("cutoffTime") or "",
                    "_lambda":      round(lam, 2),
                    "_line":        line,
                    "_outcome":     outcome,
                    "_lg_baseline": round(lg_baseline, 2),
                    "_ref_p":       round(ref_p, 4) if ref_p is not None else None,
                    "_edge_source": edge_src,
                    "_shadow_only": True,
                    "_source":      "corners_cantera",
                })

    if picks:
        log.info("[Corners Cantera] %d picks (all shadow, waiting n>=%d)",
                 len(picks), CANTERA_LIVE_N)
    return picks


# ---------------------------------------------------------------------------
# Cantera status
# ---------------------------------------------------------------------------

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
