#!/usr/bin/env python3
"""
update_intl_csv.py — Download latest martj42 international results, add new matches
to intl_results_5y.csv, and retrain pb_model.pkl if enough new matches are found.

Usage:
    python3 /home/noc/oraculo_v2/update_intl_csv.py
    python3 /home/noc/oraculo_v2/update_intl_csv.py --force-retrain
    python3 /home/noc/oraculo_v2/update_intl_csv.py --dry-run

Designed to run daily June 1-11 via cron or manually.
"""
import sys, os, csv, io, json, urllib.request, argparse
from datetime import date

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
CSV_PATH      = os.path.join(SCRIPT_DIR, 'wc2026/intl_results_5y.csv')
MODEL_PATH    = os.path.join(SCRIPT_DIR, 'wc2026/pb_model.pkl')
MODEL_SCRIPT  = os.path.join(SCRIPT_DIR, 'wc2026/fit_pb_model.py')
LOG_PATH      = os.path.join(SCRIPT_DIR, 'logs/update_intl.log')

SOURCE_URL = 'https://raw.githubusercontent.com/martj42/international_results/master/results.csv'
CUTOFF_DATE = '2021-05-01'   # keep last ~5 years
MIN_NEW_FOR_RETRAIN = 10     # retrain only if ≥10 new real matches found
RETRAIN_MODEL_SCRIPT = os.path.join(SCRIPT_DIR, 'wc2026/retrain_pb_model.py')


def log(msg):
    ts = date.today().isoformat()
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def load_existing():
    """Return set of (date, home_team, away_team) already in CSV."""
    seen = set()
    with open(CSV_PATH, newline='') as f:
        for row in csv.DictReader(f):
            if row.get('home_score', 'NA') not in ('NA', ''):
                seen.add((row['date'], row['home_team'], row['away_team']))
    return seen


def download_source():
    req = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode('utf-8', errors='replace')
    return list(csv.DictReader(io.StringIO(raw)))


def filter_new_real_matches(all_rows, existing_keys):
    """Return real matches after cutoff that aren't in existing CSV (no NA scores)."""
    new = []
    for row in all_rows:
        d = row.get('date', '')
        if d < CUTOFF_DATE:
            continue
        hs = row.get('home_score', 'NA')
        as_ = row.get('away_score', 'NA')
        if hs in ('NA', '') or as_ in ('NA', ''):
            continue
        key = (d, row['home_team'], row['away_team'])
        if key not in existing_keys:
            new.append(row)
    return new


def append_to_csv(new_rows, dry_run=False):
    fieldnames = ['date','home_team','away_team','home_score','away_score',
                  'tournament','city','country','neutral']
    if dry_run:
        log(f"DRY RUN: would append {len(new_rows)} rows")
        for row in new_rows[:5]:
            log(f"  {row['date']}: {row['home_team']} {row['home_score']}-{row['away_score']} {row['away_team']}")
        return
    with open(CSV_PATH, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        for row in new_rows:
            writer.writerow(row)
    log(f"Appended {len(new_rows)} new matches to {CSV_PATH}")


def retrain(dry_run=False):
    """Re-fit the penaltyblog DixonColes model from the updated CSV."""
    script = RETRAIN_MODEL_SCRIPT
    if not os.path.exists(script):
        log(f"Retrain script not found: {script} — skipping retrain")
        return False
    if dry_run:
        log("DRY RUN: would run retrain script")
        return True
    import subprocess
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, cwd=SCRIPT_DIR
    )
    if result.returncode == 0:
        log("Retrain succeeded")
        log(result.stdout.strip()[-500:] if result.stdout else '')
        return True
    else:
        log(f"Retrain FAILED (exit {result.returncode})")
        log(result.stderr.strip()[-500:] if result.stderr else '')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force-retrain', action='store_true')
    args = parser.parse_args()

    log("=== update_intl_csv.py start ===")

    log("Loading existing CSV...")
    existing = load_existing()
    log(f"  {len(existing)} existing real matches")

    log("Downloading source from GitHub...")
    try:
        all_rows = download_source()
    except Exception as e:
        log(f"Download failed: {e}")
        sys.exit(1)
    log(f"  Source: {len(all_rows)} total rows")

    new_rows = filter_new_real_matches(all_rows, existing)
    log(f"  New real matches found: {len(new_rows)}")

    if not new_rows:
        log("No new matches — nothing to do")
        if args.force_retrain:
            log("--force-retrain: retraining anyway")
            retrain(args.dry_run)
        return

    # Show what's new
    for row in sorted(new_rows, key=lambda x: x['date'])[:10]:
        log(f"  + {row['date']}: {row['home_team']} {row['home_score']}-{row['away_score']} "
            f"{row['away_team']} [{row['tournament']}]")
    if len(new_rows) > 10:
        log(f"  ... and {len(new_rows)-10} more")

    append_to_csv(new_rows, args.dry_run)

    if args.force_retrain or len(new_rows) >= MIN_NEW_FOR_RETRAIN:
        log(f"Triggering retrain ({len(new_rows)} new matches >= {MIN_NEW_FOR_RETRAIN} threshold)")
        retrain(args.dry_run)
    else:
        log(f"Only {len(new_rows)} new matches < {MIN_NEW_FOR_RETRAIN} threshold; skipping retrain")
        log("Run with --force-retrain to retrain anyway")

    log("=== done ===")


if __name__ == '__main__':
    main()
