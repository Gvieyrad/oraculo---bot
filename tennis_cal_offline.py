import sqlite3, sys
sys.path.insert(0, '/home/noc/oraculo_v2')
from oraculo_tennis_calib import calibrate_tennis_prob as cal

c = sqlite3.connect('/home/noc/oraculo_v2/sibila.db')
rows = c.execute("""SELECT prob_model, odds, clv, result FROM sibila_picks
  WHERE sport='tennis' AND market_type='tennis_team_win_set'
  AND prob_model>0 AND odds>1""").fetchall()
c.close()

tot = len(rows)
with_clv = [r for r in rows if r[2] is not None]
print("team_win_set total:", tot, "| con CLV:", len(with_clv))

def stats(sel, label):
    if not sel:
        print("  %-22s n=0" % label); return
    clvs = [r[2] for r in sel if r[2] is not None]
    res  = [r[3] for r in sel if r[3] in ('WIN','LOSS')]
    avg_clv = sum(clvs)/len(clvs)*100 if clvs else float('nan')
    pos = sum(1 for x in clvs if x>0)
    wr = sum(1 for x in res if x=='WIN')/len(res)*100 if res else float('nan')
    # ROI realizado
    roi = None
    pnl = sum((o-1) if rr=='WIN' else (-1) for p,o,cc,rr in sel if rr in ('WIN','LOSS'))
    nres = sum(1 for p,o,cc,rr in sel if rr in ('WIN','LOSS'))
    roi = pnl/nres*100 if nres else float('nan')
    print("  %-22s n=%3d | CLV avg=%+5.2f%% (%d/%d pos) | WR=%4.1f%% ROI=%+5.1f%% (n_res=%d)" % (
        label, len(sel), avg_clv, pos, len(clvs), wr, roi, nres))

# RAW: lo que el sistema live considera (prob cruda)
print("\n=== RAW (prob cruda del modelo) ===")
stats(rows, "todas team_win_set")
stats([r for r in rows if r[0]*r[1]-1 >= 0.10], "raw edge>=0.10")

# CALIBRADO: aplicar traductor isotonic, varios umbrales
print("\n=== CALIBRADO (prob isotonic) a distintos umbrales ===")
calrows = [(p, o, cc, rr, cal(p)) for (p,o,cc,rr) in rows]
for thr in (0.10, 0.05, 0.03, 0.0):
    sel = [(p,o,cc,rr) for (p,o,cc,rr,cp) in calrows if cp*o-1 >= thr and cp>=0.55]
    stats(sel, "cal edge>=%.2f" % thr)
