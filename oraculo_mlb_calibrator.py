import os, json, sqlite3, logging, numpy as np
from datetime import datetime

log = logging.getLogger('oraculo.mlb_cal')
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SIBILA_DB   = os.path.join(SCRIPT_DIR, 'sibila.db')
CACHE_DIR   = os.path.join(SCRIPT_DIR, '.oraculo_cache')
MIN_SAMPLES = 50
CACHE_TTL_H = 168

# Per-market-type Platt cache files; _default is fallback for unknown markets
_CAL_FILES = {
    'mlb_f5_total': os.path.join(CACHE_DIR, 'mlb_f5_total_platt.json'),
    'mlb_f5_ml':    os.path.join(CACHE_DIR, 'mlb_f5_ml_platt.json'),
    '_default':     os.path.join(CACHE_DIR, 'mlb_platt.json'),
}
os.makedirs(CACHE_DIR, exist_ok=True)


def _load_sibila_data(market_type=None, min_odds=1.1, max_odds=3.5):
    """Load resolved picks from Sibila, excluding legacy pre-v2 data."""
    try:
        conn = sqlite3.connect(SIBILA_DB)
        if market_type:
            rows = conn.execute(
                "SELECT prob_model, odds, result FROM sibila_picks "
                "WHERE market_type=? AND result IN ('WIN','LOSS') "
                "AND market_type NOT LIKE '_legacy%' "
                "AND prob_model>0 AND odds BETWEEN ? AND ?",
                (market_type, min_odds, max_odds)).fetchall()
        else:
            rows = conn.execute(
                "SELECT prob_model, odds, result FROM sibila_picks "
                "WHERE sport='baseball' AND result IN ('WIN','LOSS') "
                "AND market_type NOT LIKE '_legacy%' "
                "AND market_type != '' "
                "AND prob_model>0 AND odds BETWEEN ? AND ?",
                (min_odds, max_odds)).fetchall()
        conn.close()
    except Exception as e:
        log.warning('Sibila load error: %s', e)
        return np.array([]), np.array([])
    X, y = [], []
    for prob_model, odds, result in rows:
        if prob_model and 0 < prob_model < 1:
            X.append(prob_model)
            y.append(1 if result == 'WIN' else 0)
    return np.array(X), np.array(y)


def _fit_platt(X, y):
    from sklearn.linear_model import LogisticRegression
    eps = 1e-6
    Xl = np.log(np.clip(X, eps, 1-eps) / (1 - np.clip(X, eps, 1-eps))).reshape(-1, 1)
    lr = LogisticRegression(C=1.0, solver='lbfgs', max_iter=500)
    lr.fit(Xl, y)
    A = float(lr.coef_[0][0])
    B = float(lr.intercept_[0])
    Xc = 1 / (1 + np.exp(-(A * Xl.ravel() + B)))
    rb = float(np.mean((X - y) ** 2))
    cb = float(np.mean((Xc - y) ** 2))
    imp = (rb - cb) / rb * 100 if rb > 0 else 0.0
    return A, B, rb, cb, imp


def train_platt(market_type=None, force=False):
    """Train (or reload cached) Platt scaling for a specific market_type."""
    cal_file = _CAL_FILES.get(market_type, _CAL_FILES['_default'])
    if not force and os.path.exists(cal_file):
        try:
            with open(cal_file) as f:
                cached = json.load(f)
            age_h = (datetime.now().timestamp() - cached.get('trained_ts', 0)) / 3600
            if age_h < CACHE_TTL_H:
                log.debug('MLB cal[%s]: cache ok (%.0fh)', market_type or 'all', age_h)
                return cached.get('A'), cached.get('B'), cached.get('n_samples', 0)
        except Exception:
            pass
    X, y = _load_sibila_data(market_type=market_type)
    if len(X) < MIN_SAMPLES:
        log.warning('MLB cal[%s]: %d samples < %d min', market_type or 'all', len(X), MIN_SAMPLES)
        return None, None, 0
    A, B, rb, cb, imp = _fit_platt(X, y)
    wr = float(np.mean(y))
    avg_raw = float(np.mean(X))
    eps = 1e-6
    Xl = np.log(np.clip(X, eps, 1-eps) / (1 - np.clip(X, eps, 1-eps)))
    avg_cal = float(np.mean(1 / (1 + np.exp(-(A * Xl + B)))))
    log.info(
        'MLB Platt[%s]: n=%d A=%.4f B=%.4f '
        'avg_raw=%.3f avg_cal=%.3f WR=%.3f gap=%+.3f '
        'Brier %.4f->%.4f (%.1f%%)',
        market_type or 'all', len(X), A, B,
        avg_raw, avg_cal, wr, avg_cal - wr, rb, cb, imp)
    result = {
        'A': A, 'B': B, 'market_type': market_type, 'n_samples': len(X),
        'avg_raw': round(avg_raw, 4), 'avg_cal': round(avg_cal, 4),
        'wr': round(wr, 4), 'gap': round(avg_cal - wr, 4),
        'raw_brier': rb, 'cal_brier': cb, 'improvement_pct': round(imp, 1),
        'trained_ts': datetime.now().timestamp(), 'trained_at': datetime.now().isoformat(),
    }
    with open(cal_file, 'w') as f:
        json.dump(result, f, indent=2)
    return A, B, len(X)


def _get_platt(market_type=None):
    """Return (A, B) for market_type, training fresh if cache is stale/missing."""
    cal_file = _CAL_FILES.get(market_type, _CAL_FILES['_default'])
    try:
        with open(cal_file) as f:
            cached = json.load(f)
        age_h = (datetime.now().timestamp() - cached.get('trained_ts', 0)) / 3600
        if age_h < CACHE_TTL_H:
            return cached['A'], cached['B']
    except Exception:
        pass
    A, B, _ = train_platt(market_type=market_type)
    return A, B


def calibrate_prob(raw_prob, A=None, B=None, market_type=None):
    if A is None or B is None:
        A, B = _get_platt(market_type)
        if A is None:
            return raw_prob
    eps = 1e-6
    p = float(np.clip(raw_prob, eps, 1 - eps))
    lp = float(np.log(p / (1 - p)))
    return round(float(1.0 / (1.0 + np.exp(-(A * lp + B)))), 6)


def calibrate_picks(picks):
    """Apply per-market-type Platt calibration to a list of MLB picks."""
    calibrated = []
    for p in picks:
        raw = float(p.get('raw_model_prob') or p.get('model_prob') or 0)
        if raw <= 0:
            calibrated.append(p)
            continue
        mtype = p.get('market_type')
        A, B = _get_platt(mtype)
        if A is None:
            calibrated.append(p)
            continue
        cal = calibrate_prob(raw, A, B, market_type=mtype)
        price = float(p.get('price') or p.get('odds') or 0)
        p['raw_model_prob_uncal'] = raw
        p['raw_edge_uncal'] = round(raw * price - 1.0, 6) if price > 1 else 0.0
        # raw_model_prob is not overwritten — Sibila and Platt-retrain need the original model value
        p['model_prob'] = cal
        if price > 1:
            p['edge'] = round(cal * price - 1.0, 6)
        if abs(cal - raw) > 0.02:
            log.info('[MLBCal] %s [%s] raw=%.1f%% cal=%.1f%% (%+.1f%%)',
                     (p.get('match') or '')[:28], mtype or '?',
                     raw * 100, cal * 100, (cal - raw) * 100)
        calibrated.append(p)
    return calibrated


def print_calibration_report(market_type=None):
    X, y = _load_sibila_data(market_type=market_type)
    if len(X) < MIN_SAMPLES:
        print('Insuficientes datos: %d picks' % len(X))
        return
    A, B = _get_platt(market_type)
    if A is None:
        print('No hay calibracion entrenada.')
        return
    eps = 1e-6
    Xl = np.log(np.clip(X, eps, 1-eps) / (1 - np.clip(X, eps, 1-eps)))
    Xc = 1 / (1 + np.exp(-(A * Xl + B)))
    wr = float(np.mean(y))
    avg_cal = float(np.mean(Xc))
    print('\nMLB Calibration Report [%s] (n=%d)' % (market_type or 'all', len(X)))
    print('Platt A=%.4f B=%.4f | avg_cal=%.3f WR=%.3f gap=%+.3f' % (A, B, avg_cal, wr, avg_cal - wr))
    print('\n%-12s %8s %8s %8s %6s' % ('Band', 'Model%', 'Raw WR%', 'Cal%', 'N'))
    print('-' * 46)
    for lo, hi, label in [
        (0.4, 0.5, '40-50%'), (0.5, 0.6, '50-60%'), (0.6, 0.65, '60-65%'),
        (0.65, 0.7, '65-70%'), (0.7, 0.8, '70-80%'), (0.8, 1.0, '80%+'),
    ]:
        mask = (X >= lo) & (X < hi)
        n = int(mask.sum())
        if n < 3:
            continue
        print('%-12s %8.1f %8.1f %8.1f %6d' % (
            label, ((lo + hi) / 2) * 100,
            float(y[mask].mean()) * 100,
            float(Xc[mask].mean()) * 100, n))
    rb = float(np.mean((X - y) ** 2))
    cb = float(np.mean((Xc - y) ** 2))
    print('\nBrier: raw=%.4f -> cal=%.4f | Improvement: %.1f%%' % (rb, cb, (rb - cb) / rb * 100))


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    for mt in ['mlb_f5_total', 'mlb_f5_ml', None]:
        A, B, n = train_platt(market_type=mt, force=True)
        if A:
            print_calibration_report(market_type=mt)
        else:
            print('Sin datos suficientes para %s.' % (mt or 'combined'))
