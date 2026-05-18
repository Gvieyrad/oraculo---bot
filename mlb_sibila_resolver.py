#!/usr/bin/env python3
"""
MLB F5 shadow pick resolver.
Uses the existing mlb_f5.db cache (built by oraculo_mlb_f5.py).
Resolves: F5 Over/Under totals and F5 ML picks.
"""
import sys, os, sqlite3, logging
from datetime import datetime, timedelta
from difflib import SequenceMatcher

log = logging.getLogger('mlb_resolver')
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

SIBILA_DB = '/home/noc/oraculo_v2/sibila.db'
F5_DB     = '/home/noc/oraculo_v2/.oraculo_cache/mlb_f5.db'

# Sibila short name → MLB Stats API full team name
TEAM_MAP = {
    'ARI Diamondbacks':  'Arizona Diamondbacks',
    'ATH Athletics':     'Athletics',
    'ATL Braves':        'Atlanta Braves',
    'BAL Orioles':       'Baltimore Orioles',
    'BOS Red Sox':       'Boston Red Sox',
    'CHI Cubs':          'Chicago Cubs',
    'CHW White Sox':     'Chicago White Sox',
    'CIN Reds':          'Cincinnati Reds',
    'CLE Guardians':     'Cleveland Guardians',
    'COL Rockies':       'Colorado Rockies',
    'DET Tigers':        'Detroit Tigers',
    'HOU Astros':        'Houston Astros',
    'KC Royals':         'Kansas City Royals',
    'LA Angels':         'Los Angeles Angels',
    'LA Dodgers':        'Los Angeles Dodgers',
    'MIA Marlins':       'Miami Marlins',
    'MIL Brewers':       'Milwaukee Brewers',
    'MIN Twins':         'Minnesota Twins',
    'NY Mets':           'New York Mets',
    'NY Yankees':        'New York Yankees',
    'PHI Phillies':      'Philadelphia Phillies',
    'PIT Pirates':       'Pittsburgh Pirates',
    'SD Padres':         'San Diego Padres',
    'SEA Mariners':      'Seattle Mariners',
    'SF Giants':         'San Francisco Giants',
    'STL Cardinals':     'St. Louis Cardinals',
    'TB Rays':           'Tampa Bay Rays',
    'TEX Rangers':       'Texas Rangers',
    'TOR Blue Jays':     'Toronto Blue Jays',
    'WAS Nationals':     'Washington Nationals',
}


def _sim(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    if a == b: return 1.0
    if a in b or b in a: return 0.85
    return SequenceMatcher(None, a, b).ratio()


def _find_game(f5_conn, home_full, away_full, pick_date_str):
    """Find game in cache within ±1 day of pick date."""
    try:
        pick_dt = datetime.strptime(pick_date_str[:10], '%Y-%m-%d')
    except Exception:
        return None

    dates = [
        (pick_dt - timedelta(days=1)).strftime('%Y-%m-%d'),
        pick_dt.strftime('%Y-%m-%d'),
        (pick_dt + timedelta(days=1)).strftime('%Y-%m-%d'),
    ]

    f5_cols = [r[1] for r in f5_conn.execute('PRAGMA table_info(f5_games)').fetchall()]

    best_row, best_score = None, 0.0
    for d in dates:
        rows = f5_conn.execute(
            "SELECT * FROM f5_games WHERE date=? AND status='Final'", (d,)
        ).fetchall()
        for row in rows:
            r = dict(zip(f5_cols, row))
            hs = _sim(home_full, r['home'])
            as_ = _sim(away_full, r['away'])
            if hs < 0.5 or as_ < 0.5:
                continue
            score = hs * as_
            if score > best_score:
                best_score = score
                best_row = r

    return best_row if best_score >= 0.5 else None


def _parse_side(side_str):
    """
    Parse side string into (kind, direction, line, pick_team).
    kind: 'f5_total' | 'f5_ml'
    Returns (kind, direction, line, pick_team)
    """
    s = side_str.lower().strip()
    # Remove dome tag
    s_clean = s.replace('[dome]', '').strip()

    # F5 ML: ARI Diamondbacks (FIP ...)
    if 'ml:' in s_clean or 'f5 ml' in s_clean:
        # Extract team name between 'ml:' and '('
        import re
        m = re.search(r'ml:\s*(.+?)(?:\s*\(|$)', side_str, re.IGNORECASE)
        pick_team = m.group(1).strip() if m else ''
        return ('f5_ml', None, None, pick_team)

    # F5 Over/Under X.5
    import re
    m = re.search(r'(over|under)\s+([\d.]+)', s_clean, re.IGNORECASE)
    if m:
        direction = m.group(1).lower()
        line = float(m.group(2))
        return ('f5_total', direction, line, '')

    return (None, None, None, '')


def _refresh_cache():
    """Refresh MLB F5 cache for recent dates."""
    sys.path.insert(0, '/home/noc/oraculo_v2')
    try:
        import oraculo_mlb_f5 as mlb
        log.info('Refreshing MLB F5 cache...')
        mlb.fetch_f5_history(days_back=30, force_refresh=False)
        log.info('Cache refresh done')
    except Exception as e:
        log.warning('Cache refresh failed: %s', e)


def resolve_all_pending(dry_run=False):
    _refresh_cache()

    sib_conn = sqlite3.connect(SIBILA_DB)
    f5_conn  = sqlite3.connect(F5_DB)

    cols = [r[1] for r in sib_conn.execute('PRAGMA table_info(sibila_picks)').fetchall()]

    rows = sib_conn.execute(
        "SELECT * FROM sibila_picks WHERE sport='baseball' "
        "AND (result IS NULL OR result='') ORDER BY ts"
    ).fetchall()

    log.info('Pending MLB picks: %d', len(rows))

    def col(row, name):
        idx = cols.index(name) if name in cols else None
        return row[idx] if idx is not None else None

    resolved = not_found = skipped = 0

    for row in rows:
        pick_id = col(row, 'id')
        match   = str(col(row, 'match') or '')
        side    = str(col(row, 'side')  or '')
        ts      = str(col(row, 'ts')    or '')
        odds    = float(col(row, 'odds') or 1.5)
        stake   = float(col(row, 'shadow_stake') or col(row, 'stake') or 10.0)

        if ' vs ' not in match:
            skipped += 1
            continue

        home_sib, away_sib = match.split(' vs ', 1)
        home_full = TEAM_MAP.get(home_sib.strip())
        away_full = TEAM_MAP.get(away_sib.strip())

        if not home_full or not away_full:
            log.warning('  Unknown team: %s', match)
            skipped += 1
            continue

        kind, direction, line, pick_team = _parse_side(side)
        if kind is None:
            log.warning('  Cannot parse side: %s', side)
            skipped += 1
            continue

        game = _find_game(f5_conn, home_full, away_full, ts)
        if game is None:
            log.warning('  Not found: %s vs %s (%s)', home_full, away_full, ts[:10])
            not_found += 1
            continue

        f5_total = game.get('f5_total')
        f5_home  = game.get('f5_home')
        f5_away  = game.get('f5_away')

        if f5_total is None:
            log.warning('  No F5 data for %s (%s)', match, game.get('date'))
            not_found += 1
            continue

        if kind == 'f5_total':
            if f5_total == line:
                result = 'VOID'
            elif direction == 'over':
                result = 'WIN' if f5_total > line else 'LOSS'
            else:
                result = 'WIN' if f5_total < line else 'LOSS'
            detail = f'f5={f5_total} line={line}'

        elif kind == 'f5_ml':
            # Map pick_team sibila name to full name
            pick_full = TEAM_MAP.get(pick_team, pick_team)
            home_sim = _sim(pick_full, home_full)
            away_sim = _sim(pick_full, away_full)
            picked_home = home_sim > away_sim
            home_won = f5_home > f5_away
            if f5_home == f5_away:
                result = 'VOID'
            elif picked_home:
                result = 'WIN' if home_won else 'LOSS'
            else:
                result = 'WIN' if not home_won else 'LOSS'
            detail = f'f5={f5_home}-{f5_away} picked={pick_team}'
        else:
            skipped += 1
            continue

        pnl = (odds - 1) * stake if result == 'WIN' else (-stake if result == 'LOSS' else 0.0)
        log.info('  %s | %s | %s | %s | $%+.2f [%s]',
                 result, match[:30], side[:25], detail, pnl, game.get('date'))

        if not dry_run:
            now = datetime.utcnow().isoformat()
            sib_conn.execute(
                "UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=? WHERE id=?",
                (result, pnl, now, pick_id)
            )
            sib_conn.commit()
            resolved += 1

    f5_conn.close()

    # Summary
    settled = sib_conn.execute(
        "SELECT result, COUNT(*), SUM(pnl) FROM sibila_picks "
        "WHERE sport='baseball' AND result IN ('WIN','LOSS','VOID') GROUP BY result"
    ).fetchall()
    sib_conn.close()

    print('\n=== MLB F5 Shadow Summary ===')
    tn = tw = 0
    tpnl = 0.0
    for res, n, pnl in settled:
        pnl = pnl or 0
        print(f'  {res:4}: n={n}  PnL=${pnl:+.2f}')
        tn += n
        if res == 'WIN': tw += n
        tpnl += pnl
    if tn:
        print(f'  TOTAL: {tw}/{tn}  WR={tw/tn*100:.1f}%  PnL=${tpnl:+.2f}')
    print(f'\nThis run: resolved={resolved}  skipped={skipped}  not_found={not_found}')
    return resolved, skipped, not_found


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    resolve_all_pending(dry_run=args.dry_run)
