#!/usr/bin/env python3
"""
oraculo_odds.py - Odds Fetcher (the-odds-api.com v4)

Fetches live betting odds from The Odds API for football, basketball, and MMA.
Maps bookmaker team names to football-data.org team names for cross-referencing
with the Oraculo prediction pipeline.

IMPORTANT: Free tier = 500 requests/month (~16/day). Cache aggressively.
- Odds: 2h TTL
- Sports list: 24h TTL
- API usage tracked in cache dir
"""

import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError, URLError

log = logging.getLogger('oraculo.odds')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BASE_URL = 'https://api.the-odds-api.com/v4'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Odds API sport keys -> our competition codes
SPORT_TO_COMPETITION = {
    'soccer_epl':                 'PL',
    'soccer_spain_la_liga':       'PD',
    'soccer_italy_serie_a':      'SA',
    'soccer_germany_bundesliga': 'BL1',
    'soccer_france_ligue_one':   'FL1',
    'soccer_uefa_champs_league': 'CL',
    'soccer_brazil_campeonato':  'BSA',
    'soccer_netherlands_eredivisie': 'DED',
    'soccer_portugal_primeira_liga': 'PPL',
}

COMPETITION_TO_SPORT = {v: k for k, v in SPORT_TO_COMPETITION.items()}

# All tracked sport keys
FOOTBALL_SPORTS = list(SPORT_TO_COMPETITION.keys())
BASKETBALL_SPORTS = ['basketball_nba']
MMA_SPORTS = ['mma_mixed_martial_arts']
ALL_SPORTS = FOOTBALL_SPORTS + BASKETBALL_SPORTS + MMA_SPORTS

# Cache TTLs in seconds
TTL_ODDS = 7200       # 2 hours
TTL_SPORTS = 86400    # 24 hours

# Default API parameters
DEFAULT_REGIONS = 'eu'
DEFAULT_MARKETS = 'h2h,totals'
DEFAULT_FORMAT = 'decimal'

# ---------------------------------------------------------------------------
# Team name mapping: odds-api name -> football-data.org name
# ---------------------------------------------------------------------------

_TEAM_NAME_MAP = {
    # Premier League
    'Arsenal':              'Arsenal FC',
    'Aston Villa':          'Aston Villa FC',
    'AFC Bournemouth':      'AFC Bournemouth',
    'Brentford':            'Brentford FC',
    'Brighton and Hove Albion': 'Brighton & Hove Albion FC',
    'Brighton':             'Brighton & Hove Albion FC',
    'Burnley':              'Burnley FC',
    'Chelsea':              'Chelsea FC',
    'Crystal Palace':       'Crystal Palace FC',
    'Everton':              'Everton FC',
    'Fulham':               'Fulham FC',
    'Ipswich Town':         'Ipswich Town FC',
    'Leicester City':       'Leicester City FC',
    'Liverpool':            'Liverpool FC',
    'Luton Town':           'Luton Town FC',
    'Manchester City':      'Manchester City FC',
    'Manchester United':    'Manchester United FC',
    'Newcastle United':     'Newcastle United FC',
    'Nottingham Forest':    'Nottingham Forest FC',
    'Sheffield United':     'Sheffield United FC',
    'Tottenham Hotspur':    'Tottenham Hotspur FC',
    'West Ham United':      'West Ham United FC',
    'Wolverhampton Wanderers': 'Wolverhampton Wanderers FC',
    'Wolves':               'Wolverhampton Wanderers FC',
    'Southampton':          'Southampton FC',

    # La Liga
    'Atletico Madrid':      'Club Atletico de Madrid',
    'Athletic Bilbao':      'Athletic Club',
    'Athletic Club Bilbao': 'Athletic Club',
    'Barcelona':            'FC Barcelona',
    'Real Betis':           'Real Betis Balompie',
    'Real Betis Balompie':  'Real Betis Balompie',
    'Cadiz':                'Cadiz CF',
    'Celta Vigo':           'RC Celta de Vigo',
    'Getafe':               'Getafe CF',
    'Girona':               'Girona FC',
    'Granada':              'Granada CF',
    'Las Palmas':           'UD Las Palmas',
    'Mallorca':             'RCD Mallorca',
    'Osasuna':              'CA Osasuna',
    'Rayo Vallecano':       'Rayo Vallecano de Madrid',
    'Real Madrid':          'Real Madrid CF',
    'Real Sociedad':        'Real Sociedad de Futbol',
    'Sevilla':              'Sevilla FC',
    'Valencia':             'Valencia CF',
    'Villarreal':           'Villarreal CF',
    'Deportivo Alaves':     'Deportivo Alaves',
    'Alaves':               'Deportivo Alaves',
    'Leganes':              'CD Leganes',
    'Espanyol':             'RCD Espanyol de Barcelona',
    'Real Valladolid':      'Real Valladolid CF',

    # Serie A
    'AC Milan':             'AC Milan',
    'Atalanta':             'Atalanta BC',
    'Bologna':              'Bologna FC 1909',
    'Cagliari':             'Cagliari Calcio',
    'Empoli':               'Empoli FC',
    'Fiorentina':           'ACF Fiorentina',
    'Frosinone':            'Frosinone Calcio',
    'Genoa':                'Genoa CFC',
    'Hellas Verona':        'Hellas Verona FC',
    'Inter Milan':          'FC Internazionale Milano',
    'Internazionale':       'FC Internazionale Milano',
    'Juventus':             'Juventus FC',
    'Lazio':                'SS Lazio',
    'Lecce':                'US Lecce',
    'AC Monza':             'AC Monza',
    'Monza':                'AC Monza',
    'Napoli':               'SSC Napoli',
    'Roma':                 'AS Roma',
    'AS Roma':              'AS Roma',
    'Salernitana':          'US Salernitana 1919',
    'Sassuolo':             'US Sassuolo Calcio',
    'Torino':               'Torino FC',
    'Udinese':              'Udinese Calcio',
    'Venezia':              'Venezia FC',
    'Como':                 'Como 1907',
    'Parma':                'Parma Calcio 1913',

    # Bundesliga
    'Augsburg':             'FC Augsburg',
    'Bayer Leverkusen':     'Bayer 04 Leverkusen',
    'Bayern Munich':        'FC Bayern Munchen',
    'Borussia Dortmund':    'Borussia Dortmund',
    'Borussia Monchengladbach': "Borussia Monchengladbach",
    'Eintracht Frankfurt':  'Eintracht Frankfurt',
    'Freiburg':             'Sport-Club Freiburg',
    'SC Freiburg':          'Sport-Club Freiburg',
    'TSG Hoffenheim':       'TSG 1899 Hoffenheim',
    'Hoffenheim':           'TSG 1899 Hoffenheim',
    'FC Koln':              '1. FC Koln',
    '1. FC Koln':           '1. FC Koln',
    'Mainz':                '1. FSV Mainz 05',
    'Mainz 05':             '1. FSV Mainz 05',
    'RB Leipzig':           'RB Leipzig',
    'Union Berlin':         '1. FC Union Berlin',
    'VfB Stuttgart':        'VfB Stuttgart',
    'Stuttgart':            'VfB Stuttgart',
    'VfL Bochum':           'VfL Bochum 1848',
    'Bochum':               'VfL Bochum 1848',
    'VfL Wolfsburg':        'VfL Wolfsburg',
    'Wolfsburg':            'VfL Wolfsburg',
    'Werder Bremen':        'SV Werder Bremen',
    'Heidenheim':           '1. FC Heidenheim 1846',
    'Darmstadt':            'SV Darmstadt 98',
    'Holstein Kiel':        'Holstein Kiel',
    'FC St. Pauli':         'FC St. Pauli 1910',

    # Ligue 1
    'Paris Saint Germain':  'Paris Saint-Germain FC',
    'Paris Saint-Germain':  'Paris Saint-Germain FC',
    'PSG':                  'Paris Saint-Germain FC',
    'Olympique Marseille':  'Olympique de Marseille',
    'Marseille':            'Olympique de Marseille',
    'Lyon':                 'Olympique Lyonnais',
    'Olympique Lyonnais':   'Olympique Lyonnais',
    'AS Monaco':            'AS Monaco FC',
    'Monaco':               'AS Monaco FC',
    'Lille':                'Lille OSC',
    'Lille OSC':            'Lille OSC',
    'Nice':                 'OGC Nice',
    'OGC Nice':             'OGC Nice',
    'Lens':                 'RC Lens',
    'RC Lens':              'RC Lens',
    'Rennes':               'Stade Rennais FC 1901',
    'Stade Rennais':        'Stade Rennais FC 1901',
    'Strasbourg':           'RC Strasbourg Alsace',
    'Toulouse':             'Toulouse FC',
    'Nantes':               'FC Nantes',
    'Montpellier':          'Montpellier HSC',
    'Brest':                'Stade Brestois 29',
    'Reims':                'Stade de Reims',
    'Le Havre':             'Le Havre AC',
    'Clermont Foot':        'Clermont Foot 63',
    'Lorient':              'FC Lorient',
    'Metz':                 'FC Metz',
    'Angers':               'Angers SCO',
    'Saint-Etienne':        'AS Saint-Etienne',
    'Auxerre':              'AJ Auxerre',

    # Eredivisie
    'Ajax':                 'AFC Ajax',
    'AZ Alkmaar':           'AZ',
    'AZ':                   'AZ',
    'Feyenoord':            'Feyenoord Rotterdam',
    'PSV Eindhoven':        'PSV',
    'PSV':                  'PSV',
    'FC Twente':            'FC Twente',
    'FC Utrecht':           'FC Utrecht',
    'Vitesse':              'Vitesse',
    'Heerenveen':           'sc Heerenveen',

    # Primeira Liga
    'Benfica':              'SL Benfica',
    'Porto':                'FC Porto',
    'FC Porto':             'FC Porto',
    'Sporting CP':          'Sporting Clube de Portugal',
    'Sporting Lisbon':      'Sporting Clube de Portugal',
    'Braga':                'SC Braga',
    'SC Braga':             'SC Braga',
}

# Reverse map for lookups
_TEAM_NAME_REVERSE = {}
for _odds_name, _fdo_name in _TEAM_NAME_MAP.items():
    if _fdo_name not in _TEAM_NAME_REVERSE:
        _TEAM_NAME_REVERSE[_fdo_name] = _odds_name

# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

def _get_api_key():
    """Load API key from env var or config file."""
    key = os.environ.get('ODDS_API_KEY', '')
    if key:
        return key
    cfg_path = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r') as f:
                cfg = json.load(f)
            key = cfg.get('odds_api_key', '')
            if key and not key.startswith('TU_'):
                return key
        except Exception as e:
            log.warning('Failed to read config: %s', e)
    log.warning('No ODDS_API_KEY found in env or config')
    return ''

# ---------------------------------------------------------------------------
# Cache (using shared FileCache)
# ---------------------------------------------------------------------------

from oraculo_utils import FileCache

_cache = FileCache('odds', default_ttl=600, max_files=500)

def _cache_key(endpoint, params):
    return FileCache.make_key(endpoint, params, exclude_keys={'apiKey'})

def _cache_get(endpoint, params, ttl):
    """Return cached data if fresh, else None."""
    return _cache.get(_cache_key(endpoint, params), ttl=ttl)

def _cache_put(endpoint, params, data):
    """Store data in cache."""
    _cache.put(_cache_key(endpoint, params), data)

def clear_cache():
    """Remove all cached files."""
    return _cache.clear()

# ---------------------------------------------------------------------------
# API usage tracking
# ---------------------------------------------------------------------------

_usage_file = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'odds_usage.json')

def _load_usage():
    """Load API usage stats from disk."""
    try:
        if os.path.exists(_usage_file):
            with open(_usage_file, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {
        'requests_remaining': None,
        'requests_used': None,
        'last_checked': None,
        'daily_log': {},
    }

def _save_usage(usage):
    """Save API usage stats to disk."""
    d = os.path.dirname(_usage_file)
    if not os.path.exists(d):
        os.makedirs(d)
    try:
        with open(_usage_file, 'w') as f:
            json.dump(usage, f, indent=2)
    except Exception as e:
        log.warning('Failed to save usage stats: %s', e)

def _update_usage(headers):
    """Update usage stats from response headers."""
    usage = _load_usage()
    remaining = headers.get('x-requests-remaining', '')
    used = headers.get('x-requests-used', '')
    if remaining:
        try:
            usage['requests_remaining'] = int(remaining)
        except ValueError:
            pass
    if used:
        try:
            usage['requests_used'] = int(used)
        except ValueError:
            pass
    usage['last_checked'] = datetime.now().isoformat()
    today = datetime.now().strftime('%Y-%m-%d')
    if today not in usage['daily_log']:
        usage['daily_log'][today] = 0
    usage['daily_log'][today] += 1
    _save_usage(usage)
    if usage['requests_remaining'] is not None:
        log.info('Odds API: %d requests remaining, %s used',
                 usage['requests_remaining'],
                 usage.get('requests_used', '?'))
    return usage

def get_usage():
    """Return current API usage stats."""
    return _load_usage()

def _check_budget():
    """Check if we have API budget left. Returns True if OK to proceed."""
    usage = _load_usage()
    remaining = usage.get('requests_remaining')
    if remaining is not None and remaining <= 10:
        log.warning('Odds API budget critically low: %d requests remaining!', remaining)
        return False
    today = datetime.now().strftime('%Y-%m-%d')
    daily = usage.get('daily_log', {}).get(today, 0)
    if daily >= 20:
        log.warning('Daily request limit reached (%d today). Skipping.', daily)
        return False
    return True

# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def _request(endpoint, params=None, ttl=None):
    """
    Make a GET request to The Odds API v4.
    Returns parsed JSON data or None on error.
    """
    if params is None:
        params = {}
    if ttl is None:
        ttl = TTL_ODDS

    # Check cache first
    cached = _cache_get(endpoint, params, ttl)
    if cached is not None:
        return cached

    # Check API budget before making a real request
    if not _check_budget():
        log.error('API budget exhausted. Using stale cache or returning None.')
        # Try stale cache (ignore TTL)
        stale = _cache_get(endpoint, params, 999999999)
        if stale is not None:
            log.info('Using stale cache for %s', endpoint)
            return stale
        return None

    # Build URL with API key
    api_key = _get_api_key()
    if not api_key:
        log.error('No API key configured. Set ODDS_API_KEY env var.')
        return None

    params['apiKey'] = api_key
    url = _BASE_URL + endpoint
    if params:
        qs = '&'.join('%s=%s' % (k, v) for k, v in sorted(params.items()))
        url = url + '?' + qs

    log.info('Odds API request: %s', endpoint)

    try:
        req = Request(url)
        req.add_header('Accept', 'application/json')
        resp = urlopen(req, timeout=30)
        raw = resp.read().decode('utf-8')
        data = json.loads(raw)

        # Track usage from response headers
        hdrs = {}
        for h in ['x-requests-remaining', 'x-requests-used']:
            val = resp.headers.get(h, '')
            if val:
                hdrs[h] = val
        if hdrs:
            _update_usage(hdrs)

        # Cache the response (without apiKey in params)
        cache_params = {k: v for k, v in params.items() if k != 'apiKey'}
        _cache_put(endpoint, cache_params, data)

        return data

    except HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        log.error('Odds API HTTP %d: %s %s', e.code, endpoint, body)
        if e.code == 401:
            log.error('Invalid API key. Check ODDS_API_KEY.')
        elif e.code == 429:
            log.error('Rate limited! Monthly quota may be exhausted.')
        # Fall back to stale cache
        stale = _cache_get(endpoint, {k: v for k, v in params.items() if k != 'apiKey'}, 999999999)
        if stale is not None:
            log.info('Using stale cache after error for %s', endpoint)
            return stale
        return None

    except (URLError, Exception) as e:
        log.error('Odds API request failed: %s - %s', endpoint, e)
        stale = _cache_get(endpoint, {k: v for k, v in params.items() if k != 'apiKey'}, 999999999)
        if stale is not None:
            log.info('Using stale cache after error for %s', endpoint)
            return stale
        return None

# ---------------------------------------------------------------------------
# Team name mapping
# ---------------------------------------------------------------------------

def map_team_name(odds_name):
    """Map an odds-api team name to football-data.org team name."""
    if odds_name in _TEAM_NAME_MAP:
        return _TEAM_NAME_MAP[odds_name]
    return odds_name

def map_team_name_reverse(fdo_name):
    """Map a football-data.org team name to odds-api team name."""
    if fdo_name in _TEAM_NAME_REVERSE:
        return _TEAM_NAME_REVERSE[fdo_name]
    return fdo_name

def find_team_match(name, candidates):
    """
    Fuzzy-find a team name in a list of candidates.
    Returns (matched_name, score) or (None, 0).
    """
    if not name or not candidates:
        return None, 0
    name_lower = name.lower().strip()
    # Exact match
    for c in candidates:
        if c.lower().strip() == name_lower:
            return c, 1.0
    # Substring match
    for c in candidates:
        cl = c.lower().strip()
        if name_lower in cl or cl in name_lower:
            return c, 0.8
    # Word overlap
    name_words = set(name_lower.split())
    best = None
    best_score = 0
    for c in candidates:
        c_words = set(c.lower().strip().split())
        overlap = len(name_words & c_words)
        total = max(len(name_words), len(c_words))
        if total > 0:
            score = overlap / total
            if score > best_score and score >= 0.4:
                best = c
                best_score = score
    return best, best_score

# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

def get_available_sports():
    """
    Get list of available (active) sports from the API.
    Cached for 24 hours.
    Returns list of sport dicts with keys: key, group, title, active.
    """
    data = _request('/sports', {}, ttl=TTL_SPORTS)
    if data is None:
        return []
    # Filter to active only
    active = [s for s in data if s.get('active', False)]
    log.info('Available sports: %d active', len(active))
    return active

def get_odds_for_sport(sport_key, regions=None, markets=None):
    """
    Get odds for a specific sport key.
    Returns list of event dicts.
    """
    if regions is None:
        regions = DEFAULT_REGIONS
    if markets is None:
        markets = DEFAULT_MARKETS
    params = {
        'regions': regions,
        'markets': markets,
        'oddsFormat': DEFAULT_FORMAT,
    }
    endpoint = '/sports/%s/odds' % sport_key
    data = _request(endpoint, params, ttl=TTL_ODDS)
    if data is None:
        return []
    if not isinstance(data, list):
        log.warning('Unexpected response type for %s: %s', sport_key, type(data))
        return []
    log.info('Got %d events for %s', len(data), sport_key)
    return data

def get_football_odds(competition_code=None):
    """
    Get odds for football matches.

    Args:
        competition_code: Optional competition code (PL, PD, SA, BL1, FL1, CL, BSA, DED, PPL).
                         If None, fetches all football leagues.

    Returns list of dicts with:
        - id, sport_key, sport_title
        - commence_time (ISO 8601)
        - home_team, away_team (original odds-api names)
        - home_team_mapped, away_team_mapped (football-data.org names)
        - competition_code (our code)
        - bookmakers (list of bookmaker odds)
    """
    if competition_code:
        sport_key = COMPETITION_TO_SPORT.get(competition_code)
        if not sport_key:
            log.warning('Unknown competition code: %s', competition_code)
            return []
        sport_keys = [sport_key]
    else:
        sport_keys = FOOTBALL_SPORTS

    all_events = []
    for sk in sport_keys:
        events = get_odds_for_sport(sk)
        comp_code = SPORT_TO_COMPETITION.get(sk, '')
        for ev in events:
            ev['home_team_mapped'] = map_team_name(ev.get('home_team', ''))
            ev['away_team_mapped'] = map_team_name(ev.get('away_team', ''))
            ev['competition_code'] = comp_code
        all_events.extend(events)

    log.info('Total football events with odds: %d', len(all_events))
    return all_events

def get_basketball_odds():
    """Get NBA odds. Returns list of event dicts."""
    all_events = []
    for sk in BASKETBALL_SPORTS:
        events = get_odds_for_sport(sk)
        all_events.extend(events)
    log.info('Basketball events with odds: %d', len(all_events))
    return all_events

def get_mma_odds():
    """Get MMA odds. Returns list of event dicts."""
    all_events = []
    for sk in MMA_SPORTS:
        events = get_odds_for_sport(sk)
        all_events.extend(events)
    log.info('MMA events with odds: %d', len(all_events))
    return all_events

def get_best_odds(events):
    """
    Extract best odds per outcome across all bookmakers.

    Args:
        events: list of event dicts from get_*_odds() functions.

    Returns list of dicts:
        - id, sport_key, commence_time
        - home_team, away_team
        - home_team_mapped, away_team_mapped (if present)
        - competition_code (if present)
        - best_home: {odds, bookmaker}
        - best_draw: {odds, bookmaker} (None for 2-way markets)
        - best_away: {odds, bookmaker}
        - num_bookmakers
        - margin (implied probability sum - 1)
    """
    results = []
    for ev in events:
        best = {
            'id': ev.get('id', ''),
            'sport_key': ev.get('sport_key', ''),
            'sport_title': ev.get('sport_title', ''),
            'commence_time': ev.get('commence_time', ''),
            'home_team': ev.get('home_team', ''),
            'away_team': ev.get('away_team', ''),
            'home_team_mapped': ev.get('home_team_mapped', ''),
            'away_team_mapped': ev.get('away_team_mapped', ''),
            'competition_code': ev.get('competition_code', ''),
            'best_home': {'odds': 0, 'bookmaker': ''},
            'best_draw': None,
            'best_away': {'odds': 0, 'bookmaker': ''},
            'num_bookmakers': 0,
            'margin': None,
        }

        home_name = ev.get('home_team', '')
        away_name = ev.get('away_team', '')
        bookmakers = ev.get('bookmakers', [])
        best['num_bookmakers'] = len(bookmakers)

        for bk in bookmakers:
            bk_name = bk.get('title', bk.get('key', ''))
            for market in bk.get('markets', []):
                if market.get('key') != 'h2h':
                    continue
                outcomes = market.get('outcomes', [])
                for oc in outcomes:
                    oc_name = oc.get('name', '')
                    oc_price = oc.get('price', 0)
                    if oc_name == home_name:
                        if oc_price > best['best_home']['odds']:
                            best['best_home'] = {'odds': oc_price, 'bookmaker': bk_name}
                    elif oc_name == away_name:
                        if oc_price > best['best_away']['odds']:
                            best['best_away'] = {'odds': oc_price, 'bookmaker': bk_name}
                    elif oc_name == 'Draw':
                        if best['best_draw'] is None:
                            best['best_draw'] = {'odds': 0, 'bookmaker': ''}
                        if oc_price > best['best_draw']['odds']:
                            best['best_draw'] = {'odds': oc_price, 'bookmaker': bk_name}

        # Calculate margin from best odds
        implied = 0
        if best['best_home']['odds'] > 0:
            implied += 1.0 / best['best_home']['odds']
        if best['best_away']['odds'] > 0:
            implied += 1.0 / best['best_away']['odds']
        if best['best_draw'] and best['best_draw']['odds'] > 0:
            implied += 1.0 / best['best_draw']['odds']
        if implied > 0:
            best['margin'] = round(implied - 1.0, 4)

        results.append(best)

    return results

def get_all_sports_odds():
    """
    Convenience function to fetch odds for all tracked sports.
    Returns dict with keys: football, basketball, mma.
    Each value is a list of event dicts.
    """
    return {
        'football': get_football_odds(),
        'basketball': get_basketball_odds(),
        'mma': get_mma_odds(),
    }

def get_match_odds(home_team, away_team, competition_code=None):
    """
    Find odds for a specific match by team names.
    Accepts either odds-api or football-data.org team names.

    Returns event dict or None.
    """
    events = get_football_odds(competition_code)
    if not events:
        return None

    # Normalize search names
    home_lower = home_team.lower().strip()
    away_lower = away_team.lower().strip()

    for ev in events:
        h = ev.get('home_team', '').lower().strip()
        a = ev.get('away_team', '').lower().strip()
        hm = ev.get('home_team_mapped', '').lower().strip()
        am = ev.get('away_team_mapped', '').lower().strip()

        home_match = (home_lower == h or home_lower == hm
                      or home_lower in h or h in home_lower
                      or home_lower in hm or hm in home_lower)
        away_match = (away_lower == a or away_lower == am
                      or away_lower in a or a in away_lower
                      or away_lower in am or am in away_lower)

        if home_match and away_match:
            return ev

    log.debug('No odds found for %s vs %s', home_team, away_team)
    return None

def odds_to_probabilities(odds_dict):
    """
    Convert decimal odds to implied probabilities (normalized to sum=1).

    Args:
        odds_dict: dict from get_best_odds() with best_home, best_draw, best_away.

    Returns dict with p_home, p_draw, p_away (or None if no draw market).
    """
    home_odds = odds_dict.get('best_home', {}).get('odds', 0)
    away_odds = odds_dict.get('best_away', {}).get('odds', 0)
    draw_info = odds_dict.get('best_draw')
    draw_odds = draw_info.get('odds', 0) if draw_info else 0

    if home_odds <= 0 or away_odds <= 0:
        return None

    p_home = 1.0 / home_odds
    p_away = 1.0 / away_odds
    p_draw = 1.0 / draw_odds if draw_odds > 0 else 0

    total = p_home + p_away + p_draw
    if total <= 0:
        return None

    result = {
        'p_home': round(p_home / total, 4),
        'p_away': round(p_away / total, 4),
    }
    if p_draw > 0:
        result['p_draw'] = round(p_draw / total, 4)
    else:
        result['p_draw'] = None

    return result

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(events, title):
    """Print a summary table of events with best odds."""
    print('\n=== %s (%d events) ===' % (title, len(events)))
    best = get_best_odds(events)
    for b in best:
        home = b['home_team']
        away = b['away_team']
        comp = b.get('competition_code', '')
        t = b.get('commence_time', '')[:16]
        h_odds = b['best_home']['odds']
        a_odds = b['best_away']['odds']
        d_info = b.get('best_draw')
        d_odds = d_info['odds'] if d_info else '-'
        margin = b.get('margin', '-')
        if margin is not None and margin != '-':
            margin = '%.1f%%' % (margin * 100)
        n_bk = b['num_bookmakers']

        if comp:
            print('  [%s] %s vs %s  |  %s' % (comp, home, away, t))
        else:
            print('  %s vs %s  |  %s' % (home, away, t))
        print('    Home: %.2f (%s)  Draw: %s  Away: %.2f (%s)  | %d books, margin %s' % (
            h_odds, b['best_home']['bookmaker'],
            ('%.2f (%s)' % (d_info['odds'], d_info['bookmaker'])) if d_info and d_info['odds'] > 0 else '-',
            a_odds, b['best_away']['bookmaker'],
            n_bk, margin,
        ))

if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description='Oraculo Odds Fetcher')
    parser.add_argument('--sport', default=None,
                        help='Sport key or competition code (e.g. soccer_epl, PL, basketball_nba, mma)')
    parser.add_argument('--all', action='store_true', help='Fetch all tracked sports')
    parser.add_argument('--football', action='store_true', help='Fetch all football leagues')
    parser.add_argument('--basketball', action='store_true', help='Fetch NBA odds')
    parser.add_argument('--mma', action='store_true', help='Fetch MMA odds')
    parser.add_argument('--sports-list', action='store_true', help='List available sports')
    parser.add_argument('--usage', action='store_true', help='Show API usage stats')
    parser.add_argument('--clear-cache', action='store_true', help='Clear odds cache')
    parser.add_argument('--match', nargs=2, metavar=('HOME', 'AWAY'),
                        help='Find odds for a specific match')
    args = parser.parse_args()

    if args.clear_cache:
        clear_cache()
        sys.exit(0)

    if args.usage:
        u = get_usage()
        print('API Usage:')
        print('  Remaining: %s' % u.get('requests_remaining', 'unknown'))
        print('  Used:      %s' % u.get('requests_used', 'unknown'))
        print('  Last check: %s' % u.get('last_checked', 'never'))
        daily = u.get('daily_log', {})
        if daily:
            print('  Daily log:')
            for day in sorted(daily.keys())[-7:]:
                print('    %s: %d requests' % (day, daily[day]))
        sys.exit(0)

    if args.sports_list:
        sports = get_available_sports()
        print('Available sports (%d):' % len(sports))
        for s in sorted(sports, key=lambda x: x.get('group', '')):
            comp = SPORT_TO_COMPETITION.get(s['key'], '')
            tag = ' -> %s' % comp if comp else ''
            print('  %-40s %s%s' % (s['key'], s.get('title', ''), tag))
        sys.exit(0)

    if args.match:
        ev = get_match_odds(args.match[0], args.match[1])
        if ev:
            _print_summary([ev], '%s vs %s' % (args.match[0], args.match[1]))
        else:
            print('No odds found for %s vs %s' % (args.match[0], args.match[1]))
        sys.exit(0)

    fetched = False
    if args.all:
        data = get_all_sports_odds()
        for cat, events in data.items():
            if events:
                _print_summary(events, cat.upper())
        fetched = True

    if args.football or (args.sport and args.sport in COMPETITION_TO_SPORT):
        code = args.sport if args.sport in COMPETITION_TO_SPORT else None
        events = get_football_odds(code)
        _print_summary(events, 'Football' + (' - %s' % code if code else ''))
        fetched = True

    if args.basketball:
        events = get_basketball_odds()
        _print_summary(events, 'Basketball (NBA)')
        fetched = True

    if args.mma:
        events = get_mma_odds()
        _print_summary(events, 'MMA')
        fetched = True

    if args.sport and not fetched:
        # Try as raw sport key
        events = get_odds_for_sport(args.sport)
        if events:
            _print_summary(events, args.sport)
        else:
            print('No events found for sport: %s' % args.sport)
        fetched = True

    if not fetched:
        parser.print_help()
        print('\nExample usage:')
        print('  python oraculo_odds.py --sports-list')
        print('  python oraculo_odds.py --football')
        print('  python oraculo_odds.py --sport PL')
        print('  python oraculo_odds.py --basketball')
        print('  python oraculo_odds.py --all')
        print('  python oraculo_odds.py --match "Real Madrid" "Barcelona"')
        print('  python oraculo_odds.py --usage')
