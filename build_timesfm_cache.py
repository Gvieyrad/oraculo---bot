"""
build_timesfm_cache.py -- Populate timesfm_xg_cache.json from FBref

Run once before enabling TimesFM (early August, when domestic leagues have data):
  python3 build_timesfm_cache.py --leagues premier-league bundesliga serie-a

What it does:
  1. Fetches team list for each league from FBref
  2. For each team, scrapes last 12 match shooting logs to get xG
  3. Stores in timesfm_xg_cache.json: {"team name": [xg1, xg2, ...]}
"""

import json
import time
import argparse
import logging
import re
import os
import sys

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print('Missing deps: pip install requests beautifulsoup4')
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timesfm_xg_cache.json')
MAX_SERIES = 12
RATE_SLEEP = 4.0

FBREF_LEAGUE_IDS = {
    'premier-league':   ('9',  'Premier-League'),
    'bundesliga':       ('20', 'Bundesliga'),
    'serie-a':          ('11', 'Serie-A'),
    'la-liga':          ('12', 'La-Liga'),
    'ligue-1':          ('13', 'Ligue-1'),
    'champions-league': ('8',  'Champions-League'),
}

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; oraculo-timesfm-cache/1.0)'}


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)
    log.info('Saved cache: %d teams', len(cache))


def fetch_squad_xg(squad_id, squad_name, season='2024-2025'):
    url = f'https://fbref.com/en/squads/{squad_id}/{season}/matchlogs/all_comps/shooting/'
    log.info('Fetching %s...', squad_name)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning('Failed %s: %s', squad_name, e)
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table', id=re.compile('matchlogs'))
    if not table:
        return None
    xg_series = []
    for row in table.find('tbody').find_all('tr'):
        if row.get('class') and 'spacer' in row.get('class', []):
            continue
        xg_cell = (row.find('td', {'data-stat': 'xg_for'})
                   or row.find('td', {'data-stat': 'xg'}))
        if not xg_cell or not xg_cell.text.strip():
            continue
        try:
            xg_series.append(round(float(xg_cell.text.strip()), 3))
        except ValueError:
            continue
    return xg_series[-MAX_SERIES:] if xg_series else None


def fetch_league_teams(league_key, season='2024-2025'):
    if league_key not in FBREF_LEAGUE_IDS:
        log.error('Unknown league: %s', league_key)
        return {}
    lid, lname = FBREF_LEAGUE_IDS[league_key]
    url = f'https://fbref.com/en/comps/{lid}/{season}/stats/{season}-{lname}-Stats'
    log.info('League page: %s', url)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error('Failed league page: %s', e)
        return {}
    soup = BeautifulSoup(r.text, 'html.parser')
    teams = {}
    for a in soup.find_all('a', href=re.compile(r'/en/squads/([a-f0-9]+)/')):
        m = re.search(r'/en/squads/([a-f0-9]+)/', a['href'])
        if m:
            name = a.text.strip().lower()
            squad_id = m.group(1)
            if name and squad_id and name not in teams:
                teams[name] = squad_id
    return teams


def build_cache(leagues, season='2024-2025'):
    cache = load_cache()
    for league_key in leagues:
        log.info('=== %s ===', league_key)
        teams = fetch_league_teams(league_key, season)
        log.info('Found %d teams', len(teams))
        for team_name, squad_id in teams.items():
            if team_name in cache and len(cache[team_name]) >= 8:
                log.info('Skip %s (already cached)', team_name)
                continue
            time.sleep(RATE_SLEEP)
            series = fetch_squad_xg(squad_id, team_name, season)
            if series and len(series) >= 3:
                cache[team_name] = series
                log.info('  OK %s: %d values', team_name, len(series))
            else:
                log.warning('  SKIP %s: insufficient data', team_name)
        save_cache(cache)
    log.info('Done. %d teams in cache.', len(cache))
    return cache


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--leagues', nargs='+',
                        default=['premier-league', 'bundesliga', 'serie-a'],
                        choices=list(FBREF_LEAGUE_IDS.keys()))
    parser.add_argument('--season', default='2024-2025')
    args = parser.parse_args()
    build_cache(args.leagues, args.season)
    log.info('Next: set TIMESFM_ENABLED = True in oraculo_timesfm.py')
