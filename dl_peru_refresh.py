#!/usr/bin/env python3
"""Refresh PER_all.json from API-Football (Peru Liga 1, ID 281). Run monthly.
Note: free plan covers up to 2024 only. 2025+ requires paid tier -- those
seasons are backfilled separately via oraculo_peru_wiki_scraper.py.

2026-07-13: fixed to MERGE by season instead of overwriting the whole file.
The old version wrote json.dump(all_matches, ...) unconditionally, which
would have wiped out the 2025/2026 Wikipedia-scraped data on the next
scheduled run (cron: 1st of month) since this script only ever fetches
2022-2025 and 2025 always fails on the free tier -- net effect would have
been silently reverting PER_all.json back to 2022-2024 only, mid-season.
"""
import requests, json, os, time, shutil, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(SCRIPT_DIR, 'oraculo_config.json')))
key = cfg.get('api_football_key', '')
if not key:
    print('ERROR: no api_football_key in oraculo_config.json'); exit(1)

headers = {'x-apisports-key': key}
out_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, 'PER_all.json')

fetched_by_season = {}
for season in [2022, 2023, 2024, 2025, 2026]:
    r = requests.get('https://v3.football.api-sports.io/fixtures',
        params={'league': 281, 'season': season, 'status': 'FT'},
        headers=headers, timeout=20)
    data = r.json()
    if data.get('errors'):
        print(f'{season}: SKIP - {list(data["errors"].values())[0]}')
        time.sleep(1)
        continue
    fixtures = data.get('response', [])
    remaining = r.headers.get('x-ratelimit-requests-remaining', '?')
    print(f'{season}: {len(fixtures)} matches, remaining={remaining}')
    matches = []
    for f in fixtures:
        t = f.get('teams', {}); g = f.get('goals', {}); v = f.get('fixture', {}).get('venue', {})
        matches.append({
            'home_team':  t.get('home', {}).get('name', ''),
            'away_team':  t.get('away', {}).get('name', ''),
            'home_score': g.get('home'), 'away_score': g.get('away'),
            'date':       f.get('fixture', {}).get('date', '')[:10],
            'venue_city': v.get('city', ''), 'venue_name': v.get('name', ''),
            'season':     season,
        })
    if matches:
        fetched_by_season[season] = matches
    time.sleep(1)

if not fetched_by_season:
    print('No seasons fetched successfully -- leaving existing cache untouched.')
    exit(0)

existing = []
if os.path.exists(out):
    with open(out) as fh:
        existing = json.load(fh)

kept = [m for m in existing if m.get('season') not in fetched_by_season]
merged = kept
for season, matches in fetched_by_season.items():
    merged += matches

if os.path.exists(out):
    backup = out + '.bak_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    shutil.copy(out, backup)

with open(out, 'w') as fh:
    json.dump(merged, fh, indent=2)
print(f'Merged: kept {len(kept)} existing (untouched seasons) + {sum(len(v) for v in fetched_by_season.values())} freshly fetched = {len(merged)} total -> {out}')
