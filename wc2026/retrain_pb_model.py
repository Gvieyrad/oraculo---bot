#!/usr/bin/env python3
"""
retrain_pb_model.py — Re-fit penaltyblog DixonColes model from intl_results_5y.csv.
Run after update_intl_csv.py adds new warmup matches.

Usage:
    python3 /home/noc/oraculo_v2/wc2026/retrain_pb_model.py

Output: /home/noc/oraculo_v2/wc2026/pb_model.pkl (overwrites existing)
"""
import sys, os, csv, pickle
from datetime import date, datetime

sys.path.insert(0, '/home/noc/.local/lib/python3.12/site-packages')

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR  = os.path.dirname(SCRIPT_DIR)
CSV_PATH    = os.path.join(SCRIPT_DIR, 'intl_results_5y.csv')
MODEL_PATH  = os.path.join(SCRIPT_DIR, 'pb_model.pkl')
XI          = 0.0018   # time-decay weight (same as original fit)

print(f"[{date.today()}] retrain_pb_model.py")
print(f"  CSV: {CSV_PATH}")
print(f"  Model output: {MODEL_PATH}")
print(f"  xi={XI}")

import penaltyblog as pb
import numpy as np

# Load CSV - only real matches (no NA scores)
rows = []
with open(CSV_PATH, newline='') as f:
    for row in csv.DictReader(f):
        hs = row.get('home_score', 'NA')
        as_ = row.get('away_score', 'NA')
        if hs in ('NA', '') or as_ in ('NA', ''):
            continue
        try:
            rows.append({
                'date':       row['date'],
                'home_team':  row['home_team'],
                'away_team':  row['away_team'],
                'home_goals': int(hs),
                'away_goals': int(as_),
                'tournament': row.get('tournament', ''),
            })
        except (ValueError, KeyError):
            continue

print(f"  Loaded {len(rows)} real matches")

# Compute time-decay weights
today_str = date.today().isoformat()
def days_ago(d_str):
    try:
        return (datetime.strptime(today_str, '%Y-%m-%d') - datetime.strptime(d_str, '%Y-%m-%d')).days
    except Exception:
        return 1000

def _comp_weight(t):
    t = str(t).lower()
    if 'world cup' in t and 'qualif' not in t: return 2.0
    if 'qualif' in t: return 1.5
    if any(x in t for x in ['copa am', 'euro', 'african cup', 'afcon',
                             'gold cup', 'nations league', 'asian cup', 'concacaf nations',
                             'arab cup', 'oceania nations']): return 1.5
    if 'friendly' in t or 'international' == t.strip(): return 0.5
    return 1.0

weights = np.array([np.exp(-XI * days_ago(r['date'])) for r in rows])
comp_weights = np.array([_comp_weight(r.get('tournament', '')) for r in rows])
weights = weights * comp_weights
weights = weights / weights.max()   # normalize to [0,1]

home_goals  = [r['home_goals'] for r in rows]
away_goals  = [r['away_goals'] for r in rows]
teams_home  = [r['home_team'] for r in rows]
teams_away  = [r['away_team'] for r in rows]

teams = sorted(set(teams_home) | set(teams_away))
print(f"  Teams: {len(teams)}")

print("  Fitting DixonColes model...")
model = pb.models.DixonColesGoalModel(
    home_goals, away_goals, teams_home, teams_away, weights=weights
)
model.fit()
print("  Fit complete")

# Backup existing model (try daily stamp first, fallback to timestamp)
if os.path.exists(MODEL_PATH):
    import shutil
    _bak = MODEL_PATH + f".bak_{date.today().strftime(chr(37)+chr(89)+chr(109)+chr(37)+chr(100))}"
    try:
        shutil.copy2(MODEL_PATH, _bak)
        print(f"  Backed up → {_bak}")
    except PermissionError:
        _bak2 = MODEL_PATH + f".bak_{datetime.now().strftime(chr(37)+chr(89)+chr(109)+chr(37)+chr(100)+chr(95)+chr(37)+chr(72)+chr(37)+chr(77)+chr(37)+chr(83))}"
        try:
            shutil.copy2(MODEL_PATH, _bak2)
            print(f"  Backed up → {_bak2}")
        except Exception:
            print("  Backup skipped (permission denied)")

model.save(MODEL_PATH)
try:
    import pwd as _pwd, grp as _grp
    os.chown(MODEL_PATH, _pwd.getpwnam("noc").pw_uid, _grp.getgrnam("noc").gr_gid)
except Exception:
    pass
print(f"  Saved → {MODEL_PATH}")

# Quick sanity check
print("\n  Sanity check — predict Brazil vs Argentina:")
result = model.predict('Brazil', 'Argentina', max_goals=15)
p_home = result.home_win
p_draw = result.draw
p_away = result.away_win
print(f"    Brazil win: {p_home:.1%}  Draw: {p_draw:.1%}  Argentina win: {p_away:.1%}")
print("  retrain complete.")
