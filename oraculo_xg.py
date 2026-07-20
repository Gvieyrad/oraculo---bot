"""FBref xG scraper — caches team xG/xGA for Oraculo football model."""
import os, json, time, re, logging
from datetime import datetime, timedelta
from collections import defaultdict

log = logging.getLogger('oraculo')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
XG_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_data.json')
XG_MAX_AGE = 86400  # 24h cache

# FBref league URLs (season 2025-2026)
FBREF_LEAGUES = {
    'PL':  '/en/comps/9/Premier-League-Stats',
    'PD':  '/en/comps/12/La-Liga-Stats',
    'SA':  '/en/comps/11/Serie-A-Stats',
    'BL1': '/en/comps/20/Bundesliga-Stats',
    'FL1': '/en/comps/13/Ligue-1-Stats',
}

def _fetch_fbref_xg(league_path):
    """Scrape team xG/xGA from FBref league page."""
    import urllib.request
    url = 'https://fbref.com' + league_path
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; OracleBot/1.0)'}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.warning('FBref fetch failed for %s: %s', league_path, e)
        return {}

    teams = {}
    # Parse the "Regular season" or main table
    # Look for rows with team data: xG, xGA, xGD, Matches Played
    # FBref table pattern: <td data-stat="xg">1.5</td>
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        team_match = re.search(r'data-stat="team"[^>]*><a[^>]*>([^<]+)</a>', row)
        if not team_match:
            continue
        team = team_match.group(1).strip()
        xg = re.search(r'data-stat="xg"[^>]*>([d.]+)', row)
        xga = re.search(r'data-stat="xg_against"[^>]*>([d.]+)', row)
        mp = re.search(r'data-stat="games"[^>]*>(d+)', row)
        gf = re.search(r'data-stat="goals_for"[^>]*>(d+)', row)
        ga = re.search(r'data-stat="goals_against"[^>]*>(d+)', row)

        if xg and xga and mp:
            matches = int(mp.group(1)) or 1
            teams[team] = {
                'xg': round(float(xg.group(1)) / matches, 3),
                'xga': round(float(xga.group(1)) / matches, 3),
                'gf': round(int(gf.group(1)) / matches, 3) if gf else None,
                'ga': round(int(ga.group(1)) / matches, 3) if ga else None,
                'mp': matches,
            }
    return teams


def load_xg_data(force_refresh=False):
    """Load cached xG data, refresh if stale."""
    os.makedirs(os.path.dirname(XG_CACHE), exist_ok=True)

    if not force_refresh and os.path.exists(XG_CACHE):
        age = time.time() - os.path.getmtime(XG_CACHE)
        if age < XG_MAX_AGE:
            with open(XG_CACHE) as f:
                data = json.load(f)
            log.info('xG data loaded from cache (%d leagues)', len(data))
            return data

    log.info('Refreshing xG data from FBref...')
    data = {}
    for league, path in FBREF_LEAGUES.items():
        teams = _fetch_fbref_xg(path)
        if teams:
            data[league] = teams
            log.info('  %s: %d teams', league, len(teams))
        time.sleep(3)  # Be nice to FBref

    if data:
        with open(XG_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
        log.info('xG data saved (%d leagues)', len(data))

    return data


def get_team_xg(team_name, league, xg_data):
    """Fuzzy match team name to xG data."""
    if not xg_data or league not in xg_data:
        return None
    teams = xg_data[league]
    # Exact match
    if team_name in teams:
        return teams[team_name]
    # Fuzzy: check if any key contains or is contained
    name_lower = team_name.lower()
    for k, v in teams.items():
        if k.lower() in name_lower or name_lower in k.lower():
            return v
        # Handle common abbreviations
        parts = k.lower().split()
        if any(p in name_lower for p in parts if len(p) > 3):
            return v
    return None


def xg_adjusted_prob(home_xg, away_xg, home_xga, away_xga):
    """Compute win/draw/loss probabilities using xG-adjusted Poisson."""
    from math import exp, factorial
    # Expected goals: blend attack strength with opponent defense weakness
    home_lambda = (home_xg + away_xga) / 2
    away_lambda = (away_xg + home_xga) / 2
    # Home advantage
    home_lambda *= 1.1
    away_lambda *= 0.9

    max_goals = 7
    ph, pd, pa = 0, 0, 0
    p_over25 = 0
    for h in range(max_goals):
        for a in range(max_goals):
            p = (exp(-home_lambda) * home_lambda**h / factorial(h)) *                 (exp(-away_lambda) * away_lambda**a / factorial(a))
            if h > a: ph += p
            elif h == a: pd += p
            else: pa += p
            if h + a > 2:
                p_over25 += p
    return ph, pd, pa, p_over25


MATCH_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_matches.json')

FBREF_RESULTS = {
    'PL': '/en/comps/9/schedule/Premier-League-Scores-and-Fixtures',
    'PD': '/en/comps/12/schedule/La-Liga-Scores-and-Fixtures',
    'SA': '/en/comps/11/schedule/Serie-A-Scores-and-Fixtures',
    'BL1': '/en/comps/20/schedule/Bundesliga-Scores-and-Fixtures',
    'FL1': '/en/comps/13/schedule/Ligue-1-Scores-and-Fixtures',
}


def fetch_match_results(force_refresh=False):
    """Scrape match results from FBref for Dixon-Coles training."""
    if not force_refresh and os.path.exists(MATCH_CACHE):
        age = time.time() - os.path.getmtime(MATCH_CACHE)
        if age < 86400:
            with open(MATCH_CACHE) as f:
                return json.load(f)

    import urllib.request
    all_matches = []
    for league, path in FBREF_RESULTS.items():
        url = 'https://fbref.com' + path
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; OracleBot/1.0)'}
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            html = resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            log.warning('FBref results failed for %s: %s', league, e)
            continue

        # Parse match rows: date, home, score, away
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        for row in rows:
            date_m = re.search(r'data-stat="date"[^>]*>.*?(d{4}-d{2}-d{2})', row)
            home_m = re.search(r'data-stat="home_team"[^>]*>.*?<a[^>]*>([^<]+)</a>', row)
            away_m = re.search(r'data-stat="away_team"[^>]*>.*?<a[^>]*>([^<]+)</a>', row)
            score_m = re.search(r'data-stat="score"[^>]*>.*?(d+)[^d]+(d+)', row)

            if date_m and home_m and away_m and score_m:
                all_matches.append({
                    'date': date_m.group(1),
                    'home': home_m.group(1).strip(),
                    'away': away_m.group(1).strip(),
                    'home_goals': int(score_m.group(1)),
                    'away_goals': int(score_m.group(2)),
                    'league': league,
                })
        time.sleep(3)
        log.info('FBref results %s: %d matches parsed', league, 
                 sum(1 for m in all_matches if m.get('league') == league))

    if all_matches:
        os.makedirs(os.path.dirname(MATCH_CACHE), exist_ok=True)
        with open(MATCH_CACHE, 'w') as f:
            json.dump(all_matches, f)
        log.info('FBref: %d total match results cached', len(all_matches))

    return all_matches


# --- FALLBACK: Understat JSON API (no anti-bot) ---
UNDERSTAT_LEAGUES = {
    'PL': 'EPL',
    'PD': 'La liga',
    'SA': 'Serie A',
    'BL1': 'Bundesliga',
    'FL1': 'Ligue 1',
}

def _fetch_understat_xg(league_name, season='2025'):
    """Fetch team xG from Understat's getLeagueData JSON API (no anti-bot).
    2026-07-16: Understat moved from embedded JSON in HTML (var teamsData=...)
    to an async getLeagueData/{league}/{season} endpoint -- old regex always
    found 0 teams silently. New endpoint needs XHR-style headers or 404s, and
    always gzips the response regardless of Accept-Encoding."""
    import urllib.request, urllib.parse, gzip
    league_enc = urllib.parse.quote(league_name)
    url = f'https://understat.com/getLeagueData/{league_enc}/{season}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'https://understat.com/league/{league_enc}/{season}',
        'Accept': 'application/json',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        raw = resp.read()
        if resp.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode('utf-8', errors='replace'))
        teams_raw = data.get('teams', {})
        teams = {}
        matches = []
        for tid, tinfo in teams_raw.items():
            name = tinfo.get('title', '')
            history = tinfo.get('history', [])
            if not history:
                continue
            total_xg = sum(float(h.get('xG', 0)) for h in history)
            total_xga = sum(float(h.get('xGA', 0)) for h in history)
            mp = len(history)
            if mp > 0:
                teams[name] = {
                    'xg': round(total_xg / mp, 3),
                    'xga': round(total_xga / mp, 3),
                    'mp': mp,
                }
            # Extract match results for Dixon-Coles
            for h in history:
                matches.append({
                    'date': h.get('date', ''),
                    'home': name if h.get('h_a') == 'h' else '',
                    'away': name if h.get('h_a') == 'a' else '',
                    'home_goals': int(h.get('scored', 0)) if h.get('h_a') == 'h' else int(h.get('missed', 0)),
                    'away_goals': int(h.get('missed', 0)) if h.get('h_a') == 'h' else int(h.get('scored', 0)),
                })
        return teams, matches
    except Exception as e:
        log.warning('Understat fetch failed for %s: %s', league_name, e)
        return {}, []


def load_xg_data_v2(force_refresh=False):
    """Load xG data with Understat fallback."""
    os.makedirs(os.path.dirname(XG_CACHE), exist_ok=True)

    if not force_refresh and os.path.exists(XG_CACHE):
        age = time.time() - os.path.getmtime(XG_CACHE)
        if age < XG_MAX_AGE:
            with open(XG_CACHE) as f:
                data = json.load(f)
            if data:
                log.info('xG data loaded from cache (%d leagues)', len(data))
                return data

    # Try FBref first
    data = load_xg_data(force_refresh=True)
    if data and len(data) >= 3:
        return data

    # Fallback to Understat
    log.info('FBref blocked, using Understat fallback...')
    data = {}
    all_matches = []
    for league, us_name in UNDERSTAT_LEAGUES.items():
        teams, matches = _fetch_understat_xg(us_name)
        if teams:
            data[league] = teams
            all_matches.extend(matches)
            log.info('  Understat %s: %d teams', league, len(teams))
        time.sleep(2)

    if data:
        with open(XG_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
        # Save match results for Dixon-Coles
        if all_matches:
            # Deduplicate matches (each match appears twice, once per team)
            seen = set()
            unique = []
            for m in all_matches:
                key = m['date'] + m.get('home','') + m.get('away','')
                if key not in seen and m.get('home') and m.get('away'):
                    seen.add(key)
                    unique.append(m)
            match_cache = os.path.join(os.path.dirname(XG_CACHE), 'xg_matches.json')
            with open(match_cache, 'w') as f:
                json.dump(unique, f)
            log.info('Understat: %d match results cached for Dixon-Coles', len(unique))

    return data


# --- football-data.co.uk: Free CSVs, no anti-bot ---
FOOTBALLDATA_LEAGUES = {
    'PL': 'https://www.football-data.co.uk/mmz4281/2526/E0.csv',
    'PD': 'https://www.football-data.co.uk/mmz4281/2526/SP1.csv',
    'SA': 'https://www.football-data.co.uk/mmz4281/2526/I1.csv',
    'BL1': 'https://www.football-data.co.uk/mmz4281/2526/D1.csv',
    'FL1': 'https://www.football-data.co.uk/mmz4281/2526/F1.csv',
    'DED': 'https://www.football-data.co.uk/mmz4281/2526/N1.csv',
    'PPL': 'https://www.football-data.co.uk/mmz4281/2526/P1.csv',
    'TUR': 'https://www.football-data.co.uk/mmz4281/2526/T1.csv',
    # MLS: /new/ multi-season format — columns Home/HG not HomeTeam/FTHG
    'MLS': 'https://www.football-data.co.uk/new/USA.csv',
}

# Leagues using /new/ multi-season format; parser handles Home/HG columns + Season filter
_FOOTBALLDATA_NEW_FORMAT = {'MLS'}
_FOOTBALLDATA_MIN_SEASON = 2022  # MLS goal rate shifted upward post-2022

def fetch_footballdata_results(force_refresh=False):
    """Fetch match results from football-data.co.uk for Dixon-Coles training."""
    match_cache = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_matches.json')
    if not force_refresh and os.path.exists(match_cache):
        age = time.time() - os.path.getmtime(match_cache)
        if age < 86400:
            with open(match_cache) as f:
                return json.load(f)

    import urllib.request, csv, io
    all_matches = []
    for league, url in FOOTBALLDATA_LEAGUES.items():
        new_fmt = league in _FOOTBALLDATA_NEW_FORMAT
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            text = resp.read().decode('utf-8', errors='replace')
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                if new_fmt:
                    # /new/ format: Home/Away/HG/AG + Season column
                    season = row.get('Season', '')
                    try:
                        if season and int(season) < _FOOTBALLDATA_MIN_SEASON:
                            continue
                    except ValueError:
                        pass
                    home = row.get('Home', '')
                    away = row.get('Away', '')
                    hg = row.get('HG', '')
                    ag = row.get('AG', '')
                else:
                    home = row.get('HomeTeam', '')
                    away = row.get('AwayTeam', '')
                    hg = row.get('FTHG', '')
                    ag = row.get('FTAG', '')
                date = row.get('Date', '')
                if home and away and hg and ag:
                    try:
                        all_matches.append({
                            'date': date, 'home': home, 'away': away,
                            'home_goals': int(hg), 'away_goals': int(ag),
                            'league': league,
                        })
                    except ValueError:
                        pass
        except Exception as e:
            log.warning('football-data.co.uk %s failed: %s', league, e)
        time.sleep(1)

    if all_matches:
        os.makedirs(os.path.dirname(match_cache), exist_ok=True)
        with open(match_cache, 'w') as f:
            json.dump(all_matches, f)
        log.info('football-data.co.uk: %d match results cached', len(all_matches))

    return all_matches


def build_xg_from_results(matches):
    """Build pseudo-xG data from actual goal data (fallback when real xG unavailable)."""
    from collections import defaultdict
    team_stats = defaultdict(lambda: {'goals_for': 0, 'goals_against': 0, 'matches': 0, 'league': ''})

    for m in matches:
        h, a = m['home'], m['away']
        hg, ag = m['home_goals'], m['away_goals']
        league = m.get('league', '')

        team_stats[h]['goals_for'] += hg
        team_stats[h]['goals_against'] += ag
        team_stats[h]['matches'] += 1
        team_stats[h]['league'] = league

        team_stats[a]['goals_for'] += ag
        team_stats[a]['goals_against'] += hg
        team_stats[a]['matches'] += 1
        team_stats[a]['league'] = league

    # Build per-league xG proxy
    result = {}
    for team, stats in team_stats.items():
        mp = stats['matches']
        if mp < 5:
            continue
        league = stats['league']
        if league not in result:
            result[league] = {}
        result[league][team] = {
            'xg': round(stats['goals_for'] / mp, 3),
            'xga': round(stats['goals_against'] / mp, 3),
            'mp': mp,
        }

    return result


def load_xg_via_proxy():
    try:
        from oraculo_proxy import proxied_get, is_configured
        if not is_configured():
            return None
    except ImportError:
        return None
    log.info('Fetching FBref xG via proxy (shooting tables)...')

    # FBref xG is on /shooting/ subpage
    FBREF_SHOOTING = {
        'PL':  '/en/comps/9/shooting/Premier-League-Stats',
        'PD':  '/en/comps/12/shooting/La-Liga-Stats',
        'SA':  '/en/comps/11/shooting/Serie-A-Stats',
        'BL1': '/en/comps/20/shooting/Bundesliga-Stats',
        'FL1': '/en/comps/13/shooting/Ligue-1-Stats',
        'DED': '/en/comps/23/shooting/Eredivisie-Stats',
        'PPL': '/en/comps/32/shooting/Primeira-Liga-Stats',
        'MLS': '/en/comps/22/shooting/Major-League-Soccer-Stats',
    }

    data = {}
    for league, path in FBREF_SHOOTING.items():
        url = 'https://fbref.com' + path
        html = proxied_get(url, timeout=30)
        if not html or len(html) < 5000:
            log.warning('FBref %s shooting page failed', league)
            continue

        teams = {}
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        for row in rows:
            # Squad name
            team_m = re.search(r'data-stat="squad"[^>]*>.*?<a[^>]*>([^<]+)</a>', row)
            if not team_m:
                continue
            team = team_m.group(1).strip()

            # xG for (npxg or xg)
            xg_m  = re.search(r'data-stat="(?:npxg|xg)"[^>]*>([\d.]+)', row)
            # xG against (npxg_allowed or xg_against)  
            xga_m = re.search(r'data-stat="(?:npxg_allowed|xg_against|npxg_against)"[^>]*>([\d.]+)', row)
            # Matches played
            mp_m  = re.search(r'data-stat="(?:games|mp)"[^>]*>(\d+)', row)

            if xg_m and mp_m:
                mp = max(1, int(mp_m.group(1)))
                teams[team] = {
                    'xg':  round(float(xg_m.group(1)) / mp, 3),
                    'xga': round(float(xga_m.group(1)) / mp, 3) if xga_m else 0.0,
                    'mp':  mp,
                }

        if teams:
            data[league] = teams
            log.info('  FBref %s: %d teams via proxy (xG real)', league, len(teams))
        else:
            log.warning('  FBref %s: no teams parsed', league)
        time.sleep(3)

    if data:
        with open(XG_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
        log.info('FBref xG proxy: %d leagues, saved to cache', len(data))

    return data if data else None
