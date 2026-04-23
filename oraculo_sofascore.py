#!/usr/bin/env python3
"""
oraculo_sofascore.py - Sofascore API client for detailed match statistics.

Fetches: possession, passes, shots breakdown, corners, cards, fouls, offsides,
saves, duels, tackles - all per team per match.

Uses Sofascore's internal API (no key required).
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta

log = logging.getLogger('oraculo.sofascore')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'sofascore')

BASE_URL = 'https://api.sofascore.com/api/v1'

# Rate limit: max 1 request per second
_last_request = 0


def _fetch(endpoint):
    """Fetch from Sofascore API with rate limiting."""
    global _last_request
    from urllib.request import Request, urlopen

    elapsed = time.time() - _last_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    url = BASE_URL + endpoint
    req = Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    req.add_header('Accept', 'application/json')

    try:
        resp = urlopen(req, timeout=15)
        _last_request = time.time()
        return json.loads(resp.read())
    except Exception as e:
        log.debug('Sofascore fetch failed %s: %s', endpoint, e)
        _last_request = time.time()
        return None


def _ensure_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR


def get_match_statistics(event_id):
    """
    Get detailed match statistics by Sofascore event ID.

    Returns dict with per-team stats or None.
    """
    cache_file = os.path.join(_ensure_cache(), f'stats_{event_id}.json')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass

    data = _fetch(f'/event/{event_id}/statistics')
    if not data:
        return None

    parsed = _parse_statistics(data)
    if parsed:
        with open(cache_file, 'w') as f:
            json.dump(parsed, f)

    return parsed


def _parse_statistics(data):
    """Parse Sofascore statistics response into flat dict."""
    stats = {'home': {}, 'away': {}}

    for period in data.get('statistics', []):
        if period.get('period') != 'ALL':
            continue
        for group in period.get('groups', []):
            for item in group.get('statisticsItems', []):
                name = item.get('name', '').lower().replace(' ', '_')
                home_val = item.get('home', '')
                away_val = item.get('away', '')
                stats['home'][name] = _parse_stat_value(home_val)
                stats['away'][name] = _parse_stat_value(away_val)

    return stats if stats['home'] else None


def _parse_stat_value(val):
    """Parse stat value: '55%' -> 55, '12' -> 12, etc."""
    if val is None or val == '':
        return None
    s = str(val).replace('%', '').strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def search_event(home_team, away_team, date_str):
    """
    Search for a Sofascore event by team names and date.

    Args:
        home_team: home team name
        away_team: away team name
        date_str: 'YYYY-MM-DD'

    Returns:
        event_id (int) or None
    """
    cache_file = os.path.join(_ensure_cache(),
                              f'search_{date_str}_{home_team[:10]}_{away_team[:10]}.json'
                              .replace(' ', '_'))
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f).get('event_id')
        except Exception:
            pass

    data = _fetch(f'/sport/football/scheduled-events/{date_str}')
    if not data:
        return None

    events = data.get('events', [])
    for ev in events:
        h = ev.get('homeTeam', {}).get('name', '')
        a = ev.get('awayTeam', {}).get('name', '')
        if (_fuzzy_match(h, home_team) and _fuzzy_match(a, away_team)):
            eid = ev.get('id')
            with open(cache_file, 'w') as f:
                json.dump({'event_id': eid, 'home': h, 'away': a}, f)
            return eid

    return None


def _fuzzy_match(s1, s2):
    """Simple fuzzy team name match."""
    s1 = s1.lower().replace('fc', '').replace('cf', '').strip()
    s2 = s2.lower().replace('fc', '').replace('cf', '').strip()
    # Check if one contains the other or first word matches
    if s1 in s2 or s2 in s1:
        return True
    w1 = s1.split()[0] if s1 else ''
    w2 = s2.split()[0] if s2 else ''
    return w1 == w2 and len(w1) > 3


def get_match_stats_by_teams(home_team, away_team, date_str):
    """
    Get match statistics by team names and date.
    Combines search + stats fetch.

    Returns:
        dict with home/away stats or None
    """
    event_id = search_event(home_team, away_team, date_str)
    if not event_id:
        return None
    return get_match_statistics(event_id)


def compute_team_sofascore_stats(matches_stats, team_side_list, n=10):
    """
    Compute rolling averages from Sofascore match stats.

    Args:
        matches_stats: list of (stats_dict, 'home'|'away') tuples
        n: number of recent matches

    Returns:
        dict of averages
    """
    recent = matches_stats[-n:] if len(matches_stats) >= n else matches_stats
    if not recent:
        return _default_sofascore_stats()

    keys = ['ball_possession', 'total_shots', 'shots_on_target', 'corner_kicks',
            'fouls', 'yellow_cards', 'big_chances', 'accurate_passes',
            'duels_won', 'tackles']

    totals = {k: [] for k in keys}

    for stats, side in recent:
        if not stats or side not in stats:
            continue
        team_stats = stats[side]
        for k in keys:
            v = team_stats.get(k)
            if v is not None:
                totals[k].append(v)

    result = {}
    for k in keys:
        vals = totals[k]
        result[f'ss_{k}_avg'] = sum(vals) / len(vals) if vals else _default_sofascore_stats().get(f'ss_{k}_avg', 0)

    return result


def _default_sofascore_stats():
    return {
        'ss_ball_possession_avg': 50.0,
        'ss_total_shots_avg': 12.0,
        'ss_shots_on_target_avg': 4.0,
        'ss_corner_kicks_avg': 5.0,
        'ss_fouls_avg': 12.0,
        'ss_yellow_cards_avg': 1.5,
        'ss_big_chances_avg': 2.0,
        'ss_accurate_passes_avg': 400.0,
        'ss_duels_won_avg': 50.0,
        'ss_tackles_avg': 15.0,
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    # Test: search for a recent match
    print('Searching for Arsenal vs Chelsea...')
    eid = search_event('Arsenal', 'Chelsea', '2025-03-16')
    print(f'Event ID: {eid}')

    if eid:
        stats = get_match_statistics(eid)
        if stats:
            print(f'\nHome stats: {json.dumps(stats["home"], indent=2)[:500]}')
            print(f'\nAway stats: {json.dumps(stats["away"], indent=2)[:500]}')
