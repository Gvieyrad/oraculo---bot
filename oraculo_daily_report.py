#!/usr/bin/env python3
"""Daily performance report sent to Telegram at 08:00 UTC via cron."""
import json, os, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
CFG_FILE = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
STATE_FILE = os.path.join(SCRIPT_DIR, 'oraculo_auto_state.json')


def _send_telegram(token, chat_id, text):
    import urllib.request, urllib.parse
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'
    }).encode()
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=10)


def main():
    cfg = json.load(open(CFG_FILE))
    token = cfg.get('telegram_token', '')
    chat_id = cfg.get('telegram_chat_id', '')
    if not token or not chat_id:
        print('No telegram credentials in config')
        return

    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    bankroll = float(state.get('bankroll', 0) or 0)

    if not os.path.exists(LOG_FILE):
        _send_telegram(token, chat_id, 'Oraculo daily: no log data yet')
        return

    rows = [json.loads(l) for l in open(LOG_FILE)]
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    # Yesterday's settled bets
    yest_rows = [r for r in rows
                 if r.get('result') in ('WIN', 'LOSS')
                 and (r.get('settled_ts') or r.get('ts', ''))[:10] == str(yesterday)]
    yest_w = sum(1 for r in yest_rows if r['result'] == 'WIN')
    yest_l = len(yest_rows) - yest_w
    yest_pnl = sum(float(r.get('win_loss') or 0) for r in yest_rows)
    yest_stake = sum(float(r.get('stake') or 0) for r in yest_rows)
    yest_roi = (yest_pnl / yest_stake * 100) if yest_stake else 0

    # Last 7 days
    days7_ago = str(today - timedelta(days=7))
    d7_rows = [r for r in rows
               if r.get('result') in ('WIN', 'LOSS')
               and (r.get('settled_ts') or r.get('ts', ''))[:10] >= days7_ago]
    d7_w = sum(1 for r in d7_rows if r['result'] == 'WIN')
    d7_l = len(d7_rows) - d7_w
    d7_pnl = sum(float(r.get('win_loss') or 0) for r in d7_rows)
    d7_stake = sum(float(r.get('stake') or 0) for r in d7_rows)
    d7_roi = (d7_pnl / d7_stake * 100) if d7_stake else 0

    # All-time totals
    settled = [r for r in rows if r.get('result') in ('WIN', 'LOSS')]
    all_w = sum(1 for r in settled if r['result'] == 'WIN')
    all_l = len(settled) - all_w
    all_pnl = sum(float(r.get('win_loss') or 0) for r in settled)

    # Active bets
    active = state.get('active_bets', [])
    active_stake = sum(float(b.get('stake', 0) or 0) for b in active)

    # Sport breakdown (7d)
    sport_stats = defaultdict(lambda: {'W': 0, 'L': 0, 'pnl': 0.0})
    for r in d7_rows:
        sp = r.get('sport', '?')
        if r['result'] == 'WIN':
            sport_stats[sp]['W'] += 1
        else:
            sport_stats[sp]['L'] += 1
        sport_stats[sp]['pnl'] += float(r.get('win_loss') or 0)

    sport_lines = []
    for sp, v in sorted(sport_stats.items(), key=lambda x: -abs(x[1]['pnl'])):
        sport_lines.append(f'  {sp}: {v["W"]}W/{v["L"]}L ${v["pnl"]:+.2f}')

    sy = '+' if yest_pnl >= 0 else ''
    s7 = '+' if d7_pnl >= 0 else ''
    sa = '+' if all_pnl >= 0 else ''

    msg = f'<b>Oraculo — {today.strftime("%d %b %Y")}</b>\n\n'
    msg += f'<b>Ayer ({yesterday}):</b>\n'
    if yest_rows:
        msg += f'  {yest_w}W/{yest_l}L | ROI {sy}{yest_roi:.1f}% | PnL ${sy}{yest_pnl:.2f}\n'
    else:
        msg += '  Sin resultados\n'

    msg += f'\n<b>Ultimos 7 dias:</b>\n'
    msg += f'  {d7_w}W/{d7_l}L | ROI {s7}{d7_roi:.1f}% | PnL ${s7}{d7_pnl:.2f}\n'
    if sport_lines:
        msg += '\n'.join(sport_lines) + '\n'

    msg += f'\n<b>Total acumulado:</b> {all_w}W/{all_l}L | PnL ${sa}{all_pnl:.2f}\n'
    msg += f'<b>Bankroll:</b> ${bankroll:.2f}\n'
    msg += f'<b>Activos:</b> {len(active)} bets (${active_stake:.2f} en riesgo)\n'

    if d7_stake > 10 and d7_roi < -20:
        msg += '\n<b>ALERTA:</b> ROI 7d &lt; -20% — revisar modelo\n'

    try:
        _send_telegram(token, chat_id, msg)
        print('Report sent OK')
    except Exception as e:
        print('Send failed:', e)


if __name__ == '__main__':
    main()
