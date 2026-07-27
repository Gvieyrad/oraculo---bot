"""Resolve Rugby (NRL/MLR) shadow picks in sibila.db via octonion/rugby GitHub data.

market_type='rugby_ml' -> NRL, market_type='rugby_mlr_ml' -> MLR.
`side` column holds a label like 'NRL ML home (elo 56%)' / 'MLR ML away (elo 63%)' —
the picked outcome is the 'home'/'away' token, not a team name.
"""
import os, re, sqlite3, logging
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')

import oraculo_rugby as _rug

_LEAGUE_BY_MARKET = {'rugby_ml': 'nrl', 'rugby_mlr_ml': 'mlr'}


def _norm(name):
    return re.sub(r'[^a-z ]', '', (name or '').lower()).strip()


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    if set(a.split()) & set(b.split()):
        return 0.6
    return 0.0


def _picked_side(side_label):
    m = re.search(r'\bML\s+(home|away)\b', side_label or '', re.IGNORECASE)
    return m.group(1).lower() if m else None


def _find_rugby_result(home, away, date_str, matches):
    try:
        tgt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except Exception:
        return None
    for delta in (0, 1, -1, 2, -2, 3, -3):
        check = (tgt + timedelta(days=delta)).date()
        for d, h, a, hs, as_ in matches:
            if d.date() != check:
                continue
            if _sim(home, h) >= 0.6 and _sim(away, a) >= 0.6:
                return {'home_pts': hs, 'away_pts': as_}
            if _sim(home, a) >= 0.6 and _sim(away, h) >= 0.6:
                return {'home_pts': as_, 'away_pts': hs}
    return None


def resolve_rugby_pending(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Resolve pending Rugby (NRL/MLR) shadow picks in sibila.db. Returns count resolved."""
    rows = conn.execute("""
        SELECT id, market_type, match, side, odds, shadow_stake, real_stake, ts
        FROM sibila_picks
        WHERE market_type IN ('rugby_ml', 'rugby_mlr_ml') AND result IS NULL
        ORDER BY ts
    """).fetchall()

    if not rows:
        return 0

    _cache = {}
    resolved = 0
    for pick_id, market_type, match_str, side, odds, stake, real_stake, placed_at in rows:
        league = _LEAGUE_BY_MARKET.get(market_type)
        if league is None:
            continue
        if league not in _cache:
            try:
                _cache[league] = _rug._fetch(league)
            except Exception as e:
                log.warning('Rugby resolver: fetch failed for %s: %s', league, e)
                _cache[league] = []
        matches = _cache[league]

        ts = (placed_at or '')[:10]
        parts = (match_str or '').split(' vs ')
        picked = _picked_side(side)
        if not ts or len(parts) < 2 or picked not in ('home', 'away'):
            continue
        home_cb, away_cb = parts[0].strip(), parts[1].strip()

        result_data = _find_rugby_result(home_cb, away_cb, ts, matches)
        if result_data is None:
            log.debug('Rugby resolver: no result yet for %s (%s)', match_str, ts)
            continue

        hp, ap = result_data['home_pts'], result_data['away_pts']
        winning_side = 'home' if hp > ap else ('away' if ap > hp else None)
        won = picked == winning_side

        result    = 'WIN' if won else 'LOSS'
        use_stake = real_stake if real_stake and real_stake > 0 else stake or 1.0
        pnl       = round((odds - 1) * use_stake if won else -use_stake, 4)

        log.info('Rugby %s %s | %s | bet=%s | score %d-%d %+.2f',
                 league.upper(), result, match_str, picked, hp, ap, pnl)

        if not dry_run:
            conn.execute(
                'UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=? WHERE id=?',
                (result, pnl, datetime.utcnow().isoformat(), pick_id)
            )
            conn.commit()
        resolved += 1

    return resolved


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    db_path = os.path.join(os.path.dirname(__file__), 'sibila.db')
    conn    = sqlite3.connect(db_path)
    dry     = '--dry-run' in sys.argv
    n       = resolve_rugby_pending(conn, dry_run=dry)
    conn.close()
    print(f'Resolved {n} Rugby picks{" (DRY RUN)" if dry else ""}')
