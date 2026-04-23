"""
oraculo_xg_firecrawl.py - Firecrawl-powered xG fetcher para Understat.
Reemplaza FBref (IP-baneado) con xG reales via Firecrawl extract.
5 ligas x 1 credito = 5 creditos/refresh. Cache 24h.
"""
import os, json, time, logging
from datetime import datetime

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FC_XG_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_firecrawl.json')
FC_API_KEY = 'fc-6322cb2d2b1e474798749fee9ec1c0f0'
XG_MAX_AGE = 86400  # 24h

UNDERSTAT_URLS = {
    'PL':  'https://understat.com/league/EPL/2025',
    'SA':  'https://understat.com/league/Serie_A/2025',
    'BL1': 'https://understat.com/league/Bundesliga/2025',
    'FL1': 'https://understat.com/league/Ligue_1/2025',
    'PD':  'https://understat.com/league/La_liga/2025',
}

SCHEMA = {
    'type': 'object',
    'properties': {
        'teams': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'name':           {'type': 'string'},
                    'matches':        {'type': 'number'},
                    'xG':             {'type': 'number'},
                    'xGA':            {'type': 'number'},
                    'goals':          {'type': 'number'},
                    'goals_against':  {'type': 'number'},
                },
                'required': ['name', 'matches', 'xG', 'xGA']
            }
        }
    }
}

PROMPT = (
    'Extract all teams with their xG (expected goals for), xGA (expected goals against), '
    'number of matches played, goals scored and goals conceded from the league stats table.'
)


def _fetch_league_xg(league, url):
    """Fetch xG for one league via Firecrawl extract. Returns dict {team: {xg, xga, gf, ga, mp}}."""
    try:
        from firecrawl import Firecrawl
        fc = Firecrawl(api_key=FC_API_KEY)
        result = fc.extract(urls=[url], prompt=PROMPT, schema=SCHEMA)
        data = result.data if hasattr(result, 'data') else result
        teams_raw = data.get('teams', []) if isinstance(data, dict) else []
        teams = {}
        for t in teams_raw:
            name = t.get('name', '').strip()
            mp = int(t.get('matches', 0)) or 1
            xg_total  = float(t.get('xG', 0))
            xga_total = float(t.get('xGA', 0))
            gf = float(t.get('goals', 0))
            ga = float(t.get('goals_against', 0))
            if name and xg_total > 0:
                teams[name] = {
                    'xg':  round(xg_total  / mp, 3),
                    'xga': round(xga_total / mp, 3),
                    'gf':  round(gf / mp, 3),
                    'ga':  round(ga / mp, 3),
                    'mp':  mp,
                }
        log.info('Firecrawl xG %s: %d teams (sample: %s)',
                 league, len(teams),
                 list(teams.keys())[:3])
        return teams
    except Exception as e:
        log.warning('Firecrawl xG failed for %s: %s', league, e)
        return {}


def load_xg_firecrawl(force_refresh=False):
    """Load xG data from Firecrawl/Understat. 24h cache."""
    os.makedirs(os.path.dirname(FC_XG_CACHE), exist_ok=True)

    if not force_refresh and os.path.exists(FC_XG_CACHE):
        age = time.time() - os.path.getmtime(FC_XG_CACHE)
        if age < XG_MAX_AGE:
            try:
                with open(FC_XG_CACHE) as f:
                    data = json.load(f)
                if data and len(data) >= 3:
                    log.info('Firecrawl xG loaded from cache (%d leagues, age=%.0fh)',
                             len(data), age / 3600)
                    return data
            except Exception:
                pass

    log.info('Refreshing xG from Understat via Firecrawl...')
    data = {}
    for league, url in UNDERSTAT_URLS.items():
        teams = _fetch_league_xg(league, url)
        if teams:
            data[league] = teams
        time.sleep(1)  # rate limit: 10 RPM on extract

    if data:
        data['_updated'] = datetime.utcnow().isoformat()
        with open(FC_XG_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
        log.info('Firecrawl xG saved: %d leagues', len(data) - 1)

    return data


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    data = load_xg_firecrawl(force_refresh=True)
    for league, teams in data.items():
        if league == '_updated':
            continue
        print(f'{league}: {len(teams)} teams')
        for name, stats in list(teams.items())[:3]:
            print(f'  {name}: xG={stats["xg"]} xGA={stats["xga"]} mp={stats["mp"]}')
