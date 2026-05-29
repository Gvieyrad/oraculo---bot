#!/usr/bin/env python3
"""
pre_tournament_check.py — Jun 11 comprehensive pre-tournament checklist.
Runs: slug verify, bankroll check, service health, bracket sim, player intel.
Sends Telegram summary with full GO/NO-GO status.

Usage:
    python3 /home/noc/oraculo_v2/pre_tournament_check.py
"""
import json, os, sys, subprocess, urllib.request, urllib.parse
from datetime import datetime, date, timezone

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_FILE    = os.path.join(SCRIPT_DIR, 'oraculo_auto_state.json')
PLAYER_FILE   = os.path.join(SCRIPT_DIR, 'wc2026/wc_player_factors.json')
BRACKET_FILE  = os.path.join(SCRIPT_DIR, 'wc2026/bracket_probs.json')
LOG_PATH      = os.path.join(SCRIPT_DIR, 'logs/pre_tournament_check.log')
TG_CHAT_ID    = '1521532947'
TG_TOKEN      = os.environ.get('TELEGRAM_BOT_TOKEN', '8238423049:AAF0KtHgp2oej4HRIQ-RqhD34xZWlH_OI1o')
WC_KEY        = 'soccer-international-world-cup'

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

checks_passed = []
checks_failed = []
checks_warn   = []


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {msg}"
    print(line)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def tg(msg):
    if not TG_TOKEN:
        return
    try:
        url  = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        log(f'TG error: {e}')


def ok(label, detail=''):
    log(f'  ✓ {label}' + (f': {detail}' if detail else ''))
    checks_passed.append(label + (f' ({detail})' if detail else ''))


def fail(label, detail=''):
    log(f'  ✗ {label}' + (f': {detail}' if detail else ''))
    checks_failed.append(label + (f': {detail}' if detail else ''))


def warn(label, detail=''):
    log(f'  ⚠ {label}' + (f': {detail}' if detail else ''))
    checks_warn.append(label + (f': {detail}' if detail else ''))


def check_slug():
    log('--- 1. Cloudbet slug verification ---')
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, 'discover_wc_slug.py')],
        capture_output=True, text=True, cwd=SCRIPT_DIR
    )
    out = (result.stdout + result.stderr).strip()
    if 'works' in out or 'Found' in out:
        ok('Cloudbet slug', WC_KEY)
    elif 'not yet' in out.lower() or '404' in out:
        fail('Cloudbet slug', 'not found or no events')
    else:
        warn('Cloudbet slug', out[-200:])
    log(f'  discover output: {out[:300]}')


def check_bankroll():
    log('--- 2. Bankroll check ---')
    try:
        state = json.load(open(STATE_FILE))
        br = state.get('bankroll', 0)
        by_cur = state.get('bankroll_by_currency', {})
        usdc = by_cur.get('USDC', 0)
        usdt = by_cur.get('USDT', 0)
        if br >= 100:
            ok('Bankroll', f'${br:.2f} total (USDC={usdc}, USDT={usdt})')
        elif br >= 50:
            warn('Bankroll', f'${br:.2f} — low, WC stakes will be small')
        else:
            fail('Bankroll', f'${br:.2f} — critically low')
    except Exception as e:
        fail('Bankroll read', str(e))


def check_service():
    log('--- 3. oraculo-v2.service status ---')
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'oraculo-v2.service'],
            capture_output=True, text=True
        )
        status = result.stdout.strip()
        if status == 'active':
            ok('oraculo-v2.service', 'active/running')
        else:
            fail('oraculo-v2.service', status)
    except Exception as e:
        fail('Service check', str(e))


def check_player_intel():
    log('--- 4. Player intel freshness ---')
    try:
        pf = json.load(open(PLAYER_FILE))
        # Find most recent update
        times = [v.get('updated', '') for v in pf.values() if v.get('updated')]
        if times:
            latest = max(times)
            log(f'  Latest update: {latest}')
            # Check if within 8h
            try:
                dt = datetime.fromisoformat(latest.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                age_h = (now - dt).total_seconds() / 3600
                if age_h < 8:
                    ok('Player intel', f'updated {age_h:.1f}h ago')
                else:
                    warn('Player intel', f'last updated {age_h:.1f}h ago — run wc_player_intel.py')
            except Exception:
                warn('Player intel', f'could not parse timestamp: {latest}')

        # High-impact alerts
        alerts = [(t, v) for t, v in pf.items() if v.get('attack_factor', 1) < 0.85]
        if alerts:
            alert_str = ', '.join(f'{t}={v["attack_factor"]:.2f}' for t, v in sorted(alerts, key=lambda x: x[1]['attack_factor']))
            warn('High-impact injuries', alert_str)
        else:
            ok('Injury alerts', 'no atk<0.85 teams')

        # Mexico specifically
        mex = pf.get('Mexico', {})
        atk = mex.get('attack_factor', 1.0)
        concerns = [c['player'] for c in mex.get('concerns', [])]
        status = f'atk={atk:.2f}' + (f', concerns: {concerns}' if concerns else ', no concerns')
        if atk < 0.90:
            fail('Mexico Giménez check', status)
        else:
            ok('Mexico Giménez check', status)
    except Exception as e:
        fail('Player intel', str(e))


def run_bracket_sim():
    log('--- 5. Bracket sim (10k sims) ---')
    log('  Starting simulation...')
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, 'wc2026_bracket_sim.py'), '--sims', '10000'],
        capture_output=True, text=True, cwd=SCRIPT_DIR, timeout=300
    )
    if result.returncode == 0:
        ok('Bracket sim', 'completed, bracket_probs.json updated')
        # Extract top 5
        try:
            bp = json.load(open(BRACKET_FILE))
            top5 = sorted(bp.items(), key=lambda x: -x[1].get('win_tournament', 0))[:5]
            summary = ', '.join(f'{t} {v["win_tournament"]:.1%}' for t, v in top5)
            log(f'  Top 5 winners: {summary}')
            return summary
        except Exception:
            return 'sim done'
    else:
        fail('Bracket sim', result.stderr[-200:])
        return None


def check_wc_odds_live():
    log('--- 6. WC odds live check ---')
    try:
        cfg = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))
        key = cfg['api_key']
        url = f'https://sports-api.cloudbet.com/pub/v2/odds/competitions/{WC_KEY}'
        req = urllib.request.Request(url, headers={'X-API-Key': key, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        events = [e for e in data.get('events', []) if e.get('home') and e.get('away')]
        priced = 0
        for ev in events[:10]:
            for mkt_key in ['soccer.total_goals', 'soccer.match_odds']:
                mkt = ev.get('markets', {}).get(mkt_key, {})
                for sub in mkt.get('submarkets', {}).values():
                    for sel in sub.get('selections', []):
                        if float(sel.get('price', 0)) > 1.0:
                            priced += 1
        if priced > 0:
            ok('WC odds live', f'{len(events)} match events, prices populated')
        else:
            fail('WC odds NOT live', f'{len(events)} events but all prices 0')
    except Exception as e:
        fail('WC odds check', str(e))


def main():
    log('=== pre_tournament_check.py ===')
    log(f'Date: {date.today()} — WC starts Jun 12')

    check_slug()
    check_bankroll()
    check_service()
    check_player_intel()
    bracket_top5 = run_bracket_sim()
    check_wc_odds_live()

    # Verdict
    go_nogo = '🚀 GO' if not checks_failed else '🚫 NO-GO'
    log(f'\nVerdict: {go_nogo}')
    log(f'Passed: {len(checks_passed)}, Warnings: {len(checks_warn)}, Failed: {len(checks_failed)}')

    lines = [
        f'🏆 <b>Pre-Tournament Check — Jun 11</b>',
        f'Verdict: {go_nogo}',
        '',
        f'✅ Passed: {len(checks_passed)}',
        f'⚠️ Warnings: {len(checks_warn)}',
        f'❌ Failed: {len(checks_failed)}',
    ]
    if checks_failed:
        lines.append('\n<b>FAILED:</b>')
        for c in checks_failed:
            lines.append(f'  ❌ {c}')
    if checks_warn:
        lines.append('\n<b>WARNINGS:</b>')
        for c in checks_warn:
            lines.append(f'  ⚠️ {c}')
    if bracket_top5:
        lines.append(f'\n🎯 Bracket top 5: {bracket_top5}')
    lines.append(f'\n💰 Bankroll: actualizar antes de arrancar scanner')
    lines.append(f'▶ Jun 12: grep \'WC\\|world-cup\' logs/oraculo_auto.log')
    tg('\n'.join(lines))
    log('Telegram summary sent')
    log('=== done ===')


if __name__ == '__main__':
    main()
