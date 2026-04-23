#!/usr/bin/env python3
"""
oraculo_apifootball.py - API-Football client for lineups and match events.

Free tier: 100 requests/day via RapidAPI.
Provides: lineups, formations, injuries, player ratings, events.

Sign up: https://rapidapi.com/api-sports/api/api-football
"""

import os
import json
import time
import logging

log = logging.getLogger('oraculo.apifootball')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'apifootball')

BASE_URL = 'https://api-football-v1.p.rapidapi.com/v3'

# League IDs in API-Football
LEAGUE_IDS = {
    'PL': 39,    # Premier League
    'PD': 140,   # La Liga
    'SA': 135,   # Serie A
    'BL1': 78,   # Bundesliga
    'FL1': 61,   # Ligue 1
}


def _get_api_key():
    """Get API-Football key from env or config."""
    key = os.environ.get('API_FOOTBALL_KEY', '')
    if key:
        return key
    cfg_path = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r') as f:
                cfg = json.load(f)
            key = cfg.get('api_football_key', '')
            if key:
                return key
            # Fallback to rapidapi key
            return cfg.get('rapidapi_key', '')
        except Exception:
            pass
    return ''


def _fetch(endpoint, params=None):
    """Fetch from API-Football (direct or RapidAPI)."""
    import requests as req_lib

    api_key = _get_api_key()
    if not api_key:
        log.warning('No API_FOOTBALL_KEY configured')
        return None

    # Try direct API first (api-sports.io)
    url = 'https://v3.football.api-sports.io' + endpoint
    if params:
        from urllib.parse import urlencode
        url += '?' + urlencode(params)

    headers = {'x-apisports-key': api_key}

    try:
        resp = req_lib.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json().get('response', [])
        log.debug('API-Football %s: %s', resp.status_code, resp.text[:100])
        return None
    except Exception as e:
        log.error('API-Football fetch failed: %s', e)
        return None


def _ensure_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR


def get_lineups(fixture_id):
    """
    Get lineups for a fixture.

    Returns:
        list of team lineup dicts or None
    """
    cache_file = os.path.join(_ensure_cache(), f'lineup_{fixture_id}.json')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass

    data = _fetch('/fixtures/lineups', {'fixture': fixture_id})
    if data:
        with open(cache_file, 'w') as f:
            json.dump(data, f)
    return data


def get_fixture_stats(fixture_id):
    """
    Get match statistics for a fixture.

    Returns:
        list of team stats dicts or None
    """
    cache_file = os.path.join(_ensure_cache(), f'fstats_{fixture_id}.json')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass

    data = _fetch('/fixtures/statistics', {'fixture': fixture_id})
    if data:
        with open(cache_file, 'w') as f:
            json.dump(data, f)
    return data


def get_injuries(league_code, season=2024):
    """
    Get current injuries for a league.

    Returns:
        list of injury dicts
    """
    league_id = LEAGUE_IDS.get(league_code)
    if not league_id:
        return []

    cache_file = os.path.join(_ensure_cache(), f'injuries_{league_code}_{season}.json')
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 3600:  # 1h cache
            try:
                with open(cache_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass

    data = _fetch('/injuries', {'league': league_id, 'season': season})
    if data:
        with open(cache_file, 'w') as f:
            json.dump(data, f)
    return data or []


def search_fixture(home_team, away_team, date_str, league_code=None):
    """
    Search for a fixture by teams and date.

    Returns:
        fixture_id or None
    """
    params = {'date': date_str}
    if league_code and league_code in LEAGUE_IDS:
        params['league'] = LEAGUE_IDS[league_code]
        params['season'] = int(date_str[:4])

    cache_file = os.path.join(_ensure_cache(),
                              f'fix_{date_str}_{home_team[:8]}_{away_team[:8]}.json'
                              .replace(' ', '_'))
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f).get('fixture_id')
        except Exception:
            pass

    data = _fetch('/fixtures', params)
    if not data:
        return None

    for fix in data:
        h = fix.get('teams', {}).get('home', {}).get('name', '')
        a = fix.get('teams', {}).get('away', {}).get('name', '')
        if (home_team.lower() in h.lower() or h.lower() in home_team.lower()) and \
           (away_team.lower() in a.lower() or a.lower() in away_team.lower()):
            fid = fix.get('fixture', {}).get('id')
            with open(cache_file, 'w') as f:
                json.dump({'fixture_id': fid, 'home': h, 'away': a}, f)
            return fid

    return None


def compute_lineup_features(lineups):
    """
    Extract ML features from lineup data.

    Returns:
        dict of features
    """
    if not lineups:
        return {
            'home_formation_attack': 3, 'away_formation_attack': 3,
            'home_avg_rating': 0, 'away_avg_rating': 0,
            'home_lineup_changes': 0, 'away_lineup_changes': 0,
        }

    features = {}
    for i, team in enumerate(lineups[:2]):
        prefix = 'home' if i == 0 else 'away'
        formation = team.get('formation', '4-3-3')

        # Count attackers from formation
        parts = formation.split('-')
        attackers = int(parts[-1]) if parts else 3
        features[f'{prefix}_formation_attack'] = attackers

        # Player ratings if available
        players = team.get('startXI', [])
        ratings = []
        for p in players:
            player = p.get('player', {})
            if player.get('rating'):
                try:
                    ratings.append(float(player['rating']))
                except (ValueError, TypeError):
                    pass
        features[f'{prefix}_avg_rating'] = sum(ratings) / len(ratings) if ratings else 0
        features[f'{prefix}_lineup_changes'] = 0  # Would need previous match data

    return features


def compute_injury_features(injuries, home_team, away_team):
    """
    Count injuries/suspensions per team.

    Returns:
        dict of features
    """
    home_out = 0
    away_out = 0

    for inj in (injuries or []):
        team_name = inj.get('team', {}).get('name', '')
        if home_team.lower() in team_name.lower():
            home_out += 1
        elif away_team.lower() in team_name.lower():
            away_out += 1

    return {
        'home_injuries': home_out,
        'away_injuries': away_out,
        'injury_diff': home_out - away_out,
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    api_key = _get_api_key()
    if api_key:
        print(f'API key configured: {api_key[:8]}...')
        print('Testing fixture search...')
        fid = search_fixture('Arsenal', 'Chelsea', '2025-03-16', 'PL')
        print(f'Fixture ID: {fid}')
        if fid:
            stats = get_fixture_stats(fid)
            print(f'Stats: {len(stats)} teams')
    else:
        print('No RAPIDAPI_KEY. Set env var or add to oraculo_config.json')
        print('Sign up free: https://rapidapi.com/api-sports/api/api-football')
