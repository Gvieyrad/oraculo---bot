#!/usr/bin/env python3
"""
oraculo_multibook_lag.py
Mide lag de 39 casas vs Pinnacle (devig) en una sola llamada multi-region.
SOLO medicion -- NUNCA apuesta.
Cron: 0 * * * *  (cada hora junto al hunter)
Gate: solo gasta cuota cuando hay partido en ventana <=3h (fetch CB gratis primero).
Cuota: 3 req por llamada (1 por region eu/us/uk).
"""

import os, json, logging, sqlite3, datetime
import urllib.request

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(SCRIPT_DIR, 'lag_multibook.db')
SECRETS_FILE = '/etc/samael/secrets.env'
CB_CFG_FILE  = os.path.join(SCRIPT_DIR, 'cloudbet_config.json')

ODDS_API_BASE = 'https://api.the-odds-api.com/v4'
WINDOW_H      = 3
SANITY_CAP    = 0.15
MIN_EDGE      = 0.01
REGIONS       = 'eu,us,uk'

# (odds_api_sport_key, cloudbet_slug)
LEAGUES = [
    ('soccer_brazil_serie_b',             'soccer-brazil-brasileiro-serie-b'),
    ('soccer_norway_eliteserien',         'soccer-norway-eliteserien'),
    ('soccer_sweden_allsvenskan',         'soccer-sweden-allsvenskan'),
    ('soccer_finland_veikkausliiga',      'soccer-finland-veikkausliiga'),
    ('soccer_conmebol_copa_libertadores', 'soccer-international-clubs-copa-libertadores'),
    ('soccer_conmebol_copa_sudamericana', 'soccer-international-clubs-copa-sudamericana'),
    ('soccer_usa_mls',                    'soccer-usa-mls'),
    ('soccer_japan_j_league',             'soccer-japan-j-league'),
    ('soccer_fifa_world_cup',             'soccer-international-world-cup'),
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [multibook] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('multibook')


def load_secrets():
    if not os.path.exists(SECRETS_FILE):
        raise FileNotFoundError(f'Secrets file not found: {SECRETS_FILE}')
    with open(SECRETS_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith('ODDS_API_KEY_ORACULO='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise KeyError('ODDS_API_KEY_ORACULO not found in secrets file')


def load_cb_events_window(window_h):
    """
    Itera los slugs de CB para cada liga y cuenta eventos en ventana <=window_h h.
    Usa endpoint /pub/v2/odds/competitions/{slug} (mismo que oraculo_lag_finder).
    Gratis: no gasta cuota de The-Odds-API.
    """
    try:
        cb_cfg = json.load(open(CB_CFG_FILE))
        cb_key = cb_cfg.get('api_key', '')
    except Exception as e:
        log.warning('CB config error: %s', e)
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    win_end = now + datetime.timedelta(hours=window_h)

    found = []
    for _sport, slug in LEAGUES:
        url = f'https://sports-api.cloudbet.com/pub/v2/odds/competitions/{slug}'
        headers = {'X-API-Key': cb_key, 'Accept': 'application/json'}
        try:
            req = urllib.request.Request(url, headers=headers,
                                          method='GET')
            req.add_header('X-API-Key', cb_key)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except Exception as e:
            log.debug('CB slug %s error: %s', slug, e)
            continue

        for ev in data.get('events', []):
            try:
                kt = ev.get('cutoffTime') or ''
                if not kt:
                    continue
                kt_clean = kt.rstrip('Z')
                if '.' in kt_clean:
                    kt_clean = kt_clean[:26]
                ko = datetime.datetime.fromisoformat(kt_clean).replace(
                    tzinfo=datetime.timezone.utc)
                if now <= ko <= win_end and ev.get('status') == 'TRADING':
                    home = (ev.get('home') or {}).get('name', '')
                    away = (ev.get('away') or {}).get('name', '')
                    found.append({'name': f'{home} vs {away}', 'time': ko, 'liga': slug})
            except Exception:
                continue
    return found


def fetch_all_books(api_key, league):
    """
    Fetch odds para una liga con las 3 regiones.
    Retorna dict: ev_id -> {home, away, kickoff, bookmakers: {book: {home,draw,away}}}
    Costo: 3 req.
    """
    url = (f'{ODDS_API_BASE}/sports/{league}/odds/'
           f'?apiKey={api_key}&regions={REGIONS}'
           f'&markets=h2h&oddsFormat=decimal')
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            remaining = r.headers.get('x-requests-remaining', '?')
            events = json.loads(r.read())
        log.debug('League %s: %d events, quota=%s', league, len(events), remaining)
    except Exception as e:
        log.warning('Odds API error for %s: %s', league, e)
        return {}

    result = {}
    for ev in events:
        ev_id = ev.get('id', '')
        home  = ev.get('home_team', '')
        away  = ev.get('away_team', '')
        kt    = ev.get('commence_time', '')
        books = {}
        for bm in ev.get('bookmakers', []):
            bk = bm.get('key', '')
            for mkt in bm.get('markets', []):
                if mkt.get('key') != 'h2h':
                    continue
                odds_map = {}
                for o in mkt.get('outcomes', []):
                    name  = o.get('name', '')
                    price = float(o.get('price', 0) or 0)
                    if name == home:
                        odds_map['home'] = price
                    elif name == away:
                        odds_map['away'] = price
                    else:
                        odds_map['draw'] = price
                if odds_map:
                    books[bk] = odds_map
        result[ev_id] = {
            'home': home, 'away': away, 'kickoff': kt, 'bookmakers': books
        }
    return result


def devig_pinnacle(h, d, a):
    """Elimina vig de Pinnacle (metodo multiplicativo). Retorna fair probs."""
    if h <= 0 or a <= 0:
        return {}
    if d and d > 0:
        inv_sum = 1/h + 1/d + 1/a
        return {'home': (1/h)/inv_sum, 'draw': (1/d)/inv_sum, 'away': (1/a)/inv_sum}
    else:
        inv_sum = 1/h + 1/a
        return {'home': (1/h)/inv_sum, 'draw': 0.0, 'away': (1/a)/inv_sum}


def ensure_db():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS lag_multibook (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT,
        liga      TEXT,
        book      TEXT,
        match     TEXT,
        outcome   TEXT,
        fair_pin  REAL,
        book_odd  REAL,
        edge      REAL,
        kickoff   TEXT
    )''')
    con.execute('CREATE INDEX IF NOT EXISTS ix_ts ON lag_multibook(ts)')
    con.execute('CREATE INDEX IF NOT EXISTS ix_book ON lag_multibook(book)')
    con.commit()
    con.close()


def log_spot(ts, liga, book, match, outcome, fair_pin, book_odd, edge, kickoff):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        'INSERT INTO lag_multibook VALUES (NULL,?,?,?,?,?,?,?,?,?)',
        (ts, liga, book, match, outcome, fair_pin, book_odd, edge, kickoff)
    )
    con.commit()
    con.close()


def scan_league(api_key, league, now_ts):
    """Scanea una liga, loguea spots edge>=MIN_EDGE. Retorna n_spots."""
    events = fetch_all_books(api_key, league)
    if not events:
        return 0

    n_spots = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    win_end = now + datetime.timedelta(hours=WINDOW_H)

    for ev_id, ev in events.items():
        try:
            kt_raw = ev['kickoff'].rstrip('Z')
            ko = datetime.datetime.fromisoformat(kt_raw).replace(
                tzinfo=datetime.timezone.utc)
            if not (now <= ko <= win_end):
                continue
        except Exception:
            continue

        books = ev['bookmakers']
        pin = books.get('pinnacle', {})
        if not pin:
            continue

        fair = devig_pinnacle(
            pin.get('home', 0), pin.get('draw', 0), pin.get('away', 0))
        if not fair:
            continue

        match_name = f"{ev['home']} vs {ev['away']}"
        ko_str = ev['kickoff']

        for book, odds in books.items():
            if book == 'pinnacle':
                continue
            for outcome in ('home', 'draw', 'away'):
                fair_p = fair.get(outcome, 0)
                odd = odds.get(outcome, 0)
                if not fair_p or not odd or odd <= 0:
                    continue
                edge = odd * fair_p - 1
                if edge > SANITY_CAP:
                    continue
                if edge >= MIN_EDGE:
                    log_spot(ts=now_ts, liga=league, book=book,
                             match=match_name, outcome=outcome,
                             fair_pin=fair_p, book_odd=odd,
                             edge=edge, kickoff=ko_str)
                    n_spots += 1
                    if edge >= 0.03:
                        log.info(
                            '[%-14s] %-16s | %-28s | %-4s @%.3f fair=%.3f edge=+%.1f%%',
                            book[:14], league.split('_', 2)[-1][:16],
                            match_name[:28], outcome, odd, fair_p, edge * 100)
    return n_spots


def main():
    ensure_db()
    api_key = load_secrets()
    now_ts  = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Gate de ventana: fetch CB primero (gratis, no gasta cuota API)
    cb_events = load_cb_events_window(WINDOW_H)
    log.info('CB eventos en ventana <=3h: %d', len(cb_events))
    if not cb_events:
        log.info('Sin partidos en ventana -- saliendo sin gastar cuota')
        return

    total_spots = 0
    for sport_key, _slug in LEAGUES:
        n = scan_league(api_key, sport_key, now_ts)
        total_spots += n
        if n:
            log.info('  Liga %-35s  %d spots edge>=1%%', sport_key, n)

    # Resumen acumulado por libro
    con = sqlite3.connect(DB_PATH)
    summary = con.execute('''
        SELECT book, COUNT(*) as n, ROUND(AVG(edge)*100, 2) as avg_e,
               ROUND(MAX(edge)*100, 2) as max_e
        FROM lag_multibook
        GROUP BY book ORDER BY avg_e DESC LIMIT 15
    ''').fetchall()
    total_rows = con.execute('SELECT COUNT(*) FROM lag_multibook').fetchone()[0]
    con.close()

    log.info('=== TOP LIBROS (acumulado total=%d spots) ===', total_rows)
    for row in summary:
        log.info('  %-22s  n=%-5d  avg_edge=%.2f%%  max=%.2f%%',
                 row[0], row[1], row[2], row[3])
    log.info('Scan completado: %d spots nuevos esta corrida', total_spots)


if __name__ == '__main__':
    main()
