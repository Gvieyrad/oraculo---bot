#!/usr/bin/env python3
"""
oraculo_status.py — Unified dashboard for Oraculo + Sibila.

Shows: 3 bankrolls, active bets, today's activity, AutoTune state,
Sibila realistic per-sport, watchlist, service health.

Run: python3 /home/noc/oraculo_v2/oraculo_status.py
"""
import json, sqlite3, subprocess, os, sys
from collections import Counter
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, 'oraculo_auto_state.json')
SIBILA_DB  = os.path.join(SCRIPT_DIR, 'sibila.db')
LOG_PATH   = os.path.join(SCRIPT_DIR, 'logs', 'oraculo_auto.log')


def hr(c='='):
    print(c * 78)


def section(title):
    print()
    hr()
    print(f'  {title}')
    hr()


# ---------------------------------------------------------------------------
# 1. Real Oraculo bankroll + active bets
# ---------------------------------------------------------------------------
def show_real():
    section('1. ORACULO REAL (Cloudbet)')
    s = json.load(open(STATE_PATH))
    wins, losses = s.get('wins', 0), s.get('losses', 0)
    n_wl = wins + losses
    wr = wins / n_wl * 100 if n_wl else 0
    print(f'  Bankroll       : ${s["bankroll"]:.2f}')
    print(f'  Total PnL      : ${s["total_pnl"]:+.2f}')
    print(f'  Record         : {wins}W / {losses}L  ({wr:.1f}% WR)')
    print(f'  Active bets    : {len(s["active_bets"])}')
    print(f'  Daily staked   : ${s["daily_staked"]:.2f}')
    print(f'  Last scan      : {s["last_scan"]}')
    print(f'  Consec losses  : {s["consecutive_losses"]}')

    # Active bets by sport + currency
    by_sport = Counter((b.get('sport') or 'unknown') for b in s.get('active_bets', []))
    if by_sport:
        print(f'  By sport       : ' + ', '.join(f'{k}={v}' for k, v in by_sport.most_common()))

    # Today's settled
    settled_today = s.get('settled_today', [])
    if isinstance(settled_today, list):
        st_count = len(settled_today)
    else:
        st_count = int(settled_today or 0)
    daily_pnl = float(s.get('daily_pnl') or 0)
    print(f'  Settled today  : {st_count}  | PnL today: ${daily_pnl:+.2f}')


# ---------------------------------------------------------------------------
# 2. Sibila bankrolls (baseline + realistic)
# ---------------------------------------------------------------------------
def show_sibila_bankrolls():
    section('2. SIBILA — Bankrolls')
    c = sqlite3.connect(SIBILA_DB)
    br_base_row = c.execute("SELECT value FROM sibila_meta WHERE key='virtual_bankroll'").fetchone()
    br_real_row = c.execute("SELECT value FROM sibila_meta WHERE key='realistic_bankroll'").fetchone()
    br_base = float(br_base_row[0]) if br_base_row else 1000.0
    br_real = float(br_real_row[0]) if br_real_row else 1000.0
    print(f'  Baseline (0.25 Kelly fix) : ${br_base:8.2f}  ({br_base-1000:+.2f}, {(br_base/1000-1)*100:+.1f}%)')
    print(f'  Realistic (live SPORT_KELLY): ${br_real:8.2f}  ({br_real-1000:+.2f}, {(br_real/1000-1)*100:+.1f}%)')

    n_total = c.execute('SELECT COUNT(*) FROM sibila_picks').fetchone()[0]
    n_settled = c.execute("SELECT COUNT(*) FROM sibila_picks WHERE result IN ('WIN','LOSS','VOID')").fetchone()[0]
    n_real_primary = c.execute('SELECT COUNT(*) FROM sibila_picks WHERE realistic_stake > 0').fetchone()[0]
    print(f'  Picks total: {n_total}  | Settled: {n_settled}  | Realistic primary: {n_real_primary}')
    c.close()


# ---------------------------------------------------------------------------
# 3. Sibila realistic per-sport ROI
# ---------------------------------------------------------------------------
def show_realistic_by_sport():
    section('3. SIBILA realistic — Por deporte (deduped)')
    c = sqlite3.connect(SIBILA_DB)
    q = """
    SELECT sport,
           COUNT(*) as n,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
           SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as l,
           ROUND(SUM(realistic_stake), 2),
           ROUND(SUM(realistic_pnl), 2)
    FROM sibila_picks
    WHERE realistic_stake > 0 AND result IN ('WIN','LOSS','VOID')
    GROUP BY sport
    ORDER BY -SUM(realistic_pnl)
    """
    print(f'  {"sport":10s} {"n":>4s} {"W":>3s} {"L":>3s} {"WR":>6s} {"stake":>10s} {"PnL":>10s} {"ROI":>8s}')
    print(f'  ' + '-' * 70)
    total_stake = 0; total_pnl = 0
    for r in c.execute(q):
        sport, n, w, l, stake, pnl = r
        nwl = w + l
        wr = w/nwl*100 if nwl else 0
        roi = pnl/stake*100 if stake else 0
        total_stake += stake or 0; total_pnl += pnl or 0
        print(f'  {sport:10s} {n:4d} {w:3d} {l:3d} {wr:5.1f}% ${stake:8.2f} ${pnl:+8.2f} {roi:+6.1f}%')
    if total_stake > 0:
        print(f'  ' + '-' * 70)
        print(f'  {"TOTAL":10s} {"":4s} {"":3s} {"":3s} {"":6s} ${total_stake:8.2f} ${total_pnl:+8.2f} {total_pnl/total_stake*100:+6.1f}%')
    c.close()


# ---------------------------------------------------------------------------
# 4. Live config
# ---------------------------------------------------------------------------
def show_config():
    section('4. CONFIG VIVA')
    sys.path.insert(0, SCRIPT_DIR)
    try:
        from oraculo_runner_auto import (
            MIN_EDGE, MIN_CONF, MAX_PER_BET, MAX_TOTAL_EXPOSURE,
            PARLAYS_ENABLED, SOCCER_ENABLED, SPORT_KELLY,
        )
        print(f'  MIN_EDGE            = {MIN_EDGE}')
        print(f'  MIN_CONF            = {MIN_CONF}')
        print(f'  MAX_PER_BET         = {MAX_PER_BET}')
        print(f'  MAX_TOTAL_EXPOSURE  = {MAX_TOTAL_EXPOSURE}')
        print(f'  PARLAYS_ENABLED     = {PARLAYS_ENABLED}')
        print(f'  SOCCER_ENABLED      = {SOCCER_ENABLED}')
        print(f'  SPORT_KELLY:')
        for sp, k in SPORT_KELLY.items():
            print(f'    {sp:12s} {k}')
    except Exception as e:
        print(f'  Could not import live config: {e}')


# ---------------------------------------------------------------------------
# 5. Recent activity (last cycle + last results)
# ---------------------------------------------------------------------------
def show_recent_activity():
    section('5. ACTIVIDAD RECIENTE (último log)')
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
        # Last cycle complete line + neighbors
        for line in lines[-25:]:
            print(f'  {line.rstrip()}')
    except Exception as e:
        print(f'  Could not read log: {e}')


# ---------------------------------------------------------------------------
# 6. Service health
# ---------------------------------------------------------------------------
def show_health():
    section('6. SERVICE HEALTH')
    try:
        r = subprocess.run(['systemctl', 'is-active', 'oraculo-v2'],
                           capture_output=True, text=True, timeout=5)
        print(f'  oraculo-v2.service : {r.stdout.strip()}')
    except Exception as e:
        print(f'  systemctl check failed: {e}')
    # Recent errors
    try:
        r = subprocess.run(['journalctl', '-u', 'oraculo-v2',
                            '--since', '6 hours ago', '--no-pager'],
                           capture_output=True, text=True, timeout=10)
        errs = [l for l in r.stdout.splitlines()
                if any(k in l.lower() for k in ['error', 'exception', 'traceback'])]
        print(f'  Errors (6h)        : {len(errs)}')
        for e in errs[-3:]:
            print(f'    {e[-120:]}')
    except Exception as e:
        print(f'  journalctl check failed: {e}')


# ---------------------------------------------------------------------------
# 7. Watchlist status
# ---------------------------------------------------------------------------
def show_watchlist():
    section('7. WATCHLIST (acumulación de muestra)')
    c = sqlite3.connect(SIBILA_DB)
    # Picks per league since last week
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    rows = list(c.execute("""
        SELECT league, COUNT(*) as raw,
               COUNT(DISTINCT match) as unique_matches,
               SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
               SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as l
        FROM sibila_picks
        WHERE ts >= ? AND LOWER(sport) IN ('soccer','baseball','tennis')
        GROUP BY league
        HAVING raw >= 3
        ORDER BY -raw LIMIT 15
    """, (cutoff,)))
    print(f'  Top leagues últimos 7 días (raw>=3):')
    print(f'  {"league":40s} {"raw":>5s} {"unique":>7s} {"W":>3s} {"L":>3s}')
    for r in rows:
        lg = (r[0] or 'NULL')[:40]
        print(f'  {lg:40s} {r[1]:5d} {r[2]:7d} {r[3] or 0:3d} {r[4] or 0:3d}')
    c.close()


if __name__ == '__main__':
    print()
    print(f'  ORACULO + SIBILA DASHBOARD   ({datetime.now().isoformat()[:19]} UTC)')
    show_real()
    show_sibila_bankrolls()
    show_realistic_by_sport()
    show_config()
    show_recent_activity()
    show_health()
    show_watchlist()
    print()
    hr()
