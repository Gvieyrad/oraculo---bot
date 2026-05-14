import os, json, sqlite3, logging, numpy as np
from datetime import datetime

log = logging.getLogger('oraculo.mlb_cal')
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SIBILA_DB   = os.path.join(SCRIPT_DIR, 'sibila.db')
CAL_FILE    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'mlb_platt.json')
MIN_SAMPLES = 50
os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)


def _load_sibila_data(sport='baseball', min_odds=1.1, max_odds=3.5):
    try:
        conn = sqlite3.connect(SIBILA_DB)
        rows = conn.execute(
            "SELECT prob_model, odds, result FROM sibila_picks "
            "WHERE sport=? AND result IN ('WIN','LOSS') AND prob_model>0 AND odds BETWEEN ? AND ?",
            (sport, min_odds, max_odds)).fetchall()
        conn.close()
    except Exception as e:
        log.warning('Sibila load error: %s', e)
        return [], []

    X, y = [], []
    for prob_model, odds, result in rows:
        if prob_model and 0 < prob_model < 1:
            X.append(prob_model)
            y.append(1 if result == 'WIN' else 0)
    return np.array(X), np.array(y)


def train_platt(force=False):
    if not force and os.path.exists(CAL_FILE):
        try:
            with open(CAL_FILE) as f:
                cached = json.load(f)
            age_h = (datetime.now().timestamp() - cached.get('trained_ts', 0)) / 3600
            if age_h < 168:
                log.debug('MLB calibrator: usando cache (%.0fh)', age_h)
                return cached.get('A'), cached.get('B'), cached.get('n_samples', 0)
        except Exception:
            pass

    X, y = _load_sibila_data()
    if len(X) < MIN_SAMPLES:
        log.warning('MLB calibrator: solo %d samples, necesita %d', len(X), MIN_SAMPLES)
        return None, None, 0

    from sklearn.linear_model import LogisticRegression
    eps = 1e-6
    X_logit = np.log(np.clip(X, eps, 1-eps) / (1 - np.clip(X, eps, 1-eps))).reshape(-1, 1)

    lr = LogisticRegression(C=1.0, solver='lbfgs', max_iter=300)
    lr.fit(X_logit, y)

    A = float(lr.coef_[0][0])
    B = float(lr.intercept_[0])

    X_cal = 1 / (1 + np.exp(-(A * X_logit.ravel() + B)))
    raw_brier = float(np.mean((X - y) ** 2))
    cal_brier = float(np.mean((X_cal - y) ** 2))
    improvement = (raw_brier - cal_brier) / raw_brier * 100

    log.info('MLB Platt: n=%d A=%.3f B=%.3f | Brier raw=%.4f->cal=%.4f (%.1f%% mejor)',
             len(X), A, B, raw_brier, cal_brier, improvement)

    result = {
        'A': A, 'B': B,
        'n_samples': len(X),
        'raw_brier': raw_brier,
        'cal_brier': cal_brier,
        'improvement_pct': round(improvement, 1),
        'trained_ts': datetime.now().timestamp(),
        'trained_at': datetime.now().isoformat(),
    }
    with open(CAL_FILE, 'w') as f:
        json.dump(result, f, indent=2)
    return A, B, len(X)


def calibrate_prob(raw_prob, A=None, B=None):
    if A is None or B is None:
        try:
            with open(CAL_FILE) as f:
                cached = json.load(f)
            A, B = cached['A'], cached['B']
        except Exception:
            return raw_prob
    eps = 1e-6
    p = float(np.clip(raw_prob, eps, 1 - eps))
    logit_p = np.log(p / (1 - p))
    cal_p = 1.0 / (1.0 + np.exp(-(A * logit_p + B)))
    return round(float(cal_p), 6)


def calibrate_picks(picks):
    try:
        with open(CAL_FILE) as f:
            cached = json.load(f)
        A, B = cached['A'], cached['B']
    except Exception:
        A, B, n = train_platt()
        if A is None:
            return picks

    calibrated = []
    for p in picks:
        raw = float(p.get('raw_model_prob') or p.get('model_prob') or 0)
        if raw <= 0:
            calibrated.append(p)
            continue
        cal = calibrate_prob(raw, A, B)
        price = float(p.get('price') or p.get('odds') or 0)
        p['raw_model_prob_uncal'] = raw
        p['raw_edge_uncal'] = round(raw * price - 1.0, 6) if price > 1 else 0.0
        # raw_model_prob NO se sobreescribe: Sibila y Platt-retrain necesitan el valor original del modelo FIP
        p['model_prob'] = cal
        if price > 1:
            p['edge'] = round(cal * price - 1.0, 6)
        if abs(cal - raw) > 0.02:
            log.info('[MLBCal] %s | raw=%.1f%% cal=%.1f%% (%+.1f%%)',
                     (p.get('match') or '')[:30], raw * 100, cal * 100, (cal - raw) * 100)
        calibrated.append(p)
    return calibrated


def print_calibration_report():
    X, y = _load_sibila_data()
    if len(X) < MIN_SAMPLES:
        print('Insuficientes datos: %d picks' % len(X))
        return
    try:
        with open(CAL_FILE) as f:
            cached = json.load(f)
        A, B = cached['A'], cached['B']
    except Exception:
        A, B, _ = train_platt(force=True)
        if A is None:
            return

    eps = 1e-6
    X_logit = np.log(np.clip(X, eps, 1-eps) / (1 - np.clip(X, eps, 1-eps)))
    X_cal = 1 / (1 + np.exp(-(A * X_logit + B)))

    print('\nMLB Calibration Report (n=%d)' % len(X))
    print('Platt A=%.3f B=%.3f' % (A, B))
    print('\n%-12s %8s %8s %8s %6s' % ('Band', 'Model%', 'Raw WR%', 'Cal%', 'N'))
    print('-' * 46)

    bands = [(0.4, 0.5, '40-50%'), (0.5, 0.6, '50-60%'), (0.6, 0.65, '60-65%'),
             (0.65, 0.7, '65-70%'), (0.7, 0.8, '70-80%')]
    for lo, hi, label in bands:
        mask = (X >= lo) & (X < hi)
        n = mask.sum()
        if n < 3:
            continue
        raw_wr = y[mask].mean() * 100
        cal_prob = X_cal[mask].mean() * 100
        model_mid = ((lo + hi) / 2) * 100
        print('%-12s %8.1f %8.1f %8.1f %6d' % (label, model_mid, raw_wr, cal_prob, n))

    print('\nBrier: raw=%.4f -> cal=%.4f | Improvement: %.1f%%' % (
        np.mean((X - y) ** 2), np.mean((X_cal - y) ** 2),
        cached.get('improvement_pct', 0)))


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if '--eval' in sys.argv:
        print_calibration_report()
    else:
        A, B, n = train_platt(force=True)
        if A:
            print('Calibrador entrenado: n=%d A=%.3f B=%.3f' % (n, A, B))
            print_calibration_report()
        else:
            print('No hay suficientes datos.')
