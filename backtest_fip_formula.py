"""
Backtest de la formula FIP -> probabilidad F5 ML.
Usa datos 2026 donde tenemos pitcher gamelogs.
"""
import sqlite3, json, os
from collections import defaultdict

F5_DB = "/home/noc/oraculo_v2/.oraculo_cache/mlb_f5.db"
PITCHER_CACHE = "/home/noc/oraculo_v2/.oraculo_cache/mlb_pitcher_logs.json"
FIP_CONST = 3.10
BASE_HOME_PROB = 0.538  # MLB historical home F5 win rate

def compute_fip(hr, bb, hbp, k, ip):
    if not ip or ip < 0.1:
        return 4.50
    return (13*hr + 3*(bb+hbp) - 2*k) / ip + FIP_CONST

def pitcher_recent_fip(games, as_of, n=3):
    past = [g for g in games if g["date"] < as_of][-n:]
    if not past:
        return None
    tot_ip  = sum(g.get("ip",0)  for g in past)
    tot_k   = sum(g.get("k",0)   for g in past)
    tot_bb  = sum(g.get("bb",0)  for g in past)
    tot_hbp = sum(g.get("hbp",0) for g in past)
    tot_hr  = sum(g.get("hr",0)  for g in past)
    return compute_fip(tot_hr, tot_bb, tot_hbp, tot_k, tot_ip)

def main():
    # Load pitcher cache
    with open(PITCHER_CACHE) as f:
        cache = json.load(f)

    conn = sqlite3.connect(F5_DB)
    rows = conn.execute("""
        SELECT game_pk, date, home, away, hp_id, ap_id, f5_home, f5_away
        FROM f5_games
        WHERE status=Final AND f5_home IS NOT NULL AND f5_away IS NOT NULL
          AND hp_id IS NOT NULL AND ap_id IS NOT NULL
          AND date >= 2026-03-01
        ORDER BY date ASC
    """).fetchall()
    conn.close()
    print("2026 games with F5+pitcher data candidates:", len(rows))

    results = []
    skipped_nodata = 0
    skipped_ties = 0

    for pk, date, home, away, hp_id, ap_id, f5_home, f5_away in rows:
        # F5 result
        if f5_home == f5_away:
            skipped_ties += 1
            continue
        actual_winner = "home" if f5_home > f5_away else "away"

        # Pitcher FIP
        hp_key = "%d_2026" % hp_id
        ap_key = "%d_2026" % ap_id
        hp_games = cache.get(hp_key, {}).get("games", [])
        ap_games = cache.get(ap_key, {}).get("games", [])

        hp_fip = pitcher_recent_fip(hp_games, date)
        ap_fip = pitcher_recent_fip(ap_games, date)

        if hp_fip is None and ap_fip is None:
            skipped_nodata += 1
            continue

        if hp_fip is None: hp_fip = 4.50
        if ap_fip is None: ap_fip = 4.50

        # Formula: fip_diff positive = away pitcher worse = home advantage
        fip_diff = ap_fip - hp_fip
        pitcher_adj = max(-0.08, min(0.08, fip_diff * 0.04))
        home_prob = min(0.95, max(0.05, BASE_HOME_PROB + pitcher_adj))

        # Model pick
        if home_prob > 0.50:
            model_pick = "home"
            model_prob = home_prob * 0.95
        else:
            model_pick = "away"
            model_prob = (1.0 - home_prob) * 0.95

        correct = (model_pick == actual_winner)
        results.append({
            "date": date, "home": home, "away": away,
            "hp_fip": round(hp_fip,2), "ap_fip": round(ap_fip,2),
            "fip_diff": round(fip_diff,2),
            "pitcher_adj": round(pitcher_adj,3),
            "model_pick": model_pick, "actual": actual_winner,
            "model_prob": round(model_prob,3), "correct": correct,
        })

    print("Skipped (no pitcher data): %d" % skipped_nodata)
    print("Skipped (ties):            %d" % skipped_ties)
    print("Backtested games:          %d" % len(results))
    if not results:
        print("No data to backtest.")
        return

    wins = sum(1 for r in results if r["correct"])
    wr = wins / len(results) * 100
    print()
    print("="*50)
    print("OVERALL WR: %d/%d = %.1f%%" % (wins, len(results), wr))
    print("Random baseline would be ~50.0%")
    print("="*50)

    # Baseline: always bet home
    home_wins = sum(1 for r in results if r["actual"] == "home")
    print("Home team win rate (baseline): %.1f%%" % (home_wins/len(results)*100))
    print()

    # Bucket by FIP differential magnitude
    print("WR by |fip_diff| bucket (signal strength):")
    print("  bucket   N    WR%")
    buckets_diff = defaultdict(lambda:{"w":0,"n":0})
    for r in results:
        b = abs(r["fip_diff"])
        if b < 0.5: key = "0.0-0.5"
        elif b < 1.0: key = "0.5-1.0"
        elif b < 1.5: key = "1.0-1.5"
        elif b < 2.0: key = "1.5-2.0"
        else: key = "2.0+"
        buckets_diff[key]["n"] += 1
        if r["correct"]: buckets_diff[key]["w"] += 1
    for k in ["0.0-0.5","0.5-1.0","1.0-1.5","1.5-2.0","2.0+"]:
        v = buckets_diff[k]
        n = v["n"]
        if n == 0: continue
        print("  %-10s  %4d  %5.1f%%" % (k, n, v["w"]/n*100))

    # Does FIP sign matter?
    print()
    print("WR by who model picks:")
    home_picks = [r for r in results if r["model_pick"]=="home"]
    away_picks = [r for r in results if r["model_pick"]=="away"]
    if home_picks:
        hw = sum(1 for r in home_picks if r["correct"])
        print("  Model picks HOME: %d/%d = %.1f%%" % (hw, len(home_picks), hw/len(home_picks)*100))
    if away_picks:
        aw = sum(1 for r in away_picks if r["correct"])
        print("  Model picks AWAY: %d/%d = %.1f%%" % (aw, len(away_picks), aw/len(away_picks)*100))

    # Top over/underperforming FIP differentials
    print()
    print("Sample games where FIP most disagreed with result:")
    wrong = sorted([r for r in results if not r["correct"]],
                   key=lambda x: abs(x["fip_diff"]), reverse=True)[:10]
    print("  %-12s %-20s %-20s  hp_fip ap_fip  pick   actual" % ("date","home","away"))
    for r in wrong:
        print("  %-12s %-20s %-20s  %5.2f  %5.2f  %-5s  %s" % (
            r["date"], r["home"][:18], r["away"][:18],
            r["hp_fip"], r["ap_fip"], r["model_pick"], r["actual"]))

    # Calibration: does high model_prob actually win more?
    print()
    print("Calibration (model_prob vs actual WR):")
    print("  prob_bucket  N    actual_WR")
    cal = defaultdict(lambda:{"w":0,"n":0})
    for r in results:
        b = round(r["model_prob"] * 20) / 20  # buckets of 5%
        cal[b]["n"] += 1
        if r["correct"]: cal[b]["w"] += 1
    for b in sorted(cal.keys()):
        v = cal[b]
        if v["n"] < 5: continue
        print("  %.0f%%        %4d  %5.1f%%" % (b*100, v["n"], v["w"]/v["n"]*100))

    print()
    print("CONCLUSION:")
    if wr > 52:
        print("  FIP formula has POSITIVE edge: %.1f%% WR > 50%% baseline" % wr)
    elif wr < 48:
        print("  FIP formula has NEGATIVE edge: %.1f%% WR < 50%% baseline (FADE it)" % wr)
    else:
        print("  FIP formula has NO edge: %.1f%% WR ≈ random" % wr)

if __name__ == "__main__":
    main()
