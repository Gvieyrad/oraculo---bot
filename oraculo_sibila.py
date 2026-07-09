#!/usr/bin/env python3
"""
oraculo_sibila.py — Libro Sombra (Shadow Book)
===============================================
Registra TODOS los picks que califican (edge + conf) sin limites de bankroll.
Permite evaluar el modelo independiente del ruido de gestion de capital.

Tres metricas clave:
  1. Calibracion  -- cuando modelo dice 70%, gana 70% de las veces?
  2. CLV          -- nuestros odds son mejores que el cierre del mercado?
  3. ROI simulado -- que bankroll tendriamos sin restricciones?

Bankroll virtual: $1000 inicial, Kelly cuarto (0.25), max 5% por apuesta.
"""
import os, json, sqlite3, logging
from datetime import datetime, timedelta

log = logging.getLogger('oraculo.sibila')

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SIBILA_DB   = os.path.join(SCRIPT_DIR, 'sibila.db')
VIRTUAL_BANKROLL_START = 1000.0
KELLY_FRAC  = 0.25   # quarter Kelly
MAX_BET_PCT = 0.05   # max 5% of virtual bankroll per bet


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def _get_conn():
    conn = sqlite3.connect(SIBILA_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sibila_picks (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               TEXT NOT NULL,
        sport            TEXT,
        match            TEXT,
        league           TEXT,
        surface          TEXT,
        level            TEXT,
        market           TEXT,
        side             TEXT,
        prob_model       REAL,
        prob_book        REAL,
        edge             REAL,
        confidence       REAL,
        odds             REAL,
        shadow_stake     REAL,
        shadow_br_before REAL,
        closing_odds     REAL,
        clv              REAL,
        result           TEXT,
        pnl              REAL,
        resolved_ts      TEXT,
        placed           INTEGER DEFAULT 0,
        real_stake       REAL,
        bet_id           TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_sibila_ts     ON sibila_picks(ts);
    CREATE INDEX IF NOT EXISTS ix_sibila_sport  ON sibila_picks(sport);
    CREATE INDEX IF NOT EXISTS ix_sibila_match  ON sibila_picks(match);
    CREATE INDEX IF NOT EXISTS ix_sibila_bet_id ON sibila_picks(bet_id);
    CREATE TABLE IF NOT EXISTS sibila_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    cur = conn.execute("SELECT value FROM sibila_meta WHERE key='virtual_bankroll'")
    if cur.fetchone() is None:
        conn.execute("INSERT INTO sibila_meta VALUES ('virtual_bankroll', ?)",
                     (str(VIRTUAL_BANKROLL_START),))
    conn.commit()
    conn.close()


_init_db()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_virtual_bankroll():
    conn = _get_conn()
    row  = conn.execute("SELECT value FROM sibila_meta WHERE key='virtual_bankroll'").fetchone()
    conn.close()
    return float(row['value']) if row else VIRTUAL_BANKROLL_START


def _set_virtual_bankroll(br):
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO sibila_meta VALUES ('virtual_bankroll', ?)",
                 (str(round(br, 4)),))
    conn.commit()
    conn.close()


def _kelly_stake(edge, odds, bankroll):
    """Quarter Kelly, capped at MAX_BET_PCT of bankroll."""
    if not edge or edge <= 0 or not odds or odds <= 1:
        return 0.0
    full_kelly = edge / (odds - 1.0)
    stake = full_kelly * KELLY_FRAC * bankroll
    return round(min(stake, MAX_BET_PCT * bankroll), 2)


def _classify_level(pick):
    """Infer tournament level from league/match string."""
    league = (pick.get('league') or '').lower()
    sport  = (pick.get('sport') or '').lower()
    if sport == 'baseball':
        return 'mlb'
    if sport == 'soccer':
        return 'football'
    if 'grand slam' in league or any(x in league for x in ['australian', 'roland', 'wimbledon', 'us open']):
        return 'grand_slam'
    if 'atp1000' in league or 'masters' in league or 'madrid' in league or 'rome' in league:
        return 'atp1000'
    if 'atp500' in league or '500' in league:
        return 'atp500'
    if 'atp250' in league or '250' in league:
        return 'atp250'
    if 'challenger' in league:
        return 'challenger'
    if 'wta' in league:
        return 'wta'
    return 'other'


def _classify_market(label, sport):
    """Normalize market label to clean category."""
    lbl = (label or '').lower()
    if '[fade' in lbl:
        return 'mlb_f5_ml_fade'
    if '[counter' in lbl:
        return 'mlb_f5_ou_counter'
    if 'under' in lbl and 'set' in lbl:
        return 'sets_under'
    if 'over' in lbl and 'set' in lbl:
        return 'sets_over'
    if 'winner' in lbl or 'moneyline' in lbl or 'h2h' in lbl:
        return 'match_winner'
    if 'handicap' in lbl or ' ah' in lbl:
        return 'asian_handicap'
    if 'btts' in lbl:
        return 'btts'
    if '1x2' in lbl or 'result' in lbl:
        return 'result_1x2'
    if 'over 3.5' in lbl or 'o3.5' in lbl:
        return 'over35'
    if 'over 4.5' in lbl or 'o4.5' in lbl:
        return 'over45'
    if 'over' in lbl or 'under' in lbl:
        return 'over_under'
    return 'other'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_pick(pick: dict, placed: bool = False, real_stake: float = None, bet_id: str = None):
    """
    Record a qualifying pick to the shadow book.
    Call for EVERY pick that passes edge+conf -- not just placed ones.
    """
    try:
        odds       = float(pick.get('price') or pick.get('odds') or 0)
        edge       = float(pick.get('edge') or 0)
        prob_model = float(pick.get('model_prob') or 0)
        prob_book  = round(1.0 / odds, 4) if odds > 1 else 0.0
        conf       = float(pick.get('confidence') or prob_model)
        sport      = (pick.get('sport') or 'tennis').lower()
        surface    = (pick.get('surface') or '').lower() or None
        label      = pick.get('label') or ''

        vbr   = _get_virtual_bankroll()
        stake = _kelly_stake(edge, odds, vbr)

        conn = _get_conn()
        _mkt_type = pick.get('market_type') or _classify_market(label, sport)
        _event_id = pick.get('event_id') or pick.get('eid') or ''
        _mkt_url  = pick.get('market_url') or ''
        # Dedup: skip if same event+market already recorded (shadow picks only)
        if not placed:
            if _event_id and _mkt_url:
                _exists = conn.execute(
                    "SELECT 1 FROM sibila_picks WHERE event_id=? AND market_url=? AND placed=0 LIMIT 1",
                    (_event_id, _mkt_url)
                ).fetchone()
            else:
                # 2026-06-16: fallback dedup sin event_id/url (evita re-insertar mismo pick cada scan)
                _exists = conn.execute(
                    "SELECT 1 FROM sibila_picks WHERE match=? AND side=? AND market_type=? AND placed=0 AND date(ts)=date('now') LIMIT 1",
                    (pick.get('match') or '', label, _mkt_type)
                ).fetchone()
            if _exists:
                conn.close()
                return
            # 2026-07-06: si ya hay una apuesta REAL activa en este evento (mismo event_id),
            # no sumar mas ruido shadow -- distintos scanners pueden re-encontrar el mismo
            # partido bajo otro market_url/market_type (ver caso Mexico vs England 2026-07-04)
            if _event_id:
                _real_exists = conn.execute(
                    "SELECT 1 FROM sibila_picks WHERE event_id=? AND placed=1 LIMIT 1",
                    (_event_id,)
                ).fetchone()
                if _real_exists:
                    conn.close()
                    return
        conn.execute("""
            INSERT INTO sibila_picks
              (ts, sport, match, league, surface, level, market, side,
               prob_model, prob_book, edge, confidence, odds,
               shadow_stake, shadow_br_before,
               placed, real_stake, bet_id,
               market_type, event_id, market_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            sport,
            pick.get('match') or '',
            pick.get('league') or '',
            surface,
            _classify_level(pick),
            _classify_market(label, sport),
            label,
            round(prob_model, 4),
            prob_book,
            round(edge, 4),
            round(conf, 4),
            round(odds, 4),
            stake,
            round(vbr, 2),
            1 if placed else 0,
            round(real_stake, 4) if real_stake else None,
            bet_id,
            _mkt_type,
            str(_event_id),
            _mkt_url,
        ))
        conn.commit()
        conn.close()
        log.debug('Sibila recorded: %s | edge=%.1f%% @%.2f stake=$%.2f',
                  (pick.get('match') or '?')[:30], edge * 100, odds, stake)
    except Exception as e:
        log.debug('Sibila record_pick error: %s', e)


def mark_placed(match: str, label: str, bet_id: str, real_stake: float):
    """Update a shadow pick to mark it was actually placed as a real bet."""
    try:
        conn = _get_conn()
        conn.execute("""
            UPDATE sibila_picks SET placed=1, bet_id=?, real_stake=?
            WHERE match=? AND side=? AND result IS NULL
            ORDER BY ts DESC LIMIT 1
        """, (bet_id, real_stake, match, label))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug('Sibila mark_placed error: %s', e)


def resolve_pick(bet_id: str = None, match: str = None, label: str = None,
                 result: str = None, closing_odds: float = None):
    """
    Mark a shadow pick as resolved.
    result: 'WIN', 'LOSS', or 'VOID'
    Matches by bet_id first, then match+label.
    Updates virtual bankroll with Kelly P&L.
    """
    try:
        conn = _get_conn()
        if bet_id:
            row = conn.execute(
                "SELECT * FROM sibila_picks WHERE bet_id=? AND result IS NULL LIMIT 1",
                (bet_id,)).fetchone()
        elif match and label:
            row = conn.execute(
                "SELECT * FROM sibila_picks WHERE match=? AND side=? AND result IS NULL "
                "ORDER BY ts DESC LIMIT 1",
                (match, label)).fetchone()
        else:
            conn.close()
            return

        if not row:
            conn.close()
            return

        rid    = row['id']
        odds   = row['odds'] or 1.0
        stake  = row['shadow_stake'] or 0.0
        result_norm = (result or '').upper()

        if result_norm == 'WIN':
            pnl = round(stake * (odds - 1.0), 4)
        elif result_norm == 'LOSS':
            pnl = round(-stake, 4)
        elif result_norm == 'HALF_WIN':   # 2026-06-16: handicaps asiaticos medio-ganados
            pnl = round(stake * (odds - 1.0) / 2.0, 4)
        elif result_norm == 'HALF_LOSS':
            pnl = round(-stake / 2.0, 4)
        else:
            pnl = 0.0
            result_norm = 'VOID'

        clv = None
        if closing_odds and closing_odds > 1 and odds > 1:
            clv = round(odds / closing_odds - 1.0, 4)  # 2026-06-16: entry/closing

        vbr_before = row['shadow_br_before'] or _get_virtual_bankroll()
        new_br = _get_virtual_bankroll() + pnl  # 2026-06-16: balance actual, no snapshot stale
        _set_virtual_bankroll(new_br)

        conn.execute("""
            UPDATE sibila_picks
            SET result=?, pnl=?, resolved_ts=?,
                closing_odds=COALESCE(?, closing_odds),
                clv=COALESCE(?, clv)
            WHERE id=?
        """, (result_norm, pnl, datetime.now().isoformat(), closing_odds, clv, rid))
        conn.commit()
        conn.close()
        log.debug('Sibila resolved id=%d: %s -> %s pnl=$%.2f vbr=$%.2f',
                  rid, (match or bet_id or '?')[:30], result_norm, pnl, new_br)
    except Exception as e:
        log.debug('Sibila resolve error: %s', e)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def get_stats(days: int = 30) -> dict:
    """Compute full Sibila stats. Returns dict."""
    cutoff    = (datetime.now() - timedelta(days=days)).isoformat()
    conn      = _get_conn()
    all_picks = [dict(r) for r in conn.execute(
        "SELECT * FROM sibila_picks WHERE ts >= ?", (cutoff,)).fetchall()]
    conn.close()

    if not all_picks:
        return {'n_picks': 0, 'n_resolved': 0}

    resolved = [p for p in all_picks if p.get('result') in ('WIN', 'LOSS', 'VOID')]
    wins     = [p for p in resolved if p['result'] == 'WIN']
    losses   = [p for p in resolved if p['result'] == 'LOSS']
    n_wl     = len(wins) + len(losses)

    total_pnl     = sum(p['pnl'] or 0 for p in resolved)
    total_wagered = sum(p['shadow_stake'] or 0 for p in resolved if p['result'] != 'VOID')
    roi  = total_pnl / total_wagered if total_wagered > 0 else None
    wr   = len(wins) / n_wl if n_wl > 0 else None
    vbr  = _get_virtual_bankroll()

    clv_picks = [p for p in resolved if p.get('clv') is not None]
    avg_clv   = sum(p['clv'] for p in clv_picks) / len(clv_picks) if clv_picks else None
    avg_edge  = sum(p['edge'] or 0 for p in resolved) / len(resolved) if resolved else None

    # Segment helper
    def _segment_stats(items):
        d = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'wagered': 0.0, 'clv_sum': 0.0, 'clv_n': 0}
        for p in items:
            if p['result'] == 'WIN':    d['wins'] += 1
            elif p['result'] == 'LOSS': d['losses'] += 1
            d['pnl']    += p['pnl'] or 0
            d['wagered'] += p['shadow_stake'] or 0
            if p.get('clv') is not None:
                d['clv_sum'] += p['clv']
                d['clv_n']   += 1
        n = d['wins'] + d['losses']
        return {
            'n':       n,
            'wr':      round(d['wins'] / n * 100, 1) if n > 0 else None,
            'roi':     round(d['pnl'] / d['wagered'] * 100, 1) if d['wagered'] > 0 else None,
            'avg_clv': round(d['clv_sum'] / d['clv_n'] * 100, 2) if d['clv_n'] > 0 else None,
        }

    by_sport   = {}
    by_market  = {}
    by_level   = {}
    for p in resolved:
        by_sport.setdefault(p.get('sport') or 'unknown', []).append(p)
        by_market.setdefault(p.get('market') or 'unknown', []).append(p)
        if p.get('sport') == 'tennis':
            by_level.setdefault(p.get('level') or 'other', []).append(p)

    sport_stats  = {k: _segment_stats(v) for k, v in by_sport.items()}
    market_stats = {k: _segment_stats(v) for k, v in by_market.items()}
    level_stats  = {k: _segment_stats(v) for k, v in by_level.items()}

    # Calibration buckets (5% bands)
    buckets = {}
    for p in resolved:
        bkt = int((p.get('prob_model') or 0) * 10) * 10
        buckets.setdefault(bkt, {'n': 0, 'wins': 0})
        buckets[bkt]['n'] += 1
        if p['result'] == 'WIN':
            buckets[bkt]['wins'] += 1
    calibration = []
    for bkt in sorted(buckets):
        d = buckets[bkt]
        if d['n'] >= 3:
            aw = d['wins'] / d['n']
            calibration.append({
                'band':       '%d-%d%%' % (bkt, bkt + 10),
                'model_pct':  bkt + 5,
                'actual_pct': round(aw * 100, 1),
                'n':          d['n'],
                'delta':      round((aw - (bkt + 5) / 100) * 100, 1),
            })

    # Trend last 10
    last10    = sorted(resolved, key=lambda x: x.get('resolved_ts') or '', reverse=True)[:10]
    t_pnl     = sum(p['pnl'] or 0 for p in last10)
    t_wagered = sum(p['shadow_stake'] or 0 for p in last10)
    trend_roi = round(t_pnl / t_wagered * 100, 1) if t_wagered > 0 else None

    return {
        'days':             days,
        'n_picks':          len(all_picks),
        'n_resolved':       len(resolved),
        'n_pending':        len(all_picks) - len(resolved),
        'wins':             len(wins),
        'losses':           len(losses),
        'wr':               round(wr * 100, 1) if wr is not None else None,
        'total_pnl':        round(total_pnl, 2),
        'total_wagered':    round(total_wagered, 2),
        'roi':              round(roi * 100, 1) if roi is not None else None,
        'avg_edge':         round(avg_edge * 100, 1) if avg_edge is not None else None,
        'avg_clv':          round(avg_clv * 100, 2) if avg_clv is not None else None,
        'virtual_bankroll': round(vbr, 2),
        'vbr_pct':          round((vbr / VIRTUAL_BANKROLL_START - 1) * 100, 1),
        'trend_roi_last10': trend_roi,
        'by_sport':         sport_stats,
        'by_market':        market_stats,
        'by_level':         level_stats,
        'calibration':      calibration,
    }


def clv_report(window: int = 20, min_picks: int = 5) -> dict:
    """
    Rolling CLV health check per market_type, over real-money bets only
    (placed=1) -- catches a live model losing its edge vs the closing line.
    Returns {'alerts': [str, ...], 'segments': {market_type: {'n', 'avg_clv'}}}.
    An alert fires when the average CLV of the last `window` resolved picks
    for a market_type drops below -3%, with at least `min_picks` samples.
    """
    conn = _get_conn()
    try:
        market_types = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_type FROM sibila_picks "
            "WHERE market_type IS NOT NULL AND market_type != '' AND placed=1"
        ).fetchall()]

        alerts, segments = [], {}
        for mt in market_types:
            rows = conn.execute("""
                SELECT clv FROM sibila_picks
                WHERE market_type=? AND placed=1 AND result IS NOT NULL AND clv IS NOT NULL
                ORDER BY COALESCE(resolved_ts, ts) DESC
                LIMIT ?
            """, (mt, window)).fetchall()
            clvs = [r[0] for r in rows]
            if len(clvs) < min_picks:
                continue
            avg_clv = sum(clvs) / len(clvs)
            segments[mt] = {'n': len(clvs), 'avg_clv': round(avg_clv, 4)}
            if avg_clv < -0.03:
                alerts.append('%s: CLV promedio %.1f%% (n=%d, ultimas %d picks)'
                               % (mt, avg_clv * 100, len(clvs), window))
        return {'alerts': alerts, 'segments': segments}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Telegram formatter
# ---------------------------------------------------------------------------

def format_telegram(days: int = 30) -> str:
    s = get_stats(days)

    if s.get('n_picks', 0) == 0:
        return ('*Sibila -- Libro Sombra*\n'
                'Aun sin picks registrados.\n'
                'Se activa automaticamente en el proximo ciclo.')

    lines = ['*Sibila -- Libro Sombra* (ultimos %d dias)' % days, '']

    vbr  = s['virtual_bankroll']
    vpct = s['vbr_pct']
    em   = 'VERDE' if vpct > 5 else 'AMARILLO' if vpct > 0 else 'ROJO'
    lines.append('Bankroll virtual: *$%.0f* (%+.1f%% desde $1000) [%s]' % (vbr, vpct, em))
    lines.append('')

    nr   = s['n_resolved']
    np_  = s['n_pending']
    wr   = s.get('wr')
    roi  = s.get('roi')
    lines.append('*%d picks* | %d resueltos | %d pendientes' % (s['n_picks'], nr, np_))
    lines.append('Win rate: *%s* | ROI simulado: *%s*' % (
        ('%.0f%%' % wr) if wr is not None else 'N/A',
        ('%+.1f%%' % roi) if roi is not None else 'N/A'))
    if s.get('avg_edge'):
        lines.append('Edge promedio: %.1f%%' % s['avg_edge'])
    if s.get('trend_roi_last10') is not None:
        dir_ = '+' if s['trend_roi_last10'] > 0 else ''
        lines.append('Tendencia ult 10: %s%.1f%%' % (dir_, s['trend_roi_last10']))
    lines.append('')

    if s['by_sport']:
        lines.append('*Por deporte:*')
        for sp, d in sorted(s['by_sport'].items(), key=lambda x: -(x[1].get('roi') or -99)):
            if (d.get('n') or 0) < 2:
                continue
            r = d.get('roi')
            wr2 = ('WR %.0f%%' % d['wr']) if d.get('wr') else ''
            lines.append('  %-10s %s  ROI %s  (%d)' % (
                sp, wr2, ('%+.1f%%' % r) if r is not None else '?', d.get('n', 0)))
        lines.append('')

    if s['by_market']:
        lines.append('*Por mercado:*')
        for mk, d in sorted(s['by_market'].items(), key=lambda x: -(x[1].get('roi') or -99)):
            if (d.get('n') or 0) < 2:
                continue
            r = d.get('roi')
            lines.append('  %-18s ROI %s  (%d)' % (
                mk, ('%+.1f%%' % r) if r is not None else '?', d.get('n', 0)))
        lines.append('')

    if s['by_level']:
        lines.append('*Tennis por nivel:*')
        for lv, d in sorted(s['by_level'].items(), key=lambda x: -(x[1].get('roi') or -99)):
            if (d.get('n') or 0) < 2:
                continue
            r = d.get('roi')
            lines.append('  %-12s ROI %s  (%d)' % (
                lv, ('%+.1f%%' % r) if r is not None else '?', d.get('n', 0)))
        lines.append('')

    if s['calibration']:
        lines.append('*Calibracion modelo:*')
        for c in s['calibration']:
            delta = c['delta']
            tag   = 'OK' if abs(delta) <= 5 else '~' if abs(delta) <= 10 else 'MAL'
            lines.append('  [%s] %s -> modelo %.0f%% | real %.0f%% (n=%d)' % (
                tag, c['band'], c['model_pct'], c['actual_pct'], c['n']))
        lines.append('')

    if s.get('avg_clv') is not None:
        lines.append('CLV promedio: *%+.1f%%*' % s['avg_clv'])

    lines.append('')
    lines.append('_Sibila observa todo. Aprende de lo que el bankroll no puede ver._')
    return '\n'.join(lines)


def resolve_fade_shadow_picks(match: str, bet_team: str, result: str,
                              closing_odds: float = None) -> int:
    """
    Resolve mlb_f5_ml_fade shadow picks for a given match.
    bet_team: the team that the real bet was placed on.
    result: WIN/LOSS/VOID for bet_team.
    Logic: if fade label shows bet_team as OPP_TEAM → same result;
           if fade label shows bet_team as FADE_TEAM → inverted result.
    Virtual bankroll NOT updated — fade picks are observation-only.
    """
    import re as _re
    _inv = {'WIN': 'LOSS', 'LOSS': 'WIN', 'VOID': 'VOID'}
    result_norm = (result or '').upper()
    if result_norm not in ('WIN', 'LOSS', 'VOID'):
        result_norm = 'VOID'
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM sibila_picks "
            "WHERE match=? AND side LIKE 'F5 ML [FADE%' AND placed=0 AND result IS NULL",
            (match,)).fetchall()
        if not rows:
            conn.close()
            return 0
        resolved = 0
        now = datetime.now().isoformat()
        for row in rows:
            label = row['side'] or ''
            m = _re.search(r'\[FADE vs (.+?)\]: (.+)', label)
            if not m:
                continue
            fade_team = m.group(1).strip()
            opp_team  = m.group(2).strip()
            bt = (bet_team or '').strip()
            if bt == opp_team:
                pick_result = result_norm
            elif bt == fade_team:
                pick_result = _inv.get(result_norm, 'VOID')
            else:
                continue
            odds  = row['odds'] or 1.0
            stake = row['shadow_stake'] or 0.0
            if pick_result == 'WIN':
                pnl = round(stake * (odds - 1.0), 4)
            elif pick_result == 'LOSS':
                pnl = round(-stake, 4)
            else:
                pnl = 0.0
            clv = None
            if closing_odds and closing_odds > 1 and odds > 1:
                clv = round(odds / closing_odds - 1.0, 4)  # 2026-06-16: entry/closing
            conn.execute(
                "UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=?, "
                "closing_odds=COALESCE(closing_odds, ?), clv=COALESCE(clv, ?) WHERE id=?",
                (pick_result, pnl, now, closing_odds, clv, row['id']))
            resolved += 1
        if resolved:
            conn.commit()
            log.debug('Fade shadow resolved: %d picks for %s', resolved, match[:30])
        conn.close()
        return resolved
    except Exception as e:
        log.debug('resolve_fade_shadow_picks error: %s', e)
        return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    s = get_stats(days=90)
    if s.get('n_picks', 0) == 0:
        print('Sibila: sin picks aun. Se registran en el proximo ciclo.')
    else:
        print('Sibila Stats:')
        print('  Picks: %d | Resueltos: %d' % (s['n_picks'], s['n_resolved']))
        if s.get('roi') is not None:
            print('  ROI simulado: %+.1f%%' % s['roi'])
        if s.get('wr') is not None:
            print('  Win rate: %.1f%%' % s['wr'])
        print('  Bankroll virtual: $%.2f (%+.1f%%)' % (s['virtual_bankroll'], s['vbr_pct']))
