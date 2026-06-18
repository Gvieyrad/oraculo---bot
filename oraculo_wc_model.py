#!/usr/bin/env python3
"""
WC 2026 Dixon-Coles + ELO model (rebuilt with correct 12x4 draw).
Generated: 2026-05-20  |  48 teams  |  50 EM iters + ELO blend
"""
import math

TEAM_PARAMS = {
  "Mexico": {
    "attack": -0.2408,
    "defense": -0.045,
    "ds_name": "Mexico",
    "n_matches": 99,
    "elo": 1930,
    "elo_blend": 0.0
  },
  "South Africa": {
    "attack": -0.2041,
    "defense": 0.1737,
    "ds_name": "South Africa",
    "n_matches": 73,
    "elo": 1724,
    "elo_blend": 0.0
  },
  "Republic of Korea": {
    "attack": -0.1029,
    "defense": 0.1707,
    "ds_name": "South Korea",
    "n_matches": 71,
    "elo": 1940,
    "elo_blend": 0.0
  },
  "Czech Republic": {
    "attack": 0.0004,
    "defense": 0.3073,
    "ds_name": "Czech Republic",
    "n_matches": 62,
    "elo": 1733,
    "elo_blend": 0.0
  },
  "Canada": {
    "attack": -0.1998,
    "defense": -0.1285,
    "ds_name": "Canada",
    "n_matches": 76,
    "elo": 1875,
    "elo_blend": 0.0
  },
  "Bosnia & Herzegovina": {
    "attack": -0.1859,
    "defense": 0.4578,
    "ds_name": "Bosnia and Herzegovina",
    "n_matches": 52,
    "elo": 1660,
    "elo_blend": 0.16
  },
  "Qatar": {
    "attack": -0.3265,
    "defense": 0.7158,
    "ds_name": "Qatar",
    "n_matches": 83,
    "elo": 1642,
    "elo_blend": 0.0
  },
  "Switzerland": {
    "attack": 0.0106,
    "defense": 0.0778,
    "ds_name": "Switzerland",
    "n_matches": 67,
    "elo": 1914,
    "elo_blend": 0.0
  },
  "Brazil": {
    "attack": 0.1703,
    "defense": -0.0996,
    "ds_name": "Brazil",
    "n_matches": 67,
    "elo": 1916,
    "elo_blend": 0.0
  },
  "Morocco": {
    "attack": 0.1006,
    "defense": -0.7464,
    "ds_name": "Morocco",
    "n_matches": 83,
    "elo": 2048,
    "elo_blend": 0.0
  },
  "Haiti": {
    "attack": 0.2142,
    "defense": 0.3615,
    "ds_name": "Haiti",
    "n_matches": 51,
    "elo": 1821,
    "elo_blend": 0.18
  },
  "Scotland": {
    "attack": -0.1778,
    "defense": 0.3284,
    "ds_name": "Scotland",
    "n_matches": 60,
    "elo": 1806,
    "elo_blend": 0.0
  },
  "USA": {
    "attack": -0.16,
    "defense": 0.3786,
    "ds_name": "United States",
    "n_matches": 90,
    "elo": 1808,
    "elo_blend": 0.0
  },
  "Paraguay": {
    "attack": -0.4486,
    "defense": 0.0179,
    "ds_name": "Paraguay",
    "n_matches": 61,
    "elo": 1838,
    "elo_blend": 0.0
  },
  "Australia": {
    "attack": -0.022,
    "defense": -0.2214,
    "ds_name": "Australia",
    "n_matches": 62,
    "elo": 1957,
    "elo_blend": 0.0
  },
  "Turkey": {
    "attack": 0.0895,
    "defense": 0.2872,
    "ds_name": "Turkey",
    "n_matches": 63,
    "elo": 1951,
    "elo_blend": 0.0
  },
  "Germany": {
    "attack": 0.1834,
    "defense": 0.1239,
    "ds_name": "Germany",
    "n_matches": 66,
    "elo": 1937,
    "elo_blend": 0.0
  },
  "Curacao": {
    "attack": 0.0439,
    "defense": 0.4052,
    "ds_name": "Curaçao",
    "n_matches": 46,
    "elo": 1774,
    "elo_blend": 0.28
  },
  "Ivory Coast": {
    "attack": 0.0764,
    "defense": -0.2543,
    "ds_name": "Ivory Coast",
    "n_matches": 66,
    "elo": 1878,
    "elo_blend": 0.0
  },
  "Ecuador": {
    "attack": -0.4817,
    "defense": -0.6473,
    "ds_name": "Ecuador",
    "n_matches": 70,
    "elo": 1925,
    "elo_blend": 0.0
  },
  "Netherlands": {
    "attack": 0.3353,
    "defense": -0.0439,
    "ds_name": "Netherlands",
    "n_matches": 67,
    "elo": 1954,
    "elo_blend": 0.0
  },
  "Japan": {
    "attack": 0.2366,
    "defense": -0.4081,
    "ds_name": "Japan",
    "n_matches": 68,
    "elo": 2021,
    "elo_blend": 0.0
  },
  "Sweden": {
    "attack": 0.0497,
    "defense": 0.5164,
    "ds_name": "Sweden",
    "n_matches": 61,
    "elo": 1734,
    "elo_blend": 0.0
  },
  "Tunisia": {
    "attack": -0.1595,
    "defense": -0.1357,
    "ds_name": "Tunisia",
    "n_matches": 79,
    "elo": 1841,
    "elo_blend": 0.0
  },
  "Belgium": {
    "attack": 0.3224,
    "defense": 0.1092,
    "ds_name": "Belgium",
    "n_matches": 65,
    "elo": 1885,
    "elo_blend": 0.0
  },
  "Egypt": {
    "attack": -0.1732,
    "defense": -0.2194,
    "ds_name": "Egypt",
    "n_matches": 76,
    "elo": 1867,
    "elo_blend": 0.0
  },
  "Iran": {
    "attack": 0.1023,
    "defense": -0.0278,
    "ds_name": "Iran",
    "n_matches": 62,
    "elo": 1943,
    "elo_blend": 0.0
  },
  "New Zealand": {
    "attack": 0.1309,
    "defense": 0.2322,
    "ds_name": "New Zealand",
    "n_matches": 48,
    "elo": 1808,
    "elo_blend": 0.24
  },
  "Spain": {
    "attack": 0.3723,
    "defense": -0.2525,
    "ds_name": "Spain",
    "n_matches": 70,
    "elo": 2154,
    "elo_blend": 0.0
  },
  "Cape Verde": {
    "attack": -0.207,
    "defense": 0.2549,
    "ds_name": "Cape Verde",
    "n_matches": 59,
    "elo": 1765,
    "elo_blend": 0.02
  },
  "Saudi Arabia": {
    "attack": -0.4492,
    "defense": 0.2237,
    "ds_name": "Saudi Arabia",
    "n_matches": 86,
    "elo": 1770,
    "elo_blend": 0.0
  },
  "Uruguay": {
    "attack": -0.3298,
    "defense": -0.2676,
    "ds_name": "Uruguay",
    "n_matches": 69,
    "elo": 1854,
    "elo_blend": 0.0
  },
  "France": {
    "attack": 0.2704,
    "defense": -0.1273,
    "ds_name": "France",
    "n_matches": 69,
    "elo": 2059,
    "elo_blend": 0.0
  },
  "Senegal": {
    "attack": 0.3167,
    "defense": -0.335,
    "ds_name": "Senegal",
    "n_matches": 80,
    "elo": 1981,
    "elo_blend": 0.0
  },
  "Iraq": {
    "attack": -0.3674,
    "defense": 0.1014,
    "ds_name": "Iraq",
    "n_matches": 73,
    "elo": 1839,
    "elo_blend": 0.0
  },
  "Norway": {
    "attack": 0.3404,
    "defense": -0.0544,
    "ds_name": "Norway",
    "n_matches": 54,
    "elo": 1979,
    "elo_blend": 0.12
  },
  "Argentina": {
    "attack": 0.325,
    "defense": -0.6847,
    "ds_name": "Argentina",
    "n_matches": 72,
    "elo": 2019,
    "elo_blend": 0.0
  },
  "Algeria": {
    "attack": 0.2682,
    "defense": -0.1507,
    "ds_name": "Algeria",
    "n_matches": 80,
    "elo": 1941,
    "elo_blend": 0.0
  },
  "Austria": {
    "attack": 0.1153,
    "defense": -0.1796,
    "ds_name": "Austria",
    "n_matches": 62,
    "elo": 1854,
    "elo_blend": 0.0
  },
  "Jordan": {
    "attack": 0.0006,
    "defense": 0.4383,
    "ds_name": "Jordan",
    "n_matches": 76,
    "elo": 1905,
    "elo_blend": 0.0
  },
  "Portugal": {
    "attack": 0.2548,
    "defense": -0.0949,
    "ds_name": "Portugal",
    "n_matches": 68,
    "elo": 1938,
    "elo_blend": 0.0
  },
  "DR Congo": {
    "attack": -0.1734,
    "defense": -0.4295,
    "ds_name": "DR Congo",
    "n_matches": 61,
    "elo": 1878,
    "elo_blend": 0.0
  },
  "Uzbekistan": {
    "attack": -0.2172,
    "defense": -0.1847,
    "ds_name": "Uzbekistan",
    "n_matches": 60,
    "elo": 1934,
    "elo_blend": 0.0
  },
  "Colombia": {
    "attack": 0.2082,
    "defense": -0.0048,
    "ds_name": "Colombia",
    "n_matches": 69,
    "elo": 1916,
    "elo_blend": 0.0
  },
  "England": {
    "attack": 0.1612,
    "defense": -0.4209,
    "ds_name": "England",
    "n_matches": 71,
    "elo": 2040,
    "elo_blend": 0.0
  },
  "Croatia": {
    "attack": 0.1427,
    "defense": -0.0562,
    "ds_name": "Croatia",
    "n_matches": 67,
    "elo": 1925,
    "elo_blend": 0.0
  },
  "Ghana": {
    "attack": -0.1831,
    "defense": 0.3451,
    "ds_name": "Ghana",
    "n_matches": 61,
    "elo": 1726,
    "elo_blend": 0.0
  },
  "Panama": {
    "attack": -0.11,
    "defense": 0.271,
    "ds_name": "Panama",
    "n_matches": 87,
    "elo": 1898,
    "elo_blend": 0.0
  }
}

NAME_MAP = {
  "Mexico": "Mexico",
  "South Africa": "South Africa",
  "Republic of Korea": "South Korea",
  "Czech Republic": "Czech Republic",
  "Canada": "Canada",
  "Bosnia & Herzegovina": "Bosnia and Herzegovina",
  "Qatar": "Qatar",
  "Switzerland": "Switzerland",
  "Brazil": "Brazil",
  "Morocco": "Morocco",
  "Haiti": "Haiti",
  "Scotland": "Scotland",
  "USA": "United States",
  "Paraguay": "Paraguay",
  "Australia": "Australia",
  "Turkey": "Turkey",
  "Germany": "Germany",
  "Curacao": "Curaçao",
  "Ivory Coast": "Ivory Coast",
  "Ecuador": "Ecuador",
  "Netherlands": "Netherlands",
  "Japan": "Japan",
  "Sweden": "Sweden",
  "Tunisia": "Tunisia",
  "Belgium": "Belgium",
  "Egypt": "Egypt",
  "Iran": "Iran",
  "New Zealand": "New Zealand",
  "Spain": "Spain",
  "Cape Verde": "Cape Verde",
  "Saudi Arabia": "Saudi Arabia",
  "Uruguay": "Uruguay",
  "France": "France",
  "Senegal": "Senegal",
  "Iraq": "Iraq",
  "Norway": "Norway",
  "Argentina": "Argentina",
  "Algeria": "Algeria",
  "Austria": "Austria",
  "Jordan": "Jordan",
  "Portugal": "Portugal",
  "DR Congo": "DR Congo",
  "Uzbekistan": "Uzbekistan",
  "Colombia": "Colombia",
  "England": "England",
  "Croatia": "Croatia",
  "Ghana": "Ghana",
  "Panama": "Panama"
}

VENUE_BOOST = {
  "USA": 0.12,
  "Mexico": 0.12,
  "Canada": 0.1
}

def _get_params(team_name):
    if team_name in TEAM_PARAMS: return TEAM_PARAMS[team_name]
    for fifa, p in TEAM_PARAMS.items():
        if (p["ds_name"] == team_name
                or team_name.lower() == p["ds_name"].lower()
                or team_name.lower() == fifa.lower()): return p
    return None


# ── penaltyblog DixonColes backend (fitted 2026-05-20) ──────────
# log-loss improvement: +2.8% (1.0051 → 0.9769)
_PB_MODEL_PATH = '/home/noc/oraculo_v2/wc2026/pb_model.pkl'
_pb_model_cache = [None]
_pb_model_mtime = [0.0]

def _get_pb_model():
    import os as _os3
    try:
        _mtime = _os3.path.getmtime(_PB_MODEL_PATH)
    except Exception:
        _mtime = 0.0
    if _pb_model_cache[0] is None or _mtime > _pb_model_mtime[0]:
        import sys as _sys
        _noc_lib = '/home/noc/.local/lib/python3.12/site-packages'
        if _noc_lib not in _sys.path:
            _sys.path.insert(0, _noc_lib)
        import penaltyblog as _pb
        _pb_model_cache[0] = _pb.models.DixonColesGoalModel.load(_PB_MODEL_PATH)
        _pb_model_mtime[0] = _mtime
    return _pb_model_cache[0]

import logging as _logging
log = _logging.getLogger('oraculo')

_PB_NAME_MAP = {
    'USA': 'United States',
    'Republic of Korea': 'South Korea',
    'Curacao': 'Curaçao',
    'Bosnia & Herzegovina': 'Bosnia and Herzegovina',
}


# -- Altitude correction for high-elevation WC venues --------
ALTITUDE_VENUES = {
    'Mexico City': 0.12,  # Azteca 2240m
    'Guadalupe':   0.07,  # Akron ~1500m (Estadio Akron = Zapopan)
    'Zapopan':     0.07,  # Estadio Akron ~1500m (same venue, alternate city name in CSV)
}
_ALT_NAME_NORM = {'South Korea': 'Republic of Korea', 'United States': 'USA'}
_WC_ALTITUDE_CACHE = None

def _build_altitude_lookup():
    import csv as _csv, os as _os2
    cache = {}
    _p = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), 'wc2026/intl_results_5y.csv')
    try:
        for row in _csv.DictReader(open(_p)):
            if 'World Cup' not in row.get('tournament', ''): continue
            if row.get('home_score', 'NA') != 'NA': continue
            city = row.get('city', '')
            pen  = ALTITUDE_VENUES.get(city, 0.0)
            if pen == 0.0: continue
            h = _ALT_NAME_NORM.get(row['home_team'], row['home_team'])
            a = _ALT_NAME_NORM.get(row['away_team'], row['away_team'])
            cache[(h, a)] = pen
            cache[(row['home_team'], row['away_team'])] = pen
    except Exception:
        pass
    return cache

def get_altitude_away_penalty(home_team, away_team):
    global _WC_ALTITUDE_CACHE
    if _WC_ALTITUDE_CACHE is None:
        _WC_ALTITUDE_CACHE = _build_altitude_lookup()
    return _WC_ALTITUDE_CACHE.get((home_team, away_team), 0.0)

def predict_match(home_team, away_team, neutral=True):
    """Predict 1X2 + xG. Uses penaltyblog DC model with ELO nudge."""
    h = _PB_NAME_MAP.get(home_team, home_team)
    a = _PB_NAME_MAP.get(away_team, away_team)
    pb = _get_pb_model()
    try:
        grid = pb.predict(h, a)
        xg_h = float(grid.home_goal_expectation)
        xg_a = float(grid.away_goal_expectation)
        p_h = float(grid.home_win)
        p_d = float(grid.draw)
        p_a = float(grid.away_win)
    except Exception:
        # Fallback: ELO from TEAM_PARAMS for unknown teams
        elo_h = TEAM_PARAMS.get(home_team, {}).get('elo', 1500)
        elo_a = TEAM_PARAMS.get(away_team, {}).get('elo', 1500)
        total = elo_h + elo_a
        # 2026-06-16: p_draw ya no es 0 (antes p_h+p_a=1.0 -> p_d=0 algebraico) + log visibilidad
        log.warning('[WC] predict fallback: equipo no reconocido por penaltyblog: %s vs %s', home_team, away_team)
        _pd = max(0.10, 0.26 - abs(elo_h - elo_a) / 3000.0)
        _rem = 1.0 - _pd
        p_h = (elo_h / total) * _rem
        p_a = (elo_a / total) * _rem
        p_d = _pd
        norm = p_h + p_d + p_a
        return {'p_home': round(p_h/norm, 4), 'p_draw': round(p_d/norm, 4),
                'p_away': round(p_a/norm, 4), 'xg_home': 1.3, 'xg_away': 1.1}

    if neutral:
        # Neutral venue: symmetric average of both directions removes home advantage
        # correctly for all match quality levels (old linear fix over-corrected for
        # extreme mismatches, e.g. Brazil 67% vs Haiti when true neutral is 91%)
        try:
            grid_rev = pb.predict(a, h)
            p_h = (p_h + float(grid_rev.away_win)) / 2
            p_d = (p_d + float(grid_rev.draw)) / 2
            p_a = (p_a + float(grid_rev.home_win)) / 2
            xg_h = (xg_h + float(grid_rev.away_goal_expectation)) / 2
            xg_a = (xg_a + float(grid_rev.home_goal_expectation)) / 2
        except Exception:
            ha = p_h - p_a
            p_h -= ha * 0.30
            p_a += ha * 0.30
        norm = p_h + p_d + p_a
        p_h, p_d, p_a = p_h/norm, p_d/norm, p_a/norm

    # Host venue boost — wires VENUE_BOOST dict (was defined but never applied)
    host_boost = VENUE_BOOST.get(home_team, 0.0)
    if host_boost:
        p_h = min(0.92, p_h + host_boost)
        p_a = max(0.04, p_a - host_boost * 0.7)
        norm = p_h + p_d + p_a
        p_h, p_d, p_a = p_h/norm, p_d/norm, p_a/norm

    # ELO nudge: 5% weight toward ELO probability
    elo_h = TEAM_PARAMS.get(home_team, {}).get('elo', 1500)
    elo_a = TEAM_PARAMS.get(away_team, {}).get('elo', 1500)
    elo_edge = (elo_h - elo_a) / 400.0 * 0.05
    p_h = max(0.04, min(0.92, p_h + elo_edge))
    p_a = max(0.04, min(0.92, p_a - elo_edge))
    p_d = max(0.04, 1.0 - p_h - p_a)

    norm = p_h + p_d + p_a
    p_h, p_d, p_a = p_h/norm, p_d/norm, p_a/norm

    # Altitude: away loses ~12% xG in Mexico City (2240m), ~7% in Guadalupe/Zapopan (Akron 1500m)
    alt_penalty = get_altitude_away_penalty(home_team, away_team)
    if alt_penalty > 0:
        shift = p_a * alt_penalty
        p_a -= shift
        p_h += shift * 0.60
        p_d += shift * 0.40
        _n2 = p_h + p_d + p_a
        p_h, p_d, p_a = p_h/_n2, p_d/_n2, p_a/_n2
        xg_a = round(xg_a * (1.0 - alt_penalty), 3)

    # Injury factors: missing key attackers shift win prob toward draw/opponent
    # Formula: each % of attack_factor lost transfers 40% of that gap from p_win
    _pf2 = _load_player_factors()
    _h_atk2 = _pf2.get(home_team, {}).get('attack_factor', 1.0)
    _a_atk2 = _pf2.get(away_team, {}).get('attack_factor', 1.0)
    if _h_atk2 < 0.99:
        _sh = (1.0 - _h_atk2) * p_h * 0.40
        p_h = max(0.05, p_h - _sh)
        p_d += _sh * 0.50; p_a += _sh * 0.50
    if _a_atk2 < 0.99:
        _sh = (1.0 - _a_atk2) * p_a * 0.40
        p_a = max(0.05, p_a - _sh)
        p_d += _sh * 0.50; p_h += _sh * 0.50
    _n3 = p_h + p_d + p_a
    p_h, p_d, p_a = p_h/_n3, p_d/_n3, p_a/_n3

    # Mismatch blowout: DC underestimates xG when ELO gap is large (minnow defenses)
    # Germany(1937) vs Curacao(1774)=163 diff; Spain(2154) vs Haiti(1821)=333 diff
    _elo_raw_diff = elo_h - elo_a
    _elo_abs = abs(_elo_raw_diff)
    if _elo_abs >= 200:
        if _elo_abs >= 400:
            _xg_mult = 1.35   # extreme mismatch (Spain vs Qatar-level)
        elif _elo_abs >= 300:
            _xg_mult = 1.20   # strong mismatch (Spain vs Haiti-level)
        else:
            _xg_mult = 1.10   # moderate mismatch (Germany vs Curacao)
        if _elo_raw_diff > 0:
            xg_h = round(min(4.5, xg_h * _xg_mult), 3)
        else:
            xg_a = round(min(4.5, xg_a * _xg_mult), 3)

    return {
        'p_home': round(p_h, 4),
        'p_draw': round(p_d, 4),
        'p_away': round(p_a, 4),
        'xg_home': round(xg_h, 3),
        'xg_away': round(xg_a, 3),
        'elo_diff': int(_elo_raw_diff),
    }


def get_wc_team_xg(home_team, away_team, neutral=True):
    r = predict_match(home_team, away_team, neutral)
    return r["xg_home"], r["xg_away"]

def collusion_draw_boost(home_team, away_team):
    """Return expected draw boost for this match if it is a Round-3 game.

    During the tournament, if wc_standings.json shows both t1 and t2
    have >= 4 points (guaranteed qualification), returns a live boost of 0.18.
    Otherwise falls back to the pre-tournament probability-weighted boost.
    """
    import json as _j, os as _o
    base = _o.path.dirname(_o.path.abspath(__file__))
    cpath = _o.path.join(base, "wc2026/collusion_risk.json")
    spath = _o.path.join(base, "wc2026/wc_standings.json")
    try:
        data = _j.load(open(cpath))
        for g, info in data.items():
            for game_key in ("round3_game1", "round3_game2"):
                fg = info.get(game_key, [])
                if home_team in fg and away_team in fg:
                    # Try live standings first
                    try:
                        std = _j.load(open(spath))
                        grp_std = std.get(g, [])
                        t1 = info["teams"][0]; t2 = info["teams"][1]
                        p1 = next((r["pts"] for r in grp_std if r["team"] == t1), 0)
                        p2 = next((r["pts"] for r in grp_std if r["team"] == t2), 0)
                        played = max((r["played"] for r in grp_std), default=0)
                        if played >= 2 and p1 >= 4 and p2 >= 4:
                            # Both top teams already through — dead rubber confirmed
                            return 0.18
                        if played >= 2 and (p1 >= 4 or p2 >= 4):
                            # One team safe, mild boost
                            return 0.08
                    except Exception:
                        pass
                    return info.get("draw_boost_t1t2", 0.0)
    except Exception: pass
    return 0.0


# ── Player intelligence integration ──────────────────────────────────────────
_player_factors_cache = [None]
_player_factors_mtime = [0.0]

def _load_player_factors():
    import json as _j, os as _o
    pf = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), 'wc2026/wc_player_factors.json')
    try:
        _mt = _o.path.getmtime(pf)
    except Exception:
        _mt = 0.0
    if _player_factors_cache[0] is not None and _mt <= _player_factors_mtime[0]:
        return _player_factors_cache[0]
    try:
        _player_factors_cache[0] = _j.load(open(pf))
        _player_factors_mtime[0] = _mt
    except Exception:
        if _player_factors_cache[0] is None:
            _player_factors_cache[0] = {}
    return _player_factors_cache[0]

def get_player_adjusted_xg(home_team, away_team, neutral=True):
    home_xg, away_xg = get_wc_team_xg(home_team, away_team, neutral)
    pf = _load_player_factors()
    h_data = pf.get(home_team, {})
    a_data = pf.get(away_team, {})
    h_atk = h_data.get('attack_factor',  1.0)
    h_def = h_data.get('defense_factor', 1.0)
    a_atk = a_data.get('attack_factor',  1.0)
    a_def = a_data.get('defense_factor', 1.0)
    home_xg_adj = max(0.40, min(4.0, home_xg * h_atk * (2.0 - a_def)))
    away_xg_adj = max(0.40, min(4.0, away_xg * a_atk * (2.0 - h_def)))
    home_concerns = [c['player'] for c in h_data.get('concerns', [])]
    away_concerns = [c['player'] for c in a_data.get('concerns', [])]
    # altitude already applied inside predict_match; no double-apply here
    return round(home_xg_adj, 3), round(away_xg_adj, 3), home_concerns, away_concerns

def get_elo_mismatch(home_team, away_team):
    """Returns (elo_diff, level): level 0=competitive, 1=moderate, 2=strong mismatch."""
    elo_h = TEAM_PARAMS.get(home_team, {}).get('elo', 1500)
    elo_a = TEAM_PARAMS.get(away_team, {}).get('elo', 1500)
    diff = abs(elo_h - elo_a)
    if diff >= 300:
        return diff, 2   # block Under 2H
    if diff >= 200:
        return diff, 1   # warn only
    return diff, 0       # competitive
