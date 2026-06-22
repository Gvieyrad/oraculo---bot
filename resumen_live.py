#!/usr/bin/env python3
"""Resumen LIVE-activos, ultimos 30 dias. Solo mercados habilitados HOY (lee flags del runner).
Formato fijo pedido por el usuario 2026-06-22: no mostrar acumulado historico ni mercados muertos."""
import sqlite3, re, os, sys
from datetime import datetime, timedelta

D = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(D, 'oraculo_runner_auto.py'), encoding='utf-8').read()
def flag(name, default=False):
    m = re.search(r'^%s\s*=\s*(True|False)' % re.escape(name), src, re.M)
    return (m.group(1) == 'True') if m else default

SOCCER = flag('SOCCER_ENABLED')
TENNIS = flag('TENNIS_CALIBRATION_LIVE') or True  # tennis_team_win_set corre live ($1)
F5ML   = flag('MLB_F5_ML_ENABLED')
F5TOT  = flag('MLB_F5_TOTAL_ENABLED')

# market_type -> live? (None = siempre live)
live_mkts = set()
if SOCCER: live_mkts |= {'soccer_goals','over_under','btts','asian_handicap','wc_1x2','result_1x2','other','-'}
if TENNIS: live_mkts |= {'tennis_team_win_set','tennis'}
if F5ML:   live_mkts.add('mlb_f5_ml')
if F5TOT:  live_mkts.add('mlb_f5_total')
live_mkts.add('result_1x2')  # NBA basketball

days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
c = sqlite3.connect(os.path.join(D, 'oraculo.db'))
rows = c.execute("""SELECT sport,market_type,COUNT(*),
  SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END),
  SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END),
  ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),2),
  ROUND(SUM(CASE WHEN pnl<0 THEN pnl ELSE 0 END),2),
  ROUND(SUM(pnl),2)
  FROM bet_history WHERE settled_at>=? GROUP BY sport,market_type""", (since,)).fetchall()
c.close()

def is_live(sp, mt):
    if sp == 'baseball' and mt in ('mlb_f5_ml','mlb_f5_total'):
        return (mt == 'mlb_f5_ml' and F5ML) or (mt == 'mlb_f5_total' and F5TOT)
    if sp == 'baseball':
        return False  # full-game MLB shadow-only
    return (mt or '-') in live_mkts

live = [r for r in rows if is_live(r[0], r[1])]
dead = [r for r in rows if not is_live(r[0], r[1])]

print('=== RESUMEN LIVE-ACTIVOS — ultimos %d dias (desde %s) ===' % (days, since))
print('%-9s %-16s %3s %5s %8s %8s %8s' % ('SPORT','MERCADO','n','W-L','GANADO','PERDIDO','NETO'))
print('-'*62)
tn=tw=tl=0; tg=tp=tnet=0.0
for sp,mt,n,w,l,g,p,net in sorted(live, key=lambda x:-(x[7] or 0)):
    print('%-9s %-16s %3d %5s %+8.2f %+8.2f %+8.2f' % ((sp or '?')[:9],(mt or '-')[:16],n,'%d-%d'%(w or 0,l or 0),g or 0,p or 0,net or 0))
    tn+=n; tw+=w or 0; tl+=l or 0; tg+=g or 0; tp+=p or 0; tnet+=net or 0
print('-'*62)
print('%-26s %3d %5s %+8.2f %+8.2f %+8.2f' % ('TOTAL LIVE',tn,'%d-%d'%(tw,tl),tg,tp,tnet))
if dead:
    dn=sum(r[2] for r in dead); dnet=sum(r[7] or 0 for r in dead)
    print('\n(excluidos por NO-live: %d apuestas, neto %+.2f — %s)' % (
        dn, dnet, ', '.join(sorted(set('%s/%s'%(r[0],r[1]) for r in dead)))))
print('\nFlags: SOCCER=%s TENNIS_CAL=%s F5ML=%s F5TOT=%s' % (SOCCER, flag('TENNIS_CALIBRATION_LIVE'), F5ML, F5TOT))
