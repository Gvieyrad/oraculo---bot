import sys, urllib.request, json, difflib
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/home/noc/oraculo_v2')

KEY = '1f271971-8189-483d-ad0d-f9260c8a5896'

ob_data = json.loads(urllib.request.urlopen(
    'https://odds.oddsblaze.com/?key=%s&sportsbook=polymarket&league=fifa-world-cup' % KEY,
    timeout=8).read())
pm_lookup = {}
for ev in ob_data.get('events', []):
    h = ev['teams']['home']['name']
    a = ev['teams']['away']['name']
    ml = {}
    for odd in ev.get('odds', []):
        if odd['market'] != 'Moneyline 3-Way': continue
        sel = odd['name']
        try:
            p = int(odd['price'])
            imp = (100/(p+100)) if p > 0 else (abs(p)/(abs(p)+100))
        except: continue
        if sel == h: ml['home'] = imp
        elif sel == a: ml['away'] = imp
        else: ml['draw'] = imp
    if len(ml) >= 3:
        t = sum(ml.values())
        pm_lookup[(h.lower(), a.lower())] = {k: v/t for k,v in ml.items()}

from oraculo_runner_auto import CloudbetAPI
from oraculo_intl_elo import IntlElo

api = CloudbetAPI()
cb_events = api.get_odds('soccer-international-world-cup')
intl_elo = IntlElo(); intl_elo.load()

cutoff = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
picks = []

for ev in cb_events:
    if not ev: continue
    ev_cutoff = ev.get('cutoffTime', '')
    if ev_cutoff and ev_cutoff > cutoff: continue
    home = (ev.get('home') or {}).get('name', '')
    away = (ev.get('away') or {}).get('name', '')
    if not home or not away: continue
    markets = ev.get('markets', {})
    ft_mkt = markets.get('soccer.match_odds', {})
    odds_1x2 = {}
    for sv in ft_mkt.get('submarkets', {}).values():
        for s in sv.get('selections', []):
            out = s.get('outcome', ''); price = float(s.get('price',0) or 0)
            if out in ('home','draw','away') and price > 1.1: odds_1x2[out] = price
    if len(odds_1x2) < 2: continue

    h_lc, a_lc = home.lower(), away.lower()
    pm_ref = pm_lookup.get((h_lc, a_lc))
    if not pm_ref:
        best, best_k = 0, None
        for (ho, ao) in pm_lookup:
            sc = difflib.SequenceMatcher(None, h_lc, ho).ratio() * difflib.SequenceMatcher(None, a_lc, ao).ratio()
            if sc > best: best, best_k = sc, (ho, ao)
        if best >= 0.30 and best_k: pm_ref = pm_lookup[best_k]

    try:
        dc_ph, dc_pd, dc_pa, _, _ = intl_elo.predict_match_wc(home, away, True)
    except: continue

    date = ev_cutoff[:10] if ev_cutoff else '?'
    for out_key, dc_p in [('home', dc_ph), ('draw', dc_pd), ('away', dc_pa)]:
        if out_key not in odds_1x2: continue
        price = odds_1x2[out_key]
        dc_edge = dc_p * price - 1
        pm_p = pm_ref.get(out_key) if pm_ref else None
        pm_edge = pm_p * price - 1 if pm_p else None
        if dc_edge >= 0.10 and dc_p >= 0.55:
            mark = ' STAR' if (pm_edge is not None and pm_edge >= 0) else (' XMKT' if pm_edge is not None else '')
            picks.append((date, home, away, out_key, price, dc_p, dc_edge, pm_p, pm_edge, mark))

picks.sort()
print('Date       Match                           S    Price  DC%   DCedge  PM%   PMedge')
print('-'*90)
for row in picks:
    date,home,away,out,price,dc_p,dc_e,pm_p,pm_e,mark = row
    pm_s = '%.1f' % (pm_p*100) if pm_p else ' n/a'
    pm_e_s = '%+.1f' % (pm_e*100) if pm_e is not None else '  n/a'
    print('%s  %-16s vs %-14s  %-4s  %.3f  %.1f%%  %+.1f%%  %s%%  %s%%  %s' % (
        date, home[:16], away[:14], out[:4], price, dc_p*100, dc_e*100, pm_s, pm_e_s, mark))

ok = [p for p in picks if 'STAR' in p[9]]
print('\nDC picks >=10pct/55conf: %d | Polymarket-confirmed: %d' % (len(picks), len(ok)))
if ok:
    print('Avg DC edge (confirmed): %.1f%%' % (sum(p[6] for p in ok)/len(ok)*100))
    print('Avg PM edge (confirmed): %.1f%%' % (sum(p[8] for p in ok)/len(ok)*100))
