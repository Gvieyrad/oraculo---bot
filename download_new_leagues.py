#!/usr/bin/env python3
"""
download_new_leagues.py — Descarga y convierte datos de ligas adicionales.
Crea archivos noc-owned new_PER.json, new_NOR.json, new_IRL.json, new_CYP.json.
Uso: python3 download_new_leagues.py
"""
import json, os, csv, io, sys
from datetime import datetime as _dt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

# ── PER: convert existing root-owned PER_all.json ───────────────────────────
def convert_per():
    src = os.path.join(CSV_DIR, 'PER_all.json')
    dst = os.path.join(CSV_DIR, 'new_PER.json')
    if not os.path.exists(src):
        print('PER: source not found, skip')
        return 0
    data = json.load(open(src))
    converted = []
    for m in data:
        hg = m.get('home_score')
        ag = m.get('away_score')
        if hg is None or ag is None:
            continue
        converted.append({
            'home':       m['home_team'],
            'away':       m['away_team'],
            'home_goals': int(hg),
            'away_goals': int(ag),
            'date':       str(m.get('date', ''))[:10],
            'league':     'PER',
        })
    with open(dst, 'w') as f:
        json.dump(converted, f)
    print(f'PER: {len(converted)} matches → {dst}')
    teams = {m['home'] for m in converted} | {m['away'] for m in converted}
    print(f'     {len(teams)} teams: {sorted(teams)[:6]}...')
    return len(converted)

# ── Football-data.co.uk extra league downloader ──────────────────────────────
def _datestr(s):
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
        try:
            return _dt.strptime(s.strip(), fmt).strftime('%Y-%m-%d')
        except Exception:
            pass
    return None

def download_fdco(url, league_code, out_filename):
    """Download a CSV from football-data.co.uk extra leagues and save as standard JSON."""
    import urllib.request
    dst = os.path.join(CSV_DIR, out_filename)
    print(f'\n{league_code}: downloading {url}')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'  ERROR: {e}')
        return 0

    reader = csv.DictReader(io.StringIO(raw))
    rows   = list(reader)
    print(f'  Downloaded: {len(rows)} rows, cols: {list(reader.fieldnames)[:8]}')

    # Football-data.co.uk main format: HomeTeam,AwayTeam,FTHG,FTAG,Date
    # Extra league format may differ: Home,Away,HG,AG or similar
    fieldmap_home  = ['HomeTeam', 'Home', 'home_team', 'home']
    fieldmap_away  = ['AwayTeam', 'Away', 'away_team', 'away']
    fieldmap_hg    = ['FTHG', 'HG', 'home_score', 'home_goals', 'GH']
    fieldmap_ag    = ['FTAG', 'AG', 'away_score', 'away_goals', 'GA']
    fieldmap_date  = ['Date', 'date', 'utc_date']

    def get_field(row, candidates):
        for c in candidates:
            if c in row and row[c] not in ('', None):
                return row[c]
        return None

    converted = []
    for row in rows:
        home = get_field(row, fieldmap_home)
        away = get_field(row, fieldmap_away)
        hg   = get_field(row, fieldmap_hg)
        ag   = get_field(row, fieldmap_ag)
        dt   = get_field(row, fieldmap_date)
        if not home or not away or hg is None or ag is None:
            continue
        try:
            hg_i = int(float(hg))
            ag_i = int(float(ag))
        except (ValueError, TypeError):
            continue
        ds = _datestr(str(dt)) if dt else None
        converted.append({
            'home':       home.strip(),
            'away':       away.strip(),
            'home_goals': hg_i,
            'away_goals': ag_i,
            'date':       ds or '',
            'league':     league_code,
        })

    converted = [m for m in converted if m['date']]
    with open(dst, 'w') as f:
        json.dump(converted, f)
    print(f'  Saved: {len(converted)} matches → {dst}')
    if converted:
        teams = {m['home'] for m in converted} | {m['away'] for m in converted}
        print(f'  {len(teams)} teams: {sorted(teams)[:6]}...')
    return len(converted)

def main():
    os.makedirs(CSV_DIR, exist_ok=True)

    # Step 1: Convert PER
    n_per = convert_per()

    # Step 2: Download Norway (Eliteserien) — multiple URL attempts
    nor_urls = [
        ('https://www.football-data.co.uk/new/NOR.csv', 'NOR'),
        ('https://www.football-data.co.uk/new/N1.csv', 'NOR'),
    ]
    n_nor = 0
    for url, code in nor_urls:
        n_nor = download_fdco(url, code, 'new_NOR.json')
        if n_nor > 0:
            break
    if n_nor == 0:
        print('NOR: could not download from any URL')

    # Step 3: Download Ireland (League of Ireland)
    irl_urls = [
        ('https://www.football-data.co.uk/new/IRL.csv', 'IRL'),
        ('https://www.football-data.co.uk/new/I1.csv',  'IRL'),
    ]
    n_irl = 0
    for url, code in irl_urls:
        n_irl = download_fdco(url, code, 'new_IRL.json')
        if n_irl > 0:
            break
    if n_irl == 0:
        print('IRL: could not download from any URL')

    # Step 4: Try Cyprus (Cypriot First Division) — bonus
    cyp_urls = [
        ('https://www.football-data.co.uk/new/CYP.csv', 'CYP'),
    ]
    n_cyp = 0
    for url, code in cyp_urls:
        n_cyp = download_fdco(url, code, 'new_CYP.json')
        if n_cyp > 0:
            break

    print(f'\n=== Summary ===')
    print(f'PER: {n_per} matches')
    print(f'NOR: {n_nor} matches')
    print(f'IRL: {n_irl} matches')
    print(f'CYP: {n_cyp} matches')
    if (n_per + n_nor + n_irl) > 0:
        print('\nListo. Ahora corre:')
        print('  python3 ganagol_retrain_v2.py')
        print('para incorporar las nuevas ligas al modelo DC.')

if __name__ == '__main__':
    main()
