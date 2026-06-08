"""
Monitor de degradacion de modelos — Regla 5
Corre cada lunes via cron. Lee oraculo.db y alerta si WR cae.
"""
import sqlite3, os, sys
from datetime import datetime, timedelta
from collections import defaultdict

DB = "/home/noc/oraculo_v2/oraculo.db"
DOCS = "/home/noc/oraculo_v2/docs"
RUNNER = "/home/noc/oraculo_v2/oraculo_runner_auto.py"

# Umbrales Regla 5
WARN_WR   = 0.50   # alerta manual
KILL_WR   = 0.45   # desactivar automatico
WARN_N    = 15     # minimo picks para alerta
KILL_N    = 10     # minimo picks para kill
INACTIVE_DAYS = 14 # sin picks = aviso

# Mercados activos que queremos monitorear
ACTIVE_MARKETS = {
    ("tennis",   "tennis_team_win_set"),
    ("tennis",   "tennis"),
    ("soccer",   "over_under"),
    ("soccer",   "soccer_goals"),
    ("soccer",   "btts"),
}

def get_rolling_wr(conn, sport, market, days=30):
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT result, pnl, placed_at FROM bet_history "
        "WHERE sport=? AND market_type=? AND result IN ('WIN','LOSS') "
        "AND placed_at >= ? ORDER BY placed_at",
        (sport, market, cutoff)
    ).fetchall()
    if not rows:
        return 0, 0, None
    w = sum(1 for r in rows if r[0] == "WIN")
    n = len(rows)
    last = rows[-1][2][:10]
    return w, n, last

def disable_market(market_flag):
    with open(RUNNER) as f:
        content = f.read()
    if market_flag not in content:
        return False
    old = market_flag + " = True"
    new = market_flag + " = False       # monitor_degradacion.py auto-disabled " + datetime.utcnow().strftime("%Y-%m-%d")
    if old not in content:
        return False
    with open(RUNNER, "w") as f:
        f.write(content.replace(old, new))
    os.system("systemctl restart oraculo-v2.service")
    return True

MARKET_FLAGS = {
    ("tennis",  "tennis_team_win_set"): "TENNIS_SET_ENABLED",
    ("soccer",  "over_under"):          "SOCCER_GOALS_ENABLED",
    ("soccer",  "soccer_goals"):        "SOCCER_GOALS_ENABLED",
}

def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = ["REPORTE DEGRADACION — " + now, "=" * 55]

    conn = sqlite3.connect(DB)
    alerts = []
    kills  = []

    for sport, market in sorted(ACTIVE_MARKETS):
        w, n, last = get_rolling_wr(conn, sport, market, days=30)

        if n == 0:
            # Check if inactive
            w7, n7, last7 = get_rolling_wr(conn, sport, market, days=INACTIVE_DAYS)
            if n7 == 0:
                lines.append("AVISO   %-10s %-22s — sin picks en %dd" % (sport, market, INACTIVE_DAYS))
            continue

        wr = w / n
        status = "OK     "
        if wr < KILL_WR and n >= KILL_N:
            status = "KILL   "
            kills.append((sport, market, wr, n))
        elif wr < WARN_WR and n >= WARN_N:
            status = "ALERTA "
            alerts.append((sport, market, wr, n))

        lines.append("%s %-10s %-22s  n=%3d  WR=%5.1f%%  ultimo=%s" % (
            status, sport, market, n, wr*100, last or "?"))

    conn.close()

    lines.append("")
    if kills:
        lines.append("MERCADOS DESACTIVADOS AUTOMATICAMENTE:")
        for sport, market, wr, n in kills:
            flag = MARKET_FLAGS.get((sport, market))
            if flag:
                ok = disable_market(flag)
                lines.append("  %s/%s WR=%.1f%% n=%d — flag %s=%s" % (
                    sport, market, wr*100, n, flag, "DISABLED" if ok else "ERROR"))
            else:
                lines.append("  %s/%s WR=%.1f%% n=%d — SIN FLAG CONFIGURADO, revisar manualmente" % (
                    sport, market, wr*100, n))

    if alerts:
        lines.append("MERCADOS EN ALERTA (reducir cap a $1 manualmente):")
        for sport, market, wr, n in alerts:
            lines.append("  %s/%s WR=%.1f%% n=%d" % (sport, market, wr*100, n))

    if not kills and not alerts:
        lines.append("Sin alertas — todos los modelos dentro de umbral.")

    report = "\n".join(lines)
    print(report)

    # Save report
    os.makedirs(DOCS, exist_ok=True)
    fname = os.path.join(DOCS, "degradacion_%s.txt" % datetime.utcnow().strftime("%Y-%m-%d"))
    with open(fname, "w") as f:
        f.write(report + "\n")

    return 1 if (kills or alerts) else 0

if __name__ == "__main__":
    sys.exit(main())
