#!/usr/bin/env python3
"""oraculo_lag_finder.py — detector de lag Pinnacle->Cloudbet (Fase 1: matcher robusto).
Mide si Cloudbet se atrasa respecto a la linea sharp (Pinnacle, via The-Odds-API).
Safeguards: match solo mismo partido (home+away sim>=0.80, misma liga, kickoff +-90min,
RECHAZA si la fecha no parsea), sanity cap de edge (descarta |edge|>15% = mismatch)."""
import json, os, re, unicodedata, requests, sys
from difflib import SequenceMatcher
from datetime import datetime

def _load_odds_key():
    for line in open('/etc/samael/secrets.env'):
        if line.startswith('ODDS_API_KEY_ORACULO='):
            return line.split('=', 1)[1].strip()
    return ''
OKEY = _load_odds_key()
CKEY = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cloudbet_config.json')))['api_key']
CH = {'accept': 'application/json', 'X-API-Key': CKEY}

# (odds_api_sport, cloudbet_slug, label, tier)
LEAGUES = [
    ('soccer_brazil_serie_b',            'soccer-brazil-brasileiro-serie-b',          'BrasilB',  'minor'),
    ('soccer_norway_eliteserien',        'soccer-norway-eliteserien',                 'Noruega',  'minor'),
    ('soccer_sweden_allsvenskan',        'soccer-sweden-allsvenskan',                 'Suecia',   'minor'),
    ('soccer_finland_veikkausliiga',     'soccer-finland-veikkausliiga',              'Finlandia','minor'),
    ('soccer_conmebol_copa_libertadores','soccer-international-clubs-copa-libertadores','Libertad','minor'),
    ('soccer_conmebol_copa_sudamericana','soccer-international-clubs-copa-sudamericana','Sudamer', 'minor'),
    ('soccer_usa_mls',                   'soccer-usa-mls',                            'MLS',      'minor'),
    ('soccer_japan_j_league',            'soccer-japan-j-league',                     'JLeague',  'minor'),
    ('soccer_fifa_world_cup',            'soccer-international-world-cup',            'WC2026',   'major'),
]

SUFFIX = {'fk','fc','aa','sk','il','if','cf','sc','ec','ac','afc','cd','ca','sca','bk','ab','idrottsforening'}
ALIASES = {}  # crece a mano cuando aparezcan mismatches por nombre

def norm(name):
    if not name: return ''
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t not in SUFFIX and len(t) > 1]
    s = ''.join(toks)
    return ALIASES.get(s, s)

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return 0.0
    if a == b: return 1.0
    if (len(a) > 4 and a in b) or (len(b) > 4 and b in a): return 0.9
    return SequenceMatcher(None, a, b).ratio()

def ptime(s):
    try: return datetime.strptime(str(s)[:19].replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
    except Exception: return None

def fetch_pinnacle(sport):
    try:
        r = requests.get(f'https://api.the-odds-api.com/v4/sports/{sport}/odds/',
                         params={'apiKey': OKEY, 'regions': 'eu', 'markets': 'h2h', 'bookmakers': 'pinnacle'}, timeout=20)
        rem = r.headers.get('x-requests-remaining')
        out = []
        for e in (r.json() if r.status_code == 200 else []):
            bm = next((b for b in e.get('bookmakers', []) if b.get('key') == 'pinnacle'), None)
            if not bm: continue
            h2h = next((m for m in bm.get('markets', []) if m.get('key') == 'h2h'), None)
            if not h2h: continue
            ht, at = e.get('home_team', ''), e.get('away_team', '')
            po = {}
            for oc in h2h.get('outcomes', []):
                nm, pr = oc.get('name', ''), float(oc.get('price', 0) or 0)
                if nm == ht: po['home'] = pr
                elif nm == at: po['away'] = pr
                elif nm.lower() == 'draw': po['draw'] = pr
            tm = ptime(e.get('commence_time'))
            if ht and at and tm and ('home' in po and 'away' in po):
                out.append({'home': ht, 'away': at, 'time': tm, 'odds': po})
        return out, rem
    except Exception as e:
        return [], None

def fetch_cb_events(slug):
    try:
        r = requests.get(f'https://sports-api.cloudbet.com/pub/v2/odds/competitions/{slug}',
                         headers=CH, params={'markets': 'soccer.match_odds'}, timeout=15)
        out = []
        for e in r.json().get('events', []):
            if not isinstance(e.get('home'), dict): continue
            tm = ptime(e.get('cutoffTime'))
            if tm and e.get('status') == 'TRADING':
                out.append({'id': e.get('id'), 'home': e['home'].get('name', ''),
                            'away': (e.get('away') or {}).get('name', ''), 'time': tm})
        return out
    except Exception:
        return []

def match_events(pin, cb):
    """Match estricto: ambos equipos sim>=0.80, kickoff +-90min. Devuelve pares."""
    pairs = []
    for pe in pin:
        best, bscore = None, 0.0
        for ce in cb:
            sh, sa = sim(pe['home'], ce['home']), sim(pe['away'], ce['away'])
            if sh < 0.80 or sa < 0.80: continue
            if abs((pe['time'] - ce['time']).total_seconds()) > 90 * 60: continue  # rechaza distinto horario
            score = sh * sa
            if score > bscore: bscore, best = score, ce
        if best: pairs.append((pe, best, bscore))
    return pairs

def cb_match_odds(eid):
    """Lee SOLO soccer.match_odds (resultado), requiere las 3 selecciones ENABLED (price>1)."""
    try:
        r = requests.get(f'https://sports-api.cloudbet.com/pub/v2/odds/events/{eid}', headers=CH, timeout=12)
        mo = r.json().get('markets', {}).get('soccer.match_odds', {})
        pr = {}
        for sub in mo.get('submarkets', {}).values():
            for sel in sub.get('selections', []):
                o, p = sel.get('outcome', ''), float(sel.get('price', 0) or 0)
                if o in ('home', 'draw', 'away') and p > 1 and sel.get('status') == 'SELECTION_ENABLED':
                    pr[o] = p
        return pr if len(pr) == 3 else None
    except Exception:
        return None

SANITY = 0.15

def measure():
    """Devuelve lista de spots {liga,tier,match,outcome,fair,cb_odd,edge} con edge>=2%% sano.
    Solo cuenta partidos donde Cloudbet tiene el match_odds ENABLED."""
    spots, n_cmp = [], 0
    for sport, slug, lab, tier in LEAGUES:
        pin, _ = fetch_pinnacle(sport)
        cb = fetch_cb_events(slug)
        for pe, ce, sc in match_events(pin, cb):
            po = pe['odds']
            if 'draw' not in po: continue
            cpr = cb_match_odds(ce['id'])
            if not cpr: continue  # Cloudbet no cotiza el resultado aun -> skip
            ov = sum(1/po[k] for k in ('home','draw','away'))
            if not (1.01 <= ov <= 1.12): continue
            fair = {k: (1/po[k])/ov for k in ('home','draw','away')}
            n_cmp += 1
            for k in ('home','draw','away'):
                edge = cpr[k]*fair[k] - 1
                if abs(edge) > SANITY: continue  # mismatch probable
                if edge >= 0.02:
                    spots.append({'liga': lab, 'tier': tier, 'match': pe['home']+' v '+pe['away'],
                                  'outcome': k, 'fair': round(fair[k],4), 'cb_odd': cpr[k], 'edge': round(edge,4),
                                  'kickoff': ce['time'].isoformat()})
    return spots, n_cmp

def log_spots(spots):
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lag_measurements.db')
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE IF NOT EXISTS lag (ts TEXT, liga TEXT, tier TEXT, match TEXT, outcome TEXT, fair REAL, cb_odd REAL, edge REAL, kickoff TEXT)')
    ts = datetime.utcnow().isoformat()
    for sp in spots:
        c.execute('INSERT INTO lag VALUES (?,?,?,?,?,?,?,?,?)',
                  (ts, sp['liga'], sp['tier'], sp['match'], sp['outcome'], sp['fair'], sp['cb_odd'], sp['edge'], sp['kickoff']))
    c.commit(); c.close()

if __name__ == '__main__':
    spots, n = measure()
    log_spots(spots)
    print('[lag_finder %s] comparaciones validas (CB enabled)=%d | spots edge>=2%% sanos=%d' % (
        datetime.utcnow().strftime('%m-%d %H:%M'), n, len(spots)))
    for sp in spots:
        print('  %-9s %-5s %-26s %-5s CB@%.2f fair%.0f%% edge+%.1f%% (ko %s)' % (
            sp['liga'], sp['tier'], sp['match'][:26], sp['outcome'], sp['cb_odd'], sp['fair']*100, sp['edge']*100, sp['kickoff'][5:16]))
