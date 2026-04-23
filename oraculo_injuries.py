"""Transfermarkt injury/suspension scraper for Oraculo (v2 - fixed parsing)."""
import os, json, re, time, logging

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INJURY_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'injuries.json')
INJURY_MAX_AGE = 21600  # 6h cache

TM_LEAGUES = {
    'PL': '/premier-league/verletztespieler/wettbewerb/GB1',
    'PD': '/laliga/verletztespieler/wettbewerb/ES1',
    'SA': '/serie-a/verletztespieler/wettbewerb/IT1',
    'BL1': '/bundesliga/verletztespieler/wettbewerb/L1',
    'FL1': '/ligue-1/verletztespieler/wettbewerb/FR1',
}


def _fetch_injuries(league_path):
    """Scrape injuries from Transfermarkt via curl_cffi."""
    try:
        from curl_cffi import requests as cf_requests
        url = 'https://www.transfermarkt.com' + league_path
        r = cf_requests.get(url, impersonate='chrome', timeout=20,
                           headers={'Accept-Language': 'en-US,en;q=0.9'})
        if r.status_code != 200:
            log.warning('Transfermarkt HTTP %d', r.status_code)
            return {}
        html = r.text
    except Exception as e:
        log.warning('Transfermarkt fetch failed: %s', e)
        return {}

    injuries = {}

    # Find all team wappens with positions (title="TeamName" ... tiny_wappen)
    teams_pos = []
    for m in re.finditer(r'tiny_wappen', html):
        start = max(0, m.start() - 200)
        snippet = html[start:m.end()]
        title_m = re.search(r'title="([^"]+)"', snippet)
        if title_m:
            teams_pos.append((m.start(), title_m.group(1)))

    # Find all players with positions (hauptlink > a)
    players_pos = []
    for m in re.finditer(r'class="hauptlink"[^>]*>\s*<a[^>]*>([^<]+)</a>', html):
        players_pos.append((m.start(), m.group(1).strip()))

    # Map each player to the nearest team wappen AFTER it in the HTML
    for p_pos, p_name in players_pos:
        nearest = 'Unknown'
        for t_pos, t_name in teams_pos:
            if t_pos > p_pos:
                nearest = t_name
                break
        injuries.setdefault(nearest, []).append({
            'player': p_name,
            'market_value': 0,
        })

    # Remove Unknown if empty
    if 'Unknown' in injuries and not injuries['Unknown']:
        del injuries['Unknown']

    return injuries


def load_injuries(force_refresh=False):
    """Load cached injury data, refresh if stale (6h)."""
    os.makedirs(os.path.dirname(INJURY_CACHE), exist_ok=True)

    if not force_refresh and os.path.exists(INJURY_CACHE):
        age = time.time() - os.path.getmtime(INJURY_CACHE)
        if age < INJURY_MAX_AGE:
            with open(INJURY_CACHE) as f:
                return json.load(f)

    log.info('Refreshing injury data from Transfermarkt...')
    data = {}
    for league, path in TM_LEAGUES.items():
        injuries = _fetch_injuries(path)
        if injuries:
            data[league] = injuries
            total = sum(len(v) for v in injuries.values())
            teams = len([k for k in injuries if k != 'Unknown'])
            log.info('  %s: %d teams, %d injuries', league, teams, total)
        time.sleep(4)

    if data:
        with open(INJURY_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
    return data


def get_team_injuries(team_name, league, injury_data):
    """Get injury count and impact for a team. Returns (count, impact_score)."""
    if not injury_data or league not in injury_data:
        return 0, 0.0
    teams = injury_data[league]
    # Fuzzy match
    team_lower = team_name.lower()
    for k, v in teams.items():
        k_lower = k.lower()
        if k_lower in team_lower or team_lower in k_lower:
            count = len(v)
            total_value = sum(p.get('market_value', 0) for p in v)
            impact = min(1.0, total_value / 50_000_000) if total_value > 0 else min(0.5, count * 0.05)
            return count, impact
        # Partial match on significant words
        k_parts = [p for p in k_lower.split() if len(p) > 3 and p not in ('united', 'city', 'club')]
        if any(p in team_lower for p in k_parts):
            count = len(v)
            total_value = sum(p.get('market_value', 0) for p in v)
            impact = min(1.0, total_value / 50_000_000) if total_value > 0 else min(0.5, count * 0.05)
            return count, impact
    return 0, 0.0
