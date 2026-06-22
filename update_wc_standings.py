#!/usr/bin/env python3
"""
update_wc_standings.py
Fetch WC 2026 group standings and write wc_standings.json.

Sources (tried in order):
  1. football-data.org API  (free token: https://www.football-data.org/client/register)
     → set FOOTBALL_DATA_TOKEN in /etc/samael/secrets.env
  2. Wikipedia group pages  (no token, scraping fallback)

Run from cron every 2h during the tournament (Jun 12 – Jul 3, 2026).
Outside tournament window: exits quickly with no writes.
"""
import json, os, re, html, sys, datetime, urllib.request, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('wc_standings')

BASE         = '/home/noc/oraculo_v2/wc2026'
STANDINGS_F  = os.path.join(BASE, 'wc_standings.json')
GROUPS_F     = os.path.join(BASE, 'wc2026_groups.json')
SECRETS_F    = '/etc/samael/secrets.env'

# Tournament window (skip outside to save requests)
TOUR_START = datetime.date(2026, 6, 12)
TOUR_END   = datetime.date(2026, 7, 4)

# WC 2026 team name normalisations (source → our canonical name)
NAME_MAP = {
    'Bosnia and Herzegovina': 'Bosnia & Herzegovina',
    'Bosnia & Herzegowina':   'Bosnia & Herzegovina',
    'Bosnia-Herzegovina':     'Bosnia & Herzegovina',
    'Republic of Korea':      'Republic of Korea',
    'Korea Republic':         'Republic of Korea',
    'South Korea':            'Republic of Korea',
    'United States':          'USA',
    'United States of America': 'USA',
    'DR Congo':               'DR Congo',
    'Congo DR':               'DR Congo',
    'Congo, DR':              'DR Congo',
    "Côte d'Ivoire":          'Ivory Coast',
    'Cote d\'Ivoire':         'Ivory Coast',
    'Curaçao':                'Curacao',
    'Curacao':                'Curacao',
    'Türkiye':                'Turkey',
}

def norm(name):
    name = html.unescape(name.strip())
    return NAME_MAP.get(name, name)

def load_secrets():
    token = os.environ.get('FOOTBALL_DATA_TOKEN', '')
    if not token and os.path.exists(SECRETS_F):
        for line in open(SECRETS_F):
            if line.startswith('FOOTBALL_DATA_TOKEN='):
                token = line.split('=', 1)[1].strip()
    return token

# ── Source 1: football-data.org ───────────────────────────────────────────────
def fetch_football_data(token):
    url = 'https://api.football-data.org/v4/competitions/WC/standings'
    req = urllib.request.Request(url, headers={'X-Auth-Token': token})
    r = urllib.request.urlopen(req, timeout=12)
    d = json.loads(r.read())
    standings = {}
    for standing in d.get('standings', []):
        grp = standing.get('group', '')
        # Group names like "GROUP_A" → "A"
        grp = re.sub(r'(?i)^group[\s_]*', '', grp).strip().upper()  # "Group A"/"GROUP_A" -> "A"
        if not grp:
            continue
        table = []
        for entry in standing.get('table', []):
            table.append({
                'team':   norm(entry['team']['name']),
                'pts':    entry.get('points', 0),
                'w':      entry.get('won', 0),
                'd':      entry.get('draw', 0),
                'l':      entry.get('lost', 0),
                'gf':     entry.get('goalsFor', 0),
                'ga':     entry.get('goalsAgainst', 0),
                'gd':     entry.get('goalDifference', 0),
                'played': entry.get('playedGames', 0),
            })
        if table:
            standings[grp] = table
    return standings

# ── Source 2: Wikipedia per-group scraping ────────────────────────────────────
def _strip_tags(s):
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s).strip()

def _cell_int(s):
    s = _strip_tags(s)
    # Handle em-dash, minus signs, etc.
    s = s.replace('−', '-').replace('–', '-')
    try:
        return int(s)
    except ValueError:
        return 0

def fetch_wikipedia_group(letter):
    url = f'https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_Group_{letter}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 WC-standings-bot/1.0 (educational)'
    })
    content = urllib.request.urlopen(req, timeout=12).read().decode('utf-8')

    # Find the first wikitable (the group standings table)
    table_m = re.search(r'(<table[^>]*wikitable[^>]*>.*?</table>)', content, re.DOTALL)
    if not table_m:
        return []

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
    result = []
    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        if len(cells) < 9:
            continue
        cleaned = [_strip_tags(c) for c in cells]
        # Skip header rows (Pos, Team, Pld …)
        if not cleaned[0].isdigit():
            continue
        try:
            team  = norm(cleaned[1])
            pld   = _cell_int(cells[2])
            w     = _cell_int(cells[3])
            d     = _cell_int(cells[4])
            l     = _cell_int(cells[5])
            gf    = _cell_int(cells[6])
            ga    = _cell_int(cells[7])
            gd    = gf - ga
            # pts is usually at index 9; fall back to calculated
            pts = _cell_int(cells[9]) if len(cells) > 9 else w * 3 + d
            result.append({'team': team, 'pts': pts, 'w': w, 'd': d, 'l': l,
                            'gf': gf, 'ga': ga, 'gd': gd, 'played': pld})
        except Exception:
            continue
    return result

def fetch_wikipedia_all(groups):
    standings = {}
    for letter in sorted(groups.keys()):
        try:
            rows = fetch_wikipedia_group(letter)
            if rows:
                standings[letter] = rows
                log.info('Wikipedia Group %s: %d teams, played=%s',
                         letter, len(rows), [r['played'] for r in rows])
        except Exception as e:
            log.warning('Wikipedia Group %s failed: %s', letter, e)
    return standings

# ── Merge with existing zeros ─────────────────────────────────────────────────
def merge_with_existing(new_data, groups):
    """Keep all 48 teams; fill missing ones with zeros."""
    existing = {}
    if os.path.exists(STANDINGS_F):
        with open(STANDINGS_F) as f:
            existing = json.load(f)

    result = {}
    for grp, teams in groups.items():
        new_grp = {r['team']: r for r in new_data.get(grp, [])}
        old_grp = {r['team']: r for r in existing.get(grp, [])}
        merged = []
        for team in teams:
            if team in new_grp:
                merged.append(new_grp[team])
            elif team in old_grp:
                merged.append(old_grp[team])
            else:
                merged.append({'team': team, 'pts': 0, 'w': 0, 'd': 0, 'l': 0,
                                'gf': 0, 'ga': 0, 'gd': 0, 'played': 0})
        # Sort by pts desc, gd desc, gf desc
        merged.sort(key=lambda x: (-x['pts'], -x['gd'], -x['gf']))
        result[grp] = merged
    return result

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    today = datetime.date.today()

    # Skip outside tournament window
    if today < TOUR_START:
        days = (TOUR_START - today).days
        log.info('Tournament starts in %d days (Jun 12). Nothing to fetch yet.', days)
        return 0
    if today > TOUR_END:
        log.info('Tournament ended. Standings frozen.')
        return 0

    groups = json.load(open(GROUPS_F))
    token  = load_secrets()

    new_standings = {}

    # Try football-data.org first
    if token:
        try:
            log.info('Fetching from football-data.org...')
            new_standings = fetch_football_data(token)
            log.info('Got standings for groups: %s', sorted(new_standings.keys()))
        except Exception as e:
            log.warning('football-data.org failed: %s', e)

    # Fallback: Wikipedia
    if not new_standings:
        log.info('Falling back to Wikipedia scraper...')
        new_standings = fetch_wikipedia_all(groups)

    if not new_standings:
        log.error('All sources failed. Standings not updated.')
        return 1

    merged = merge_with_existing(new_standings, groups)
    with open(STANDINGS_F, 'w') as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    log.info('Wrote %s (%d groups)', STANDINGS_F, len(merged))

    # Log summary
    for grp in sorted(merged.keys()):
        top2 = merged[grp][:2]
        safe = [t['team'] for t in top2 if t['played'] >= 2 and t['pts'] >= 4]
        if safe:
            log.info('Group %s — teams already through: %s', grp, safe)

    return 0

if __name__ == '__main__':
    sys.exit(main())
