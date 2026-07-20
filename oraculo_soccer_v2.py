"""Soccer corners + bookings model v2 for Oráculo.

Two-layer approach:
  Layer B — Base rate scanner: compares bookmaker odds vs historical
            Over/Under frequency per league+line. No team model needed.
  Layer A — Form model: rolling last-5 team stats + referee multiplier
            blended with league prior. Replaces flat season averages.

Backtest v1 showed r=0.020 (no signal) from season-average Poisson.
Root cause: season averages wash out match-specific variance. Fixes:
  - Rolling form captures recent team behavior (hot/cold streaks)
  - Referee multiplier captures the biggest single source of variance
  - League prior prevents overfitting on thin team samples
"""
import os, json, math, logging
from collections import defaultdict
from datetime import datetime

log = logging.getLogger('oraculo.soccer_v2')

import requests, time as _time

# Referees with Over 35.5 booking pts rate >= 65% (from 1424-match backtest)
# Format: partial name → {over35_pct, avg_bp, n}
HIGH_CARD_REFS = {
    'attwell':    {'over35': 0.77, 'avg_bp': 49.1, 'n': 22},
    'salisbury':  {'over35': 0.75, 'avg_bp': 43.3, 'n': 12},
    'england':    {'over35': 0.73, 'avg_bp': 46.1, 'n': 22},
    'brooks':     {'over35': 0.70, 'avg_bp': 45.5, 'n': 10},
    'bramall':    {'over35': 0.62, 'avg_bp': 43.8, 'n': 21},
    'hooper':     {'over35': 0.60, 'avg_bp': 43.3, 'n': 15},
}
REF_MIN_OVER35 = 0.68  # only bet when ref has >=68% Over 35.5 rate

# football-data.org competition IDs
_FD_COMP_IDS = {
    'PL': 2021, 'BL1': 2002, 'FL1': 2015, 'SA': 2019, 'PD': 2014, 'UCL': 2001,
}
_FD_FIXTURE_CACHE = {}   # (comp_id, date_str) → {match_key: referee}
_FD_CACHE_TTL = 3600 * 6  # 6h

def _fetch_referee(home, away, league_code, api_key=None):
    """Fetch referee assignment from football-data.org for a given match.
    Returns referee name (str) or None if unknown/not assigned yet.
    """
    if not api_key:
        try:
            import json, os
            cfg = json.load(open(os.path.join(os.path.dirname(__file__), 'oraculo_config.json')))
            api_key = cfg.get('football_data_org_key', '')
        except Exception:
            return None
    if not api_key:
        return None

    comp_id = _FD_COMP_IDS.get(league_code)
    if not comp_id:
        return None

    cache_key = (comp_id,)
    now = _time.time()
    cached = _FD_FIXTURE_CACHE.get(cache_key)
    if cached and now - cached.get('_ts', 0) < _FD_CACHE_TTL:
        matches = cached
    else:
        try:
            url = 'https://api.football-data.org/v4/competitions/%d/matches' % comp_id
            resp = requests.get(url,
                headers={'X-Auth-Token': api_key},
                params={'status': 'SCHEDULED'},
                timeout=10)
            resp.raise_for_status()
            data = resp.json()
            matches = {'_ts': now}
            for m in data.get('matches', []):
                ht = (m.get('homeTeam') or {}).get('shortName', '') or (m.get('homeTeam') or {}).get('name', '')
                at = (m.get('awayTeam') or {}).get('shortName', '') or (m.get('awayTeam') or {}).get('name', '')
                ref = ((m.get('referees') or [{}])[0]).get('name', '') if m.get('referees') else ''
                if ht and at:
                    key = '%s|%s' % (ht.lower().strip(), at.lower().strip())
                    matches[key] = ref
            _FD_FIXTURE_CACHE[cache_key] = matches
            log.debug('Soccer ref cache: %d matches loaded for comp %d', len(matches)-1, comp_id)
        except Exception as e:
            log.debug('Soccer ref fetch error [%s]: %s', league_code, e)
            return None

    # Try to match home/away names (partial)
    home_l = home.lower().strip()
    away_l = away.lower().strip()

    for key, ref in matches.items():
        if key == '_ts':
            continue
        parts = key.split('|')
        if len(parts) != 2:
            continue
        kh, ka = parts
        # Match if any word overlaps (handles 'Liverpool FC' vs 'Liverpool')
        if (any(w in home_l for w in kh.split() if len(w) > 3) and
            any(w in away_l for w in ka.split() if len(w) > 3)):
            return ref or None
    return None


def _is_high_card_ref(referee_name):
    """Returns (is_high_card, ref_stats) for a given referee name."""
    if not referee_name:
        return False, None
    rn = referee_name.lower()
    for key, stats in HIGH_CARD_REFS.items():
        if key in rn:
            return stats['over35'] >= REF_MIN_OVER35, stats
    return False, None


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

LEAGUE_FILES = ['E0_2526.json', 'D1_2526.json', 'F1_2526.json',
                'SP1_2526.json', 'I1_2526.json']

UCL_CARD_MULTIPLIER   = 1.45
UCL_CORNER_HOME_BOOST = 1.05

# Historical base rates per league per line (from backtest N=1424).
# Format: {league_code: {line: over_pct}}
LEAGUE_BASE_RATES = {
    'PL':   {25.5: 0.779, 35.5: 0.588, 45.5: 0.387, 55.5: 0.252, 65.5: 0.158},
    'BL1':  {25.5: 0.798, 35.5: 0.613, 45.5: 0.391, 55.5: 0.263, 65.5: 0.169},
    'FL1':  {25.5: 0.789, 35.5: 0.620, 45.5: 0.426, 55.5: 0.289, 65.5: 0.174},
    'SA':   {25.5: 0.770, 35.5: 0.597, 45.5: 0.387, 55.5: 0.257, 65.5: 0.160},
    'PD':   {25.5: 0.793, 35.5: 0.648, 45.5: 0.503, 55.5: 0.334, 65.5: 0.207},
    # football-data codes
    'E0':   {25.5: 0.779, 35.5: 0.588, 45.5: 0.387, 55.5: 0.252, 65.5: 0.158},
    'D1':   {25.5: 0.798, 35.5: 0.613, 45.5: 0.391, 55.5: 0.263, 65.5: 0.169},
    'F1':   {25.5: 0.789, 35.5: 0.620, 45.5: 0.426, 55.5: 0.289, 65.5: 0.174},
    'I1':   {25.5: 0.770, 35.5: 0.597, 45.5: 0.387, 55.5: 0.257, 65.5: 0.160},
    'SP1':  {25.5: 0.793, 35.5: 0.648, 45.5: 0.503, 55.5: 0.334, 65.5: 0.207},
    # UCL: assume similar to PL/BL1 average (no specific data)
    'UCL':  {25.5: 0.780, 35.5: 0.595, 45.5: 0.400, 55.5: 0.260, 65.5: 0.165},
    # global fallback
    '_':    {25.5: 0.777, 35.5: 0.600, 45.5: 0.418, 55.5: 0.264, 65.5: 0.162},
}

LEAGUE_MEAN_BP = {
    'PL': 40.1, 'E0': 40.1,
    'BL1': 43.1, 'D1': 43.1,
    'FL1': 44.9, 'F1': 44.9,
    'SA': 42.0, 'I1': 42.0,
    'PD': 51.0, 'SP1': 51.0,
    'UCL': 41.0,
    '_': 44.1,
}

# Cloudbet competition key → league code for base rate lookup
CB_TO_LEAGUE = {
    'soccer-england-premier-league':                            'PL',
    'soccer-germany-bundesliga':                                'BL1',
    'soccer-france-ligue-1':                                    'FL1',
    'soccer-italy-serie-a':                                     'SA',
    'soccer-spain-laliga':                                      'PD',
    'soccer-international-clubs-uefa-champions-league':         'UCL',
    'soccer-international-clubs-uefa-europa-league':            'UCL',
    'soccer-usa-mls':                                           'MLS',
}


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


# ── Base rate scanner (Layer B) ───────────────────────────────────────────────

def base_rate_edge(league_code, line, outcome, bookmaker_price):
    """Return (model_prob, edge) using only historical base rate for this league+line."""
    rates = LEAGUE_BASE_RATES.get(league_code) or LEAGUE_BASE_RATES['_']
    # Find nearest line in table
    nearest_line = min(rates.keys(), key=lambda l: abs(l - line))
    p_over = rates[nearest_line]
    if outcome == 'over':
        prob = p_over
    else:
        prob = 1.0 - p_over
    edge = prob * bookmaker_price - 1.0
    return round(prob, 4), round(edge, 4)



# ── Team name normalization ───────────────────────────────────────────────────

# Cloudbet → CSV canonical name mapping (add as discovered)
_CB_TO_CSV = {
    'Liverpool':                    'Liverpool FC',
    'Chelsea':                      'Chelsea FC',
    'Arsenal':                      'Arsenal FC',
    'Everton':                      'Everton FC',
    'Fulham':                       'Fulham FC',
    'Brentford':                    'Brentford FC',
    'Crystal Palace':               'Crystal Palace FC',
    'Aston Villa':                  'Aston Villa FC',
    'Brighton & Hove Albion':       'Brighton & Hove Albion FC',
    'Nottingham Forest':            'Nottingham Forest FC',
    'Wolverhampton Wanderers':      'Wolverhampton Wanderers FC',
    'Bournemouth':                  'AFC Bournemouth',
    'Manchester City':              'Manchester City FC',
    'Manchester United':            'Manchester United FC',
    'Newcastle United':             'Newcastle United FC',
    'Tottenham Hotspur':            'Tottenham Hotspur FC',
    'West Ham United':              'West Ham United FC',
    'Leicester City':               'Leicester City FC',
    'Leeds United':                 'Leeds United FC',
    'Ipswich Town':                 'Ipswich Town FC',
    'Southampton':                  'Southampton FC',
    'Sunderland AFC':               'Sunderland AFC',
    # Bundesliga
    'Bayern Munchen':               'FC Bayern Munchen',
    'Borussia Dortmund':            'Borussia Dortmund',
    'Bayer Leverkusen':             'Bayer 04 Leverkusen',
    'Frankfurt':                    'Ein Frankfurt',
    'Union Berlin':                 '1. FC Union Berlin',
    'Mainz 05':                     '1. FSV Mainz 05',
    'Heidenheim':                   '1. FC Heidenheim 1846',
    # La Liga
    'Real Madrid':                  'Real Madrid CF',
    'Barcelona':                    'FC Barcelona',
    'Atletico de Madrid':           'Club Atletico de Madrid',
    'Alaves':                       'Deportivo Alaves',
    'Athletic Bilbao':              'Athletic Club',
    # Serie A
    'Inter Milan':                  'FC Internazionale Milano',
    'AC Milan':                     'AC Milan',
    'Fiorentina':                   'ACF Fiorentina',
    'Atalanta':                     'Atalanta BC',
    # Ligue 1
    'Monaco':                       'AS Monaco FC',
    'Lyon':                         'Olympique Lyonnais',
    'Marseille':                    'Olympique de Marseille',
    'PSG':                          'Paris Saint-Germain FC',
    'Paris Saint-Germain':          'Paris Saint-Germain FC',
}

_CSV_TO_CB = {v: k for k, v in _CB_TO_CSV.items()}

def _norm_team(name):
    """Resolve Cloudbet team name → CSV canonical name for form model lookup."""
    if not name:
        return name
    if name in _CB_TO_CSV:
        return _CB_TO_CSV[name]
    # Fuzzy: try adding FC/AFC suffix
    for suffix in (' FC', ' AFC', ' CF', ' SC', ' BC', ' CFC'):
        if (name + suffix) in {m for m in _CB_TO_CSV.values()}:
            return name + suffix
    return name

# ── Form model (Layer A) ──────────────────────────────────────────────────────

class SoccerModelV2:
    def __init__(self):
        self._matches = []          # all matches sorted by date
        self._ref_stats = {}        # referee → {y, r, n}
        self._global_ref_avg = None

    def load(self):
        all_m = []
        for fname in LEAGUE_FILES:
            fpath = os.path.join(CSV_DIR, fname)
            if not os.path.exists(fpath):
                log.debug('Missing CSV: %s', fname)
                continue
            try:
                with open(fpath) as f:
                    data = json.load(f)
                for m in data:
                    d = _parse_date(m.get('utc_date', ''))
                    all_m.append({
                        'date': d,
                        'league': m.get('competition_code', fname[:2]),
                        'home': m.get('home_team', m.get('home_team_csv', '')),
                        'away': m.get('away_team', m.get('away_team_csv', '')),
                        'referee': (m.get('referee') or '').strip(),
                        'hc': m.get('home_corners', 0) or 0,
                        'ac': m.get('away_corners', 0) or 0,
                        'hy': m.get('home_yellow', 0) or 0,
                        'ay': m.get('away_yellow', 0) or 0,
                        'hr': m.get('home_red', 0) or 0,
                        'ar': m.get('away_red', 0) or 0,
                        'hf': m.get('home_fouls', 0) or 0,
                        'af': m.get('away_fouls', 0) or 0,
                    })
            except Exception as e:
                log.debug('Load error %s: %s', fname, e)

        # Sort chronologically (None dates go to front = treated as oldest)
        self._matches = sorted(all_m, key=lambda m: m['date'] or datetime.min.replace(tzinfo=__import__('datetime').timezone.utc))

        # Build referee stats (season-level is fine — referees don't have streaks)
        ref = defaultdict(lambda: {'y': 0.0, 'r': 0.0, 'n': 0})
        for m in self._matches:
            rn = m['referee']
            if rn:
                r = ref[rn]
                r['y'] += m['hy'] + m['ay']
                r['r'] += m['hr'] + m['ar']
                r['n'] += 1
        self._ref_stats = {k: v for k, v in ref.items() if v['n'] >= 3}

        # Global referee average cards per match
        total_y = sum(v['y'] for v in ref.values())
        total_n = sum(v['n'] for v in ref.values())
        self._global_ref_avg_y = total_y / total_n if total_n > 0 else 3.5
        total_r = sum(v['r'] for v in ref.values())
        self._global_ref_avg_r = total_r / total_n if total_n > 0 else 0.12

        log.info('SoccerModelV2: %d matches, %d referees loaded',
                 len(self._matches), len(self._ref_stats))
        return self

    def _referee_multiplier(self, referee_name):
        """Returns (y_mult, r_mult) vs global average. Unknown ref → (1.0, 1.0)."""
        if not referee_name:
            return 1.0, 1.0
        # Fuzzy match
        ref_key = None
        if referee_name in self._ref_stats:
            ref_key = referee_name
        else:
            rn_lower = referee_name.lower()
            for k in self._ref_stats:
                if k.lower() in rn_lower or rn_lower in k.lower():
                    ref_key = k
                    break
        if ref_key is None:
            return 1.0, 1.0
        rs = self._ref_stats[ref_key]
        n  = rs['n']
        ref_y = rs['y'] / n
        ref_r = rs['r'] / n
        y_mult = ref_y / self._global_ref_avg_y if self._global_ref_avg_y > 0 else 1.0
        r_mult = ref_r / self._global_ref_avg_r if self._global_ref_avg_r > 0 else 1.0
        # Shrink towards 1.0 for refs with few matches
        shrink = min(1.0, n / 15.0)
        y_mult = 1.0 + (y_mult - 1.0) * shrink
        r_mult = 1.0 + (r_mult - 1.0) * shrink
        return round(y_mult, 3), round(r_mult, 3)

    def _team_form(self, team_name, as_home, before_date, n=5):
        """Rolling last-n stats for team before given date."""
        csv_name = _norm_team(team_name)  # resolve Cloudbet name → CSV name
        relevant = []
        for m in reversed(self._matches):
            if before_date and m['date'] and m['date'] >= before_date:
                continue
            mh = m['home']; ma = m['away']
            if as_home and (mh == csv_name or mh == team_name):
                relevant.append({'cf': m['hc'], 'ca': m['ac'],
                                  'y': m['hy'], 'r': m['hr'], 'fo': m['hf']})
            elif not as_home and (ma == csv_name or ma == team_name):
                relevant.append({'cf': m['ac'], 'ca': m['hc'],
                                  'y': m['ay'], 'r': m['ar'], 'fo': m['af']})
            if len(relevant) >= n:
                break
        if not relevant:
            return None
        k = len(relevant)
        return {
            'avg_cf': sum(r['cf'] for r in relevant) / k,
            'avg_ca': sum(r['ca'] for r in relevant) / k,
            'avg_y':  sum(r['y']  for r in relevant) / k,
            'avg_r':  sum(r['r']  for r in relevant) / k,
            'avg_fo': sum(r['fo'] for r in relevant) / k,
            'n': k,
        }

    def predict_bookings(self, home_name, away_name,
                         referee=None, league=None, is_ucl=False,
                         before_date=None, form_n=5, league_weight=0.45):
        """
        Hybrid booking pts prediction.
        league_weight: how much to pull towards league mean (0=pure form, 1=pure base rate).
        """
        league_mean = LEAGUE_MEAN_BP.get(league or '_') or LEAGUE_MEAN_BP['_']
        if is_ucl:
            league = 'UCL'
            league_mean = LEAGUE_MEAN_BP['UCL']

        hf = self._team_form(_norm_team(home_name), as_home=True,  before_date=before_date, n=form_n)
        af = self._team_form(_norm_team(away_name), as_home=False, before_date=before_date, n=form_n)

        if hf is None and af is None:
            # No data for either team — fall back to league mean only
            return {
                'exp_bp': league_mean, 'std_bp': 25.0,
                'source': 'league_prior_only',
                'p_home_first_booking': 0.5, 'p_away_first_booking': 0.5,
            }

        # Fill missing side with league mean per-side estimate
        h_y = hf['avg_y'] if hf else league_mean * 0.05
        a_y = af['avg_y'] if af else league_mean * 0.05
        h_r = hf['avg_r'] if hf else 0.06
        a_r = af['avg_r'] if af else 0.06

        mult_ucl = UCL_CARD_MULTIPLIER if is_ucl else 1.0

        # Referee multiplier
        y_mult, r_mult = self._referee_multiplier(referee)

        h_y_adj = h_y * mult_ucl * y_mult
        a_y_adj = a_y * mult_ucl * y_mult
        h_r_adj = h_r * mult_ucl * r_mult
        a_r_adj = a_r * mult_ucl * r_mult

        form_exp_bp = 10 * (h_y_adj + a_y_adj) + 25 * (h_r_adj + a_r_adj)

        # Blend: league prior + form model
        n_samples = (hf['n'] if hf else 0) + (af['n'] if af else 0)
        # More form samples → trust form more
        dynamic_lw = max(0.20, league_weight - (n_samples - form_n) * 0.02)
        exp_bp = dynamic_lw * league_mean + (1.0 - dynamic_lw) * form_exp_bp

        # Variance: use form variance if available, else global std
        var_bp = 100 * (h_y_adj + a_y_adj) + 625 * (h_r_adj + a_r_adj)
        std_bp = max(8.0, math.sqrt(var_bp) if var_bp > 0 else 25.0)

        rate_h = h_y_adj + 2 * h_r_adj
        rate_a = a_y_adj + 2 * a_r_adj
        total_rate = rate_h + rate_a
        p_home_first = rate_h / total_rate if total_rate > 0 else 0.5

        n_h = hf['n'] if hf else 0
        n_a = af['n'] if af else 0
        y_m, r_m = round(y_mult, 2), round(r_mult, 2)

        return {
            'exp_bp': round(exp_bp, 1),
            'std_bp': round(std_bp, 1),
            'form_exp_bp': round(form_exp_bp, 1),
            'league_mean': league_mean,
            'league_weight': round(dynamic_lw, 2),
            'referee': referee or 'unknown',
            'ref_y_mult': y_m, 'ref_r_mult': r_m,
            'p_home_first_booking': round(p_home_first, 4),
            'p_away_first_booking': round(1 - p_home_first, 4),
            'n_home': n_h, 'n_away': n_a,
            'source': 'form+prior+referee',
        }

    def predict_corners(self, home_name, away_name,
                        is_ucl=False, before_date=None, form_n=5):
        hf = self._team_form(home_name, as_home=True,  before_date=before_date, n=form_n)
        af = self._team_form(away_name, as_home=False, before_date=before_date, n=form_n)
        if not hf or not af:
            return None
        lh = (hf['avg_cf'] + af['avg_ca']) / 2.0
        la = (af['avg_cf'] + hf['avg_ca']) / 2.0
        if is_ucl:
            lh *= UCL_CORNER_HOME_BOOST
        total = lh + la if (lh + la) > 0 else 1.0
        p_home = lh / total
        return {
            'lambda_home': round(lh, 2), 'lambda_away': round(la, 2),
            'total_expected': round(total, 2),
            'p_home': round(p_home, 4), 'p_away': round(1 - p_home, 4),
            'n_home': hf['n'], 'n_away': af['n'],
        }

    def p_over_booking_points(self, pred, line):
        if not pred:
            return 0.5
        z = (line - pred['exp_bp']) / pred['std_bp']
        return round(_norm_cdf(-z), 4)


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_soccer_v2(api, state, model=None, dry_run=False,
                   min_edge_base=0.05, min_edge_model=0.10,
                   min_conf=0.52):
    """
    Soccer scanner v2 — two-layer picks:
      'base_rate': pure league base rate vs bookmaker price (Layer B)
      'form_model': base rate + rolling form + referee adjustment (Layer A)
    Only picks where BOTH layers agree are flagged as high-confidence.
    """
    picks = []
    if model is None:
        model = SoccerModelV2().load()

    COMPS = [
        ('soccer-international-clubs-uefa-champions-league', True,  'UCL'),
        ('soccer-england-premier-league',                    False, 'PL'),
        ('soccer-germany-bundesliga',                        False, 'BL1'),
        ('soccer-france-ligue-1',                            False, 'FL1'),
        ('soccer-italy-serie-a',                             False, 'SA'),
        ('soccer-spain-laliga',                              False, 'PD'),
    ]

    for comp_key, is_ucl, league_code in COMPS:
        try:
            events = api.get_odds(comp_key)
        except Exception as e:
            log.debug('Soccer v2 fetch %s: %s', comp_key, e)
            continue
        if not events:
            continue

        for ev in events:
            if not ev or not isinstance(ev, dict):
                continue
            if ev.get('type') == 'EVENT_TYPE_OUTRIGHT':
                continue
            home_obj = ev.get('home') or {}
            away_obj = ev.get('away') or {}
            home = home_obj.get('name', '') if isinstance(home_obj, dict) else str(home_obj)
            away = away_obj.get('name', '') if isinstance(away_obj, dict) else str(away_obj)
            if not home or not away:
                continue

            eid     = str(ev.get('id', ''))
            markets = ev.get('markets', {})
            match_s = '%s vs %s' % (home, away)

            # Fetch referee from football-data.org (cached 6h)
            _ref_name = _fetch_referee(home, away, league_code)
            _ref_ok, _ref_stats = _is_high_card_ref(_ref_name)

            # ── REFEREE FILTER: only bet Over 35.5 when high-card ref assigned ──
            # Base rate has 0% edge; edge comes entirely from referee variance.
            # Attwell/Salisbury/England → Over35.5 WR 73-77% vs 60% implied.
            if not _ref_ok:
                ref_info = ('%s (%.0f%% Over35.5)' % (_ref_name, _ref_stats['over35']*100)) if (_ref_name and _ref_stats) else ('unknown' if not _ref_name else _ref_name)
                log.debug('Soccer [ref-skip %s]: %s — ref not in high-card whitelist', match_s[:28], ref_info)
                continue

            log.info('Soccer [ref-OK] %s | ref=%s Over35.5=%.0f%% avg_bp=%.1f',
                     match_s[:35], _ref_name, _ref_stats['over35']*100, _ref_stats['avg_bp'])

            # Model prediction (Layer A)
            bpred = model.predict_bookings(
                home, away,
                referee=_ref_name,
                league=league_code,
                is_ucl=is_ucl,
            )
            cpred = model.predict_corners(home, away, is_ucl=is_ucl)

            # ── total_booking_points O/U ──
            tbp = markets.get('soccer.total_booking_points', {})
            for sv in tbp.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    if sel.get('status') != 'SELECTION_ENABLED':
                        continue
                    price   = float(sel.get('price', 0) or 0)
                    murl    = sel.get('marketUrl', '')
                    outcome = sel.get('outcome', '')
                    params  = str(sel.get('params', ''))
                    if price < 1.05 or not murl:
                        continue
                    try:
                        line = float(params.split('total=')[-1])
                    except Exception:
                        continue
                    if outcome not in ('over', 'under'):
                        continue

                    # Layer B: base rate
                    br_prob, br_edge = base_rate_edge(league_code, line, outcome, price)

                    # Layer A: form model
                    p_over_model = model.p_over_booking_points(bpred, line)
                    model_prob   = p_over_model if outcome == 'over' else 1.0 - p_over_model
                    model_edge   = model_prob * price - 1.0

                    # Consensus: both must agree direction and have edge
                    both_positive = br_edge >= min_edge_base and model_edge >= min_edge_base
                    # Pure base-rate pick (lower threshold)
                    base_only     = br_edge >= min_edge_base + 0.02 and br_prob >= min_conf

                    lbl = 'Booking pts %s %.1f' % (outcome.capitalize(), line)

                    if both_positive and model_prob >= min_conf:
                        picks.append({
                            'match': match_s, 'league': comp_key,
                            'event_id': eid, 'market_url': murl,
                            'price': price, 'label': lbl,
                            'model_prob': round(model_prob, 4),
                            'raw_model_prob': round(model_prob, 4),
                            'base_rate_prob': br_prob,
                            'base_rate_edge': round(br_edge, 4),
                            'model_edge': round(model_edge, 4),
                            'edge': round((br_edge + model_edge) / 2, 4),
                            'sport': 'soccer', 'market': 'booking_pts', '_referee': _ref_name or '',
                            'layer': 'consensus',
                            'ref_y_mult': bpred.get('ref_y_mult', 1.0),
                            'exp_bp': bpred.get('exp_bp'),
                        })
                    elif base_only:
                        picks.append({
                            'match': match_s, 'league': comp_key,
                            'event_id': eid, 'market_url': murl,
                            'price': price, 'label': lbl,
                            'model_prob': br_prob,
                            'raw_model_prob': br_prob,
                            'base_rate_prob': br_prob,
                            'base_rate_edge': round(br_edge, 4),
                            'model_edge': round(model_edge, 4),
                            'edge': round(br_edge, 4),
                            'sport': 'soccer', 'market': 'booking_pts', '_referee': _ref_name or '',
                            'layer': 'base_rate',
                            'exp_bp': bpred.get('exp_bp'),
                        })

            # ── booking_nr (first booking) — only if form model has signal ──
            if bpred and bpred.get('n_home', 0) >= 3 and bpred.get('n_away', 0) >= 3:
                bnr = markets.get('soccer.booking_nr', {})
                for sv in bnr.get('submarkets', {}).values():
                    for sel in sv.get('selections', []):
                        if sel.get('status') != 'SELECTION_ENABLED':
                            continue
                        price   = float(sel.get('price', 0) or 0)
                        murl    = sel.get('marketUrl', '')
                        outcome = sel.get('outcome', '')
                        if price < 1.05 or not murl:
                            continue
                        if outcome == 'home':
                            prob = bpred['p_home_first_booking']
                            lbl  = 'First booking: %s' % home
                        elif outcome == 'away':
                            prob = bpred['p_away_first_booking']
                            lbl  = 'First booking: %s' % away
                        else:
                            continue
                        edge = prob * price - 1.0
                        if edge >= min_edge_model and prob >= min_conf:
                            picks.append({
                                'match': match_s, 'league': comp_key,
                                'event_id': eid, 'market_url': murl,
                                'price': price, 'label': lbl,
                                'model_prob': prob, 'raw_model_prob': prob,
                                'edge': round(edge, 4),
                                'sport': 'soccer', 'market': 'booking_nr',
                                'layer': 'form_model',
                            })

            # ── corner_nr (corner winner) ──
            if cpred:
                for mk_name in ('soccer.corner_nr', 'soccer.last_corner'):
                    mkt = markets.get(mk_name, {})
                    lbl_prefix = 'Corner N' if 'corner_nr' in mk_name else 'Last corner'
                    for sv in mkt.get('submarkets', {}).values():
                        for sel in sv.get('selections', []):
                            if sel.get('status') != 'SELECTION_ENABLED':
                                continue
                            price   = float(sel.get('price', 0) or 0)
                            murl    = sel.get('marketUrl', '')
                            outcome = sel.get('outcome', '')
                            if price < 1.05 or not murl:
                                continue
                            if outcome == 'home':
                                prob = cpred['p_home']
                                lbl  = '%s: %s' % (lbl_prefix, home)
                            elif outcome == 'away':
                                prob = cpred['p_away']
                                lbl  = '%s: %s' % (lbl_prefix, away)
                            else:
                                continue
                            edge = prob * price - 1.0
                            if edge >= min_edge_model and prob >= min_conf:
                                picks.append({
                                    'match': match_s, 'league': comp_key,
                                    'event_id': eid, 'market_url': murl,
                                    'price': price, 'label': lbl,
                                    'model_prob': prob, 'raw_model_prob': prob,
                                    'edge': round(edge, 4),
                                    'sport': 'soccer', 'market': mk_name.split('.')[-1],
                                    'layer': 'form_model',
                                })

    log.info('Soccer v2: %d picks (consensus=%d, base_rate=%d, form=%d)',
             len(picks),
             sum(1 for p in picks if p.get('layer') == 'consensus'),
             sum(1 for p in picks if p.get('layer') == 'base_rate'),
             sum(1 for p in picks if p.get('layer') == 'form_model'))
    for p in picks:
        log.info('  [SCv2/%s] %s | %s | edge=%.1f%% @%.2f',
                 p.get('layer','?')[:3], p['match'][:32], p['label'][:28],
                 p['edge']*100, p['price'])
    return picks


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    m = SoccerModelV2().load()

    # Print referee stats (top 10 most strict/lenient)
    print('\n=== TOP REFEREES (cards per match) ===')
    refs = [(k, v['y']/v['n'], v['r']/v['n'], v['n'])
            for k, v in m._ref_stats.items() if v['n'] >= 5]
    refs.sort(key=lambda x: x[1] + 8*x[2], reverse=True)
    print(f'{"Referee":<22} {"Y/m":>5} {"R/m":>5} {"N":>4}  {"BP/m":>6}')
    print('-' * 50)
    for ref, y, r, n in refs[:15]:
        bp = 10*y + 25*r
        print(f'{ref:<22} {y:>5.2f} {r:>5.3f} {n:>4}  {bp:>6.1f}')

    print('\n=== SAMPLE PREDICTIONS ===')
    for home, away, league, ref, is_ucl in [
        ('Liverpool FC', 'Chelsea FC',          'PL',  'A Taylor', False),
        ('FC Bayern Munchen', 'Paris Saint-Germain FC', 'UCL', None, True),
        ('Real Madrid CF', 'FC Barcelona',      'PD',  'C del Cerro Grande', False),
    ]:
        bp = m.predict_bookings(home, away, referee=ref, league=league, is_ucl=is_ucl)
        cp = m.predict_corners(home, away, is_ucl=is_ucl)
        print(f'\n{home} vs {away} ({league}{"  ref="+ref if ref else ""})')
        if bp:
            print(f'  Booking pts: exp={bp["exp_bp"]} std={bp["std_bp"]}  '
                  f'form={bp.get("form_exp_bp","?"):.1f}  league_mean={bp["league_mean"]}  '
                  f'ref_y={bp["ref_y_mult"]}x')
            print(f'  P(home 1st booking)={bp["p_home_first_booking"]*100:.1f}%  '
                  f'n_home={bp["n_home"]} n_away={bp["n_away"]}')
            for line in [35.5, 45.5]:
                po = m.p_over_booking_points(bp, line)
                pu = 1 - po
                br_po, _ = base_rate_edge(league, line, 'over', 1.85)
                print(f'  Over {line}: model={po*100:.1f}%  base_rate={br_po*100:.1f}%')
        if cp:
            print(f'  Corners: lh={cp["lambda_home"]:.1f} la={cp["lambda_away"]:.1f}  '
                  f'P(home)={cp["p_home"]*100:.1f}%')


# ── Soccer Goals Model (Poisson, no referee required) ───────────────────────
# League Over 2.5 base rates (2023-25 average)
_LEAGUE_O25 = {
    'soccer-england-premier-league':       0.548,
    'soccer-germany-bundesliga':           0.592,
    'soccer-italy-serie-a':                0.548,
    'soccer-spain-laliga':                0.548,
    'soccer-france-ligue-1':               0.529,
    'soccer-netherlands-eredivisie':       0.581,
    'soccer-portugal-primeira-liga':       0.537,
    'soccer-england-championship':         0.521,
    'soccer-international-clubs-uefa-champions-league':             0.570,
    'soccer-europa-league':                0.551,
    # New leagues (from backtest 2024-25 N=300-552)
    'soccer-germany-2-bundesliga':         0.573,
    'soccer-spain-laliga-2':                0.507,
    'soccer-france-ligue-2':               0.473,
    'soccer-italy-serie-b':                0.484,
    'soccer-scotland-premiership':         0.510,
    'soccer-belgium-first-division-a':              0.560,
    # MLS cantera — base rate 2022-2026 (N=2290). Shadow only until N>=30 live picks.
    'soccer-usa-mls':                      0.588,
}
_LEAGUE_O15 = {k: v + 0.22 for k, v in _LEAGUE_O25.items()}
_DEFAULT_O25 = 0.540
_DEFAULT_O15 = 0.760

# Under 1.5 2H base rates (from 2424-25 data — more conservative leagues have higher rate)
_LEAGUE_U15_2H = {
    'soccer-england-premier-league':       0.450,  # backtest 3503m: actual 45.0% (was 50.0%)
    'soccer-germany-bundesliga':           0.493,  # backtest actual 49.3%
    'soccer-italy-serie-a':                0.570,  # backtest actual 57.0% (was 52.6%)
    'soccer-spain-laliga':                0.574,  # backtest actual 57.4% (was 52.6%)
    'soccer-france-ligue-1':               0.528,  # backtest actual 52.8% (was 56.0%)
    'soccer-netherlands-eredivisie':       0.510,
    'soccer-portugal-primeira-liga':       0.530,
    'soccer-england-championship':         0.572,
    'soccer-germany-2-bundesliga':         0.510,
    'soccer-spain-laliga-2':                0.550,
    'soccer-france-ligue-2':               0.618,
    'soccer-italy-serie-b':                0.562,
    'soccer-international-clubs-uefa-champions-league':             0.520,
    'soccer-europa-league':                0.520,
    'soccer-scotland-premiership':         0.580,
    'soccer-belgium-first-division-a':              0.540,
}
_DEFAULT_U15_2H = 0.540

# CSV URLs: current season first, prev season as fallback
_CSV_BASE = 'https://www.football-data.co.uk/mmz4281/'
_LEAGUE_CSV_SUFFIXES = {
    'soccer-england-premier-league':    ('2526/E0.csv', '2425/E0.csv'),
    'soccer-germany-bundesliga':        ('2526/D1.csv', '2425/D1.csv'),
    'soccer-italy-serie-a':            ('2526/I1.csv', '2425/I1.csv'),
    'soccer-spain-laliga':            ('2526/SP1.csv', '2425/SP1.csv'),
    'soccer-france-ligue-1':           ('2526/F1.csv', '2425/F1.csv'),
    'soccer-netherlands-eredivisie':   ('2526/N1.csv', '2425/N1.csv'),
    'soccer-portugal-primeira-liga':   ('2526/P1.csv', '2425/P1.csv'),
    'soccer-england-championship':     ('2526/E1.csv', '2425/E1.csv'),
    'soccer-germany-2-bundesliga':     ('2526/D2.csv', '2425/D2.csv'),
    'soccer-spain-laliga-2':            ('2526/SP2.csv', '2425/SP2.csv'),
    'soccer-france-ligue-2':           ('2526/F2.csv', '2425/F2.csv'),
    'soccer-italy-serie-b':            ('2526/I2.csv', '2425/I2.csv'),
    'soccer-international-clubs-uefa-champions-league':         ('2526/UCL.csv', '2425/UCL.csv'),
    'soccer-scotland-premiership':     ('2526/SC0.csv', '2425/SC0.csv'),
    'soccer-belgium-first-division-a':          ('2526/B1.csv', '2425/B1.csv'),
}
_CSV_FORM_DATA = {}   # comp_key → {'form': {...}, 'ts': float}
_CSV_FORM_TTL  = 3600 * 6


def _load_peru_form(n_games=6):
    """Load Peru Liga 1 form from PER_all.json (API-Football cached data).
    Returns same format as _load_csv_form: {team: {h_sc, h_cc, h_n, a_sc, a_cc, a_n}}
    """
    import os as _os
    from collections import defaultdict as _dd
    cache_path = _os.path.join(_os.path.dirname(__file__), '.oraculo_cache', 'csv', 'PER_all.json')
    if not _os.path.exists(cache_path):
        return {}
    try:
        import json as _json
        data = _json.load(open(cache_path, 'r'))
        if not data:
            return {}
        # Sort by date ascending
        data.sort(key=lambda m: m.get('date', ''))
        team_seq = _dd(list)
        for m in data:
            ht = m.get('home_team', '').strip()
            at = m.get('away_team', '').strip()
            hs = m.get('home_score')
            as_ = m.get('away_score')
            if ht and at and hs is not None and as_ is not None:
                team_seq[ht].append((int(hs), int(as_), True))
                team_seq[at].append((int(as_), int(hs), False))
        form = {}
        for team, seq in team_seq.items():
            last = seq[-n_games:]
            hg = [(s, c) for s, c, ih in last if ih]
            ag = [(s, c) for s, c, ih in last if not ih]
            form[team] = {
                'h_sc': sum(s for s, c in hg) / len(hg) if hg else None,
                'h_cc': sum(c for s, c in hg) / len(hg) if hg else None,
                'h_n':  len(hg),
                'a_sc': sum(s for s, c in ag) / len(ag) if ag else None,
                'a_cc': sum(c for s, c in ag) / len(ag) if ag else None,
                'a_n':  len(ag),
            }
        return form
    except Exception:
        return {}

def _load_csv_form(comp_key, n_games=6):
    """Load rolling team form (last N games) from football-data.co.uk CSV.
    Returns: {team_name: {'h_sc','h_cc','h_n','a_sc','a_cc','a_n'}}
    Falls back to empty dict if CSV unavailable.
    """
    import time as _t, csv as _csv, io as _io
    from datetime import datetime as _dt
    from collections import defaultdict as _dd

    now = _t.time()
    cached = _CSV_FORM_DATA.get(comp_key)
    if cached and now - cached.get('ts', 0) < _CSV_FORM_TTL:
        return cached['form']

    # Peru Liga 1: use local API-Football JSON cache
    if comp_key == 'soccer-peru-primera-division':
        _peru_form = _load_peru_form(n_games)
        _CSV_FORM_DATA[comp_key] = {'form': _peru_form, 'ts': now}
        return _peru_form

    suffixes = _LEAGUE_CSV_SUFFIXES.get(comp_key, ())
    rows = []
    for suf in suffixes:
        try:
            resp = requests.get(_CSV_BASE + suf, timeout=15)
            resp.raise_for_status()
            parsed = [r for r in _csv.DictReader(_io.StringIO(resp.text))
                      if r.get('HomeTeam') and r.get('FTHG')]
            if len(parsed) >= 20:
                rows = parsed
                break
        except Exception:
            pass

    form = {}
    if rows:
        def _pdate(d):
            for fmt in ['%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d']:
                try: return _dt.strptime(d, fmt)
                except: pass
            return _dt.min

        rows.sort(key=lambda r: _pdate(r.get('Date', '')))
        team_seq = _dd(list)
        for r in rows:
            ht = r.get('HomeTeam', '').strip()
            at = r.get('AwayTeam', '').strip()
            try:
                hg = int(float(r.get('FTHG') or 0))
                ag = int(float(r.get('FTAG') or 0))
            except Exception:
                continue
            if ht: team_seq[ht].append((hg, ag, True))
            if at: team_seq[at].append((ag, hg, False))

        for team, seq in team_seq.items():
            last = seq[-n_games:]
            hg = [(s, c) for s, c, ih in last if ih]
            ag = [(s, c) for s, c, ih in last if not ih]
            form[team] = {
                'h_sc': sum(s for s, c in hg) / len(hg) if hg else None,
                'h_cc': sum(c for s, c in hg) / len(hg) if hg else None,
                'h_n':  len(hg),
                'a_sc': sum(s for s, c in ag) / len(ag) if ag else None,
                'a_cc': sum(c for s, c in ag) / len(ag) if ag else None,
                'a_n':  len(ag),
            }

    _CSV_FORM_DATA[comp_key] = {'form': form, 'ts': now}
    return form


def _xg_from_csv(home_cb, away_cb, csv_form, lg_h=1.52, lg_a=1.18):
    """Dixon-Coles xG from CSV form. Returns (home_xg, away_xg, n_samples)."""
    from difflib import SequenceMatcher as _SM

    def _best(name, cands):
        nl = name.lower()
        best, bests = None, 0.0
        for c in cands:
            cl = c.lower()
            if cl == nl: return c
            s = _SM(None, nl, cl).ratio()
            if nl in cl or cl in nl: s = max(s, 0.75)
            if s > bests: bests = s; best = c
        return best if bests >= 0.5 else None

    home_csv = _CB_TO_CSV.get(home_cb, home_cb)
    away_csv = _CB_TO_CSV.get(away_cb, away_cb)
    cands = list(csv_form.keys())

    if csv_form and home_csv not in csv_form:
        home_csv = _best(home_csv, cands) or home_csv
    if csv_form and away_csv not in csv_form:
        away_csv = _best(away_csv, cands) or away_csv

    hf = csv_form.get(home_csv, {})
    af = csv_form.get(away_csv, {})

    h_att = hf.get('h_sc'); a_def = af.get('a_cc')
    a_att = af.get('a_sc'); h_def = hf.get('h_cc')

    if h_att is not None and a_def is not None and lg_a > 0:
        home_xg = max(0.3, min(3.5, h_att * (a_def / lg_a)))
    else:
        home_xg = lg_h

    if a_att is not None and h_def is not None and lg_h > 0:
        away_xg = max(0.2, min(3.0, a_att * (h_def / lg_h)))
    else:
        away_xg = lg_a

    n_samples = hf.get('h_n', 0) + af.get('a_n', 0)
    return home_xg, away_xg, n_samples

# Team attack/defense lookup cache:  team_name -> {'scored': float, 'conceded': float, 'n': int}
_TEAM_FORM_CACHE = {}
_FORM_CACHE_TS   = {}
_FORM_TTL = 3600 * 4  # refresh every 4h


def _fetch_team_form(comp_key, api, max_events=12):
    """Estimate team goal rates from Cloudbet settled odds (as proxy for form).
    We don't have real match results, so we use league prior + home/away bias only.
    Returns dict: team -> {'home_scored': x, 'away_scored': x}
    """
    import time as _t
    now = _t.time()
    if comp_key in _TEAM_FORM_CACHE and now - _FORM_CACHE_TS.get(comp_key, 0) < _FORM_TTL:
        return _TEAM_FORM_CACHE[comp_key]

    # We use simplified priors: home teams score ~1.55 goals/game, away ~1.20
    # These are adjusted slightly by a team strength proxy (odds-based)
    result = {}
    try:
        events = api.get_odds(comp_key) or []
        for ev in events[:max_events]:
            home = (ev.get('home') or {}).get('name', '')
            away = (ev.get('away') or {}).get('name', '')
            if not home or not away:
                continue
            mkts = ev.get('markets', {})
            ml = mkts.get('soccer.match_odds', {})
            # Get moneyline implied probs
            h_prob = a_prob = 0.45
            for sv in ml.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    pr = float(sel.get('price', 0) or 0)
                    if pr < 1.01:
                        continue
                    oc = sel.get('outcome', '')
                    if oc == 'home' and pr > 1:
                        h_prob = round(1/pr, 3)
                    elif oc == 'away' and pr > 1:
                        a_prob = round(1/pr, 3)
                break
            # Strength proxy: strong favorites score more, weak teams score less
            h_str = min(1.25, max(0.75, h_prob * 2.0))
            a_str = min(1.25, max(0.75, a_prob * 2.0))
            result.setdefault(home, {'home_str': 1.0, 'away_str': 1.0})
            result.setdefault(away, {'home_str': 1.0, 'away_str': 1.0})
            result[home]['home_str'] = h_str
            result[away]['away_str'] = a_str
    except Exception:
        pass

    _TEAM_FORM_CACHE[comp_key] = result
    _FORM_CACHE_TS[comp_key] = now
    return result


def _poisson_over(lam, k_max=10):
    """P(X > k_max-0.5) where X ~ Poisson(lam). Returns P(goals >= k_max)."""
    import math
    p_le = 0.0
    for k in range(k_max):
        p_le += math.exp(-lam) * (lam ** k) / math.factorial(k)
    return round(1.0 - p_le, 4)



# Referee goal-rate cache: comp_key → {ref_name: {ft_goals, h2_goals, n}, _league_ft, _league_h2, ts}
_REF_GOALS_CACHE = {}
_REF_GOALS_TTL   = 43200  # 12 hours


def _build_referee_goal_stats(comp_key: str) -> dict:
    """
    Compute per-referee goals/match stats from football-data.co.uk CSV.
    Returns dict: {ref_name: {'ft': avg_ft_goals, 'h2': avg_h2_goals, 'n': n_matches},
                   '_league_ft': float, '_league_h2': float}
    """
    import time as _t, csv as _csv, io as _io

    now = _t.time()
    cached = _REF_GOALS_CACHE.get(comp_key)
    if cached and now - cached.get('ts', 0) < _REF_GOALS_TTL:
        return cached

    suffixes = _LEAGUE_CSV_SUFFIXES.get(comp_key, ())
    rows = []
    for suf in suffixes:
        try:
            resp = requests.get(_CSV_BASE + suf, timeout=15)
            resp.raise_for_status()
            parsed = [r for r in _csv.DictReader(_io.StringIO(resp.text))
                      if r.get('HomeTeam') and r.get('FTHG') and r.get('Referee')]
            if len(parsed) >= 15:
                rows = parsed
                break
        except Exception:
            pass

    result = {'ts': now, '_league_ft': 2.6, '_league_h2': 1.17}
    if not rows:
        _REF_GOALS_CACHE[comp_key] = result
        return result

    ref_stats = {}
    total_ft = total_h2 = total_n = 0
    for r in rows:
        ref = (r.get('Referee') or '').strip()
        if not ref:
            continue
        try:
            fthg = float(r.get('FTHG') or 0)
            ftag = float(r.get('FTAG') or 0)
            hthg = float(r.get('HTHG') or fthg / 2)
            htag = float(r.get('HTAG') or ftag / 2)
        except (ValueError, TypeError):
            continue
        ft_goals = fthg + ftag
        h2_goals = (fthg - hthg) + (ftag - htag)
        ref_l = ref.lower()
        if ref_l not in ref_stats:
            ref_stats[ref_l] = {'ft_sum': 0.0, 'h2_sum': 0.0, 'n': 0}
        ref_stats[ref_l]['ft_sum'] += ft_goals
        ref_stats[ref_l]['h2_sum'] += h2_goals
        ref_stats[ref_l]['n'] += 1
        total_ft += ft_goals
        total_h2 += h2_goals
        total_n  += 1

    if total_n > 0:
        result['_league_ft'] = round(total_ft / total_n, 3)
        result['_league_h2'] = round(total_h2 / total_n, 3)
    for ref_l, st in ref_stats.items():
        if st['n'] >= 5:
            result[ref_l] = {
                'ft':  round(st['ft_sum'] / st['n'], 3),
                'h2':  round(st['h2_sum'] / st['n'], 3),
                'n':   st['n'],
            }

    _REF_GOALS_CACHE[comp_key] = result
    log.debug('Referee goal stats: %d refs for %s | lg_ft=%.2f lg_h2=%.2f',
              len(ref_stats), comp_key, result['_league_ft'], result['_league_h2'])
    return result


def _get_ref_goals_mult(referee: str, comp_key: str, period: str = 'ft') -> float:
    """
    Return a goals multiplier for xg adjustment based on referee history.
    period: 'ft' (full-time) or 'h2' (second-half)
    Multiplier = ref_avg / league_avg, capped to [0.88, 1.12].
    Returns 1.0 if referee unknown or insufficient data.
    """
    if not referee:
        return 1.0
    stats = _build_referee_goal_stats(comp_key)
    ref_l = referee.lower()
    # Fuzzy match: try substring
    ref_key = None
    for key in stats:
        if key.startswith('_'):
            continue
        if key in ref_l or ref_l in key or any(w in ref_l for w in key.split() if len(w) > 3):
            ref_key = key
            break
    if not ref_key:
        return 1.0
    ref_data = stats.get(ref_key, {})
    if ref_data.get('n', 0) < 5:
        return 1.0
    league_avg = stats.get('_league_' + period, 0)
    ref_avg    = ref_data.get(period, 0)
    if not league_avg:
        return 1.0
    mult = ref_avg / league_avg
    return round(min(max(mult, 0.88), 1.12), 4)

def scan_soccer_goals(api, state, comp_keys=None, dry_run=False,
                      min_edge=0.06, min_conf=0.58):
    """
    Scan soccer Over/Under goals markets using Poisson model + real CSV form.
    Markets: FT O/U 2.5, 2H O/U 2.5, 2H U 1.5.
    xG source: football-data.co.uk rolling form (Dixon-Coles), odds proxy fallback.
    """
    picks = []
    if comp_keys is None:
        comp_keys = list(_LEAGUE_O25.keys())

    for comp_key in comp_keys:
        events = api.get_odds(comp_key) or []
        if not events:
            continue

        base_o25   = _LEAGUE_O25.get(comp_key, _DEFAULT_O25)
        base_o15   = _LEAGUE_O15.get(comp_key, _DEFAULT_O15)
        base_u15_2h = _LEAGUE_U15_2H.get(comp_key, _DEFAULT_U15_2H)

        # Primary: real form from CSV; fallback: match-odds proxy
        csv_form   = _load_csv_form(comp_key)
        odds_form  = _fetch_team_form(comp_key, api) if not csv_form else {}

        for ev in events:
            if not ev or not isinstance(ev, dict):
                continue
            home = (ev.get('home') or {}).get('name', '')
            away = (ev.get('away') or {}).get('name', '')
            eid  = str(ev.get('id', ''))
            if not home or not away:
                continue

            # Skip far-future matches (>14d) -- prevents PL 2026/27 season noise
            _ko_raw = ev.get('cutoffTime', '')
            if _ko_raw:
                try:
                    _ko_dt = datetime.strptime(_ko_raw[:19].replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
                    if (_ko_dt - datetime.utcnow()).total_seconds() > 14 * 86400:
                        continue
                except Exception:
                    pass

            # Referee goal-rate multiplier (improves xg calibration)
            _league_code = CB_TO_LEAGUE.get(comp_key, '_')
            _referee = _fetch_referee(home, away, _league_code) or ''
            _ref_mult_ft = _get_ref_goals_mult(_referee, comp_key, 'ft')
            _ref_mult_h2 = _get_ref_goals_mult(_referee, comp_key, 'h2')

            # xG: WC 2026 Dixon-Coles model → CSV form → odds proxy
            if comp_key == 'soccer-international-world-cup':
                # Use Dixon-Coles + ELO international model (Phase 2)
                try:
                    import sys as _sys
                    if '/home/noc/oraculo_v2' not in _sys.path:
                        _sys.path.insert(0, '/home/noc/oraculo_v2')
                    import oraculo_wc_model as _wc_m
                    home_xg, away_xg, _h_concerns, _a_concerns = _wc_m.get_player_adjusted_xg(home, away)
                    trust = 0.9   # DC+ELO model on 5y international results
                    n_samp = 50   # treated as high-sample source
                    if _h_concerns or _a_concerns:
                        log.info('WC [intel] %s vs %s concerns: H=%s A=%s xG %.2f-%.2f',
                                 home, away, _h_concerns, _a_concerns, home_xg, away_xg)
                    # Collusion boost: Group-3 simultaneous final games
                    # When setup materialises, under-probability increases significantly
                    _collusion_boost = _wc_m.collusion_draw_boost(home, away)
                    if _collusion_boost > 0.05:
                        # Reduce total_xg proportionally to collusion draw boost
                        # More draws → fewer goals → under more likely
                        # A +12% draw boost ~ -0.25 expected goals in total
                        _xg_reduction = _collusion_boost * 2.0  # empirical scale
                        home_xg = max(home_xg * (1.0 - _xg_reduction * 0.5), 0.5)
                        away_xg = max(away_xg * (1.0 - _xg_reduction * 0.5), 0.5)
                        log.info("WC [collusion-%s-vs-%s]: boost=+%.1f%% xG %.2f-%.2f",
                                 home, away, _collusion_boost*100, home_xg, away_xg)
                except Exception as _wc_e:
                    log.warning('WC DC model failed %s vs %s: %s', home, away, _wc_e)
                    home_xg = 1.2; away_xg = 1.1; trust = 0.4; n_samp = 0
            elif csv_form:
                home_xg, away_xg, n_samp = _xg_from_csv(home, away, csv_form)
                # Blend with base if thin sample (< 3 home/away games each)
                trust = min(1.0, n_samp / 6.0)
            else:
                h_str = odds_form.get(home, {}).get('home_str', 1.0)
                a_str = odds_form.get(away, {}).get('away_str', 1.0)
                home_xg = 1.52 * h_str
                away_xg = 1.18 * a_str
                trust = 0.5  # less trust in odds proxy
                n_samp = 0

            total_xg = (home_xg + away_xg) * _ref_mult_ft
            xg_2h    = total_xg * 0.45 * (_ref_mult_h2 / max(_ref_mult_ft, 0.01))

            # Poisson probs — blend model with league prior
            p_o25_m  = _poisson_over(total_xg, k_max=3)
            p_o25    = trust * p_o25_m + (1.0 - trust) * base_o25
            p_u25_ft = 1.0 - p_o25

            p_o25_2h_m = _poisson_over(xg_2h, k_max=3)
            p_o25_2h   = trust * p_o25_2h_m + (1.0 - trust) * base_o25
            p_u25_2h   = 1.0 - p_o25_2h

            # Under 1.5 2H: Poisson P(X <= 1) where X ~ Poisson(xg_2h)
            import math as _math
            p_u15_2h_m = (_math.exp(-xg_2h) * (1.0 + xg_2h))  # P(0) + P(1)
            p_u15_2h   = trust * p_u15_2h_m + (1.0 - trust) * base_u15_2h

            mkts    = ev.get('markets', {})
            match_s = '{} vs {}'.format(home, away)

            # Market candidates: (mkt_key, period_label, line, prob_over, prob_under, min_odds)
            # FT O/U BLOCKED: N=1 real bet (LOSS, xG 2.1) — insufficient data to calibrate FT model
            # Revisit when N>=10 FT bets resolved. Only 2H markets active.
            _candidates = [
                ('soccer.total_goals_period_second_half', '2H',  2.5, p_o25_2h, p_u25_2h, 1.25),
                ('soccer.total_goals_period_second_half', '2H',  1.5, None,     p_u15_2h, 1.15),
            ]

            for _mkt_key, _period, _tgt_line, _p_over, _p_under, _min_odds in _candidates:
                tg = mkts.get(_mkt_key, {})
                if not tg:
                    continue
                for sub_k, sub_v in tg.get('submarkets', {}).items():
                    for sel in sub_v.get('selections', []):
                        if sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                            continue
                        price   = float(sel.get('price', 0) or 0)
                        murl    = sel.get('marketUrl', '')
                        outcome = sel.get('outcome', '')
                        params  = str(sel.get('params', ''))
                        if price < _min_odds or not murl or outcome not in ('over', 'under'):
                            continue
                        try:
                            line = float(params.split('total=')[-1])
                        except Exception:
                            continue
                        if abs(line - _tgt_line) > 0.01:
                            continue
                        # Under 1.5 2H: only under side
                        if _tgt_line == 1.5 and outcome == 'over':
                            continue
                        # Block PL Under 1.5: Sibila WR 16.7% vs 84.6% elsewhere (xG underestimates PL 2H scoring)
                        if _tgt_line == 1.5 and 'premier' in comp_key:
                            continue

                        prob = _p_under if outcome == 'under' else _p_over
                        if prob is None:
                            continue

                        # Stricter min_conf for shorter odds markets
                        _min_conf = 0.78 if _tgt_line == 1.5 else min_conf
                        _min_edge = 0.08 if _tgt_line == 1.5 else min_edge

                        if prob < _min_conf:
                            continue
                        implied = 1.0 / price
                        edge    = prob - implied
                        if edge < _min_edge or edge > 0.35:
                            continue

                        picks.append({
                            'match':       match_s,
                            'league':      comp_key,
                            '_referee':    _referee,
                            '_ref_mult':   _ref_mult_ft,
                            'event_id':    eid,
                            'market_url':  murl,
                            'price':       price,
                            'label':       'Goals {} {} {} (xG {:.1f})'.format(
                                            _period, outcome.title(), line, xg_2h if _period=='2H' else total_xg),
                            'model_prob':  round(prob, 4),
                            'raw_model_prob': round(prob, 4),
                            'edge':        round(edge, 4),
                            'sport':       'soccer',
                            'market_type': 'soccer_goals',
                            '_xg_home':    round(home_xg, 2),
                            '_xg_away':    round(away_xg, 2),
                            '_csv_form':   bool(csv_form),
                            '_n_samp':     n_samp,
                            'cutoff_time': ev.get('cutoffTime', ''),
                        })

    # ── WC 2026 1X2 market scanning ──────────────────────────────────────────
    # Uses penaltyblog DC model probs; only for WC competition key
    _WC_KEY = 'soccer-international-world-cup'
    if _WC_KEY in (comp_keys or []):
        try:
            import sys as _sys2
            if '/home/noc/oraculo_v2' not in _sys2.path:
                _sys2.path.insert(0, '/home/noc/oraculo_v2')
            if '/home/noc/.local/lib/python3.12/site-packages' not in _sys2.path:
                _sys2.path.insert(0, '/home/noc/.local/lib/python3.12/site-packages')
            import oraculo_wc_model as _wc_1x2
        except Exception as _e:
            _wc_1x2 = None
            log.warning('WC 1X2: could not import model: %s', _e)

        if _wc_1x2:
            # Re-fetch WC events for 1X2 scan
            _wc_evts = api.get_odds(_WC_KEY) or []
            for _ev in (_wc_evts or []):
                if not _ev or _ev.get('type') == 'EVENT_TYPE_OUTRIGHT':
                    continue
                _h = (_ev.get('home') or {}).get('name', '')
                _a = (_ev.get('away') or {}).get('name', '')
                if not _h or not _a:
                    continue
                _mkts = _ev.get('markets', {})
                _ml = _mkts.get('soccer.match_odds', {})
                if not _ml:
                    continue
                # Get model probabilities
                try:
                    _res = _wc_1x2.predict_match(_h, _a, neutral=True)
                    _ph_m = _res['p_home']
                    _pd_m = _res['p_draw']
                    _pa_m = _res['p_away']
                except Exception as _e2:
                    log.debug('WC 1X2 predict failed %s vs %s: %s', _h, _a, _e2)
                    continue

                _match_s_1x2 = '{} vs {}'.format(_h, _a)
                _eid_1x2 = str(_ev.get('id', ''))

                for _sv_k, _sv_v in _ml.get('submarkets', {}).items():
                    for _sel in _sv_v.get('selections', []):
                        if _sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                            continue
                        _price = float(_sel.get('price', 0) or 0)
                        _murl  = _sel.get('marketUrl', '')
                        _oc    = _sel.get('outcome', '')
                        if _price < 1.20 or not _murl or _oc not in ('home', 'draw', 'away'):
                            continue

                        _p_model = {'home': _ph_m, 'draw': _pd_m, 'away': _pa_m}.get(_oc, 0)
                        _implied = 1.0 / _price
                        _edge_1x2 = _p_model - _implied
                        # Higher bar for 1X2: need 8% edge (more uncertain market)
                        _min_edge_1x2 = max(min_edge, 0.08)

                        if _edge_1x2 < _min_edge_1x2 or _edge_1x2 > 0.35:
                            continue
                        if _p_model < 0.50:  # skip long shots (<50% confidence)
                            continue

                        picks.append({
                            'match':         _match_s_1x2,
                            'league':        _WC_KEY,
                            '_referee':      '',
                            '_ref_mult':     1.0,
                            'event_id':      _eid_1x2,
                            'market_url':    _murl,
                            'price':         _price,
                            'label':         'WC 1X2 {} (DC {:.0f}%)'.format(_oc.title(), _p_model*100),
                            'model_prob':    round(_p_model, 4),
                            'raw_model_prob': round(_p_model, 4),
                            'edge':          round(_edge_1x2, 4),
                            'sport':         'soccer',
                            'market_type':   'wc_1x2',
                            '_xg_home':      _res.get('xg_home', 0),
                            '_xg_away':      _res.get('xg_away', 0),
                            '_csv_form':     False,
                            '_n_samp':       50,
                        })
            log.info('WC 1X2: %d picks added', sum(1 for p in picks if p.get('market_type') == 'wc_1x2'))
    # ────────────────────────────────────────────────────────────────────────────

    picks.sort(key=lambda p: p['edge'], reverse=True)
    log.info('Soccer goals: %d picks found across %d competitions', len(picks), len(comp_keys))
    return picks
