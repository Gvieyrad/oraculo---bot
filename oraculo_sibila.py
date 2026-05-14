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
# Realistic shadow track (added 2026-05-12)
# Mirrors live Oraculo SPORT_KELLY config. Keep in sync with
# oraculo_runner_auto.py:53-78. Only the FIRST occurrence per
# (match, market, side) carries realistic_stake; duplicates stay NULL.
# ---------------------------------------------------------------------------
REALISTIC_BANKROLL_START = 1000.0
REALISTIC_SPORT_KELLY = {
    'tennis':     0.15,
    'baseball':   0.15,
    'basketball': 0.10,
    'darts':      0.10,
    'soccer':     0.20,
}
REALISTIC_KELLY_FRAC        = 0.25
REALISTIC_MAX_PER_BET       = 0.05
REALISTIC_MIN_STAKE         = 0.50
REALISTIC_LOSS_STREAK_LIMIT = 5
REALISTIC_LOSS_STREAK_FACTOR= 0.50
REALISTIC_CIRCUIT_BREAKER   = 10.0


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


def _get_realistic_bankroll():
    conn = _get_conn()
    row  = conn.execute("SELECT value FROM sibila_meta WHERE key='realistic_bankroll'").fetchone()
    conn.close()
    return float(row['value']) if row else REALISTIC_BANKROLL_START


def _set_realistic_bankroll(br):
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO sibila_meta VALUES ('realistic_bankroll', ?)",
                 (str(round(br, 4)),))
    conn.commit()
    conn.close()


def _realistic_consecutive_losses(conn=None):
    """Count consecutive realistic losses on resolved primary rows."""
    own = False
    if conn is None:
        conn = _get_conn(); own = True
    rows = conn.execute("""
        SELECT result FROM sibila_picks
        WHERE realistic_stake IS NOT NULL AND realistic_stake > 0
          AND result IN ('WIN','LOSS') AND realistic_pnl IS NOT NULL
        ORDER BY resolved_ts DESC LIMIT 10
    """).fetchall()
    if own: conn.close()
    streak = 0
    for r in rows:
        if r['result'] == 'LOSS': streak += 1
        else: break
    return streak


def _realistic_kelly_stake(prob, odds, bk, sport, consecutive_losses=0):
    """Mirror of oraculo_runner_auto.kelly_stake() — sport-specific Kelly fraction."""
    if bk < REALISTIC_CIRCUIT_BREAKER:
        return 0.0
    if not prob or prob <= 0 or not odds or odds <= 1:
        return 0.0
    b = odds - 1
    full_kelly = (b * prob - (1 - prob)) / b
    if full_kelly <= 0:
        return 0.0
    sport_l = (sport or '').lower()
    frac = REALISTIC_SPORT_KELLY.get(sport_l, REALISTIC_KELLY_FRAC)
    kelly = max(0, full_kelly * frac)
    kelly = min(kelly, REALISTIC_MAX_PER_BET)
    stake = bk * kelly
    if consecutive_losses >= REALISTIC_LOSS_STREAK_LIMIT:
        stake *= REALISTIC_LOSS_STREAK_FACTOR
    return round(stake, 2) if stake >= REALISTIC_MIN_STAKE else 0.0


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
    if 'under' in lbl and 'set' in lbl:
        return 'sets_under'
    if 'over' in lbl and 'set' in lbl:
        return 'sets_over'
    # F5 ML: baseball first 5 innings moneyline (label='F5 ML: NYY ...' or market='moneyline_innings_1_to_5')
    if ('f5' in lbl and 'ml' in lbl) or 'innings' in lbl:
        return 'f5_moneyline'
    if 'winner' in lbl or 'moneyline' in lbl or 'h2h' in lbl:
        return 'match_winner'
    if 'handicap' in lbl or ' ah' in lbl:
        return 'asian_handicap'
    if 'btts' in lbl:
        return 'btts'
    if '1x2' in lbl or 'result' in lbl:
        return 'result_1x2'
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
        prob_model = float(pick.get('raw_model_prob_uncal') or pick.get('raw_model_prob') or pick.get('model_prob') or 0)
        prob_book  = round(1.0 / odds, 4) if odds > 1 else 0.0
        conf       = float(pick.get('confidence') or prob_model)
        sport      = (pick.get('sport') or 'tennis').lower()
        surface    = (pick.get('surface') or '').lower() or None
        label      = pick.get('label') or ''

        vbr   = _get_virtual_bankroll()
        stake = _kelly_stake(edge, odds, vbr)

        match_val  = pick.get('match') or ''
        market_val = _classify_market(label, sport)
        side_val   = label

        conn = _get_conn()

        # Dedup: skip if same pick already recorded in the last 2h (scanner runs hourly)
        _recent = conn.execute(
            "SELECT id FROM sibila_picks WHERE match=? AND COALESCE(market,'')=? "
            "AND COALESCE(side,'')=? AND ts >= datetime('now', '-2 hours') LIMIT 1",
            (match_val, market_val or '', side_val or '')).fetchone()
        if _recent:
            conn.close()
            return

        # Realistic track: only set on FIRST unresolved occurrence per (match,market,side).
        # Duplicates from later scan cycles keep realistic_* NULL so bankroll doesn't compound.
        existing = conn.execute(
            "SELECT realistic_stake FROM sibila_picks "
            "WHERE match=? AND COALESCE(market,'')=? AND COALESCE(side,'')=? "
            "  AND realistic_stake IS NOT NULL "
            "ORDER BY ts ASC LIMIT 1",
            (match_val, market_val or '', side_val or '')
        ).fetchone()
        if existing is not None:
            realistic_stake = None
            realistic_br_before = None
        else:
            r_bk = _get_realistic_bankroll()
            r_streak = _realistic_consecutive_losses(conn)
            realistic_stake = _realistic_kelly_stake(prob_model, odds, r_bk, sport, r_streak)
            realistic_br_before = round(r_bk, 2) if realistic_stake > 0 else None

        conn.execute("""
            INSERT OR IGNORE INTO sibila_picks
              (ts, sport, match, league, surface, level, market, side,
               prob_model, prob_book, edge, confidence, odds,
               shadow_stake, shadow_br_before,
               placed, real_stake, bet_id, market_type,
               realistic_stake, realistic_br_before)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            sport,
            match_val,
            pick.get('league') or '',
            surface,
            _classify_level(pick),
            market_val,
            side_val,
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
            pick.get('market_type') or '',
            realistic_stake,
            realistic_br_before,
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


def resolve_shadow_picks(match: str, side: str = None, result: str = None,
                         closing_odds: float = None) -> int:
    """
    Resolve all shadow picks (placed=0) for a given match+side.
    Called automatically after a real bet settles so the simulation
    tracks the same outcomes without depending on real bet placement.
    Returns number of picks resolved.
    """
    try:
        conn = _get_conn()
        if side:
            rows = conn.execute(
                "SELECT * FROM sibila_picks "
                "WHERE match=? AND side=? AND placed=0 AND result IS NULL"
                " AND ts >= datetime('now', '-7 days')",
                (match, side)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sibila_picks "
                "WHERE match=? AND placed=0 AND result IS NULL"
                " AND ts >= datetime('now', '-7 days')",
                (match,)).fetchall()

        if not rows:
            conn.close()
            return 0

        result_norm = (result or '').upper()
        if result_norm not in ('WIN', 'LOSS', 'VOID', 'PUSH', 'HALF_WIN', 'HALF_LOSS'):
            result_norm = 'VOID'
        now = datetime.now().isoformat()
        resolved = 0
        total_pnl = 0.0

        total_realistic_pnl = 0.0
        for row in rows:
            odds  = row['odds'] or 1.0
            stake = row['shadow_stake'] or 0.0

            if result_norm in ('WIN', 'HALF_WIN'):
                pnl = round(stake * (odds - 1.0), 4)
            elif result_norm in ('LOSS', 'HALF_LOSS'):
                pnl = round(-stake, 4)
            else:
                pnl = 0.0

            # Realistic pnl — only on primary rows (realistic_stake IS NOT NULL)
            r_stake = row['realistic_stake']
            r_pnl = None
            if r_stake is not None and r_stake > 0:
                if result_norm in ('WIN', 'HALF_WIN'):
                    r_pnl = round(r_stake * (odds - 1.0), 4)
                elif result_norm in ('LOSS', 'HALF_LOSS'):
                    r_pnl = round(-r_stake, 4)
                else:
                    r_pnl = 0.0
                total_realistic_pnl += r_pnl

            clv = None
            if closing_odds and closing_odds > 1 and odds > 1:
                clv = round(closing_odds / odds - 1.0, 4)

            conn.execute(
                "UPDATE sibila_picks "
                "SET result=?, pnl=?, realistic_pnl=COALESCE(?, realistic_pnl), "
                "resolved_ts=?, "
                "closing_odds=COALESCE(closing_odds, ?), clv=COALESCE(clv, ?) "
                "WHERE id=?",
                (result_norm, pnl, r_pnl, now, closing_odds, clv, row['id']))
            total_pnl += pnl
            resolved += 1

        # update virtual bankroll so future stakes are sized correctly
        if resolved > 0 and result_norm not in ('VOID', 'PUSH'):
            row_br = conn.execute(
                "SELECT value FROM sibila_meta WHERE key='virtual_bankroll'"
            ).fetchone()
            cur_br = float(row_br['value']) if row_br else VIRTUAL_BANKROLL_START
            new_br = max(cur_br + total_pnl, 1.0)
            conn.execute(
                "INSERT OR REPLACE INTO sibila_meta VALUES ('virtual_bankroll', ?)",
                (str(round(new_br, 4)),)
            )

            # Realistic bankroll — only primary rows contributed via total_realistic_pnl
            if total_realistic_pnl != 0:
                row_r = conn.execute(
                    "SELECT value FROM sibila_meta WHERE key='realistic_bankroll'"
                ).fetchone()
                cur_r = float(row_r['value']) if row_r else REALISTIC_BANKROLL_START
                new_r = max(cur_r + total_realistic_pnl, 1.0)
                conn.execute(
                    "INSERT OR REPLACE INTO sibila_meta VALUES ('realistic_bankroll', ?)",
                    (str(round(new_r, 4)),)
                )
        conn.commit()
        conn.close()
        log.debug('Sibila shadow resolved: %d picks for %s -> %s',
                  resolved, (match or '')[:30], result_norm)
        return resolved
    except Exception as ex:
        log.debug('Sibila resolve_shadow error: %s', ex)
        return 0


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
        # real bets store stake in real_stake; shadow bets use shadow_stake
        stake  = row['real_stake'] or row['shadow_stake'] or 0.0
        result_norm = (result or '').upper()

        if result_norm == 'WIN':
            pnl = round(stake * (odds - 1.0), 4)
        elif result_norm == 'LOSS':
            pnl = round(-stake, 4)
        else:
            pnl = 0.0
            result_norm = 'VOID'

        # Realistic pnl — only if this row is the primary (has realistic_stake)
        r_stake = row['realistic_stake']
        r_pnl = None
        if r_stake is not None and r_stake > 0:
            if result_norm == 'WIN':
                r_pnl = round(r_stake * (odds - 1.0), 4)
            elif result_norm == 'LOSS':
                r_pnl = round(-r_stake, 4)
            else:
                r_pnl = 0.0
            if r_pnl != 0:
                cur_r = _get_realistic_bankroll()
                _set_realistic_bankroll(max(cur_r + r_pnl, 1.0))

        clv = None
        if closing_odds and closing_odds > 1 and odds > 1:
            clv = round(closing_odds / odds - 1.0, 4)

        vbr_before = row['shadow_br_before'] or _get_virtual_bankroll()
        new_br = vbr_before + pnl
        _set_virtual_bankroll(new_br)

        conn.execute("""
            UPDATE sibila_picks
            SET result=?, pnl=?, realistic_pnl=COALESCE(?, realistic_pnl),
                resolved_ts=?, closing_odds=?, clv=?
            WHERE id=?
        """, (result_norm, pnl, r_pnl, datetime.now().isoformat(), closing_odds, clv, rid))
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