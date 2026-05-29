#!/usr/bin/env python3
"""
check_wc_odds.py — Daily odds-population check for WC 2026 Cloudbet markets.
Run Jun 1-10 via cron. Sends Telegram on first detection of non-zero odds.

Usage:
    python3 /home/noc/oraculo_v2/check_wc_odds.py
"""
import json, os, sys, urllib.request, urllib.parse
from datetime import date

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CB_CFG       = os.path.join(SCRIPT_DIR, 'cloudbet_config.json')
STATE_FILE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'wc_odds_state.json')
LOG_PATH     = os.path.join(SCRIPT_DIR, 'logs/check_wc_odds.log')
WC_KEY       = 'soccer-international-world-cup'
MARKETS      = ['soccer.total_goals', 'soccer.match_odds', 'soccer.total_goals_period_second_half']
TG_CHAT_ID   = '1521532947'
TG_TOKEN     = os.environ.get('TELEGRAM_BOT_TOKEN', '8238423049:AAF0KtHgp2oej4HRIQ-RqhD34xZWlH_OI1o')

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)


def log(msg):
    line = f"[{date.today()}] {msg}"
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


def cb_get(path, key):
    url = f'https://sports-api.cloudbet.com{path}'
    req = urllib.request.Request(url, headers={'X-API-Key': key, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, 'w'), indent=2)


def check_market_prices(events, market_key):
    """Return (total_events_with_market, events_with_nonzero_price)."""
    with_market = 0
    with_price  = 0
    sample      = []
    for ev in events[:20]:
        markets = ev.get('markets', {})
        if market_key not in markets:
            continue
        with_market += 1
        sequences = markets[market_key].get('submarkets', {})
        has_price = False
        for sub in sequences.values():
            for sel in sub.get('selections', []):
                p = float(sel.get('price', 0))
                if p > 1.0:
                    has_price = True
        if has_price:
            with_price += 1
            if len(sample) < 2:
                home = ev.get('home', {}).get('name', '?')
                away = ev.get('away', {}).get('name', '?')
                sample.append(f'{home} vs {away}')
    return with_market, with_price, sample


def main():
    log('=== check_wc_odds.py start ===')
    try:
        cfg = json.load(open(CB_CFG))
        api_key = cfg['api_key']
    except Exception as e:
        log(f'Config error: {e}')
        sys.exit(1)

    state = load_state()

    # Fetch WC competition events
    try:
        data   = cb_get(f'/pub/v2/odds/competitions/{WC_KEY}', api_key)
        events = data.get('events', [])
        match_events = [e for e in events if e.get('home') and e.get('away')]
        log(f'WC events: {len(events)} total, {len(match_events)} matches')
    except Exception as e:
        log(f'Cloudbet fetch failed: {e}')
        tg(f'⚠️ <b>WC Odds Check FAILED</b>\n{date.today()}\nError: {e}')
        sys.exit(1)

    results = {}
    all_populated = True
    for mkt in MARKETS:
        with_mkt, with_price, sample = check_market_prices(match_events, mkt)
        results[mkt] = {'with_market': with_mkt, 'with_price': with_price, 'sample': sample}
        populated = with_price > 0
        if not populated:
            all_populated = False
        log(f'  {mkt}: {with_mkt} events with market, {with_price} with non-zero price — {"✓ LIVE" if populated else "⏳ still 0"}')
        for s in sample:
            log(f'    example: {s}')

    today_str = date.today().isoformat()
    already_notified = state.get('notified_date', '') == today_str
    prev_any_live    = state.get('any_live', False)

    any_live = any(r['with_price'] > 0 for r in results.values())

    if any_live and not prev_any_live:
        # First time prices appeared
        lines = [f'🎰 <b>WC 2026 ODDS ARE LIVE on Cloudbet!</b> ({today_str})']
        for mkt, r in results.items():
            status = f"{r['with_price']}/{r['with_market']} matches priced"
            lines.append(f'  • {mkt}: {status}')
        lines.append('\n▶ Verificar: <code>oraculo_runner_auto.py</code> escaneará en el próximo ciclo.')
        lines.append('💰 Confirmar bankroll antes de Jun 12.')
        tg('\n'.join(lines))
        log('Telegram: FIRST LIVE notification sent')
        state['any_live']        = True
        state['first_live_date'] = today_str
    elif any_live and not already_notified:
        # Still live — daily status (don't spam, only once per day)
        counts = ', '.join(f"{r['with_price']}/{r['with_market']}" for r in results.values())
        tg(f'✅ <b>WC odds activos</b> ({today_str})\ntotal_goals / match_odds: {counts} matches priced')
        log('Telegram: daily live status sent')
    elif not any_live:
        log(f'Odds still 0 — no notification (prev_any_live={prev_any_live})')
        if not prev_any_live and date.today().isoformat() >= '2026-06-01':
            # Jun 1+ with no prices — warn if it's been 2+ days
            first_check = state.get('first_check_date', today_str)
            if first_check <= '2026-06-02' and today_str >= '2026-06-03':
                tg(f'⚠️ <b>WC odds aún en 0</b> — {today_str}\nCloudbet no ha cargado precios. Verificar manualmente.')
                log('Telegram: warning odds still 0 after Jun 3')

    state['last_check']        = today_str
    state['notified_date']     = today_str if any_live else state.get('notified_date', '')
    state['first_check_date']  = state.get('first_check_date', today_str)
    save_state(state)

    # Mexico Giménez check — read current player intel
    try:
        pf = json.load(open(os.path.join(SCRIPT_DIR, 'wc2026/wc_player_factors.json')))
        mex = pf.get('Mexico', {})
        atk = mex.get('attack_factor', 1.0)
        concerns = [c['player'] for c in mex.get('concerns', [])]
        if atk < 0.95:
            log(f'Mexico squad alert: atk={atk:.3f}, concerns={concerns}')
            if not state.get('mexico_alerted') and date.today().isoformat() >= '2026-06-01':
                tg(f'⚠️ <b>Mexico squad alert</b>\natk={atk:.2f}, concerns: {", ".join(concerns)}')
                state['mexico_alerted'] = True
                save_state(state)
        else:
            log(f'Mexico squad: atk={atk:.3f} — sin alertas')
    except Exception as e:
        log(f'Player intel check error: {e}')

    log('=== done ===')


if __name__ == '__main__':
    main()
