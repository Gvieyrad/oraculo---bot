#!/usr/bin/env python3
"""
auto_retrain_wc.py — Auto-retrain DC model during WC 2026 when >=5 new match results detected.
Cron (Jun 12 – Jul 19): 0 */6 * * * python3 /home/noc/oraculo_v2/auto_retrain_wc.py >> /home/noc/oraculo_v2/logs/auto_retrain.log 2>&1
"""
import subprocess, csv, os, sys
from datetime import date

SCRIPT_DIR  = '/home/noc/oraculo_v2'
CSV_PATH    = f'{SCRIPT_DIR}/wc2026/intl_results_5y.csv'
MARKER      = f'{SCRIPT_DIR}/wc2026/.last_retrain_count'
RETRAIN_SC  = f'{SCRIPT_DIR}/wc2026/retrain_pb_model.py'
UPDATE_SC   = f'{SCRIPT_DIR}/update_intl_csv.py'
MIN_NEW     = 5   # retrain threshold: matches since last retrain

today = date.today()
if not (date(2026, 6, 12) <= today <= date(2026, 7, 19)):
    print(f"[{today}] Outside tournament window (Jun 12–Jul 19) — skip")
    sys.exit(0)

print(f"[{today}] auto_retrain_wc.py start")

# Step 1: pull new results into CSV
r = subprocess.run(['python3', UPDATE_SC], capture_output=True, text=True)
if r.stdout:
    print(r.stdout[-400:])
if r.returncode != 0:
    print(f"  update_intl_csv failed (exit {r.returncode}) — aborting retrain")
    sys.exit(1)

# Step 2: count completed WC 2026 matches
wc_done = 0
with open(CSV_PATH, newline='') as f:
    for row in csv.DictReader(f):
        if 'World Cup' not in row.get('tournament', ''):
            continue
        if row.get('home_score', 'NA') in ('NA', ''):
            continue
        if row.get('date', '') >= '2026-06-12':
            wc_done += 1

# Step 3: compare to last retrain marker
last = 0
if os.path.exists(MARKER):
    try:
        last = int(open(MARKER).read().strip())
    except Exception:
        pass

new_since = wc_done - last
print(f"  WC completed: {wc_done}  last retrain: {last}  new: {new_since}")

if new_since < MIN_NEW:
    print(f"  < {MIN_NEW} new matches — skip retrain")
    sys.exit(0)

# Step 4: retrain
print(f"  Retraining ({new_since} new matches)...")
r2 = subprocess.run(['python3', RETRAIN_SC], capture_output=True, text=True)
print(r2.stdout[-600:] if r2.stdout else "(no output)")
if r2.returncode != 0:
    print("RETRAIN FAILED:\n" + r2.stderr[:400])
    sys.exit(1)

# Step 5: update marker (model pkl mtime handles live cache invalidation)
open(MARKER, 'w').write(str(wc_done))
print(f"  OK — marker → {wc_done}")
