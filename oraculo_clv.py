#!/usr/bin/env python3
"""
oraculo_clv.py — Closing Line Value tracker
============================================
CLV = entry_odds / closing_odds - 1

Si CLV promedio > +3%  → edge real, escalar con confianza
Si CLV promedio < 0%   → ganamos por varianza, corregir modelo

Fuente: Trademate Sports methodology + Pinnacle "How to Bet Smarter"

Integración:
  1. _log_prediction() ya guarda entry_odds en predictions_log.jsonl
  2. CLVTracker.record_closing() se llama desde el loop de resultados
  3. /clv en Telegram muestra resumen

Generado en Semana 2 del plan de mejoras Oráculo
"""
import os, json, re, time, logging
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

log = logging.getLogger('oraculo.clv')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLV_FILE   = os.path.join(SCRIPT_DIR, 'clv_log.jsonl')

# Mapeo de deportes Cloudbet → sport_key de TheOddsAPI
_SPORT_KEYS = {
    'tennis':   'tennis_atp',
    'baseball': 'baseball_mlb',
    'soccer':   'soccer_epl',         # fallback generico
    'basketball': 'basketball_nba',
}

# Cache de closing odds para no re-fetchear
_closing_cache = {}   # event_key → {'odds': float, 'ts': float}
_CACHE_TTL = 3600     # 1 hora


def _get_api_key():
    cfg_path = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
    try:
        return json.load(open(cfg_path)).get('odds_api_key', '')
    except Exception:
        return ''



# Path to odds history database (populated by live scraper, Cloudbet event IDs)
_ODDS_HISTORY_DB = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'odds_history.db')


def _fetch_closing_odds_local(event_id: str, market_type: str, label: str) -> float:
    """
    Look up the last recorded price for this event in odds_history.db.
    Supports soccer over/under full-match, 2H and 1H period markets.
    Returns decimal odds or 0.0 if not found.
    """
    if not event_id or not os.path.exists(_ODDS_HISTORY_DB):
        return 0.0
    try:
        import sqlite3
        label_lower = label.lower()
        m = re.search(r'(over|under)\s+([\d.]+)', label_lower)
        if not m:
            return 0.0
        direction = m.group(1)
        line = m.group(2)
        if '2h' in label_lower or 'second half' in label_lower:
            url_pat = '%%total_goals_period_second_half/%s?total=%s%%' % (direction, line)
        elif '1h' in label_lower or 'first half' in label_lower:
            url_pat = '%%total_goals_period_first_half/%s?total=%s%%' % (direction, line)
        else:
            url_pat = '%%total_goals/%s?total=%s%%' % (direction, line)
        conn = sqlite3.connect(_ODDS_HISTORY_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT price FROM odds_snapshots WHERE event_id=? AND market_url LIKE ? "
            "AND market_url NOT LIKE '%team%' ORDER BY timestamp DESC LIMIT 1",
            (str(event_id), url_pat)
        )
        row = cur.fetchone()
        conn.close()
        if row and float(row[0] or 0) > 1.0:
            log.debug('CLV local: event=%s %s %s -> %.3f', event_id, direction, line, row[0])
            return float(row[0])
    except Exception as e:
        log.debug('CLV local lookup error: %s', e)
    return 0.0


def _fetch_closing_odds(match: str, sport: str, entry_ts: str) -> float:
    """
    Fetch Pinnacle/best closing odds from TheOddsAPI for a match.
    Returns the implied probability (1/odds) or 0 if not found.
    match: "Player A vs Player B" or "Team A vs Team B"
    sport: 'tennis', 'baseball', etc.
    entry_ts: ISO timestamp when we placed the bet
    """
    api_key = _get_api_key()
    if not api_key:
        return 0.0

    sport_key = _SPORT_KEYS.get(sport, '')
    if not sport_key:
        return 0.0

    cache_key = '%s|%s' % (match, sport_key)
    cached = _closing_cache.get(cache_key)
    if cached and (time.time() - cached['ts']) < _CACHE_TTL:
        return cached['odds']

    try:
        url = ('https://api.the-odds-api.com/v4/sports/%s/odds/'
               '?apiKey=%s&regions=us,eu&markets=h2h&oddsFormat=decimal'
               '&bookmakers=pinnacle' % (sport_key, api_key))
        req = Request(url)
        req.add_header('Accept', 'application/json')
        resp = urlopen(req, timeout=15)
        events = json.loads(resp.read().decode('utf-8'))

        # Match by team/player names (fuzzy)
        parts = [p.strip().lower() for p in match.split(' vs ')]
        if len(parts) < 2:
            return 0.0
        home_q, away_q = parts[0], parts[1]

        best_odds = 0.0
        for ev in events:
            home = ev.get('home_team', '').lower()
            away = ev.get('away_team', '').lower()
            # Check if any word matches
            hm = any(w in home for w in home_q.split() if len(w) > 3)
            am = any(w in away for w in away_q.split() if len(w) > 3)
            if not (hm and am):
                continue
            # Extract home team odds (first outcome = home)
            for bk in ev.get('bookmakers', []):
                for mkt in bk.get('markets', []):
                    if mkt.get('key') != 'h2h':
                        continue
                    outcomes = mkt.get('outcomes', [])
                    for out in outcomes:
                        oname = out.get('name', '').lower()
                        if any(w in oname for w in home_q.split() if len(w) > 3):
                            best_odds = float(out.get('price', 0))
                            break
                    if best_odds:
                        break
                if best_odds:
                    break
            if best_odds:
                break

        _closing_cache[cache_key] = {'odds': best_odds, 'ts': time.time()}
        return best_odds

    except Exception as e:
        log.debug('CLV fetch error for %s: %s', match[:30], e)
        return 0.0



# Short-lived cache for pre-placement sharp reference (5 min)
_novig_cache: dict = {}
_NOVIG_TTL = 300        # 5 minutes per match lookup
_sports_cache: dict = {}  # {prefix: [(key, title), ...], ts: float}
_SPORTS_TTL = 43200     # 12 hours for sports list


def _get_active_sport_keys(sport: str) -> list:
    """Return list of active Odds API sport keys for a given sport prefix."""
    api_key = _get_api_key()
    if not api_key:
        return []
    prefix = {'tennis': 'tennis', 'soccer': 'soccer', 'baseball': 'baseball',
              'basketball': 'basketball'}.get(sport, '')
    if not prefix:
        return []
    cached = _sports_cache.get(prefix)
    if cached and (time.time() - cached['ts']) < _SPORTS_TTL:
        return cached['keys']
    try:
        from urllib.request import urlopen, Request as _Req
        url = 'https://api.the-odds-api.com/v4/sports?apiKey=%s' % api_key
        req = _Req(url)
        req.add_header('Accept', 'application/json')
        resp = urlopen(req, timeout=12)
        sports = json.loads(resp.read().decode('utf-8'))
        keys = [s['key'] for s in sports
                if s.get('key', '').startswith(prefix) and s.get('active', True)]
        _sports_cache[prefix] = {'keys': keys, 'ts': time.time()}
        log.debug('Odds API active %s keys: %s', prefix, keys)
        return keys
    except Exception as e:
        log.debug('_get_active_sport_keys error: %s', e)
        return []


def _best_sport_key(sport: str, comp_key: str) -> str:
    """Select the most likely Odds API sport key given a sport and Cloudbet comp_key."""
    keys = _get_active_sport_keys(sport)
    if not keys:
        return ''
    if not comp_key:
        return keys[0] if keys else ''
    # Score each key by word overlap with comp_key
    comp_words = set(comp_key.lower().replace('-', '_').split('_'))
    best, best_score = '', 0
    for k in keys:
        k_words = set(k.lower().split('_'))
        score = len(comp_words & k_words)
        if score > best_score:
            best_score, best = score, k
    return best or (keys[0] if keys else '')


def get_novig_prob(match: str, sport: str, pick_label: str,
                  comp_key: str = '') -> 'float | None':
    """
    Return Pinnacle no-vig probability for our pick direction.
    match:      "PlayerA vs PlayerB"
    sport:      'tennis', 'soccer', 'baseball', etc.
    pick_label: label of our pick (e.g. "Winner: Nadal", "Home")
    comp_key:   Cloudbet comp key used to resolve the tournament (optional)
    Returns float or None if Pinnacle data unavailable.
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    sport_key = _best_sport_key(sport, comp_key)
    if not sport_key:
        return None

    cache_key = '%s|%s' % (match, sport_key)
    cached = _novig_cache.get(cache_key)
    if cached and (time.time() - cached['ts']) < _NOVIG_TTL:
        return cached.get('prob')

    try:
        from urllib.request import urlopen, Request as _Req
        url = ('https://api.the-odds-api.com/v4/sports/%s/odds/'
               '?apiKey=%s&regions=us,eu&markets=h2h&oddsFormat=decimal'
               '&bookmakers=pinnacle' % (sport_key, api_key))
        req = _Req(url)
        req.add_header('Accept', 'application/json')
        resp = urlopen(req, timeout=12)
        events = json.loads(resp.read().decode('utf-8'))

        parts = [p.strip().lower() for p in match.split(' vs ')]
        if len(parts) < 2:
            return None
        home_q, away_q = parts[0], parts[1]
        label_lower = pick_label.lower()

        # Detect which side our pick is on
        pick_is_home = None
        for w in home_q.split():
            if len(w) > 3 and w in label_lower:
                pick_is_home = True
                break
        if pick_is_home is None:
            for w in away_q.split():
                if len(w) > 3 and w in label_lower:
                    pick_is_home = False
                    break
        if pick_is_home is None:
            if 'home' in label_lower:
                pick_is_home = True
            elif 'away' in label_lower:
                pick_is_home = False
        if pick_is_home is None:
            _novig_cache[cache_key] = {'prob': None, 'ts': time.time()}
            return None

        for ev in events:
            home = ev.get('home_team', '').lower()
            away = ev.get('away_team', '').lower()
            hm = any(w in home for w in home_q.split() if len(w) > 3)
            am = any(w in away for w in away_q.split() if len(w) > 3)
            if not (hm and am):
                continue
            for bk in ev.get('bookmakers', []):
                if bk.get('key') != 'pinnacle':
                    continue
                for mkt in bk.get('markets', []):
                    if mkt.get('key') != 'h2h':
                        continue
                    outcomes = mkt.get('outcomes', [])
                    price_home = price_away = 0.0
                    for out in outcomes:
                        oname = out.get('name', '').lower()
                        price = float(out.get('price', 0) or 0)
                        if not price:
                            continue
                        is_home_out = any(w in oname for w in home_q.split() if len(w) > 3)
                        is_away_out = any(w in oname for w in away_q.split() if len(w) > 3)
                        if is_home_out:
                            price_home = price
                        elif is_away_out:
                            price_away = price
                    if price_home > 1.0 and price_away > 1.0:
                        raw_h = 1.0 / price_home
                        raw_a = 1.0 / price_away
                        total = raw_h + raw_a
                        if total <= 0:
                            continue
                        prob = round((raw_h / total if pick_is_home else raw_a / total), 4)
                        _novig_cache[cache_key] = {'prob': prob, 'ts': time.time()}
                        log.debug('Pinnacle novig %s [%s]: %.3f', match[:35],
                                  'H' if pick_is_home else 'A', prob)
                        return prob
    except Exception as e:
        log.debug('get_novig_prob error (%s): %s', match[:30], e)

    _novig_cache[cache_key] = {'prob': None, 'ts': time.time()}
    return None

def compute_clv(entry_odds: float, closing_odds: float) -> float:
    """CLV = entry_odds / closing_odds - 1
    Positive = we got better odds than market closed at (we had edge)
    Negative = market was sharper than us
    """
    if not entry_odds or not closing_odds or entry_odds <= 1 or closing_odds <= 1:
        return None
    return round(entry_odds / closing_odds - 1, 4)  # 2026-06-16: corregido signo


def record_closing_for_settled(predictions_file: str):
    """
    For each settled bet without CLV, try to fetch closing odds
    and compute CLV. Updates predictions_log.jsonl in place.
    Called from the settlement loop.
    """
    if not os.path.exists(predictions_file):
        return 0

    try:
        lines = open(predictions_file).readlines()
    except Exception:
        return 0

    updated = 0
    new_lines = []
    for line in lines:
        try:
            entry = json.loads(line.strip())
        except Exception:
            new_lines.append(line)
            continue

        # Only process settled bets that don't have CLV yet
        if entry.get('result') and entry.get('clv') is None:
            entry_odds  = entry.get('odds', 0)
            match       = entry.get('match', '')
            sport       = entry.get('sport', 'tennis')
            ts          = entry.get('ts', '')
            market_type = entry.get('market_type', '')
            label       = entry.get('label', '')
            event_id    = entry.get('event_id', '')

            closing = _fetch_closing_odds_local(event_id, market_type, label)
            if not closing:
                closing = _fetch_closing_odds(match, sport, ts)
            clv = compute_clv(float(entry_odds), closing) if closing else None
            if clv is not None:
                entry['clv'] = clv
                entry['closing_odds'] = closing
                updated += 1
                log.debug('CLV recorded: %s | entry=%.2f close=%.2f clv=%+.1f%%',
                          match[:30], entry_odds, closing, clv * 100)

        new_lines.append(json.dumps(entry) + '\n')

    if updated:
        with open(predictions_file, 'w') as f:
            f.writelines(new_lines)
        log.info('CLV: updated %d bets', updated)

    return updated


def get_clv_summary(predictions_file: str, days: int = 30) -> dict:
    """
    Compute CLV statistics from predictions log.
    Returns dict with avg_clv, n_bets, by_sport, trend.
    """
    if not os.path.exists(predictions_file):
        return {}

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    bets = []
    try:
        for line in open(predictions_file):
            try:
                e = json.loads(line.strip())
            except Exception:
                continue
            if e.get('ts', '') < cutoff:
                continue
            if e.get('clv') is not None:
                bets.append(e)
    except Exception:
        return {}

    if not bets:
        return {'n': 0, 'avg_clv': None, 'msg': 'No CLV data yet'}

    clvs = [b['clv'] for b in bets]
    avg  = sum(clvs) / len(clvs)
    pos  = sum(1 for c in clvs if c > 0)

    # By sport
    by_sport = {}
    for b in bets:
        sp = b.get('sport', '?')
        if sp not in by_sport:
            by_sport[sp] = []
        by_sport[sp].append(b['clv'])

    sport_summary = {}
    for sp, vals in by_sport.items():
        sport_summary[sp] = {'avg': round(sum(vals)/len(vals)*100, 1), 'n': len(vals)}

    # Last 10 trend
    last10 = clvs[-10:] if len(clvs) >= 10 else clvs
    trend  = sum(last10) / len(last10) if last10 else 0

    # Edge assessment
    if avg > 0.05:
        assessment = 'EDGE REAL FUERTE — escalar bankroll'
    elif avg > 0.03:
        assessment = 'Edge real — mantener estrategia'
    elif avg > 0.00:
        assessment = 'Edge marginal — monitorear'
    elif avg > -0.02:
        assessment = 'Sin edge claro — revisar modelo'
    else:
        assessment = 'EDGE NEGATIVO — pausar y revisar'

    return {
        'n':          len(bets),
        'avg_clv':    round(avg * 100, 2),
        'avg_clv_raw': avg,
        'pos_pct':    round(pos / len(bets) * 100, 1),
        'trend_10':   round(trend * 100, 2),
        'by_sport':   sport_summary,
        'assessment': assessment,
        'days':       days,
    }


def format_clv_telegram(predictions_file: str) -> str:
    """Format CLV summary for Telegram /clv command."""
    s = get_clv_summary(predictions_file, days=30)

    if not s or s.get('n', 0) == 0:
        # Show what we have in the log without CLV yet
        total_bets = 0
        try:
            total_bets = sum(1 for l in open(predictions_file)
                             if json.loads(l.strip()).get('result'))
        except Exception:
            pass
        return ('*CLV Tracker*\n'
                'Aun no hay datos de closing odds.\n'
                '%d bets liquidados esperando closing odds de Pinnacle.\n\n'
                'El tracker se activa automaticamente en el proximo ciclo.' % total_bets)

    lines = ['*CLV — Closing Line Value* (ultimos %d dias)' % s['days'], '']

    # Main number
    clv_emoji = '🟢' if s['avg_clv'] > 3 else '🟡' if s['avg_clv'] > 0 else '🔴'
    lines.append('%s Avg CLV: *%+.1f%%*' % (clv_emoji, s['avg_clv']))
    lines.append('Bets con CLV: %d | Positivos: %.0f%%' % (s['n'], s['pos_pct']))
    lines.append('Tendencia (ult 10): *%+.1f%%*' % s['trend_10'])
    lines.append('')

    # By sport
    lines.append('*Por deporte:*')
    for sp, d in sorted(s['by_sport'].items()):
        em = '🟢' if d['avg'] > 3 else '🟡' if d['avg'] > 0 else '🔴'
        lines.append('  %s %-10s %+.1f%% (%d bets)' % (em, sp, d['avg'], d['n']))
    lines.append('')

    # Assessment
    lines.append('*Diagnostico:*')
    lines.append(s['assessment'])
    lines.append('')
    lines.append('_CLV > 3% = edge probado. CLV < 0% = revisar modelo._')

    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    pf = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
    print('Procesando CLV para bets liquidados...')
    n = record_closing_for_settled(pf)
    print('Actualizados: %d bets' % n)
    s = get_clv_summary(pf)
    if s.get('n', 0) > 0:
        print('Avg CLV: %+.1f%% (%d bets)' % (s['avg_clv'], s['n']))
        print('Assessment:', s['assessment'])
    else:
        print('Sin datos CLV aun (closing odds no disponibles para bets pasados)')

class CLVOracle:
    """
    Clase wrapper para el runner: fetch + resolve CLV por pick.
    Usa las funciones existentes del modulo como backend.
    """

    def __init__(self, odds_api_key=None, rlm_tracker=None):
        self._api_key = odds_api_key
        self._rlm = rlm_tracker
        self._cache = {}

    def fetch_and_record(self, picks=None, predictions_file=None):
        """Registra closing odds para picks ya asentados. Retorna conteo."""
        try:
            if predictions_file:
                return record_closing_for_settled(predictions_file)
            return 0
        except Exception:
            return 0

    def resolve_clv_for_pick(self, pick, entry_odds=None, settled_ts=None):
        """
        Obtiene closing odds para un pick ya resuelto.
        Retorna dict con closing_odds_betfair/pinnacle y clv_betfair/pinnacle.
        """
        try:
            match  = pick.get('match', '')
            sport  = pick.get('sport', 'tennis')
            ts_str = pick.get('ts') or ''
            closing = _fetch_closing_odds(match, sport, ts_str)
            if not closing or closing <= 1:
                return {}
            clv = compute_clv(float(entry_odds or 0), closing) if entry_odds else None
            return {
                'closing_odds_betfair':  closing,
                'closing_odds_pinnacle': None,
                'clv_betfair':           clv,
                'clv_pinnacle':          None,
                'clv_source':            'betfair',
            }
        except Exception:
            return {}

    def clv_quality_label(self, clv):
        """Etiqueta de calidad CLV para logs/Telegram."""
        if clv is None:
            return ''
        if clv >= 0.05:
            return 'SHARP'
        if clv >= 0.02:
            return 'GOOD'
        if clv >= 0:
            return 'FLAT'
        return 'NEGATIVE'

    def record_cloudbet_clv(self, api, active_bets, sibila_db_path):
        """
        For each active bet, fetch current Cloudbet odds and store in Sibila.
        Called every scan cycle. Last snapshot before cutoff = closing odds proxy.
        CLV = entry_odds / current_odds - 1 (positive = we got better price than now).
        """
        import sqlite3 as _sq3, json as _json, os as _os
        if not active_bets:
            return
        # Load Cloudbet API key for authenticated requests
        try:
            _cfg_path = _os.path.join(_os.path.dirname(__file__), 'cloudbet_config.json')
            _cb_key = _json.load(open(_cfg_path)).get('api_key', '')
        except Exception:
            _cb_key = ''
        if not _cb_key:
            return
        updated = 0
        try:
            con = _sq3.connect(sibila_db_path, timeout=10)
            cur = con.cursor()
            for bet in active_bets:
                murl     = bet.get('market_url', '')
                entry_o  = float(bet.get('odds', 0) or 0)
                bet_id   = bet.get('bet_id', '')
                event_id = bet.get('event_id', '')
                if not murl or entry_o <= 1 or not event_id:
                    continue
                # Fetch current odds via event endpoint (requires auth)
                try:
                    import requests as _req
                    r = _req.get(
                        f'https://sports-api.cloudbet.com/pub/v2/odds/events/{event_id}',
                        headers={'accept': 'application/json', 'X-API-Key': _cb_key},
                        timeout=8)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    # Traverse: markets[market_key] -> submarkets[period] -> selections
                    market_key = murl.split('/')[0]
                    current_price = 0.0
                    mkt = data.get('markets', {}).get(market_key, {})
                    for sub in mkt.get('submarkets', {}).values():
                        for sel in sub.get('selections', []):
                            if sel.get('marketUrl', '') == murl:
                                current_price = float(sel.get('price', 0) or 0)
                                break
                        if current_price:
                            break
                    if current_price <= 1:
                        continue
                    # CLV: entry / current - 1  (positive = we beat the line)
                    clv = entry_o / current_price - 1.0
                    # Update Sibila closing_odds + clv for this bet
                    if bet_id:
                        cur.execute(
                            'UPDATE sibila_picks SET closing_odds=?, clv=? WHERE bet_id=?',
                            (round(current_price, 3), round(clv, 4), bet_id))
                    else:
                        cur.execute(
                            'UPDATE sibila_picks SET closing_odds=?, clv=? '
                            'WHERE placed=1 AND result IS NULL '
                            'AND ABS(odds - ?) < 0.01 ORDER BY ts DESC LIMIT 1',
                            (round(current_price, 3), round(clv, 4), entry_o))
                    updated += 1
                except Exception:
                    continue
            con.commit()
            con.close()
        except Exception as e:
            log.debug('CLV cloudbet record error: %s', e)
        if updated:
            log.info('[CLV] Updated %d active bets with Cloudbet closing odds', updated)

    def record_shadow_clv(self, api, sibila_db_path):
        """2026-06-18: CLV para picks SHADOW abiertos (placed=0) -> cantera medible.
        Espeja record_cloudbet_clv pero lee filas del DB, agrupa por event_id (1 call
        por evento), re-captura cada ciclo (ultima antes de resolver = cierre proxy).
        Acotado a picks de los ultimos 5 dias para limitar carga API."""
        import sqlite3 as _sq3, json as _json, os as _os
        from datetime import datetime as _dt, timedelta as _td
        try:
            _cfg_path = _os.path.join(_os.path.dirname(__file__), 'cloudbet_config.json')
            _cb_key = _json.load(open(_cfg_path)).get('api_key', '')
        except Exception:
            _cb_key = ''
        if not _cb_key:
            return
        _cutoff = (_dt.utcnow() - _td(days=5)).isoformat()
        updated = 0
        try:
            con = _sq3.connect(sibila_db_path, timeout=10)
            cur = con.cursor()
            cur.execute(
                "SELECT id, event_id, market_url, odds FROM sibila_picks "
                "WHERE (placed=0 OR placed IS NULL) AND result IS NULL "
                "AND event_id IS NOT NULL AND event_id!='' "
                "AND market_url IS NOT NULL AND market_url!='' AND ts > ?", (_cutoff,))
            rows = cur.fetchall()
            by_ev = {}
            for _id, eid, murl, odds in rows:
                by_ev.setdefault(eid, []).append((_id, murl, float(odds or 0)))
            import requests as _req
            for eid, picks in by_ev.items():
                try:
                    r = _req.get(
                        f'https://sports-api.cloudbet.com/pub/v2/odds/events/{eid}',
                        headers={'accept': 'application/json', 'X-API-Key': _cb_key},
                        timeout=8)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    # Guard pre-game: una vez iniciado el evento los precios son in-play
                    # (F5 ML se mueve violento con cada carrera) -> seria basura, no cierre.
                    _ct = str(data.get("cutoffTime", "") or "")
                    if _ct:
                        try:
                            _ko = _dt.fromisoformat(_ct.replace("Z", "+00:00"))
                            if _dt.now(_ko.tzinfo) >= _ko:
                                continue
                        except Exception:
                            pass
                    price_map = {}
                    for mkt in data.get('markets', {}).values():
                        for sub in mkt.get('submarkets', {}).values():
                            for sel in sub.get('selections', []):
                                mu = sel.get('marketUrl', '')
                                if mu:
                                    price_map[mu] = float(sel.get('price', 0) or 0)
                    for _id, murl, entry_o in picks:
                        cp = price_map.get(murl, 0.0)
                        if cp <= 1 or entry_o <= 1:
                            continue
                        clv = entry_o / cp - 1.0
                        cur.execute(
                            'UPDATE sibila_picks SET closing_odds=?, clv=? WHERE id=?',
                            (round(cp, 3), round(clv, 4), _id))
                        updated += 1
                except Exception:
                    continue
            con.commit()
            con.close()
        except Exception as e:
            log.debug('Shadow CLV record error: %s', e)
        if updated:
            log.info('[CLV] Updated %d shadow picks with closing odds', updated)
