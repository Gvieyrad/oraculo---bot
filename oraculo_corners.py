#!/usr/bin/env python3
# oraculo_corners.py - Corners Cantera Model
# Poisson model for soccer total corners O/U.
# CANTERA ONLY: all picks shadow_only=True until n>=60 WR validated.
import os, json, math, logging, difflib
from collections import defaultdict

log = logging.getLogger("oraculo.corners")

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR   = os.path.join(SCRIPT_DIR, ".oraculo_cache", "csv")

MIN_TEAM_MATCHES = 5
MIN_EDGE         = 0.07
MIN_CONF         = 0.58
CANTERA_LIVE_N   = 60
_SEASONS         = ("2526", "2425")


def _poisson_pmf(k, lam):
    if lam <= 0: return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_over(lam, line):
    k_max = int(math.floor(line))
    return 1.0 - sum(_poisson_pmf(k, lam) for k in range(0, k_max + 1))


class CornersModel:

    def __init__(self):
        self.home_attack  = {}
        self.home_defense = {}
        self.away_attack  = {}
        self.away_defense = {}
        self.ref_avg      = {}
        self.league_avg   = {}
        self.global_home  = 4.8
        self.global_away  = 5.0
        self.trained      = False

    def load(self):
        raw_ha  = defaultdict(list)
        raw_hd  = defaultdict(list)
        raw_aa  = defaultdict(list)
        raw_ad  = defaultdict(list)
        ref_t   = defaultdict(list)
        lg_h    = defaultdict(list)
        lg_a    = defaultdict(list)
        n       = 0

        for fname in sorted(os.listdir(CACHE_DIR)):
            if not fname.endswith(".json"): continue
            if not any(s in fname for s in _SEASONS): continue
            league = fname.replace("_2526.json", "").replace("_2425.json", "")
            try:
                matches = json.load(open(os.path.join(CACHE_DIR, fname)))
            except Exception:
                continue
            for m in matches:
                hc = m.get("home_corners")
                ac = m.get("away_corners")
                if hc is None or ac is None: continue
                try:
                    hc, ac = int(hc), int(ac)
                except (ValueError, TypeError):
                    continue
                home = (m.get("home_team") or "").strip()
                away = (m.get("away_team") or "").strip()
                if not home or not away: continue
                ref = (m.get("referee") or "").strip()

                raw_ha[home].append(hc); raw_hd[home].append(ac)
                raw_aa[away].append(ac); raw_ad[away].append(hc)
                lg_h[league].append(hc); lg_a[league].append(ac)
                if ref: ref_t[ref].append(hc + ac)
                n += 1

        if n < 100:
            log.warning("CornersModel: only %d matches loaded", n)
            return False

        for team, vals in raw_ha.items():
            if len(vals) >= MIN_TEAM_MATCHES:
                self.home_attack[team]  = sum(vals) / len(vals)
                self.home_defense[team] = sum(raw_hd[team]) / len(raw_hd[team])
        for team, vals in raw_aa.items():
            if len(vals) >= MIN_TEAM_MATCHES:
                self.away_attack[team]  = sum(vals) / len(vals)
                self.away_defense[team] = sum(raw_ad[team]) / len(raw_ad[team])
        for league, vals in lg_h.items():
            if vals:
                la = lg_a.get(league, [self.global_away])
                self.league_avg[league] = (sum(vals)/len(vals), sum(la)/len(la))
        for ref, totals in ref_t.items():
            if len(totals) >= 8:
                self.ref_avg[ref] = sum(totals) / len(totals)

        all_h = [v for vals in lg_h.values() for v in vals]
        all_a = [v for vals in lg_a.values() for v in vals]
        if all_h: self.global_home = sum(all_h) / len(all_h)
        if all_a: self.global_away = sum(all_a) / len(all_a)

        self.trained = True
        log.info("CornersModel: %d matches, %d home teams, %d away teams, %d refs",
                 n, len(self.home_attack), len(self.away_attack), len(self.ref_avg))
        return True

    def _fuzzy(self, name, lookup):
        if not name: return None
        if name in lookup: return name
        key = name.lower().strip()
        for c in lookup:
            if c.lower() == key: return c
        m = difflib.get_close_matches(name, list(lookup.keys()), n=1, cutoff=0.72)
        return m[0] if m else None

    def predict(self, home_team, away_team, league=None, referee=None):
        if not self.trained: return None, {}

        lg_home, lg_away = self.league_avg.get(
            league or "", (self.global_home, self.global_away))

        ht = self._fuzzy(home_team, self.home_attack) or home_team
        at = self._fuzzy(away_team, self.away_attack) or away_team

        ha = self.home_attack.get(ht,  lg_home)
        hd = self.home_defense.get(ht, lg_away)
        aa = self.away_attack.get(at,  lg_away)
        ad = self.away_defense.get(at, lg_home)

        lam_h = max(2.0, min(9.0, ha * (ad / lg_home) if lg_home > 0 else ha))
        lam_a = max(2.0, min(9.0, aa * (hd / lg_away) if lg_away > 0 else aa))
        lam   = lam_h + lam_a

        if referee:
            rk = self._fuzzy(referee, self.ref_avg)
            if rk:
                delta = self.ref_avg[rk] - (self.global_home + self.global_away)
                lam = max(4.0, min(22.0, lam + delta * 0.3))

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

    model = CornersModel()
    if not model.load():
        log.warning("[Corners] Model load failed")
        return []

    headers = {"X-API-Key": api.api_key, "accept": "application/json"}
    picks = []

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
            hn = home_obj.get("name") or ""
            an = away_obj.get("name") or ""
            if not hn or not an: continue

            lam, p_over = model.predict(hn, an, league=league_code)
            if lam is None: continue

            lg_h, lg_a = model.league_avg.get(
                league_code, (model.global_home, model.global_away))

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

                label = "Corners {} {:.1f}".format(outcome.capitalize(), line)
                match_str = "{} vs {}".format(hn, an)
                log.info("[Corners Cantera] %s | %s | edge=%.1f%% p=%.1f%% @%.2f lam=%.1f",
                         match_str[:35], label, edge*100, model_p*100, price, lam)

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
                    "_lg_baseline": round(lg_h + lg_a, 2),
                    "_shadow_only": True,
                    "_source":      "corners_cantera",
                })

    if picks:
        log.info("[Corners Cantera] %d picks (all shadow, waiting n>=%d)", len(picks), CANTERA_LIVE_N)
    return picks


def corners_cantera_status():
    try:
        import sqlite3
        from oraculo_sibila import SIBILA_DB
        conn = sqlite3.connect(SIBILA_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n,"
            " SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,"
            " SUM(CASE WHEN result IS NOT NULL AND result != 'VOID' THEN 1 ELSE 0 END) as resolved"
            " FROM sibila_picks WHERE market = 'corners_total' AND placed = 0"
        ).fetchone()
        conn.close()
        n, resolved, wins = row["n"] or 0, row["resolved"] or 0, row["wins"] or 0
        wr    = round(wins / resolved, 3) if resolved else None
        ready = resolved >= CANTERA_LIVE_N and wr is not None and wr >= 0.57
        return {"n_shadow": n, "resolved": resolved, "wins": wins,
                "win_rate": wr, "ready_for_live": ready, "threshold_n": CANTERA_LIVE_N}
    except Exception as e:
        return {"error": str(e)}
