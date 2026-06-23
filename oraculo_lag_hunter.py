#!/usr/bin/env python3
"""oraculo_lag_hunter.py — cazador de lag Pinnacle->Cloudbet + captura CLV para validar.
GATEADO: HUNTER_LIVE=False => dry-run (loguea spots + captura CLV del cierre, NO apuesta).
Validacion: si CLV agregado de los spots es +, el lag es real -> flip HUNTER_LIVE=True.
Reusa oraculo_lag_finder (signal). Cron cada 3h (reemplaza al lag_finder)."""
import json, os, uuid, sqlite3, requests
from datetime import datetime, timezone
import oraculo_lag_finder as LF

HUNTER_LIVE   = False   # <<< GATE: True = apuesta REAL. False = dry-run + captura CLV.
STAKE         = 1.00
CURRENCY      = 'USDC'
MIN_EDGE      = 0.02
MAX_DAILY_BETS = 10
MIN_ODDS, MAX_ODDS = 1.30, 4.00
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SCRIPT_DIR, 'lag_hunter.db')
CB_KEY = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))['api_key']
CB_BASE = 'https://sports-api.cloudbet.com'
CBH = {'accept': 'application/json', 'X-API-Key': CB_KEY}

def _db():
    c = sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS hunts (ts TEXT, live INTEGER, liga TEXT, match TEXT,
                 outcome TEXT, cb_odd REAL, pin_fair REAL, edge REAL, event_id TEXT, market_url TEXT,
                 stake REAL, bet_id TEXT, status TEXT, kickoff TEXT, closing_odd REAL, clv REAL)''')
    return c

def cb_odds_urls(eid):
    try:
        r = requests.get(f'{CB_BASE}/pub/v2/odds/events/{eid}', headers=CBH, timeout=12)
        mo = r.json().get('markets', {}).get('soccer.match_odds', {})
        out = {}
        for sub in mo.get('submarkets', {}).values():
            for sel in sub.get('selections', []):
                o, p, mu = sel.get('outcome',''), float(sel.get('price',0) or 0), sel.get('marketUrl','')
                if o in ('home','draw','away') and p > 1 and sel.get('status')=='SELECTION_ENABLED' and mu:
                    out[o] = (p, mu)
        return out if len(out) == 3 else None
    except Exception:
        return None

def place_straight(event_id, market_url, price, stake):
    payload = {'referenceId': str(uuid.uuid4()), 'currency': CURRENCY, 'stake': str(round(stake,2)),
               'acceptPartialStake': True, 'priceChange': {'value': 'BETTER'},
               'selection': {'eventId': str(event_id), 'marketUrl': market_url, 'price': str(price)}}
    r = requests.post(f'{CB_BASE}/pub/v4/bets/place/straight', headers=CBH, json=payload, timeout=15)
    resp = r.json(); st = resp.get('state', resp.get('status',''))
    return resp.get('betId','') if st in ('ACCEPTED','PENDING_ACCEPTANCE') else None

def capture_closing(c):
    """Re-fetchea Cloudbet de cada spot pre-kickoff -> closing_odd (ultima antes del horario) + CLV.
    CLV = entry_odd / closing_odd - 1 (>0 si Cloudbet bajo hacia Pinnacle = ganamos al cierre)."""
    now = datetime.now(timezone.utc)
    rows = c.execute("SELECT rowid, event_id, market_url, cb_odd, kickoff FROM hunts WHERE kickoff > ?",
                     (now.isoformat(),)).fetchall()
    upd = 0
    for rid, eid, murl, entry, ko in rows:
        cur = cb_odds_urls(eid)
        if not cur: continue
        cp = next((p for o,(p,mu) in cur.items() if mu == murl), None)
        if not cp or cp <= 1: continue
        clv = round(entry/cp - 1, 4)
        c.execute("UPDATE hunts SET closing_odd=?, clv=? WHERE rowid=?", (round(cp,3), clv, rid)); upd += 1
    c.commit()
    return upd

def hunt():
    c = _db()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    placed = c.execute("SELECT COUNT(*) FROM hunts WHERE live=1 AND ts LIKE ?", (today+'%',)).fetchone()[0]
    n_spots = n_act = 0
    for sport, slug, lab, tier in LF.LEAGUES:
        pin, _ = LF.fetch_pinnacle(sport); cb = LF.fetch_cb_events(slug)
        for pe, ce, sc in LF.match_events(pin, cb):
            po = pe['odds']
            if 'draw' not in po: continue
            cpr = cb_odds_urls(ce['id'])
            if not cpr: continue
            ov = sum(1/po[k] for k in ('home','draw','away'))
            if not (1.01 <= ov <= 1.12): continue
            fair = {k: (1/po[k])/ov for k in ('home','draw','away')}
            for k in ('home','draw','away'):
                odd, murl = cpr[k]; edge = odd*fair[k] - 1
                if edge < MIN_EDGE or abs(edge) > 0.15 or not (MIN_ODDS <= odd <= MAX_ODDS): continue
                n_spots += 1
                if c.execute("SELECT 1 FROM hunts WHERE event_id=? AND outcome=? LIMIT 1", (ce['id'],k)).fetchone():
                    continue
                bet_id, status = '', 'DRYRUN'
                if HUNTER_LIVE and placed < MAX_DAILY_BETS:
                    bet_id = place_straight(ce['id'], murl, odd, STAKE)
                    status = 'PLACED' if bet_id else 'REJECTED'
                    if bet_id: placed += 1; n_act += 1
                c.execute("INSERT INTO hunts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (datetime.now(timezone.utc).isoformat(), int(HUNTER_LIVE), lab,
                           pe['home']+' v '+pe['away'], k, odd, round(fair[k],4), round(edge,4),
                           ce['id'], murl, STAKE, bet_id, status, ce['time'].isoformat(), None, None))
    upd = capture_closing(c)
    c.close()
    mode = 'LIVE' if HUNTER_LIVE else 'DRY-RUN'
    print('[lag_hunter %s %s] spots edge>=%.0f%%: %d | apostados: %d | CLV actualizados: %d' % (
        mode, datetime.now(timezone.utc).strftime('%m-%d %H:%M'), MIN_EDGE*100, n_spots, n_act, upd))

if __name__ == '__main__':
    hunt()
