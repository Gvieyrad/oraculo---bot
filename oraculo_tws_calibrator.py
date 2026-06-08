"""
Platt scaling calibrator for tennis_team_win_set market.

Shadow mode only — computes calibrated edge/prob and logs them,
but does NOT modify any pick fields used by placement logic.

Fitted on Sibila N=54 resolved tws bets (as of 2026-05-21).
Retrained weekly from live Sibila data once N >= MIN_SAMPLES.

Usage in runner (shadow only):
    from oraculo_tws_calibrator import shadow_log_platt, train_platt_tws
    shadow_log_platt(tennis_picks, log)
"""
import os, json, sqlite3, logging, math
from datetime import datetime

log = logging.getLogger('oraculo.tws_cal')
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SIBILA_DB   = os.path.join(SCRIPT_DIR, 'sibila.db')
CAL_FILE    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tws_platt.json')
MIN_SAMPLES = 30
CACHE_TTL_H = 168  # retrain weekly

# Fallback values fitted on Sibila N=54 (2026-05-21)
# calibrated_prob = sigmoid(A * logit(raw_prob) + B)
FALLBACK_A = 0.357
FALLBACK_B = 0.088

os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)


def _load_sibila_tws():
    """Load all resolved tennis_team_win_set bets from Sibila."""
    try:
        conn = sqlite3.connect(SIBILA_DB)
        rows = conn.execute(
            "SELECT prob_model, odds, result FROM sibila_picks "
            "WHERE sport='tennis' AND result IN ('WIN','LOSS') AND prob_model > 0 "
            "  AND (market_type='tennis_team_win_set' "
            "       OR (market_type='' AND side LIKE '%wins set%'))",
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning('TWSCal: Sibila error: %s', e)
        return [], []

    X, y = [], []
    for prob_model, odds, result in rows:
        if prob_model and 0 < prob_model < 1:
            X.append(float(prob_model))
            y.append(1 if result == 'WIN' else 0)
    return X, y


def train_platt_tws(force=False):
    """Fit Platt scaling on Sibila tws data. Caches result for TTL hours."""
    if not force and os.path.exists(CAL_FILE):
        try:
            with open(CAL_FILE) as f:
                cached = json.load(f)
            age_h = (datetime.now().timestamp() - cached.get('trained_ts', 0)) / 3600
            if age_h < CACHE_TTL_H:
                log.debug('TWSCal: cache hit (%.0fh old, n=%d)', age_h, cached.get('n_samples', 0))
                return cached.get('A', FALLBACK_A), cached.get('B', FALLBACK_B), cached.get('n_samples', 0)
        except Exception:
            pass

    X_list, y_list = _load_sibila_tws()
    n = len(X_list)

    if n < MIN_SAMPLES:
        log.info('TWSCal: solo %d samples (min %d) — usando fallback A=%.3f B=%.3f',
                 n, MIN_SAMPLES, FALLBACK_A, FALLBACK_B)
        return FALLBACK_A, FALLBACK_B, n

    # Platt scaling via simple gradient descent (avoid sklearn dependency)
    eps = 1e-9

    def logit(p):
        p = max(eps, min(1 - eps, p))
        return math.log(p / (1 - p))

    def sigmoid(x):
        return 1.0 / (1.0 + math.exp(-x))

    def log_loss(A, B):
        total = 0.0
        for x, y in zip(X_list, y_list):
            lx = logit(x)
            p = sigmoid(A * lx + B)
            p = max(eps, min(1 - eps, p))
            total -= y * math.log(p) + (1 - y) * math.log(1 - p)
        return total / len(X_list)

    # Nelder-Mead simplex (pure Python, no scipy needed)
    try:
        from scipy.optimize import minimize as _sp_min
        res = _sp_min(lambda ab: log_loss(ab[0], ab[1]), [1.0, 0.0], method='Nelder-Mead',
                      options={'xatol': 1e-5, 'fatol': 1e-5, 'maxiter': 2000})
        A_fit, B_fit = res.x
    except ImportError:
        # Fallback: coordinate descent
        A_fit, B_fit = 1.0, 0.0
        lr = 0.05
        for _ in range(500):
            dA = dB = 0.0
            for x, y in zip(X_list, y_list):
                lx = logit(x)
                p = sigmoid(A_fit * lx + B_fit)
                err = p - y
                dA += err * lx
                dB += err
            A_fit -= lr * dA / n
            B_fit -= lr * dB / n

    # Compute Brier scores
    raw_brier = sum((x - y) ** 2 for x, y in zip(X_list, y_list)) / n
    cal_brier = sum((sigmoid(A_fit * logit(x) + B_fit) - y) ** 2
                    for x, y in zip(X_list, y_list)) / n
    improvement = (raw_brier - cal_brier) / raw_brier * 100 if raw_brier > 0 else 0

    log.info('TWSCal: fit n=%d A=%.3f B=%.3f | Brier %.4f->%.4f (%.1f%% mejor)',
             n, A_fit, B_fit, raw_brier, cal_brier, improvement)

    result = {
        'A': A_fit, 'B': B_fit,
        'n_samples': n,
        'raw_brier': raw_brier,
        'cal_brier': cal_brier,
        'improvement_pct': round(improvement, 1),
        'trained_ts': datetime.now().timestamp(),
        'trained_at': datetime.now().isoformat(),
    }
    with open(CAL_FILE, 'w') as f:
        json.dump(result, f, indent=2)
    return A_fit, B_fit, n


def calibrate_prob(raw_prob, A=None, B=None):
    """Apply Platt calibration to a single probability."""
    if A is None or B is None:
        try:
            with open(CAL_FILE) as f:
                d = json.load(f)
            A, B = d['A'], d['B']
        except Exception:
            A, B = FALLBACK_A, FALLBACK_B

    eps = 1e-9
    p = max(eps, min(1 - eps, float(raw_prob)))
    logit_p = math.log(p / (1 - p))
    return 1.0 / (1.0 + math.exp(-(A * logit_p + B)))


def shadow_log_platt(tennis_picks, logger=None):
    """
    Shadow mode: log calibrated edge/prob for each tws pick.
    Does NOT modify any pick fields — placement logic is unaffected.
    """
    _log = logger or log
    try:
        A, B, n_samples = train_platt_tws()
    except Exception as e:
        _log.debug('TWSCal shadow: skipped (%s)', e)
        return

    tws = [p for p in tennis_picks if p.get('market_type') == 'tennis_team_win_set']
    if not tws:
        return

    _log.info('TWSCal shadow (A=%.3f B=%.3f n_sibila=%d):', A, B, n_samples)
    for p in tws:
        raw_prob = float(p.get('model_prob') or p.get('confidence') or 0)
        odds = float(p.get('price') or p.get('odds') or 0)
        raw_edge = float(p.get('edge') or 0)
        if raw_prob <= 0 or odds <= 0:
            continue

        cal_prob = calibrate_prob(raw_prob, A, B)
        cal_edge = cal_prob - (1.0 / odds) if odds > 0 else 0.0

        label = (p.get('label') or p.get('side') or '')[:28]
        passes_raw = raw_edge >= 0.18 and odds >= 1.40
        passes_cal = cal_edge >= 0.18 and odds >= 1.40
        status = ''
        if passes_raw and not passes_cal:
            status = ' [CAL_WOULD_BLOCK]'
        elif not passes_raw and passes_cal:
            status = ' [CAL_WOULD_PASS]'
        elif passes_raw and passes_cal:
            status = ' [BOTH_PASS]'
        else:
            status = ' [BOTH_BLOCK]'

        _log.info(
            '  tws shadow | %-28s | raw_p=%.3f cal_p=%.3f | '
            'raw_e=%+.3f cal_e=%+.3f | odds=%.2f%s',
            label, raw_prob, cal_prob, raw_edge, cal_edge, odds, status
        )
