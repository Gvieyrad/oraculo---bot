#!/usr/bin/env python3
"""Refresh PER_all.json from API-Football (Peru Liga 1, ID 281). Run monthly."""
import requests, json, os, time

cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oraculo_config.json')))
key = cfg.get('api_football_key', '')
if not key:
    print("ERROR: no api_football_key in oraculo_config.json"); exit(1)

headers = {'x-apisports-key': key}
out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.oraculo_cache', 'csv')
os.makedirs(out_dir, exist_ok=True)

all_matches = []
for season in [2023, 2024, 2025]:
    r = requests.get('https://v3.football.api-sports.io/fixtures',
        params={'league': 281, 'season': season, 'status': 'FT'},
        headers=headers, timeout=20)
    fixtures = r.json().get('response', [])
    remaining = r.headers.get('x-ratelimit-requests-remaining', '?')
    print(f'{season}: {len(fixtures)} matches, remaining={remaining}')
    for f in fixtures:
        t = f.get('teams', {}); g = f.get('goals', {}); v = f.get('fixture', {}).get('venue', {})
        all_matches.append({
            'home_team':  t.get('home', {}).get('name', ''),
            'away_team':  t.get('away', {}).get('name', ''),
            'home_score': g.get('home'), 'away_score': g.get('away'),
            'date':       f.get('fixture', {}).get('date', '')[:10],
            'venue_city': v.get('city', ''), 'venue_name': v.get('name', ''),
            'season':     season,
        })
    time.sleep(1)

out = out_dir + '/PER_all.json'
with open(out, 'w') as fh:
    json.dump(all_matches, fh, indent=2)
print(f'Saved {len(all_matches)} matches -> {out}')
