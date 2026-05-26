"""
oraculo_auto_analyzer.py — AutoAnalyzer for Oraculo

Reads sibila.db, computes segment stats, and updates oraculo_filters.json
when a segment has ≥MIN_PICKS picks and WR is statistically below breakeven.

Run: python3 oraculo_auto_analyzer.py [--dry-run]
Cron: 0 5 * * * python3 /home/noc/oraculo_v2/oraculo_auto_analyzer.py >> /var/log/oraculo_analyzer.log 2>&1
"""

import sqlite3
import json
import os
import sys
import logging
from datetime import datetime
from math import sqrt
from scipy import stats as scipy_stats

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, 'sibila.db')
FILTERS_PATH = os.path.join(SCRIPT_DIR, 'oraculo_filters.json')
LOG_PATH   = '/var/log/oraculo_analyzer.log'

MIN_PICKS      = 30
P_THRESHOLD    = 0.05
DRY_RUN        = '--dry-run' in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [analyzer] %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger('analyzer')


# ── helpers ──────────────────────────────────────────────────────────────────

def breakeven_wr(avg_odds: float) -> float:
    """Minimum WR needed to profit at given average odds (1/odds)."""
    if avg_odds <= 1.0:
        return 1.0
    return 1.0 / avg_odds


def binomial_pvalue(wins: int, n: int, p0: float) -> float:
    """One-sided p-value: P(WR <= observed | true WR = p0). Lower = more significant."""
    if n == 0:
        return 1.0
    from scipy import stats as _st
    return float(_st.binomtest(wins, n, p0, alternative='less').pvalue)


def load_filters() -> dict:
    with open(FILTERS_PATH) as f:
        return json.load(f)


def save_filters(filters: dict, reason: str):
    filters['_meta']['last_updated'] = datetime.utcnow().strftime('%Y-%m-%d')
    filters['_meta']['updated_by'] = 'auto_analyzer'
    with open(FILTERS_PATH, 'w') as f:
        json.dump(filters, f, indent=2, ensure_ascii=False)
    log.info('Filters saved: %s', reason)


def send_telegram(msg: str):
    """Send Telegram notification. Reads token/chat from oraculo env."""
    try:
        import subprocess, shlex
        token_path = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tg_token.txt')
        chat_path  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tg_chat.txt')
        if not os.path.exists(token_path) or not os.path.exists(chat_path):
            # Fall back: read from runner config
            runner_path = os.path.join(SCRIPT_DIR, 'oraculo_runner_auto.py')
            token, chat = None, None
            with open(runner_path) as rf:
                for line in rf:
                    if 'TELEGRAM_TOKEN' in line and '=' in line and '#' not in line.split('=')[0]:
                        token = line.split('=', 1)[1].strip().strip('"\'')
                    if 'TELEGRAM_CHAT' in line and '=' in line and '#' not in line.split('=')[0]:
                        chat = line.split('=', 1)[1].strip().strip('"\'')
            if not token or not chat:
                log.warning('Telegram token/chat not found, skipping notify')
                return
        else:
            token = open(token_path).read().strip()
            chat  = open(chat_path).read().strip()

        import urllib.request, urllib.parse
        url  = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}).encode()
        urllib.request.urlopen(url, data, timeout=10)
        log.info('Telegram sent')
    except Exception as e:
        log.warning('Telegram failed: %s', e)


# ── analysis functions ────────────────────────────────────────────────────────

def analyze_tennis_comps(conn, filters: dict) -> list[dict]:
    """Find tennis comps with bad WR (n>=MIN_PICKS, p<P_THRESHOLD)."""
    rows = conn.execute("""
        SELECT
            CASE
                WHEN league LIKE '%tennis-atp-%' THEN REPLACE(SUBSTR(league, INSTR(league,'tennis-atp-')+11), '-', '-')
                ELSE league
            END as comp_raw,
            league,
            COUNT(*) n,
            SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
            ROUND(AVG(CASE WHEN result='WIN' THEN 1.0 ELSE 0.0 END)*100,1) wr,
            ROUND(SUM(pnl),2) pnl,
            ROUND(AVG(odds),3) avg_odds
        FROM sibila_picks
        WHERE sport='tennis' AND result IN ('WIN','LOSS') AND is_duplicate=0
        GROUP BY league
        HAVING n >= ?
        ORDER BY pnl ASC
    """, (MIN_PICKS,)).fetchall()

    candidates = []
    current_blocks = set(filters.get('tennis', {}).get('blocked_comp_substrings', []))

    for comp_raw, league, n, wins, wr, pnl, avg_odds in rows:
        be = breakeven_wr(avg_odds)
        pval = binomial_pvalue(wins, n, be)
        log.info('Tennis comp: %s | n=%d WR=%.1f%% BE=%.1f%% pval=%.3f pnl=$%.2f',
                 league[:45], n, wr, be*100, pval, pnl)

        if pval < P_THRESHOLD and wr/100 < be:
            # Extract a substring key from the league
            # e.g. 'tennis-atp-hamburg-germany' → 'hamburg'
            parts = league.split('-')
            # Find a distinguishing substring (not 'tennis', 'atp', 'wta', country suffixes)
            skip_parts = {'tennis','atp','wta','germany','france','spain','uk','usa',
                          'italy','australia','czech','swiss','sweden','singles','men','women'}
            key = next((p for p in parts if p and p not in skip_parts and len(p) > 3), None)
            if key and key not in current_blocks:
                candidates.append({
                    'sport': 'tennis',
                    'type': 'comp_substring',
                    'key': key,
                    'league': league,
                    'n': n, 'wr': wr, 'pnl': pnl, 'pval': pval, 'avg_odds': avg_odds
                })

    return candidates


def analyze_tennis_odds(conn, filters: dict) -> list[dict]:
    """Find tennis markets where high odds are consistently unprofitable."""
    markets = ['tennis_winner', 'tennis_winner_and_total', 'tennis_team_win_set']
    candidates = []
    current_caps = filters.get('tennis', {}).get('max_odds', {})

    # Check if raising the cap to a tighter value would be beneficial
    for market in markets:
        rows = conn.execute("""
            SELECT
                CASE WHEN odds < 1.5 THEN 1
                     WHEN odds < 1.7 THEN 2
                     WHEN odds < 1.9 THEN 3
                     WHEN odds < 2.1 THEN 4
                     WHEN odds < 2.3 THEN 5
                     WHEN odds < 2.5 THEN 6
                     ELSE 7 END as bucket,
                MIN(odds) min_o, MAX(odds) max_o,
                COUNT(*) n,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
                ROUND(AVG(CASE WHEN result='WIN' THEN 1.0 ELSE 0.0 END)*100,1) wr,
                ROUND(SUM(pnl),2) pnl,
                ROUND(AVG(odds),3) avg_odds
            FROM sibila_picks
            WHERE sport='tennis' AND result IN ('WIN','LOSS') AND is_duplicate=0
              AND market_type=?
            GROUP BY bucket
            ORDER BY bucket DESC
        """, (market,)).fetchall()

        for bucket, min_o, max_o, n, wins, wr, pnl, avg_odds in rows:
            if n < MIN_PICKS:
                continue
            be = breakeven_wr(avg_odds)
            pval = binomial_pvalue(wins, n, be)
            current_cap = current_caps.get(market, 99.0)
            log.info('Tennis odds %s [%.2f-%.2f]: n=%d WR=%.1f%% pval=%.3f pnl=$%.2f cap=%.2f',
                     market, min_o, max_o, n, wr, be*100, pval, pnl, current_cap)
            if pval < P_THRESHOLD and max_o < current_cap and wr/100 < be:
                candidates.append({
                    'sport': 'tennis',
                    'type': 'odds_cap',
                    'market': market,
                    'new_cap': round(min_o - 0.01, 2),
                    'current_cap': current_cap,
                    'n': n, 'wr': wr, 'pnl': pnl, 'pval': pval
                })

    return candidates


def analyze_soccer_leagues(conn, filters: dict) -> list[dict]:
    """Find soccer league+line combos that are consistently losing."""
    rows = conn.execute("""
        SELECT
            SUBSTR(league, INSTR(league,'soccer-')+7) as lg_short,
            league,
            CASE WHEN side LIKE '%1.5%' THEN 'U1.5' ELSE 'U2.5' END as line,
            COUNT(*) n,
            SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
            ROUND(AVG(CASE WHEN result='WIN' THEN 1.0 ELSE 0.0 END)*100,1) wr,
            ROUND(SUM(pnl),2) pnl,
            ROUND(AVG(odds),3) avg_odds
        FROM sibila_picks
        WHERE sport='soccer' AND result IN ('WIN','LOSS') AND is_duplicate=0
        GROUP BY league, line
        HAVING n >= ?
        ORDER BY pnl ASC
    """, (MIN_PICKS,)).fetchall()

    candidates = []
    current_blocks = set(
        f"{b['league']}:{b['line']}"
        for b in filters.get('soccer', {}).get('blocked_league_line', [])
    )

    for lg_short, league, line, n, wins, wr, pnl, avg_odds in rows:
        be = breakeven_wr(avg_odds)
        pval = binomial_pvalue(wins, n, be)
        log.info('Soccer %s %s: n=%d WR=%.1f%% BE=%.1f%% pval=%.3f pnl=$%.2f',
                 lg_short[:30], line, n, wr, be*100, pval, pnl)
        key = f'{league}:{line}'
        if pval < P_THRESHOLD and wr/100 < be and key not in current_blocks:
            candidates.append({
                'sport': 'soccer',
                'type': 'league_line',
                'league': league,
                'line': line,
                'n': n, 'wr': wr, 'pnl': pnl, 'pval': pval, 'avg_odds': avg_odds
            })

    return candidates


def analyze_baseball_markets(conn, filters: dict) -> list[dict]:
    """Find baseball market conditions that are consistently losing."""
    # Check F5 Over by line and venue
    rows = conn.execute("""
        SELECT
            CASE WHEN side LIKE '%Over 4 %' OR (side LIKE '%Over 4%' AND side NOT LIKE '%4.5%') THEN 'O4'
                 WHEN side LIKE '%Over 4.5%' THEN 'O4.5'
                 WHEN side LIKE '%Over 5 %' OR (side LIKE '%Over 5%' AND side NOT LIKE '%5.5%') THEN 'O5'
                 WHEN side LIKE '%Over 5.5%' THEN 'O5.5'
                 ELSE 'other' END as line,
            CASE WHEN side LIKE '%dome%' THEN 'dome' ELSE 'outdoor' END as venue,
            COUNT(*) n,
            SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
            ROUND(AVG(CASE WHEN result='WIN' THEN 1.0 ELSE 0.0 END)*100,1) wr,
            ROUND(SUM(pnl),2) pnl,
            ROUND(AVG(odds),3) avg_odds
        FROM sibila_picks
        WHERE sport='baseball' AND result IN ('WIN','LOSS') AND is_duplicate=0
          AND market_type='mlb_f5_total' AND side LIKE '%Over%'
        GROUP BY line, venue
        HAVING n >= ?
    """, (MIN_PICKS,)).fetchall()

    candidates = []
    current_blocks = set(filters.get('baseball', {}).get('blocked_conditions', []))

    for line, venue, n, wins, wr, pnl, avg_odds in rows:
        be = breakeven_wr(avg_odds)
        pval = binomial_pvalue(wins, n, be)
        log.info('Baseball F5 Over %s %s: n=%d WR=%.1f%% BE=%.1f%% pval=%.3f pnl=$%.2f',
                 line, venue, n, wr, be*100, pval, pnl)
        key = f'mlb_f5_over_{venue}'
        if pval < P_THRESHOLD and wr/100 < be and key not in current_blocks:
            candidates.append({
                'sport': 'baseball',
                'type': 'condition',
                'key': key,
                'line': line, 'venue': venue,
                'n': n, 'wr': wr, 'pnl': pnl, 'pval': pval
            })

    return candidates


# ── apply candidates ─────────────────────────────────────────────────────────

def apply_candidates(candidates: list[dict], filters: dict, dry_run: bool) -> list[str]:
    """Apply candidate blocks to filters dict. Returns list of human-readable changes."""
    changes = []
    now = datetime.utcnow().strftime('%Y-%m-%d')

    for c in candidates:
        sport = c['sport']
        desc  = (f"{sport} | n={c['n']} WR={c['wr']:.1f}% pnl=${c['pnl']:.2f} "
                 f"pval={c['pval']:.3f}")

        if c['type'] == 'comp_substring':
            key = c['key']
            entry = {'key': key, 'reason': desc, 'blocked_at': now}
            if not dry_run:
                if key not in filters[sport]['blocked_comp_substrings']:
                    filters[sport]['blocked_comp_substrings'].append(key)
                    filters[sport]['blocked_comp_substrings_reason'][key] = desc
                    filters['auto_analyzer']['auto_blocked'].append(entry)
            changes.append(f'BLOCK tennis comp substring "{key}": {desc}')

        elif c['type'] == 'odds_cap':
            market = c['market']
            new_cap = c['new_cap']
            entry = {'market': market, 'new_cap': new_cap, 'reason': desc, 'blocked_at': now}
            if not dry_run:
                filters[sport]['max_odds'][market] = new_cap
                filters[sport]['max_odds_reason'][market] = desc
                filters['auto_analyzer']['auto_blocked'].append(entry)
            changes.append(f'TIGHTEN odds cap {market} → {new_cap}: {desc}')

        elif c['type'] == 'league_line':
            league = c['league']
            line   = c['line']
            entry  = {'league': league, 'line': line, 'reason': desc, 'blocked_at': now}
            if not dry_run:
                filters['soccer']['blocked_league_line'].append({'league': league, 'line': line})
                filters['soccer']['blocked_league_line_reason'][f'{league}:{line}'] = desc
                filters['auto_analyzer']['auto_blocked'].append(entry)
            changes.append(f'BLOCK soccer {league} {line}: {desc}')

        elif c['type'] == 'condition':
            key = c['key']
            entry = {'key': key, 'reason': desc, 'blocked_at': now}
            if not dry_run:
                if key not in filters['baseball']['blocked_conditions']:
                    filters['baseball']['blocked_conditions'].append(key)
                    filters['baseball']['blocked_conditions_reason'][key] = desc
                    filters['auto_analyzer']['auto_blocked'].append(entry)
            changes.append(f'BLOCK baseball condition {key}: {desc}')

    return changes


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    log.info('=== AutoAnalyzer START%s ===', ' [DRY RUN]' if DRY_RUN else '')

    conn    = sqlite3.connect(DB_PATH)
    filters = load_filters()

    all_candidates = []
    all_candidates += analyze_tennis_comps(conn, filters)
    all_candidates += analyze_tennis_odds(conn, filters)
    all_candidates += analyze_soccer_leagues(conn, filters)
    all_candidates += analyze_baseball_markets(conn, filters)

    conn.close()

    if not all_candidates:
        log.info('No new blocks found. Filters unchanged.')
        return

    log.info('%d candidate block(s) found:', len(all_candidates))
    for c in all_candidates:
        log.info('  → %s', c)

    changes = apply_candidates(all_candidates, filters, DRY_RUN)

    if not DRY_RUN and changes:
        save_filters(filters, f'{len(changes)} new block(s)')
        msg = (f'<b>Oraculo AutoAnalyzer</b>\n'
               f'{len(changes)} nuevo(s) bloqueo(s):\n\n' +
               '\n'.join(f'• {c}' for c in changes))
        send_telegram(msg)
        log.info('Done. Changes applied and Telegram sent.')
    elif DRY_RUN:
        log.info('DRY RUN — no changes written. Would have applied:')
        for c in changes:
            log.info('  %s', c)
    else:
        log.info('No changes to apply.')

    log.info('=== AutoAnalyzer END ===')


if __name__ == '__main__':
    main()
