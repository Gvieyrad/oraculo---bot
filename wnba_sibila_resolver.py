"""Resolve WNBA shadow picks in sibila.db via ESPN WNBA scoreboard."""
import sqlite3, json, logging, requests
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')

_ESPN_WNBA_CACHE = {}

def _fetch_espn_wnba(date_str: str) -> list:
    if date_str in _ESPN_WNBA_CACHE:
        return _ESPN_WNBA_CACHE[date_str]
    compact = date_str.replace('-', '')
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard',
            params={'dates': compact}, timeout=15)
        r.raise_for_status()
        events = r.json().get('events', [])
    except Exception as e:
        log.debug('ESPN WNBA fetch failed %s: %s', date_str, e)
        _ESPN_WNBA_CACHE[date_str] = []
        return []

    results = []
    for ev in events:
        comp   = ev.get('competitions', [{}])[0]
        status = comp.get('status', {}).get('type', {}).get('name', '')
        if status not in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
            continue
        teams = comp.get('competitors', [])
        if len(teams) < 2:
            continue
        ho = next((t for t in teams if t.get('homeAway') == 'home'), teams[0])
        aw = next((t for t in teams if t.get('homeAway') == 'away'), teams[1])
        try:
            hp = int(float(ho.get('score', 0) or 0))
            ap = int(float(aw.get('score', 0) or 0))
        except Exception:
            continue
        if hp == 0 and ap == 0:
            continue
        results.append({
            'home': ho.get('team', {}).get('displayName', ''),
            'away': aw.get('team', {}).get('displayName', ''),
            'home_pts': hp, 'away_pts': ap,
            'winner': ho.get('team', {}).get('displayName', '') if hp > ap
                      else aw.get('team', {}).get('displayName', ''),
        })
    _ESPN_WNBA_CACHE[date_str] = results
    return results


def _sim(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    wa = set(a.split())
    wb = set(b.split())
    if wa & wb:
        return 0.6
    return 0.0


def _find_wnba_result(home: str, away: str, date_str: str):
    try:
        tgt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except Exception:
        return None
    for delta in [0, 1, -1]:
        check = (tgt + timedelta(days=delta)).strftime('%Y-%m-%d')
        for m in _fetch_espn_wnba(check):
            if _sim(home, m['home']) >= 0.5 and _sim(away, m['away']) >= 0.5:
                return m
            # Also try reversed (Cloudbet home/away sometimes swapped)
            if _sim(home, m['away']) >= 0.5 and _sim(away, m['home']) >= 0.5:
                # Swap: return with home/away flipped
                return {**m, 'home': m['away'], 'away': m['home'],
                        'home_pts': m['away_pts'], 'away_pts': m['home_pts'],
                        'winner': m['winner']}
    return None


def resolve_wnba_pending(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Resolve pending WNBA picks in sibila.db. Returns count resolved."""
    rows = conn.execute("""
        SELECT id, side, odds, shadow_stake, real_stake, ts, event_id, market_url
        FROM sibila_picks
        WHERE sport='basketball' AND league='basketball-usa-wnba'
          AND result IS NULL
        ORDER BY ts
    """).fetchall()

    if not rows:
        return 0

    resolved = 0
    for pick_id, side, odds, stake, real_stake, placed_at, event_id, market_url in rows:
        ts = (placed_at or '')[:10]
        if not ts:
            continue

        # Parse home/away from side label: "WNBA: NY Liberty"
        team_bet = side.replace('WNBA: ', '').strip() if side else ''

        # We need the match — look up in sibila_picks.match field
        row2 = conn.execute('SELECT match FROM sibila_picks WHERE id=?', (pick_id,)).fetchone()
        if not row2:
            continue
        match_str = row2[0] or ''
        # match format: "NY Liberty vs PHX Mercury (w)"
        parts = match_str.split(' vs ')
        if len(parts) < 2:
            continue
        home_cb, away_cb = parts[0].strip(), parts[1].strip()

        # Normalize names for ESPN lookup
        from oraculo_wnba import _resolve_name
        home = _resolve_name(home_cb)
        away = _resolve_name(away_cb)

        result_data = _find_wnba_result(home, away, ts)
        if result_data is None:
            log.debug('WNBA resolver: no result yet for %s vs %s (%s)', home, away, ts)
            continue

        # Determine win/loss: team_bet must match winner
        team_full = _resolve_name(team_bet)
        won = _sim(team_full, result_data['winner']) >= 0.5

        result   = 'WIN' if won else 'LOSS'
        use_stake = real_stake if real_stake and real_stake > 0 else stake or 1.0
        pnl      = round((odds - 1) * use_stake if won else -use_stake, 4)

        log.info('WNBA %s | %s vs %s | bet=%s | %s %+.2f',
                 result, home_cb, away_cb, team_bet, result_data['winner'], pnl)

        if not dry_run:
            conn.execute(
                'UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=? WHERE id=?',
                (result, pnl, datetime.utcnow().isoformat(), pick_id)
            )
            conn.commit()
            resolved += 1

    return resolved


if __name__ == '__main__':
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    db_path  = os.path.join(os.path.dirname(__file__), 'sibila.db')
    conn     = sqlite3.connect(db_path)
    dry      = '--dry-run' in sys.argv
    n        = resolve_wnba_pending(conn, dry_run=dry)
    conn.close()
    print(f'Resolved {n} WNBA picks{"(DRY RUN)" if dry else ""}')
