#!/usr/bin/env python3
"""
Soccer shadow pick resolver — uses football-data.co.uk CSVs.
Resolves: Goals 2H Over/Under X.5 and Booking pts Over/Under X.5.
Cannot resolve: Corner N, Last corner, First booking (sequence markets).
"""
import sys, os, sqlite3, requests, csv, io, re, logging
from datetime import datetime
from difflib import SequenceMatcher

log = logging.getLogger('soccer_resolver')
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

SIBILA_DB = '/home/noc/oraculo_v2/sibila.db'

# League slug → [primary URL, fallback URL]
def _league_urls(slug):
    CURRENT = '2526'
    PREV    = '2425'
    MAP = {
        'soccer-england-premier-league':          ('E0', True),
        'soccer-england-championship':            ('E1', True),
        'soccer-england-league-1':               ('E2', False),
        'soccer-spain-la-liga':                  ('SP1', True),
        'soccer-germany-bundesliga':             ('D1', True),
        'soccer-italy-serie-a':                  ('I1', True),
        'soccer-france-ligue-1':                 ('F1', True),
        'soccer-netherlands-eredivisie':         ('N1', True),
        'soccer-portugal-primeira-liga':         ('P1', True),
        'soccer-turkey-super-lig':               ('T1', True),
        'soccer-belgium-jupiler':                ('B1', True),
        'soccer-scotland-premiership':           ('SC0', True),
        'soccer-international-clubs-uefa-champions-league': ('UCL', False),
        'soccer-international-clubs-europa-league':        ('EL', False),
        'soccer-international-clubs-conference-league':    ('ECL', False),
    }
    code, has_prev = MAP.get(slug, (None, False))
    if code is None:
        return []
    base = 'https://www.football-data.co.uk/mmz4281'
    urls = [f'{base}/{CURRENT}/{code}.csv']
    if has_prev:
        urls.append(f'{base}/{PREV}/{code}.csv')
    return urls

_csv_cache = {}

def _fetch_csv(url):
    if url in _csv_cache:
        return _csv_cache[url]
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        rows = [row for row in reader if row.get('HomeTeam') or row.get('Home')]
        _csv_cache[url] = rows
        log.debug(f'Loaded {len(rows)} rows from {url}')
        return rows
    except Exception as e:
        log.debug(f'CSV fetch failed {url}: {e}')
        return []

def _sim(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    if a == b: return 1.0
    if a in b or b in a: return 0.85
    return SequenceMatcher(None, a, b).ratio()

def _parse_teams(match_str):
    for sep in [' vs ', ' - ', ' v ', ' — ']:
        if sep in match_str:
            parts = match_str.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return match_str.strip(), ''

def _find_in_csv(rows, home, away, date_str):
    if not rows:
        return None
    hcol = 'HomeTeam' if 'HomeTeam' in rows[0] else 'Home'
    acol = 'AwayTeam' if 'AwayTeam' in rows[0] else 'Away'
    try:
        tgt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except:
        tgt = None

    best_row, best_score = None, 0.0
    for row in rows:
        rh = row.get(hcol, '')
        ra = row.get(acol, '')
        if not rh or not ra:
            continue
        hs  = _sim(home, rh)
        as_ = _sim(away, ra)
        if hs < 0.5 or as_ < 0.5:
            continue
        score = hs * as_
        if tgt:
            d_str = row.get('Date', '')
            for fmt in ['%d/%m/%Y', '%Y-%m-%d', '%d/%m/%y']:
                try:
                    row_dt = datetime.strptime(d_str, fmt)
                    if abs((row_dt - tgt).days) <= 2:
                        score += 0.4
                    break
                except:
                    pass
        if score > best_score:
            best_score = score
            best_row = row

    return best_row if best_score >= 0.5 else None

def _parse_market(side_str):
    """
    Parse bet side string → (market_kind, direction, line)
    market_kind: 'goals_2h', 'goals_ft', 'booking_pts', 'corners_total', 'unknown'
    direction: 'over' | 'under'
    line: float
    """
    s = side_str.lower()
    # Extract line number
    nums = re.findall(r'\d+\.?\d*', s)
    line_candidates = [float(n) for n in nums if '.' in n or float(n) > 3]

    def extract_dir():
        if 'under' in s: return 'under'
        if 'over' in s:  return 'over'
        return None

    direction = extract_dir()

    # Goals 2H
    if '2h' in s and 'goal' in s:
        line = next((l for l in line_candidates if 0.5 <= l <= 6.5), None)
        return ('goals_2h', direction, line)

    # Booking pts
    if 'booking' in s and 'pts' in s:
        line = next((l for l in sorted(line_candidates, reverse=True) if l > 5), None)
        if line is None and line_candidates:
            line = max(line_candidates)
        return ('booking_pts', direction, line)

    # Goals FT (no 2H qualifier)
    if 'goal' in s and 'ft' in s:
        line = next((l for l in line_candidates if 0.5 <= l <= 6.5), None)
        return ('goals_ft', direction, line)

    # Total corners
    if 'corner' in s and ('total' in s or 'over' in s or 'under' in s):
        line = next((l for l in line_candidates if l > 3), None)
        return ('corners_total', direction, line)

    return ('unknown', direction, None)

def _eval_result(row, market_kind, direction, line):
    """Evaluate WIN/LOSS given CSV row and parsed market."""
    def gi(col_name):
        v = row.get(col_name, '')
        try: return int(float(v)) if v and v.strip() else 0
        except: return 0

    if market_kind == 'goals_2h':
        fthg = gi('FTHG'); ftag = gi('FTAG')
        hthg = gi('HTHG'); htag = gi('HTAG')
        if not (fthg or ftag or hthg or htag) and not row.get('FTHG'):
            return None, None  # no HT data
        goals = (fthg - hthg) + (ftag - htag)
        detail = f'{fthg}-{ftag} (HT {hthg}-{htag}) 2H={goals}'
        won = goals < line if direction == 'under' else goals > line
        return won, detail

    if market_kind == 'booking_pts':
        hy = gi('HY'); ay = gi('AY'); hr = gi('HR'); ar = gi('AR')
        pts = 10 * (hy + ay) + 25 * (hr + ar)
        detail = f'HY={hy} AY={ay} HR={hr} AR={ar} pts={pts}'
        won = pts < line if direction == 'under' else pts > line
        return won, detail

    if market_kind == 'goals_ft':
        fthg = gi('FTHG'); ftag = gi('FTAG')
        goals = fthg + ftag
        detail = f'FT {fthg}-{ftag} total={goals}'
        won = goals < line if direction == 'under' else goals > line
        return won, detail

    if market_kind == 'corners_total':
        hc = gi('HC'); ac = gi('AC')
        corners = hc + ac
        detail = f'HC={hc} AC={ac} total={corners}'
        won = corners < line if direction == 'under' else corners > line
        return won, detail

    return None, None

def resolve_all_pending(dry_run=False):
    conn = sqlite3.connect(SIBILA_DB)
    cols = [r[1] for r in conn.execute('PRAGMA table_info(sibila_picks)').fetchall()]
    has_updated_at = 'updated_at' in cols

    rows = conn.execute(
        "SELECT * FROM sibila_picks WHERE sport='soccer' "
        "AND (result IS NULL OR result='') ORDER BY ts"
    ).fetchall()

    log.info(f'Pending soccer picks: {len(rows)}')

    def col(row, name):
        idx = cols.index(name) if name in cols else None
        return row[idx] if idx is not None else None

    resolved = 0
    skipped  = 0  # sequence markets (first booking, corner N, etc.)
    not_found = 0

    for row in rows:
        pick_id = col(row, 'id')
        match   = str(col(row, 'match')   or '')
        side    = str(col(row, 'side')    or '')
        league  = str(col(row, 'league')  or '')
        ts      = str(col(row, 'ts')      or '')[:10]
        odds    = float(col(row, 'odds')  or 1.5)
        stake   = float(col(row, 'shadow_stake') or col(row, 'stake') or 10.0)

        # Parse market
        market_kind, direction, line = _parse_market(side)

        # Skip markets that can't be resolved from CSV
        if market_kind == 'unknown':
            s_low = side.lower()
            if any(k in s_low for k in ['first booking', 'corner n:', 'last corner',
                                         'first corner', 'next corner']):
                log.info(f'  SKIP (sequence market): {match} | {side}')
                skipped += 1
            else:
                log.warning(f'  UNKNOWN market: {match} | {side}')
                skipped += 1
            continue

        if direction is None or line is None:
            log.warning(f'  Cannot parse line: {match} | {side}')
            skipped += 1
            continue

        # Find CSV result
        home, away = _parse_teams(match)
        if not away:
            log.warning(f'  Cannot parse teams: {match}')
            not_found += 1
            continue

        urls = _league_urls(league)
        if not urls:
            log.warning(f'  No URL for league: {league}')
            not_found += 1
            continue

        result_row = None
        found_url  = None
        for url in urls:
            csv_rows = _fetch_csv(url)
            r = _find_in_csv(csv_rows, home, away, ts)
            if r:
                result_row = r
                found_url  = url
                break

        if result_row is None:
            log.warning(f'  Not found: {home} vs {away} ({ts}) [{league}]')
            not_found += 1
            continue

        won, detail = _eval_result(result_row, market_kind, direction, line)

        if won is None:
            log.warning(f'  No data for {market_kind}: {home} vs {away}')
            not_found += 1
            continue

        result = 'WIN' if won else 'LOSS'
        pnl    = (odds - 1) * stake if won else -stake
        lname  = found_url.split('/')[-1]

        log.info(f'  {result} | {home} vs {away} | {side[:30]} | {detail} | ${pnl:+.2f} [{lname}]')

        if not dry_run:
            now = datetime.utcnow().isoformat()
            if has_updated_at:
                conn.execute(
                    "UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=? WHERE id=?",
                    (result, pnl, now, pick_id)
                )
            else:
                conn.execute(
                    "UPDATE sibila_picks SET result=?, pnl=? WHERE id=?",
                    (result, pnl, pick_id)
                )
            conn.commit()
            resolved += 1

    conn.close()

    # Final summary
    conn2 = sqlite3.connect(SIBILA_DB)
    settled = conn2.execute(
        "SELECT result, COUNT(*), SUM(pnl) FROM sibila_picks "
        "WHERE sport='soccer' AND result IN ('WIN','LOSS','VOID') GROUP BY result"
    ).fetchall()
    conn2.close()

    print('\n=== Soccer Shadow Summary ===')
    tn, tw, tpnl = 0, 0, 0.0
    for res, n, pnl in settled:
        pnl = pnl or 0
        print(f'  {res:4}: n={n}  PnL=${pnl:+.2f}')
        tn += n
        if res == 'WIN': tw += n
        tpnl += pnl
    if tn:
        print(f'  TOTAL: {tw}/{tn}  WR={tw/tn*100:.1f}%  PnL=${tpnl:+.2f}')
    print(f'\nThis run: resolved={resolved}  skipped(sequence)={skipped}  not_found={not_found}')
    return resolved, skipped, not_found

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    resolve_all_pending(dry_run=args.dry_run)
