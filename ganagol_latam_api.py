#!/usr/bin/env python3
"""
ganagol_latam_api.py — Descarga Chile y Uruguay desde api-football.com (free tier)

Uso:
  export APIFOOTBALL_KEY="tu_key_aqui"
  python3 ganagol_latam_api.py

Guarda en:
  .oraculo_cache/csv/new_CHI.json
  .oraculo_cache/csv/new_URU.json

Después correr:
  python3 ganagol_retrain.py --force
"""
import os, sys, json, time, logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('latam_api')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

API_KEY = os.environ.get('APIFOOTBALL_KEY', '')
BASE_URL = 'https://v3.football.api-sports.io'

# League IDs en api-football.com
LEAGUES = {
    'CHI': {'id': 265, 'name': 'Chile Primera División'},
    'URU': {'id': 278, 'name': 'Uruguay Primera División'},
}

# Temporadas a descargar (de más reciente a más antiguo)
SEASONS = [2024, 2023, 2022, 2021, 2020]

# Mapeo de nombres de equipos chilenos/uruguayos
_TEAM_MAP = {
    # Chile
    'Universidad de Chile':  'Universidad de Chile',
    'Universidad Católica':  'Universidad Católica',
    'Universidad Catolica':  'Universidad Católica',
    'Colo-Colo':             'Colo Colo',
    'Colo Colo':             'Colo Colo',
    'Cobresal':              'Cobresal',
    'Huachipato':            'Huachipato',
    'Palestino':             'Palestino',
    'Audax Italiano':        'Audax Italiano',
    'Everton':               'Everton Vina',
    'Everton de Viña':       'Everton Vina',
    'O\'Higgins':            "O'Higgins",
    'Curicó Unido':          'Curico Unido',
    'Deportes Antofagasta':  'Antofagasta',
    'Deportes Iquique':      'Iquique',
    'La Calera':             'La Calera',
    'Unión La Calera':       'La Calera',
    'Santiago Wanderers':    'Santiago Wanderers',
    'Unión Española':        'Union Espanola',
    'Deportes La Serena':    'La Serena',
    'La Serena':             'La Serena',
    'Ñublense':              'Nublense',
    'Magallanes':            'Magallanes',
    'Cobreloa':              'Cobreloa',
    'Rangers':               'Rangers Talca',
    'Limache':               'Limache',
    # Uruguay
    'Nacional':              'Nacional',
    'Peñarol':               'Penarol',
    'Peñarol Montevideo':    'Penarol',
    'Club Atlético Peñarol': 'Penarol',
    'Danubio':               'Danubio',
    'Defensor Sporting':     'Defensor',
    'River Plate':           'River Plate Uru',
    'Club Atlético River':   'River Plate Uru',
    'Fénix':                 'Fenix',
    'Cerro':                 'CA Cerro',
    'Club Atlético Cerro':   'CA Cerro',
    'Liverpool':             'Liverpool Uru',
    'Liverpool FC':          'Liverpool Uru',
    'Racing':                'Racing Uru',
    'Club Atlético Racing':  'Racing Uru',
    'Rentistas':             'Rentistas',
    'Plaza Colonia':         'Plaza Colonia',
    'Progreso':              'Progreso',
    'Boston River':          'Boston River',
    'Wanderers':             'M. Wanderers',
    'Montevideo Wanderers':  'M. Wanderers',
    'Central Español':       'Central Espanol',
    'Deportivo Maldonado':   'Dep. Maldonado',
    'Torque':                'Montevideo City',
    'Montevideo City Torque':'Montevideo City',
}


def _api_get(endpoint, params):
    if not API_KEY:
        log.error('APIFOOTBALL_KEY no está definido. Exporta la variable de entorno.')
        sys.exit(1)
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{BASE_URL}/{endpoint}?{query}'
    req = Request(url)
    req.add_header('x-apisports-key', API_KEY)
    req.add_header('x-rapidapi-host', 'v3.football.api-sports.io')
    try:
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read().decode('utf-8'))
        remaining = resp.headers.get('x-ratelimit-requests-remaining', '?')
        log.info('API call OK → %s | remaining calls: %s', url[:80], remaining)
        return data
    except HTTPError as e:
        log.error('HTTP %s — %s', e.code, url)
        return None


def download_league(league_code):
    cfg = LEAGUES[league_code]
    all_matches = []
    os.makedirs(CACHE_DIR, exist_ok=True)

    for season in SEASONS:
        log.info('Descargando %s temporada %s...', cfg['name'], season)
        data = _api_get('fixtures', {'league': cfg['id'], 'season': season, 'status': 'FT'})
        if not data:
            continue

        fixtures = data.get('response', [])
        log.info('  %d partidos terminados en %s', len(fixtures), season)

        for f in fixtures:
            try:
                goals = f.get('goals', {})
                hg = goals.get('home')
                ag = goals.get('away')
                if hg is None or ag is None:
                    continue

                home_raw = f['teams']['home']['name']
                away_raw = f['teams']['away']['name']
                home = _TEAM_MAP.get(home_raw, home_raw)
                away = _TEAM_MAP.get(away_raw, away_raw)

                date_str = f['fixture'].get('date', '')[:10]

                all_matches.append({
                    'home':       home,
                    'away':       away,
                    'home_goals': int(hg),
                    'away_goals': int(ag),
                    'date':       date_str,
                    'league':     league_code,
                })
            except Exception as e:
                log.debug('Skip fixture: %s', e)

        time.sleep(0.5)  # respeta rate limit

    cache_file = os.path.join(CACHE_DIR, f'new_{league_code}.json')
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(all_matches, f, ensure_ascii=False)
    log.info('%s: %d partidos guardados → %s', league_code, len(all_matches), cache_file)
    return all_matches


def main():
    log.info('=== Latam API Downloader ===')
    if not API_KEY:
        print('\n⚠  APIFOOTBALL_KEY no configurada.')
        print('   1. Regístrate gratis en https://dashboard.api-football.com')
        print('   2. Copia tu API key del panel')
        print('   3. Ejecuta:  export APIFOOTBALL_KEY="tu_key"')
        print('   4. Vuelve a correr este script\n')
        sys.exit(1)

    for code in LEAGUES:
        matches = download_league(code)
        teams = sorted(set(m['home'] for m in matches) | set(m['away'] for m in matches))
        print(f'\n{LEAGUES[code]["name"]}: {len(matches)} partidos, {len(teams)} equipos')
        for t in teams:
            print(f'  {t}')

    print('\nListo. Ahora corre:')
    print('  python3 ganagol_retrain.py --force')
    print('para reentrenar el modelo con Chile y Uruguay.')


if __name__ == '__main__':
    main()
