"""
Soccer corners/bookings model backtest.
Uses the same historical CSVs as oraculo_soccer.py.
Splits by match order (first 80% = train, last 20% = test per team).
Measures: booking pts calibration, corner winner accuracy, EV per market.
"""
import os, json, math, sys
from collections import defaultdict

SCRIPT_DIR = '/home/noc/oraculo_v2'
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

LEAGUE_FILES = ['E0_2526.json', 'D1_2526.json', 'F1_2526.json',
                'SP1_2526.json', 'I1_2526.json']

UCL_CARD_MULTIPLIER   = 1.45
UCL_CORNER_HOME_BOOST = 1.05


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def load_all_matches():
    matches = []
    for fname in LEAGUE_FILES:
        fpath = os.path.join(CSV_DIR, fname)
        if not os.path.exists(fpath):
            print(f'  MISSING: {fname}')
            continue
        with open(fpath) as f:
            data = json.load(f)
        league = fname.replace('_2526.json', '')
        for m in data:
            ht = m.get('home_team', '')
            at = m.get('away_team', '')
            if not ht or not at:
                continue
            hc = m.get('home_corners', 0) or 0
            ac = m.get('away_corners', 0) or 0
            hy = m.get('home_yellow', 0) or 0
            ay = m.get('away_yellow', 0) or 0
            hr = m.get('home_red', 0) or 0
            ar = m.get('away_red', 0) or 0
            hf = m.get('home_fouls', 0) or 0
            af = m.get('away_fouls', 0) or 0
            date = m.get('date', '') or m.get('Date', '')
            actual_bp = 10 * (hy + ay) + 25 * (hr + ar)
            corner_winner = 'home' if hc > ac else ('away' if ac > hc else 'draw')
            matches.append({
                'league': league,
                'date': date,
                'home': ht, 'away': at,
                'home_corners': hc, 'away_corners': ac,
                'home_yellow': hy, 'away_yellow': ay,
                'home_red': hr, 'away_red': ar,
                'home_fouls': hf, 'away_fouls': af,
                'actual_bp': actual_bp,
                'corner_winner': corner_winner,
                'total_corners': hc + ac,
            })
    return matches


def build_model_at(train_matches):
    """Build SoccerModel stats from a list of training matches."""
    home = defaultdict(lambda: dict(cf=0, ca=0, y=0, r=0, fo=0, n=0))
    away = defaultdict(lambda: dict(cf=0, ca=0, y=0, r=0, fo=0, n=0))
    for m in train_matches:
        ht, at = m['home'], m['away']
        h = home[ht]; a = away[at]
        h['cf'] += m['home_corners']; h['ca'] += m['away_corners']
        h['y'] += m['home_yellow']; h['r'] += m['home_red']
        h['fo'] += m['home_fouls']; h['n'] += 1
        a['cf'] += m['away_corners']; a['ca'] += m['home_corners']
        a['y'] += m['away_yellow']; a['r'] += m['away_red']
        a['fo'] += m['away_fouls']; a['n'] += 1
    return home, away


def predict_match(home_stats, away_stats, home_name, away_name, is_ucl=False):
    hs = home_stats.get(home_name)
    as_ = away_stats.get(away_name)
    if not hs or not as_ or hs['n'] == 0 or as_['n'] == 0:
        return None, None

    # --- corners ---
    lh = (hs['cf'] / hs['n'] + as_['ca'] / as_['n']) / 2.0
    la = (as_['cf'] / as_['n'] + hs['ca'] / hs['n']) / 2.0
    if is_ucl:
        lh *= UCL_CORNER_HOME_BOOST
    total_c = lh + la if (lh + la) > 0 else 1.0
    p_home_corner = lh / total_c

    # --- bookings ---
    mult = UCL_CARD_MULTIPLIER if is_ucl else 1.0
    h_y = (hs['y'] / hs['n']) * mult
    a_y = (as_['y'] / as_['n']) * mult
    h_r = (hs['r'] / hs['n']) * mult
    a_r = (as_['r'] / as_['n']) * mult
    exp_bp   = 10 * (h_y + a_y) + 25 * (h_r + a_r)
    var_bp   = 100 * (h_y + a_y) + 625 * (h_r + a_r)
    std_bp   = math.sqrt(var_bp) if var_bp > 0 else 5.0
    rate_h   = h_y + 2 * h_r
    rate_a   = a_y + 2 * a_r
    total_rate = rate_h + rate_a
    p_home_first = rate_h / total_rate if total_rate > 0 else 0.5

    corners = {'lambda_home': lh, 'lambda_away': la, 'p_home': p_home_corner,
               'n_home': hs['n'], 'n_away': as_['n']}
    bookings = {'exp_bp': exp_bp, 'std_bp': std_bp,
                'p_home_first': p_home_first, 'p_away_first': 1 - p_home_first,
                'n_home': hs['n'], 'n_away': as_['n']}
    return corners, bookings


def p_over_bp(exp_bp, std_bp, line):
    if std_bp <= 0:
        return 0.5
    z = (line - exp_bp) / std_bp
    return _norm_cdf(-z)


# ──────────────────────────────────────────────────────────────────────────────
def run_backtest():
    print('Loading matches...')
    all_matches = load_all_matches()
    print(f'Total: {len(all_matches)} matches across {len(LEAGUE_FILES)} leagues\n')

    # ── 1. Global distribution analysis ──
    bps = [m['actual_bp'] for m in all_matches]
    avg_bp   = sum(bps) / len(bps)
    var_bp_g = sum((b - avg_bp)**2 for b in bps) / len(bps)
    std_bp_g = math.sqrt(var_bp_g)
    print('=== BOOKING POINTS — Real distribution ===')
    print(f'Mean={avg_bp:.1f}  Std={std_bp_g:.1f}  N={len(bps)}')
    for line in [25.5, 35.5, 45.5, 55.5, 65.5]:
        over  = sum(1 for b in bps if b > line)
        under = sum(1 for b in bps if b <= line)
        print(f'  Over  {line}: {over/len(bps)*100:.1f}%  ({over}/{len(bps)})')
    print()

    corners_all = [m['total_corners'] for m in all_matches]
    avg_c = sum(corners_all) / len(corners_all)
    home_wins  = sum(1 for m in all_matches if m['corner_winner'] == 'home')
    away_wins  = sum(1 for m in all_matches if m['corner_winner'] == 'away')
    draws_c    = sum(1 for m in all_matches if m['corner_winner'] == 'draw')
    print('=== CORNERS — Real distribution ===')
    print(f'Avg total corners = {avg_c:.1f}')
    print(f'Home wins corner: {home_wins/len(all_matches)*100:.1f}%  '
          f'Away: {away_wins/len(all_matches)*100:.1f}%  '
          f'Draw: {draws_c/len(all_matches)*100:.1f}%')
    print()

    # ── 2. Walk-forward backtest (global: train first 80%, test last 20%) ──
    n_train = int(len(all_matches) * 0.80)
    train   = all_matches[:n_train]
    test    = all_matches[n_train:]
    print(f'=== WALK-FORWARD BACKTEST  (train={n_train}, test={len(test)}) ===\n')

    home_stats, away_stats = build_model_at(train)

    # Collect per-match predictions vs actuals
    bp_errors   = []  # (predicted_exp, actual)
    corner_hits = []  # (predicted_p_home, actual_home_wins: 0/1)
    bp_results_by_line = defaultdict(lambda: {'correct': 0, 'total': 0,
                                               'roi_fake': 0.0, 'bets': []})

    # Simulate at typical bookmaker odds: over @ 1.85, under @ 1.85 (fair line)
    FAKE_ODDS = 1.85
    MIN_EDGE  = 0.08   # 8% edge required (lower for calibration check)
    MIN_CONF  = 0.50

    n_predicted = 0
    n_no_data   = 0

    for m in test:
        cp, bp = predict_match(home_stats, away_stats, m['home'], m['away'])
        if cp is None or bp is None:
            n_no_data += 1
            continue
        n_predicted += 1
        bp_errors.append((bp['exp_bp'], m['actual_bp']))
        corner_hits.append((cp['p_home'], 1 if m['corner_winner'] == 'home' else 0))

        for line in [25.5, 35.5, 45.5, 55.5]:
            p_over  = p_over_bp(bp['exp_bp'], bp['std_bp'], line)
            p_under = 1.0 - p_over
            actual_over = m['actual_bp'] > line

            # Simulate: bet over if edge >= MIN_EDGE
            edge_over  = p_over  * FAKE_ODDS - 1.0
            edge_under = p_under * FAKE_ODDS - 1.0

            if edge_over >= MIN_EDGE and p_over >= MIN_CONF:
                won = actual_over
                bp_results_by_line[f'Over  {line}']['total']    += 1
                bp_results_by_line[f'Over  {line}']['correct']  += int(won)
                bp_results_by_line[f'Over  {line}']['roi_fake'] += (FAKE_ODDS - 1 if won else -1)
                bp_results_by_line[f'Over  {line}']['bets'].append(
                    (p_over, FAKE_ODDS, int(won)))
            if edge_under >= MIN_EDGE and p_under >= MIN_CONF:
                won = not actual_over
                bp_results_by_line[f'Under {line}']['total']    += 1
                bp_results_by_line[f'Under {line}']['correct']  += int(won)
                bp_results_by_line[f'Under {line}']['roi_fake'] += (FAKE_ODDS - 1 if won else -1)
                bp_results_by_line[f'Under {line}']['bets'].append(
                    (p_under, FAKE_ODDS, int(won)))

    print(f'Predicted: {n_predicted}  /  No-data (unknown team): {n_no_data}\n')

    # ── 3. Calibration: exp_bp vs actual_bp ──
    if bp_errors:
        preds   = [p for p, a in bp_errors]
        actuals = [a for p, a in bp_errors]
        mean_pred   = sum(preds) / len(preds)
        mean_actual = sum(actuals) / len(actuals)
        mae = sum(abs(p - a) for p, a in bp_errors) / len(bp_errors)
        rmse = math.sqrt(sum((p - a)**2 for p, a in bp_errors) / len(bp_errors))
        bias = mean_pred - mean_actual  # positive = model over-predicts

        print('=== BOOKING POINTS CALIBRATION ===')
        print(f'Model mean prediction: {mean_pred:.1f}')
        print(f'Actual mean:           {mean_actual:.1f}')
        print(f'Bias (pred - actual):  {bias:.1f}  ({"OVER-predicts" if bias > 0 else "UNDER-predicts"} by {abs(bias):.1f} pts)')
        print(f'MAE:  {mae:.1f} pts')
        print(f'RMSE: {rmse:.1f} pts')

        # Calibration multiplier: what multiplier M gives mean_pred * M = mean_actual?
        mult = mean_actual / mean_pred if mean_pred > 0 else 1.0
        print(f'\nRequired calibration multiplier: {mult:.3f}')
        print(f'  i.e., multiply exp_bp by {mult:.3f} to match actual mean\n')

        # Correlation
        n = len(bp_errors)
        cov = sum((p - mean_pred) * (a - mean_actual) for p, a in bp_errors) / n
        std_p = math.sqrt(sum((p - mean_pred)**2 for p, a in bp_errors) / n)
        std_a = math.sqrt(sum((a - mean_actual)**2 for p, a in bp_errors) / n)
        corr = cov / (std_p * std_a) if std_p * std_a > 0 else 0
        print(f'Pearson correlation (pred vs actual): {corr:.3f}')
        print()

    # ── 4. Corner prediction accuracy ──
    if corner_hits:
        n_c = len(corner_hits)
        # Brier score
        brier = sum((p - a)**2 for p, a in corner_hits) / n_c
        baseline_brier = sum((0.5 - a)**2 for p, a in corner_hits) / n_c
        # Accuracy at threshold 0.55
        correct = sum(1 for p, a in corner_hits if (p >= 0.55 and a == 1) or (p < 0.45 and a == 0))
        confident = sum(1 for p, a in corner_hits if p >= 0.55 or p < 0.45)
        print('=== CORNER WINNER CALIBRATION ===')
        print(f'Brier score:    {brier:.4f}  (baseline 50/50: {baseline_brier:.4f})')
        print(f'Skill score:    {(1 - brier/baseline_brier)*100:.1f}%')
        if confident > 0:
            print(f'Accuracy at |edge|≥0.55: {correct/confident*100:.1f}%  (n={confident})')
        print()

    # ── 5. Booking pts O/U betting simulation ──
    print('=== BOOKING POINTS O/U BETTING SIMULATION ===')
    print(f'(Fake odds={FAKE_ODDS}, min_edge={MIN_EDGE*100:.0f}%, min_conf={MIN_CONF*100:.0f}%)\n')
    print(f'{"Market":<12} {"N":>5} {"Hit%":>7} {"ROI%":>8} {"Verdict"}')
    print('-' * 50)

    total_bets = 0
    total_roi  = 0.0
    for mkt, res in sorted(bp_results_by_line.items()):
        n  = res['total']
        if n == 0:
            continue
        wr  = res['correct'] / n * 100
        roi = res['roi_fake'] / n * 100
        total_bets += n
        total_roi  += res['roi_fake']
        verdict = 'PROFIT' if roi > 0 else ('MARGINAL' if roi > -5 else 'LOSE')
        print(f'{mkt:<12} {n:>5} {wr:>6.1f}% {roi:>7.1f}% {verdict}')

    if total_bets > 0:
        print(f'\nTOTAL: {total_bets} bets | ROI = {total_roi/total_bets*100:.1f}%')

    # ── 6. By-line base rates for real thresholds ──
    print('\n\n=== BASE RATES (full dataset) — use as prior ===')
    print(f'{"Line":<10} {"Over%":>8} {"Under%":>8}  {"Fair over odds":>15}')
    print('-' * 48)
    for line in [25.5, 35.5, 40.5, 45.5, 50.5, 55.5, 60.5, 65.5]:
        over  = sum(1 for b in bps if b > line) / len(bps)
        under = 1.0 - over
        fair_over_odds = round(1 / over, 2) if over > 0 else 'inf'
        fair_und_odds  = round(1 / under, 2) if under > 0 else 'inf'
        print(f'{line:<10} {over*100:>7.1f}%  {under*100:>7.1f}%  '
              f'O={fair_over_odds}  U={fair_und_odds}')

    # ── 7. Per-league analysis ──
    print('\n\n=== PER-LEAGUE BOOKING PTS ===')
    by_league = defaultdict(list)
    for m in all_matches:
        by_league[m['league']].append(m['actual_bp'])
    for lg, vals in sorted(by_league.items()):
        avg = sum(vals) / len(vals)
        over_45 = sum(1 for v in vals if v > 45.5) / len(vals) * 100
        print(f'  {lg:<6}: n={len(vals):>4}  mean_bp={avg:>5.1f}  Over45.5={over_45:.1f}%')

    # ── 8. Minimum edge needed for positive EV given fair odds ──
    print('\n\n=== MINIMUM REAL EDGE NEEDED FOR POSITIVE EV ===')
    print('(given market odds with ~5% margin)\n')
    for line in [35.5, 45.5]:
        base_rate_over = sum(1 for b in bps if b > line) / len(bps)
        base_rate_under = 1 - base_rate_over
        # bookmaker over odds: fair_odds / 0.95
        fair_over = 1 / base_rate_over if base_rate_over > 0 else 99
        fair_und  = 1 / base_rate_under if base_rate_under > 0 else 99
        bk_over = round(fair_over * 0.95, 2)
        bk_und  = round(fair_und * 0.95, 2)
        # Break-even probability at those odds
        be_over = 1 / bk_over
        be_und  = 1 / bk_und
        print(f'Line {line}:')
        print(f'  Base rate: Over={base_rate_over*100:.1f}%  Under={base_rate_under*100:.1f}%')
        print(f'  Typical bk odds: Over={bk_over}  Under={bk_und}')
        print(f'  Break-even prob: Over≥{be_over*100:.1f}%  Under≥{be_und*100:.1f}%')
        print(f'  Need edge vs base: Over={+(be_over-base_rate_over)*100:+.1f}pp  '
              f'Under={+(be_und-base_rate_under)*100:+.1f}pp')
        print()

    # ── 9. Model-specific diagnosis: are errors systematic? ──
    print('=== MODEL BIAS BY TIER ===')
    tiers = [(0, 30, 'low_bp'), (30, 45, 'mid_bp'), (45, 999, 'high_bp')]
    for lo, hi, label in tiers:
        subset = [(p, a) for p, a in bp_errors if lo <= a < hi]
        if not subset:
            continue
        preds_t   = [p for p, a in subset]
        actuals_t = [a for p, a in subset]
        mp = sum(preds_t) / len(preds_t)
        ma = sum(actuals_t) / len(actuals_t)
        print(f'  actual_bp=[{lo}-{hi}): n={len(subset):>4}  '
              f'pred_mean={mp:.1f}  actual_mean={ma:.1f}  bias={mp-ma:+.1f}')

    print('\nDone.')


if __name__ == '__main__':
    run_backtest()
