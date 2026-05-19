#!/usr/bin/env python3
"""
oraculo_xg_weather.py - xG (Understat) + Weather (Open-Meteo) data loader.

Fetches:
- xG per match from Understat (6 top leagues)
- Weather at match time from Open-Meteo (free, no key)

Caches all data locally in .oraculo_cache/xg/ and .oraculo_cache/weather/
"""

import os
import json
import time
import logging
from datetime import datetime

log = logging.getLogger('oraculo.xg_weather')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Understat league names
UNDERSTAT_LEAGUES = {
    'PL': 'EPL',
    'PD': 'La_Liga',
    'SA': 'Serie_A',
    'BL1': 'Bundesliga',
    'FL1': 'Ligue_1',
}

# Stadium coordinates (lat, lon) for weather lookups
STADIUM_COORDS = {
    # Premier League
    'Arsenal FC': (51.555, -0.108),
    'Aston Villa FC': (52.509, -1.885),
    'AFC Bournemouth': (50.735, -1.838),
    'Brentford FC': (51.491, -0.289),
    'Brighton & Hove Albion FC': (50.862, -0.084),
    'Chelsea FC': (51.482, -0.191),
    'Crystal Palace FC': (51.398, -0.086),
    'Everton FC': (53.439, -2.966),
    'Fulham FC': (51.475, -0.222),
    'Ipswich Town FC': (52.055, 1.145),
    'Leicester City FC': (52.620, -1.142),
    'Liverpool FC': (53.431, -2.961),
    'Manchester City FC': (53.483, -2.200),
    'Manchester United FC': (53.463, -2.291),
    'Newcastle United FC': (54.976, -1.622),
    'Nottingham Forest FC': (52.940, -1.133),
    'Southampton FC': (50.906, -1.391),
    'Tottenham Hotspur FC': (51.604, -0.066),
    'West Ham United FC': (51.539, -0.016),
    'Wolverhampton Wanderers FC': (52.590, -2.130),
    # La Liga
    'Real Madrid CF': (40.453, -3.688),
    'FC Barcelona': (41.381, 2.123),
    'Club Atletico de Madrid': (40.436, -3.600),
    'Sevilla FC': (37.384, -5.971),
    'Real Betis Balompie': (37.357, -5.982),
    'Real Sociedad de Futbol': (43.301, -1.974),
    'Villarreal CF': (39.944, -0.104),
    'Athletic Club': (43.264, -2.949),
    # Serie A
    'Juventus FC': (45.110, 7.641),
    'AC Milan': (45.478, 9.124),
    'FC Internazionale Milano': (45.478, 9.124),
    'SSC Napoli': (40.828, 14.193),
    'AS Roma': (41.934, 12.455),
    'SS Lazio': (41.934, 12.455),
    'Atalanta BC': (45.709, 9.681),
    'ACF Fiorentina': (43.781, 11.282),
    'Bologna FC 1909': (44.492, 11.310),
    'Genoa CFC': (44.416, 8.953),
    # Bundesliga
    'FC Bayern Munchen': (48.219, 11.625),
    'Borussia Dortmund': (51.493, 7.452),
    'Bayer 04 Leverkusen': (51.038, 7.002),
    'RB Leipzig': (51.346, 12.348),
    'Eintracht Frankfurt': (50.069, 8.645),
    # Ligue 1
    'Paris Saint-Germain FC': (48.842, 2.253),
    'Olympique de Marseille': (43.270, 5.396),
    'Olympique Lyonnais': (45.765, 4.982),
    'AS Monaco FC': (43.728, 7.415),
    'Lille OSC': (50.612, 3.130),
}

# Default coords (London) if stadium not found
DEFAULT_COORDS = (51.5, -0.1)


# ---------------------------------------------------------------------------
# xG from Understat
# ---------------------------------------------------------------------------

def load_xg_data(leagues=None, seasons=None):
    """
    Load xG data from Understat for multiple leagues and seasons.

    Returns:
        list of dicts: [{home_team, away_team, xg_home, xg_away,
                         goals_home, goals_away, datetime, forecast, ...}]
    """
    if leagues is None:
        leagues = ['PL', 'PD', 'SA', 'BL1', 'FL1']
    if seasons is None:
        seasons = ['2023', '2024']  # 2023/24 and 2024/25

    cache_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg')
    os.makedirs(cache_dir, exist_ok=True)

    all_data = []

    for org_code in leagues:
        us_code = UNDERSTAT_LEAGUES.get(org_code)
        if not us_code:
            continue

        for season in seasons:
            cache_file = os.path.join(cache_dir, f'{us_code}_{season}.json')

            # Check cache (24h TTL)
            if os.path.exists(cache_file):
                age = time.time() - os.path.getmtime(cache_file)
                if age < 86400:
                    try:
                        with open(cache_file, 'r') as f:
                            cached = json.load(f)
                        all_data.extend(cached)
                        log.info('xG cache hit: %s %s (%d matches)', org_code, season, len(cached))
                        continue
                    except Exception:
                        pass

            # Fetch from Understat
            try:
                from understatapi import UnderstatClient
                with UnderstatClient() as client:
                    matches = client.league(league=us_code).get_match_data(season=season)

                parsed = []
                for m in matches:
                    if not m.get('isResult'):
                        continue
                    entry = {
                        'understat_id': m['id'],
                        'home_team_us': m['h']['title'],
                        'away_team_us': m['a']['title'],
                        'competition_code': org_code,
                        'xg_home': float(m['xG']['h']),
                        'xg_away': float(m['xG']['a']),
                        'goals_home': int(m['goals']['h']),
                        'goals_away': int(m['goals']['a']),
                        'datetime': m['datetime'],
                        'forecast_home': float(m.get('forecast', {}).get('w', 0)),
                        'forecast_draw': float(m.get('forecast', {}).get('d', 0)),
                        'forecast_away': float(m.get('forecast', {}).get('l', 0)),
                    }
                    parsed.append(entry)

                # Cache
                with open(cache_file, 'w') as f:
                    json.dump(parsed, f)
                all_data.extend(parsed)
                log.info('xG fetched: %s %s (%d matches)', org_code, season, len(parsed))
                time.sleep(1)  # Rate limit

            except ImportError:
                log.warning('understatapi not installed: pip install understatapi')
                break
            except Exception as e:
                log.error('Failed to fetch xG for %s %s: %s', org_code, season, e)

    return all_data


def compute_team_xg_stats(xg_data, team_name_us, n=10):
    """
    Compute rolling xG stats for a team.

    Returns dict with xg averages.
    """
    team_matches = [m for m in xg_data
                    if m['home_team_us'] == team_name_us
                    or m['away_team_us'] == team_name_us]

    recent = team_matches[-n:] if len(team_matches) >= n else team_matches
    if not recent:
        return {
            'xg_for_avg': 1.3, 'xg_against_avg': 1.3,
            'xg_diff_avg': 0.0, 'xg_over25_rate': 0.5,
            'overperform_avg': 0.0,
        }

    xg_for = []
    xg_against = []
    goals_for = []
    goals_against = []

    for m in recent:
        if m['home_team_us'] == team_name_us:
            xg_for.append(m['xg_home'])
            xg_against.append(m['xg_away'])
            goals_for.append(m['goals_home'])
            goals_against.append(m['goals_away'])
        else:
            xg_for.append(m['xg_away'])
            xg_against.append(m['xg_home'])
            goals_for.append(m['goals_away'])
            goals_against.append(m['goals_home'])

    n = len(recent)
    avg_xg_for = sum(xg_for) / n
    avg_xg_against = sum(xg_against) / n
    avg_goals_for = sum(goals_for) / n

    over25 = sum(1 for i in range(n) if xg_for[i] + xg_against[i] > 2.5)

    return {
        'xg_for_avg': round(avg_xg_for, 3),
        'xg_against_avg': round(avg_xg_against, 3),
        'xg_diff_avg': round(avg_xg_for - avg_xg_against, 3),
        'xg_over25_rate': round(over25 / n, 3),
        'overperform_avg': round(avg_goals_for - avg_xg_for, 3),
    }


# ---------------------------------------------------------------------------
# Weather from Open-Meteo
# ---------------------------------------------------------------------------

def get_match_weather(home_team, match_date, match_hour=15):
    """
    Get weather for a match using Open-Meteo archive API.

    Args:
        home_team: team name (football-data.org format) for stadium lookup
        match_date: 'YYYY-MM-DD' string
        match_hour: kickoff hour (0-23), default 15

    Returns:
        dict: {temperature, precipitation, wind_speed, humidity}
    """
    cache_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'weather')
    os.makedirs(cache_dir, exist_ok=True)

    cache_key = f'{home_team}_{match_date}_{match_hour}'
    cache_file = os.path.join(cache_dir,
                              cache_key.replace(' ', '_').replace('/', '_') + '.json')

    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass

    lat, lon = STADIUM_COORDS.get(home_team, DEFAULT_COORDS)

    try:
        from urllib.request import Request, urlopen
        url = (f'https://archive-api.open-meteo.com/v1/archive?'
               f'latitude={lat}&longitude={lon}'
               f'&hourly=temperature_2m,precipitation,windspeed_10m,relativehumidity_2m'
               f'&start_date={match_date}&end_date={match_date}'
               f'&timezone=auto')

        req = Request(url)
        req.add_header('User-Agent', 'Oraculo/1.0')
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())

        hourly = data.get('hourly', {})
        times = hourly.get('time', [])

        # Find closest hour
        target_idx = match_hour
        if target_idx >= len(times):
            target_idx = len(times) - 1

        result = {
            'temperature': hourly.get('temperature_2m', [15])[target_idx],
            'precipitation': hourly.get('precipitation', [0])[target_idx],
            'wind_speed': hourly.get('windspeed_10m', [10])[target_idx],
            'humidity': hourly.get('relativehumidity_2m', [60])[target_idx],
        }

        # Cache
        with open(cache_file, 'w') as f:
            json.dump(result, f)

        return result

    except Exception as e:
        log.debug('Weather fetch failed for %s %s: %s', home_team, match_date, e)
        return {'temperature': 15.0, 'precipitation': 0.0,
                'wind_speed': 10.0, 'humidity': 60.0}


def get_weather_features(home_team, match_datetime_str):
    """
    Extract weather features from a match datetime string.

    Args:
        home_team: team name for stadium coords
        match_datetime_str: ISO datetime string

    Returns:
        dict of weather features for ML model
    """
    try:
        dt = datetime.fromisoformat(match_datetime_str.replace('Z', '+00:00'))
        date_str = dt.strftime('%Y-%m-%d')
        hour = dt.hour
    except Exception:
        date_str = match_datetime_str[:10] if len(match_datetime_str) >= 10 else '2025-01-01'
        hour = 15

    w = get_match_weather(home_team, date_str, hour)

    return {
        'weather_temp': w['temperature'],
        'weather_rain': w['precipitation'],
        'weather_wind': w['wind_speed'],
        'weather_humidity': w['humidity'],
        'weather_is_rainy': 1.0 if w['precipitation'] > 0.5 else 0.0,
        'weather_is_windy': 1.0 if w['wind_speed'] > 30 else 0.0,
        'weather_is_cold': 1.0 if w['temperature'] < 5 else 0.0,
    }


# ---------------------------------------------------------------------------
# Unified feature builder
# ---------------------------------------------------------------------------

# Team name mapping: Understat -> football-data.org (partial, extend as needed)
_US_TO_ORG = {
    'Manchester United': 'Manchester United FC',
    'Manchester City': 'Manchester City FC',
    'Liverpool': 'Liverpool FC',
    'Arsenal': 'Arsenal FC',
    'Chelsea': 'Chelsea FC',
    'Tottenham': 'Tottenham Hotspur FC',
    'Newcastle United': 'Newcastle United FC',
    'West Ham': 'West Ham United FC',
    'Aston Villa': 'Aston Villa FC',
    'Brighton': 'Brighton & Hove Albion FC',
    'Crystal Palace': 'Crystal Palace FC',
    'Fulham': 'Fulham FC',
    'Brentford': 'Brentford FC',
    'Everton': 'Everton FC',
    'Wolverhampton Wanderers': 'Wolverhampton Wanderers FC',
    'Nottingham Forest': 'Nottingham Forest FC',
    'Bournemouth': 'AFC Bournemouth',
    'Leicester': 'Leicester City FC',
    'Ipswich': 'Ipswich Town FC',
    'Southampton': 'Southampton FC',
    'Barcelona': 'FC Barcelona',
    'Real Madrid': 'Real Madrid CF',
    'Atletico Madrid': 'Club Atletico de Madrid',
    'Sevilla': 'Sevilla FC',
    'Juventus': 'Juventus FC',
    'Inter': 'FC Internazionale Milano',
    'Napoli': 'SSC Napoli',
    'AC Milan': 'AC Milan',
    'Roma': 'AS Roma',
    'Lazio': 'SS Lazio',
    'Bayern Munich': 'FC Bayern Munchen',
    'Borussia Dortmund': 'Borussia Dortmund',
    'Bayer Leverkusen': 'Bayer 04 Leverkusen',
    'RB Leipzig': 'RB Leipzig',
    'Paris Saint Germain': 'Paris Saint-Germain FC',
    'Marseille': 'Olympique de Marseille',
    'Lyon': 'Olympique Lyonnais',
    'Monaco': 'AS Monaco FC',
    'Lille': 'Lille OSC',
}
_ORG_TO_US = {v: k for k, v in _US_TO_ORG.items()}




# Weather forecast cache (1h TTL) for pre-match adjustments
_forecast_cache: dict = {}
_FORECAST_TTL = 3600


def get_forecast_adjustment(home_team: str, match_cutoff_ts: str) -> float:
    """
    Return a probability multiplier for soccer over/under markets based on
    forecast weather at the match venue. Uses Open-Meteo forecast API (free, no key).

    Returns float in [0.82, 1.05]:
      < 1.0 = bad weather reduces scoring (apply to over/btts_yes probability)
      > 1.0 = ideal conditions slightly boost scoring (cap 1.05)
    Falls back to 1.0 on any error.

    Only meaningful for outdoor stadiums; call only for over/under markets.
    """
    try:
        from datetime import datetime, timezone
        dt_str = match_cutoff_ts[:10] if match_cutoff_ts else ''
        if not dt_str:
            return 1.0

        cache_key = '%s_%s' % (home_team, dt_str)
        cached = _forecast_cache.get(cache_key)
        if cached and (time.time() - cached['ts']) < _FORECAST_TTL:
            return cached['adj']

        lat, lon = STADIUM_COORDS.get(home_team, DEFAULT_COORDS)

        from urllib.request import Request, urlopen
        url = (
            'https://api.open-meteo.com/v1/forecast?'
            'latitude=%.4f&longitude=%.4f'
            '&daily=precipitation_sum,windspeed_10m_max,temperature_2m_max,temperature_2m_min'
            '&start_date=%s&end_date=%s'
            '&timezone=UTC'
        ) % (lat, lon, dt_str, dt_str)

        req = Request(url)
        req.add_header('User-Agent', 'Oraculo/1.0')
        resp = urlopen(req, timeout=8)
        data = json.loads(resp.read())

        daily = data.get('daily', {})
        rain   = float((daily.get('precipitation_sum') or [0])[0] or 0)
        wind   = float((daily.get('windspeed_10m_max') or [0])[0] or 0)
        t_max  = float((daily.get('temperature_2m_max') or [20])[0] or 20)
        t_min  = float((daily.get('temperature_2m_min') or [10])[0] or 10)

        adj = 1.0
        # Rain effect: heavy rain suppresses goals (tired legs, wet ball)
        if rain > 10:
            adj *= 0.87
        elif rain > 5:
            adj *= 0.92
        elif rain > 2:
            adj *= 0.96
        # Wind effect
        if wind > 60:
            adj *= 0.90
        elif wind > 45:
            adj *= 0.94
        elif wind > 30:
            adj *= 0.97
        # Cold: winter matches in southern Europe; slight suppressor
        if t_max < 5:
            adj *= 0.96
        # Perfect conditions: slight boost (bounded)
        adj = round(min(max(adj, 0.82), 1.05), 4)

        _forecast_cache[cache_key] = {'adj': adj, 'ts': time.time()}
        log.debug('Weather forecast %s %s: rain=%.1fmm wind=%.0fkm/h adj=%.3f',
                  home_team, dt_str, rain, wind, adj)
        return adj
    except Exception as e:
        log.debug('get_forecast_adjustment error (%s): %s', home_team, e)
        return 1.0

def build_xg_weather_features(match, xg_data):
    """
    Build xG + weather features for a match.

    Args:
        match: match dict with home_team, away_team, utc_date
        xg_data: list of xG match dicts from load_xg_data()

    Returns:
        dict of features to merge into main feature dict
    """
    features = {}

    # xG features
    home_us = _ORG_TO_US.get(match.get('home_team', ''), match.get('home_team', ''))
    away_us = _ORG_TO_US.get(match.get('away_team', ''), match.get('away_team', ''))

    h_xg = compute_team_xg_stats(xg_data, home_us, n=10)
    a_xg = compute_team_xg_stats(xg_data, away_us, n=10)

    features['home_xg_for_avg'] = h_xg['xg_for_avg']
    features['home_xg_against_avg'] = h_xg['xg_against_avg']
    features['home_xg_diff'] = h_xg['xg_diff_avg']
    features['home_xg_over25_rate'] = h_xg['xg_over25_rate']
    features['home_overperform'] = h_xg['overperform_avg']

    features['away_xg_for_avg'] = a_xg['xg_for_avg']
    features['away_xg_against_avg'] = a_xg['xg_against_avg']
    features['away_xg_diff'] = a_xg['xg_diff_avg']
    features['away_xg_over25_rate'] = a_xg['xg_over25_rate']
    features['away_overperform'] = a_xg['overperform_avg']

    features['xg_total_predict'] = h_xg['xg_for_avg'] + a_xg['xg_for_avg']
    features['xg_diff'] = h_xg['xg_diff_avg'] - a_xg['xg_diff_avg']

    # Weather features
    home_team = match.get('home_team', '')
    utc_date = match.get('utc_date', match.get('datetime', ''))
    w_feats = get_weather_features(home_team, utc_date)
    features.update(w_feats)

    return features


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    # Test xG
    print('Loading xG data...')
    xg = load_xg_data(leagues=['PL'], seasons=['2024'])
    print(f'xG matches: {len(xg)}')

    if xg:
        stats = compute_team_xg_stats(xg, 'Arsenal', n=10)
        print(f'\nArsenal xG stats: {stats}')

    # Test weather
    print('\nTesting weather...')
    w = get_match_weather('Arsenal FC', '2025-03-15', 15)
    print(f'Arsenal 2025-03-15 15:00: {w}')
