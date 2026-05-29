#!/usr/bin/env python3
"""
oraculo_football_csv.py - Football-Data.co.uk CSV Loader

Downloads and parses historical match data with detailed stats:
corners, cards, shots, shots on target, fouls.

Source: https://www.football-data.co.uk
Free, unlimited historical data.
"""

import os
import csv
import json
import time
import logging
from datetime import datetime
from io import StringIO

log = logging.getLogger('oraculo.csv')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

# League codes mapping (football-data.org code -> football-data.co.uk code)
LEAGUE_MAP = {
    'PL':  'E0',    # Premier League
    'ELC': 'E1',    # Championship
    'PD':  'SP1',   # La Liga
    'SA':  'I1',    # Serie A
    'BL1': 'D1',    # Bundesliga
    'FL1': 'F1',    # Ligue 1
    'DED': 'N1',    # Eredivisie
    'PPL': 'P1',    # Primeira Liga
    'TUR': 'T1',    # Turkey Super Lig
    'BEL': 'B1',    # Belgium Jupiler Pro
    'SWE': 'SE1',   # Sweden Allsvenskan (not available in winter)
    'NOR': 'NO1',   # Norway Eliteserien (not available in winter)
    'SWZ': 'SZ1',   # Switzerland Super League (not available in winter)
    'BL2': 'D2',    # Germany 2.Bundesliga
    'SB':  'I2',    # Italy Serie B
    'FL2': 'F2',    # France Ligue 2
}

# Team name mapping: football-data.co.uk -> football-data.org
_TEAM_MAP = {
    # Premier League
    'Man United': 'Manchester United FC',
    'Man City': 'Manchester City FC',
    'Tottenham': 'Tottenham Hotspur FC',
    'Newcastle': 'Newcastle United FC',
    'West Ham': 'West Ham United FC',
    'Wolves': 'Wolverhampton Wanderers FC',
    'Nott\'m Forest': 'Nottingham Forest FC',
    'Nottingham Forest': 'Nottingham Forest FC',
    'Brighton': 'Brighton & Hove Albion FC',
    'Crystal Palace': 'Crystal Palace FC',
    'Leicester': 'Leicester City FC',
    'Ipswich': 'Ipswich Town FC',
    'Southampton': 'Southampton FC',
    'Bournemouth': 'AFC Bournemouth',
    'Liverpool': 'Liverpool FC',
    'Arsenal': 'Arsenal FC',
    'Chelsea': 'Chelsea FC',
    'Everton': 'Everton FC',
    'Fulham': 'Fulham FC',
    'Aston Villa': 'Aston Villa FC',
    'Brentford': 'Brentford FC',
    # La Liga
    'Ath Madrid': 'Club Atletico de Madrid',
    'Ath Bilbao': 'Athletic Club',
    'Betis': 'Real Betis Balompie',
    'Celta': 'RC Celta de Vigo',
    'Sociedad': 'Real Sociedad de Futbol',
    'Vallecano': 'Rayo Vallecano de Madrid',
    'La Coruna': 'RC Deportivo La Coruna',
    'Espanol': 'RCD Espanyol de Barcelona',
    'Las Palmas': 'UD Las Palmas',
    'Leganes': 'CD Leganes',
    'Mallorca': 'RCD Mallorca',
    'Osasuna': 'CA Osasuna',
    'Valladolid': 'Real Valladolid CF',
    'Villarreal': 'Villarreal CF',
    'Sevilla': 'Sevilla FC',
    'Valencia': 'Valencia CF',
    'Getafe': 'Getafe CF',
    'Girona': 'Girona FC',
    'Alaves': 'Deportivo Alaves',
    'Barcelona': 'FC Barcelona',
    'Real Madrid': 'Real Madrid CF',
    # Serie A
    'Inter': 'FC Internazionale Milano',
    'AC Milan': 'AC Milan',
    'Juventus': 'Juventus FC',
    'Napoli': 'SSC Napoli',
    'Roma': 'AS Roma',
    'Lazio': 'SS Lazio',
    'Atalanta': 'Atalanta BC',
    'Fiorentina': 'ACF Fiorentina',
    'Bologna': 'Bologna FC 1909',
    'Torino': 'Torino FC',
    'Udinese': 'Udinese Calcio',
    'Genoa': 'Genoa CFC',
    'Cagliari': 'Cagliari Calcio',
    'Empoli': 'Empoli FC',
    'Parma': 'Parma Calcio 1913',
    'Verona': 'Hellas Verona FC',
    'Como': 'Como 1907',
    'Lecce': 'US Lecce',
    'Monza': 'AC Monza',
    'Venezia': 'Venezia FC',
    # Bundesliga
    'Bayern Munich': 'FC Bayern Munchen',
    'Dortmund': 'Borussia Dortmund',
    'Leverkusen': 'Bayer 04 Leverkusen',
    'Leipzig': 'RB Leipzig',
    'Frankfurt': 'Eintracht Frankfurt',
    "M'gladbach": 'Borussia Monchengladbach',
    'Wolfsburg': 'VfL Wolfsburg',
    'Freiburg': 'Sport-Club Freiburg',
    'Hoffenheim': 'TSG 1899 Hoffenheim',
    'Mainz': '1. FSV Mainz 05',
    'Augsburg': 'FC Augsburg',
    'Stuttgart': 'VfB Stuttgart',
    'Union Berlin': '1. FC Union Berlin',
    'Werder Bremen': 'SV Werder Bremen',
    'Bochum': 'VfL Bochum 1848',
    'Heidenheim': '1. FC Heidenheim 1846',
    'St Pauli': 'FC St. Pauli 1910',
    'Holstein Kiel': 'Holstein Kiel',
    # Ligue 1
    'Paris SG': 'Paris Saint-Germain FC',
    'Marseille': 'Olympique de Marseille',
    'Lyon': 'Olympique Lyonnais',
    'Monaco': 'AS Monaco FC',
    'Lille': 'Lille OSC',
    'Nice': 'OGC Nice',
    'Lens': 'RC Lens',
    'Rennes': 'Stade Rennais FC 1901',
    'Strasbourg': 'RC Strasbourg Alsace',
    'Toulouse': 'Toulouse FC',
    'Nantes': 'FC Nantes',
    'Brest': 'Stade Brestois 29',
    'Reims': 'Stade de Reims',
    'Montpellier': 'Montpellier HSC',
    'Angers': 'Angers SCO',
    'St Etienne': 'AS Saint-Etienne',
    'Le Havre': 'Le Havre AC',
    'Auxerre': 'AJ Auxerre',
}

# Reverse map for lookup
_TEAM_MAP_REV = {v: k for k, v in _TEAM_MAP.items()}


def _ensure_cache():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    return CACHE_DIR


def _season_code(year_start=2025):
    """Convert year to season code: 2025 -> '2526'."""
    return '%02d%02d' % (year_start % 100, (year_start + 1) % 100)


def download_league_csv(league_code_org, season_start=2025, force=False):
    """
    Download CSV from football-data.co.uk.

    Args:
        league_code_org: football-data.org code (PL, PD, SA, BL1, FL1)
        season_start: season start year (2025 for 2025-26)
        force: force re-download even if cached

    Returns:
        list of match dicts with stats, or empty list on failure
    """
    csv_code = LEAGUE_MAP.get(league_code_org)
    if not csv_code:
        log.warning('Unknown league code: %s', league_code_org)
        return []

    season = _season_code(season_start)
    cache_file = os.path.join(_ensure_cache(), f'{csv_code}_{season}.json')

    # Check cache (24h TTL)
    if not force and os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 86400:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass

    url = f'https://www.football-data.co.uk/mmz4281/{season}/{csv_code}.csv'
    log.info('Downloading %s', url)

    try:
        from urllib.request import Request, urlopen
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 Oraculo/1.0')
        resp = urlopen(req, timeout=30)
        raw = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.error('Failed to download %s: %s', url, e)
        return []

    matches = _parse_csv(raw, league_code_org)
    log.info('Parsed %d matches from %s %s', len(matches), league_code_org, season)

    # Cache
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(matches, f, ensure_ascii=False)
    except Exception as e:
        log.warning('Cache write failed: %s', e)

    return matches


def _parse_csv(raw_text, league_code):
    """Parse football-data.co.uk CSV text into match dicts."""
    matches = []
    reader = csv.DictReader(StringIO(raw_text))

    for row in reader:
        try:
            home = row.get('HomeTeam', '').strip()
            away = row.get('AwayTeam', '').strip()
            if not home or not away:
                continue

            fthg = row.get('FTHG', '')
            ftag = row.get('FTAG', '')
            if not fthg or not ftag:
                continue

            m = {
                'home_team_csv': home,
                'away_team_csv': away,
                'home_team': _TEAM_MAP.get(home, home),
                'away_team': _TEAM_MAP.get(away, away),
                'competition_code': league_code,
                'home_score': int(fthg),
                'away_score': int(ftag),
                'ht_home': _safe_int(row.get('HTHG')),
                'ht_away': _safe_int(row.get('HTAG')),
                'result': row.get('FTR', ''),
                'referee': row.get('Referee', ''),
                # Stats
                'home_shots': _safe_int(row.get('HS')),
                'away_shots': _safe_int(row.get('AS')),
                'home_shots_target': _safe_int(row.get('HST')),
                'away_shots_target': _safe_int(row.get('AST')),
                'home_corners': _safe_int(row.get('HC')),
                'away_corners': _safe_int(row.get('AC')),
                'home_yellow': _safe_int(row.get('HY')),
                'away_yellow': _safe_int(row.get('AY')),
                'home_red': _safe_int(row.get('HR')),
                'away_red': _safe_int(row.get('AR')),
                'home_fouls': _safe_int(row.get('HF')),
                'away_fouls': _safe_int(row.get('AF')),
            }

            # Parse date
            date_str = row.get('Date', '')
            time_str = row.get('Time', '15:00')
            m['utc_date'] = _parse_date(date_str, time_str)

            matches.append(m)
        except Exception as e:
            log.debug('Skip row: %s', e)

    return matches


def _safe_int(val):
    """Convert to int, return None if not possible."""
    if val is None or val == '':
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _parse_date(date_str, time_str='15:00'):
    """Parse DD/MM/YYYY or DD/MM/YY to ISO format."""
    for fmt in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime('%Y-%m-%dT') + (time_str or '15:00') + ':00Z'
        except ValueError:
            continue
    return ''


def load_all_leagues(leagues=None, season=2025):
    """
    Load stats for all leagues.

    Args:
        leagues: list of league codes (default: top 5)
        season: season start year

    Returns:
        list of all match dicts with stats
    """
    if leagues is None:
        leagues = ['PL', 'PD', 'SA', 'BL1', 'FL1']

    all_matches = []
    for code in leagues:
        matches = download_league_csv(code, season)
        all_matches.extend(matches)
        if matches:
            log.info('%s: %d matches loaded', code, len(matches))

    all_matches.sort(key=lambda m: m.get('utc_date', ''))
    return all_matches


def compute_team_stats(matches, team_name, n=10):
    """
    Compute rolling stats for a team from CSV data.

    Returns dict with averages for corners, cards, shots, shots_on_target.
    """
    team_matches = []
    for m in matches:
        if m['home_team'] == team_name or m['away_team'] == team_name:
            team_matches.append(m)

    recent = team_matches[-n:] if len(team_matches) >= n else team_matches
    if not recent:
        return {
            'corners_avg': 5.0, 'corners_conceded_avg': 5.0,
            'yellow_avg': 1.5, 'red_avg': 0.1,
            'shots_avg': 12.0, 'shots_target_avg': 4.0,
            'shots_conceded_avg': 12.0, 'shots_target_conceded_avg': 4.0,
            'fouls_avg': 12.0,
        }

    corners = []
    corners_c = []
    yellows = []
    reds = []
    shots = []
    shots_t = []
    shots_c = []
    shots_tc = []
    fouls = []

    for m in recent:
        if m['home_team'] == team_name:
            if m['home_corners'] is not None:
                corners.append(m['home_corners'])
                corners_c.append(m['away_corners'] or 0)
            if m['home_yellow'] is not None:
                yellows.append(m['home_yellow'])
            if m['home_red'] is not None:
                reds.append(m['home_red'])
            if m['home_shots'] is not None:
                shots.append(m['home_shots'])
                shots_c.append(m['away_shots'] or 0)
            if m['home_shots_target'] is not None:
                shots_t.append(m['home_shots_target'])
                shots_tc.append(m['away_shots_target'] or 0)
            if m['home_fouls'] is not None:
                fouls.append(m['home_fouls'])
        elif m['away_team'] == team_name:
            if m['away_corners'] is not None:
                corners.append(m['away_corners'])
                corners_c.append(m['home_corners'] or 0)
            if m['away_yellow'] is not None:
                yellows.append(m['away_yellow'])
            if m['away_red'] is not None:
                reds.append(m['away_red'])
            if m['away_shots'] is not None:
                shots.append(m['away_shots'])
                shots_c.append(m['home_shots'] or 0)
            if m['away_shots_target'] is not None:
                shots_t.append(m['away_shots_target'])
                shots_tc.append(m['home_shots_target'] or 0)
            if m['away_fouls'] is not None:
                fouls.append(m['away_fouls'])

    def avg(lst, default=0.0):
        return sum(lst) / len(lst) if lst else default

    return {
        'corners_avg': avg(corners, 5.0),
        'corners_conceded_avg': avg(corners_c, 5.0),
        'yellow_avg': avg(yellows, 1.5),
        'red_avg': avg(reds, 0.1),
        'shots_avg': avg(shots, 12.0),
        'shots_target_avg': avg(shots_t, 4.0),
        'shots_conceded_avg': avg(shots_c, 12.0),
        'shots_target_conceded_avg': avg(shots_tc, 4.0),
        'fouls_avg': avg(fouls, 12.0),
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    matches = load_all_leagues()
    print(f'\nTotal: {len(matches)} matches')

    if matches:
        # Show sample
        m = matches[-1]
        print(f'\nLast match: {m["home_team"]} {m["home_score"]}-{m["away_score"]} {m["away_team"]}')
        print(f'  Corners: {m["home_corners"]}-{m["away_corners"]}')
        print(f'  Yellow:  {m["home_yellow"]}-{m["away_yellow"]}')
        print(f'  Shots:   {m["home_shots"]}-{m["away_shots"]}')
        print(f'  SoT:     {m["home_shots_target"]}-{m["away_shots_target"]}')

        # Team stats example
        stats = compute_team_stats(matches, matches[-1]['home_team'])
        print(f'\n{matches[-1]["home_team"]} averages:')
        for k, v in stats.items():
            print(f'  {k}: {v:.1f}')

# ── New leagues (football-data.co.uk/new/) ────────────────────────────────────
NEW_LEAGUE_CODES = {
    'ARG': 'Argentina Liga Profesional',
    'BRA': 'Brasil Serie A',
    'RUS': 'Russia Premier League',
    'JPN': 'Japan J1 League',
}

_NEW_TEAM_MAP = {
    # Argentina
    'River Plate': 'River Plate',
    'Boca Juniors': 'Boca Juniors',
    'Racing Club': 'Racing Club',
    'Independiente': 'Independiente',
    'San Lorenzo': 'San Lorenzo',
    'Huracan': 'Huracan',
    'Lanus': 'Lanus',
    'Belgrano': 'Belgrano',
    'Talleres': 'Talleres',
    'Estudiantes': 'Estudiantes',
    'Defensa y Justicia': 'Defensa y Justicia',
    'Atletico Tucuman': 'Atletico Tucuman',
    'Tigre': 'Tigre',
    'Godoy Cruz': 'Godoy Cruz',
    "Newell's Old Boys": "Newell's",
    'Rosario Central': 'Rosario Central',
    'Arsenal de Sarandi': 'Arsenal Sarandi',
    'Banfield': 'Banfield',
    'Colon': 'Colon',
    'Gimnasia LP': 'Gimnasia LP',
    'Platense': 'Platense',
    'Velez Sarsfield': 'Velez',
    'Sarmiento': 'Sarmiento',
    'Union': 'Union Santa Fe',
    'Argentinos Juniors': 'Argentinos',
    'Central Cordoba': 'Central Cordoba',
    'Barracas Central': 'Barracas',
    'Instituto': 'Instituto',
    'Riestra': 'Riestra',
    # Brasil
    'Flamengo': 'Flamengo',
    'Fluminense': 'Fluminense',
    'Palmeiras': 'Palmeiras',
    'Atletico Mineiro': 'Atletico Mineiro',
    'Sao Paulo': 'Sao Paulo',
    'Corinthians': 'Corinthians',
    'Gremio': 'Gremio',
    'Internacional': 'Internacional',
    'Santos': 'Santos',
    'Vasco da Gama': 'Vasco',
    'Atletico PR': 'Athletico Paranaense',
    'Athletico Paranaense': 'Athletico Paranaense',
    'Botafogo': 'Botafogo',
    'Bahia': 'Bahia',
    'Fortaleza': 'Fortaleza',
    'Bragantino': 'Bragantino',
    'Cruzeiro': 'Cruzeiro',
    'Vitoria': 'Vitoria',
    'Atletico Goianiense': 'Atletico GO',
    'Cuiaba': 'Cuiaba',
    'Ceara': 'Ceara',
    'Juventude': 'Juventude',
    'Mirassol': 'Mirassol',
    'Coritiba': 'Coritiba',
    'America MG': 'America MG',
    'Criciuma': 'Criciuma',
    'Sport Recife': 'Sport Recife',
    # Rusia
    'Spartak Moscow': 'Spartak Moscow',
    'CSKA Moscow': 'CSKA Moscow',
    'Lokomotiv Moscow': 'Lokomotiv',
    'Zenit': 'Zenit',
    'Krasnodar': 'Krasnodar',
    'Dynamo Moscow': 'Dynamo Moscow',
    'Rostov': 'Rostov',
    'Rubin Kazan': 'Rubin Kazan',
    'Akhmat Grozny': 'Akhmat',
    'CSKA Moskva': 'CSKA Moscow',
    'Spartak Moskva': 'Spartak Moscow',
    'Lokomotiv Moskva': 'Lokomotiv',
    'FK Krasnodar': 'Krasnodar',
    'Dynamo Moskva': 'Dynamo Moscow',
    'Torpedo Moscow': 'Torpedo Moscow',
    'Ural': 'Ural',
    'Sochi': 'Sochi',
    'Khimki': 'Khimki',
    'Nizhny Novgorod': 'Nizhny Novgorod',
    'Krylya Sovetov': 'Krylya Sovetov',
    'FK Ufa': 'Ufa',
    'Arsenal Tula': 'Arsenal Tula',
    # Japan J1
    'Cerezo Osaka': 'Cerezo Osaka',
    'Gamba Osaka': 'Gamba Osaka',
    'Vissel Kobe': 'Vissel Kobe',
    'Urawa Reds': 'Urawa Reds',
    'Kashima Antlers': 'Kashima',
    'Kawasaki Frontale': 'Kawasaki',
    'Yokohama F Marinos': 'Yokohama FM',
    'Yokohama Marinos': 'Yokohama FM',
    'Nagoya Grampus': 'Nagoya',
    'Sanfrecce Hiroshima': 'Hiroshima',
    'FC Tokyo': 'FC Tokyo',
    'Consadole Sapporo': 'Sapporo',
    'Shonan Bellmare': 'Shonan',
    'Sagan Tosu': 'Sagan Tosu',
    'Vegalta Sendai': 'Sendai',
    'Jubilo Iwata': 'Jubilo',
    'Avispa Fukuoka': 'Fukuoka',
    'Albirex Niigata': 'Niigata',
    'Kashiwa Reysol': 'Kashiwa',
    'Shimizu S-Pulse': 'Shimizu',
    'Kyoto Sanga': 'Kyoto',
    'Tokyo Verdy': 'Tokyo Verdy',
    'Machida Zelvia': 'Machida',
}


def download_new_league_csv(code, force=False):
    if code not in NEW_LEAGUE_CODES:
        log.warning('Unknown new league code: %s', code)
        return []

    cache_file = os.path.join(_ensure_cache(), f'new_{code}.json')

    if not force and os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 43200:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass

    url = f'https://www.football-data.co.uk/new/{code}.csv'
    log.info('Downloading new league %s from %s', code, url)

    try:
        from urllib.request import Request, urlopen
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 Oraculo/1.0')
        resp = urlopen(req, timeout=30)
        raw = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.error('Failed to download %s: %s', url, e)
        return []

    matches = _parse_new_league_csv(raw, code)
    log.info('Parsed %d matches from %s', len(matches), code)

    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(matches, f, ensure_ascii=False)
    except Exception as e:
        log.warning('Cache write failed: %s', e)

    return matches


def _parse_new_league_csv(raw_text, league_code):
    # new/ CSVs use: Home, Away, HG, AG  (not HomeTeam/FTHG/FTAG)
    matches = []
    reader = csv.DictReader(StringIO(raw_text))
    for row in reader:
        try:
            home_raw = row.get('Home', row.get('HomeTeam', '')).strip()
            away_raw = row.get('Away', row.get('AwayTeam', '')).strip()
            if not home_raw or not away_raw:
                continue
            hg = row.get('HG', row.get('FTHG', '')).strip()
            ag = row.get('AG', row.get('FTAG', '')).strip()
            if not hg or not ag:
                continue
            home = _NEW_TEAM_MAP.get(home_raw, home_raw)
            away = _NEW_TEAM_MAP.get(away_raw, away_raw)
            date_iso = _parse_date(row.get('Date', '').strip())
            matches.append({
                'home':       home,
                'away':       away,
                'home_goals': int(hg),
                'away_goals': int(ag),
                'date':       date_iso,
                'league':     league_code,
            })
        except Exception as e:
            log.debug('Skip row: %s', e)
    return matches
