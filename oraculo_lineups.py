"""Pre-match lineup scraper — Sofascore/FlashScore for confirmed XIs."""
import os, json, re, time, logging
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LINEUP_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'lineups.json')


def _fetch_sofascore_lineups(event_id):
    """Fetch confirmed lineups from Sofascore API."""
    import urllib.request
    url = f'https://api.sofascore.com/api/v1/event/{event_id}/lineups'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)  # May 403 from server IPs
        data = json.loads(resp.read().decode('utf-8'))
        return data
    except Exception:
        return None


def _fetch_sofascore_events(date_str=None):
    """Fetch today's events from Sofascore to map to Cloudbet events."""
    import urllib.request
    if date_str is None:
        date_str = datetime.utcnow().strftime('%Y-%m-%d')
    url = f'https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)  # May 403 from server IPs
        data = json.loads(resp.read().decode('utf-8'))
        return data.get('events', [])
    except Exception:
        pass
    # Proxy fallback
    try:
        from oraculo_proxy import proxied_get, is_configured
        if is_configured():
            html = proxied_get(url, timeout=30)
            if html:
                data = json.loads(html)
                return data.get('events', [])
    except Exception:
        pass
    return []


def _match_event(home, away, sofascore_events):
    """Fuzzy match Cloudbet event to Sofascore event."""
    home_l = home.lower().strip()
    away_l = away.lower().strip()
    for ev in sofascore_events:
        h = (ev.get('homeTeam', {}).get('name', '') or '').lower()
        a = (ev.get('awayTeam', {}).get('name', '') or '').lower()
        # Exact or partial match
        if (home_l in h or h in home_l) and (away_l in a or a in away_l):
            return ev
        # Try short names
        hs = (ev.get('homeTeam', {}).get('shortName', '') or '').lower()
        as_ = (ev.get('awayTeam', {}).get('shortName', '') or '').lower()
        if hs and as_ and (home_l in hs or hs in home_l) and (away_l in as_ or as_ in away_l):
            return ev
    return None


def get_confirmed_lineup(home, away, sofascore_events=None):
    """Get confirmed lineup for a match. Returns dict with starters count, key absences.

    Returns None if lineups not yet confirmed.
    Returns dict: {
        'home_starters': 11, 'away_starters': 11,
        'confirmed': True/False,
        'home_missing_regulars': 0, 'away_missing_regulars': 0,
        'home_players': [...], 'away_players': [...]
    }
    """
    if sofascore_events is None:
        sofascore_events = _fetch_sofascore_events()

    ev = _match_event(home, away, sofascore_events)
    if not ev:
        return None

    eid = ev.get('id')
    if not eid:
        return None

    lineups = _fetch_sofascore_lineups(eid)
    if not lineups:
        return None

    result = {'confirmed': False, 'sofascore_id': eid}

    if lineups.get('confirmed'):
        result['confirmed'] = True

    for side in ['home', 'away']:
        team_data = lineups.get(side, {})
        players = team_data.get('players', [])
        starters = [p for p in players if p.get('substitute', True) is False
                     or p.get('position', '') != 'SUB']
        # Some APIs nest differently
        if not starters:
            starters = players[:11] if len(players) >= 11 else players

        result[f'{side}_starters'] = len(starters)
        result[f'{side}_players'] = [
            p.get('player', {}).get('name', p.get('name', ''))
            for p in starters
        ]

    return result


def lineup_adjustment(lineup_info):
    """Calculate probability adjustment based on lineup info.

    Returns (home_adj, away_adj) — additive adjustments to win probability.
    Positive = better for that side.
    """
    if not lineup_info or not lineup_info.get('confirmed'):
        return 0, 0

    home_adj = 0
    away_adj = 0

    # Fewer than 11 starters = something unusual
    home_st = lineup_info.get('home_starters', 11)
    away_st = lineup_info.get('away_starters', 11)

    if home_st < 11:
        home_adj -= 0.02 * (11 - home_st)
    if away_st < 11:
        away_adj -= 0.02 * (11 - away_st)

    return home_adj, away_adj


def load_lineups_for_today():
    """Load and cache today's Sofascore events for lineup matching."""
    os.makedirs(os.path.dirname(LINEUP_CACHE), exist_ok=True)

    # Check cache freshness (1h)
    if os.path.exists(LINEUP_CACHE):
        age = time.time() - os.path.getmtime(LINEUP_CACHE)
        if age < 3600:
            with open(LINEUP_CACHE) as f:
                return json.load(f)

    events = _fetch_sofascore_events()
    # Also fetch tomorrow
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
    events += _fetch_sofascore_events(tomorrow)

    if events:
        with open(LINEUP_CACHE, 'w') as f:
            json.dump(events, f)
        log.info('Sofascore: %d events cached for lineup matching', len(events))

    return events
