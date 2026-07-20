"""CLV Auto-tuner de gate parameters WC.
Cron: 0 */6 * * * (cada 6h para reaccionar en R32/QF/SF)
Lee sibila.db, calcula CLV por fase, ajusta gate_params_auto.json.
Solo actua cuando N_placed_bets >= MIN_N por fase.
"""
import sqlite3, json, os, logging
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB  = os.path.join(SCRIPT_DIR, 'sibila.db')
OUT = os.path.join(SCRIPT_DIR, 'gate_params_auto.json')
LOG = os.path.join(SCRIPT_DIR, 'logs', 'autotuner.log')

MIN_N   = 30
MAX_DP  = 0.05
BOUNDS  = {'min_prob': (0.55, 0.85), 'min_odds': (1.25, 1.75), 'xg_ratio': (1.40, 3.00)}
PHASE_DATES = {
    'groups': ('2026-06-11', '2026-06-27'),
    'r32':    ('2026-06-28', '2026-07-06'),
    'qf':     ('2026-07-10', '2026-07-11'),
    'sf':     ('2026-07-14', '2026-07-15'),
    'final':  ('2026-07-19', '2026-07-19'),
}
BASE = {
    'groups': {'min_prob': 0.80, 'min_odds': 1.35, 'xg_ratio': 2.50},
    'r32':    {'min_prob': 0.70, 'min_odds': 1.45, 'xg_ratio': 1.75},
    'qf':     {'min_prob': 0.65, 'min_odds': 1.50, 'xg_ratio': 1.60},
    'sf':     {'min_prob': 0.60, 'min_odds': 1.55, 'xg_ratio': 1.50},
    'final':  {'min_prob': 0.55, 'min_odds': 1.60, 'xg_ratio': 1.40},
}

os.makedirs(os.path.join(SCRIPT_DIR, 'logs'), exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('autotuner')


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def get_clv_stats(conn, phase):
    s, e = PHASE_DATES[phase]
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as n,
               AVG(COALESCE(clv, 0))  as avg_clv,
               SUM(clv > 0)           as clv_pos,
               AVG(odds)              as avg_odds
        FROM sibila_picks
        WHERE league = 'FIFA_WC'
          AND ts BETWEEN ? AND ?
          AND placed = 1
          AND clv IS NOT NULL
          AND result IN ('WIN', 'LOSS')
    """, (s + 'T00:00', e + 'T23:59'))
    r = cur.fetchone()
    return {'n': r[0] or 0, 'avg_clv': r[1] or 0.0,
            'clv_pos': r[2] or 0, 'avg_odds': r[3] or 1.5}


def compute_override(phase, stats):
    n, clv = stats['n'], stats['avg_clv']
    if n < MIN_N:
        log.info('[%s] N=%d < %d minimo — sin ajuste', phase, n, MIN_N)
        return None
    base = BASE[phase]
    adj  = dict(base)
    if clv < -0.02:
        delta = clamp(abs(clv) * 2, 0.01, MAX_DP)
        adj['min_prob'] = clamp(base['min_prob'] + delta, *BOUNDS['min_prob'])
        log.info('[%s] CLV=%.1f%% -> min_prob %.2f->%.2f', phase, clv*100,
                 base['min_prob'], adj['min_prob'])
    elif clv > 0.05 and n > 50:
        delta = clamp(clv * 1.5, 0.01, MAX_DP * 0.5)
        adj['min_prob'] = clamp(base['min_prob'] - delta, *BOUNDS['min_prob'])
        log.info('[%s] CLV=%.1f%% N=%d -> min_prob %.2f->%.2f', phase, clv*100, n,
                 base['min_prob'], adj['min_prob'])
    if stats['avg_odds'] < 1.55 and base['xg_ratio'] > 1.60:
        adj['xg_ratio'] = clamp(base['xg_ratio'] - 0.10, *BOUNDS['xg_ratio'])
    diff = {k: v for k, v in adj.items() if abs(v - base.get(k, 0)) > 0.001}
    return diff if diff else None


def main():
    log.info('=== CLV AutoTuner %s ===', datetime.utcnow().isoformat()[:16])
    if not os.path.exists(DB):
        log.error('sibila.db no encontrado')
        print('sibila.db no encontrado — sin cambios')
        return
    conn = sqlite3.connect(DB)
    overrides = {}
    for phase in PHASE_DATES:
        stats = get_clv_stats(conn, phase)
        log.info('[%s] N=%d avg_CLV=%.2f%% avg_odds=%.3f',
                 phase, stats['n'], stats['avg_clv']*100, stats['avg_odds'])
        ov = compute_override(phase, stats)
        if ov:
            overrides[phase] = ov
    conn.close()
    with open(OUT, 'w') as f:
        json.dump(overrides, f, indent=2)
    msg = overrides if overrides else 'sin overrides (N<30 para todas las fases)'
    log.info('gate_params_auto.json: %s', msg)
    print('AutoTuner OK:', msg)


if __name__ == '__main__':
    main()
