#!/usr/bin/env python3
# Tier 1 robustez (2026-06-17): alerta si WR live de un mercado diverge del shadow (caza tipo F5 ML 58%->24%)
import sqlite3, os, json
SD=os.path.dirname(os.path.abspath(__file__))
d=sqlite3.connect(os.path.join(SD,'sibila.db'))
def wr(placed, mt):
    r=d.execute("SELECT COUNT(*),SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) FROM sibila_picks WHERE market_type=? AND placed=? AND result IN ('WIN','LOSS')",(mt,placed)).fetchone()
    return (r[0] or 0, (100*r[1]/r[0]) if r[0] else None)
mkts=[r[0] for r in d.execute("SELECT DISTINCT market_type FROM sibila_picks WHERE market_type IS NOT NULL AND market_type!=''").fetchall()]
alerts=[]
print('mercado | shadow WR (n) | live WR (n) | divergencia')
for mt in mkts:
    ns,ws=wr(0,mt); nl,wl=wr(1,mt)
    if ns>=20 and nl>=20 and ws is not None and wl is not None:
        div=abs(ws-wl)
        flag='  <-- ALERTA' if div>15 else ''
        print('  %-22s shadow=%2.0f%%(n%d) live=%2.0f%%(n%d) div=%.0fpp%s'%(mt,ws,ns,wl,nl,div,flag))
        if div>15: alerts.append('%s: shadow %.0f%% vs live %.0f%% (div %.0fpp)'%(mt,ws,wl,div))
if alerts:
    try:
        import oraculo_runner_auto as R
        R.send_whatsapp('Oraculo DIVERGENCIA shadow/live:'+chr(10)+'- '+(chr(10)+'- ').join(alerts))
    except Exception as e: print('telegram err',e)
    print('ALERTAS:',len(alerts))
else:
    print('Sin divergencias >15pp (mercados con n>=20 shadow Y live)')
