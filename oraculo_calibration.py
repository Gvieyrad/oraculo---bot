"""Model calibration — Brier score, calibration plot, reliability tracking."""
import os, json, logging, math
from collections import defaultdict
from datetime import datetime

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_FILE = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
CALIBRATION_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'calibration.json')


def compute_brier_score(predictions):
    """Compute Brier score (lower = better calibrated).

    Brier = mean((predicted_prob - actual_outcome)^2)
    Perfect = 0, random = 0.25, always 50% = 0.25
    Good model < 0.20, excellent < 0.15
    """
    if not predictions:
        return None

    total = 0
    n = 0
    for p in predictions:
        prob = p.get('model_prob', 0.5)
        result = p.get('result', '')
        if result == 'WIN':
            actual = 1
        elif result == 'LOSS':
            actual = 0
        else:
            continue
        if prob == 0.0:  # Skip entries with missing model_prob (legacy data)
            continue
        total += (prob - actual) ** 2
        n += 1

    return total / n if n > 0 else None


def compute_log_loss(predictions):
    """Log loss — measures confidence calibration. Lower = better."""
    if not predictions:
        return None

    total = 0
    n = 0
    eps = 1e-7
    for p in predictions:
        prob = max(eps, min(1 - eps, p.get('model_prob', 0.5)))
        result = p.get('result', '')
        _raw_prob = p.get('model_prob', 0.5)
        if _raw_prob == 0.0:  # Skip entries with missing model_prob (legacy data)
            continue
        if result == 'WIN':
            total -= math.log(prob)
        elif result == 'LOSS':
            total -= math.log(1 - prob)
        else:
            continue
        n += 1

    return total / n if n > 0 else None


def calibration_bins(predictions, n_bins=10):
    """Group predictions into probability bins and compare predicted vs actual.

    Returns list of dicts: [{bin_center, predicted_avg, actual_avg, count}]
    This is the data for a calibration plot.
    Perfect calibration: predicted_avg == actual_avg for all bins.
    """
    if not predictions:
        return []

    bins = defaultdict(lambda: {'total_prob': 0, 'total_wins': 0, 'count': 0})
    for p in predictions:
        prob = p.get('model_prob', 0.5)
        result = p.get('result', '')
        if result not in ('WIN', 'LOSS'):
            continue
        if prob == 0.0:  # Skip entries with missing model_prob (legacy data)
            continue

        bin_idx = min(int(prob * n_bins), n_bins - 1)
        bins[bin_idx]['total_prob'] += prob
        bins[bin_idx]['total_wins'] += (1 if result == 'WIN' else 0)
        bins[bin_idx]['count'] += 1

    result = []
    for i in range(n_bins):
        b = bins.get(i)
        if b and b['count'] > 0:
            result.append({
                'bin_center': (i + 0.5) / n_bins,
                'predicted_avg': round(b['total_prob'] / b['count'], 4),
                'actual_avg': round(b['total_wins'] / b['count'], 4),
                'count': b['count'],
                'gap': round(abs(b['total_prob'] / b['count'] - b['total_wins'] / b['count']), 4),
            })

    return result


def expected_calibration_error(bins):
    """ECE — weighted average of |predicted - actual| across bins. Lower = better.
    ECE < 0.05 = well calibrated, < 0.10 = acceptable, > 0.15 = poor
    """
    if not bins:
        return None
    total_samples = sum(b['count'] for b in bins)
    if total_samples == 0:
        return None
    ece = sum(b['count'] * b['gap'] for b in bins) / total_samples
    return round(ece, 4)


def per_sport_calibration(predictions):
    """Separate calibration metrics by sport."""
    by_sport = defaultdict(list)
    for p in predictions:
        sport = p.get('sport', 'unknown')
        by_sport[sport].append(p)

    results = {}
    for sport, preds in by_sport.items():
        brier = compute_brier_score(preds)
        ll = compute_log_loss(preds)
        bins = calibration_bins(preds, 5)
        ece = expected_calibration_error(bins)
        wins = sum(1 for p in preds if p.get('result') == 'WIN')
        total = sum(1 for p in preds if p.get('result') in ('WIN', 'LOSS'))
        results[sport] = {
            'brier_score': round(brier, 4) if brier else None,
            'log_loss': round(ll, 4) if ll else None,
            'ece': ece,
            'win_rate': round(wins / total, 4) if total else None,
            'sample_size': total,
            'bins': bins,
        }

    return results


def per_edge_band_analysis(predictions):
    """Analyze actual win rates by predicted edge band."""
    bands = {
        '3-5%': (0.03, 0.05),
        '5-10%': (0.05, 0.10),
        '10-20%': (0.10, 0.20),
        '20%+': (0.20, 1.0),
    }
    results = {}
    for label, (lo, hi) in bands.items():
        band_preds = [p for p in predictions
                      if lo <= p.get('edge', 0) < hi and p.get('result') in ('WIN', 'LOSS')
                      and p.get('model_prob', 0) > 0]
        if not band_preds:
            continue
        wins = sum(1 for p in band_preds if p.get('result') == 'WIN')
        avg_odds = sum(p.get('odds', 0) for p in band_preds) / len(band_preds)
        avg_prob = sum(p.get('model_prob', 0) for p in band_preds) / len(band_preds)
        total_staked = sum(p.get('stake', 0) for p in band_preds)
        total_pnl = sum(
            p.get('stake', 0) * (p.get('odds', 0) - 1) if p.get('result') == 'WIN'
            else -p.get('stake', 0)
            for p in band_preds
        )
        results[label] = {
            'count': len(band_preds),
            'wins': wins,
            'win_rate': round(wins / len(band_preds), 4),
            'avg_predicted_prob': round(avg_prob, 4),
            'avg_odds': round(avg_odds, 3),
            'roi': round(total_pnl / total_staked, 4) if total_staked else 0,
        }

    return results


def full_calibration_report():
    """Generate complete calibration report from predictions log."""
    if not os.path.exists(PREDICTIONS_FILE):
        return None

    preds = []
    with open(PREDICTIONS_FILE) as f:
        for line in f:
            try:
                e = json.loads(line.strip())
                if e.get('result') in ('WIN', 'LOSS'):
                    preds.append(e)
            except Exception:
                pass

    if len(preds) < 10:
        return None

    preds = [p for p in preds if p.get('model_prob', 0) > 0]
    if len(preds) < 5:
        return None

    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'sample_size': len(preds),
        'overall': {
            'brier_score': round(compute_brier_score(preds), 4),
            'log_loss': round(compute_log_loss(preds), 4),
            'ece': expected_calibration_error(calibration_bins(preds)),
            'calibration_bins': calibration_bins(preds),
        },
        'by_sport': per_sport_calibration(preds),
        'by_edge_band': per_edge_band_analysis(preds),
    }

    # Diagnosis
    brier = report['overall']['brier_score']
    ece = report['overall']['ece']
    diagnosis = []
    if brier and brier < 0.15:
        diagnosis.append('EXCELLENT calibration (Brier < 0.15)')
    elif brier and brier < 0.20:
        diagnosis.append('GOOD calibration (Brier < 0.20)')
    elif brier and brier < 0.25:
        diagnosis.append('FAIR calibration (Brier < 0.25)')
    else:
        diagnosis.append('POOR calibration (Brier >= 0.25) — model may be overconfident')

    if ece and ece < 0.05:
        diagnosis.append('Well calibrated probabilities (ECE < 5%)')
    elif ece and ece < 0.10:
        diagnosis.append('Acceptable calibration (ECE < 10%)')
    elif ece:
        diagnosis.append('Poor calibration (ECE >= 10%) — probabilities unreliable')

    # Check overconfidence
    bins = report['overall']['calibration_bins']
    overconfident = sum(1 for b in bins if b['predicted_avg'] > b['actual_avg'] + 0.05)
    underconfident = sum(1 for b in bins if b['actual_avg'] > b['predicted_avg'] + 0.05)
    if overconfident > underconfident:
        diagnosis.append('Model tends to be OVERCONFIDENT — reduce probabilities')
    elif underconfident > overconfident:
        diagnosis.append('Model tends to be UNDERCONFIDENT — increase probabilities or bet more')

    report['diagnosis'] = diagnosis

    # Save
    os.makedirs(os.path.dirname(CALIBRATION_CACHE), exist_ok=True)
    with open(CALIBRATION_CACHE, 'w') as f:
        json.dump(report, f, indent=2)
    log.info('[Calibration] Brier=%.4f ECE=%.4f | %s', brier or 0, ece or 0, diagnosis[0])

    return report
