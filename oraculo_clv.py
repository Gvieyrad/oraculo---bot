#!/usr/bin/env python3
"""
oraculo_clv.py — Closing Line Value tracker
============================================
CLV = closing_odds / entry_odds - 1

Si CLV promedio > +3%  → edge real, escalar con confianza
Si CLV promedio < 0%   → ganamos por varianza, corregir modelo

Fuente: Trademate Sports methodology + Pinnacle "How to Bet Smarter"

Integración:
  1. _log_prediction() ya guarda entry_odds en predictions_log.jsonl
  2. CLVTracker.record_closing() se llama desde el loop de resultados
  3. /clv en Telegram muestra resumen

Generado en Semana 2 del plan de mejoras Oráculo
"""
import os, json, time, logging
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


def compute_clv(entry_odds: float, closing_odds: float) -> float:
    """CLV = closing_odds / entry_odds - 1
    Positive = we got better odds than market closed at (we had edge)
    Negative = market was sharper than us
    """
    if not entry_odds or not closing_odds or entry_odds <= 1 or closing_odds <= 1:
        return None
    return round(closing_odds / entry_odds - 1, 4)


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
            entry_odds = entry.get('odds', 0)
            match     = entry.get('match', '')
            sport     = entry.get('sport', 'tennis')
            ts        = entry.get('ts', '')

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
        import sqlite3 as _sq3
        if not active_bets:
            return
        updated = 0
        try:
            con = _sq3.connect(sibila_db_path, timeout=10)
            cur = con.cursor()
            for bet in active_bets:
                murl    = bet.get('market_url', '')
                entry_o = float(bet.get('odds', 0) or 0)
                bet_id  = bet.get('bet_id', '')
                if not murl or entry_o <= 1:
                    continue
                # Fetch current odds from Cloudbet
                try:
                    import requests as _req
                    r = _req.get(
                        f'https://sports-api.cloudbet.com/pub/v2/odds/markets?marketUrl={murl}',
                        headers={'accept': 'application/json'},
                        timeout=8)
                    data = r.json()
                    # Extract best price for this selection
                    current_price = 0.0
                    for sub in data.get('submarkets', {}).values():
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
