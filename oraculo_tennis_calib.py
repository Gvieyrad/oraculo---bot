"""
oraculo_tennis_calib.py — Capa de calibracion isotonic para el modelo de tennis.

Problema (2026-06-18): el modelo de tennis infla prob ~+15% (modelo 69.6% avg vs real 54.8%,
gap consistente en todos los buckets). Eso genera los picks de edge alto (-EV) que el filtro
del 17-jun bloquea como parche. Fix de raiz: mapear prob_model -> prob_real.

Solucion: IsotonicRegression (monotonica, no-parametrica) sobre (prob_model -> WIN).

USO:
  1. Fittear con DATOS LIMPIOS (~30-jun, post Sibila-ciega 3-16):
       python3 oraculo_tennis_calib.py --fit --since 2026-06-17
  2. Wire en el runner (RECIEN cuando este fitteado y validado):
       from oraculo_tennis_calib import calibrate_tennis_prob
       p = calibrate_tennis_prob(p_raw)

SEGURIDAD: NO-OP hasta que exista el pickle. Si no hay calibrador fitteado,
calibrate_tennis_prob() devuelve la prob sin cambios. Crear este archivo NO cambia
nada en vivo hasta que (a) se fittee y (b) se wire explicitamente.
"""
import os, pickle, logging
log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CALIB_PATH = os.path.join(SCRIPT_DIR, 'tennis_calibrator.pkl')
MIN_SAMPLES = 60  # no fittear con menos: overfitting a ruido

_cache = {'model': None, 'mtime': 0.0}


def calibrate_tennis_prob(p):
    """prob cruda del modelo -> prob calibrada. NO-OP si no hay calibrador fitteado."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return p
    try:
        if not os.path.exists(CALIB_PATH):
            return p  # no-op: aun no fitteado
        mt = os.path.getmtime(CALIB_PATH)
        if _cache['model'] is None or mt != _cache['mtime']:
            with open(CALIB_PATH, 'rb') as f:
                _cache['model'] = pickle.load(f)
            _cache['mtime'] = mt
        pc = float(_cache['model'].predict([p])[0])
        return min(0.98, max(0.02, pc))
    except Exception as e:
        log.debug('tennis calib apply error: %s', e)
        return p


def fit_tennis_calibrator(sibila_db, since_date, dry_run=False):
    """Fittea IsotonicRegression sobre tennis resuelto desde since_date."""
    import sqlite3
    from sklearn.isotonic import IsotonicRegression
    con = sqlite3.connect(sibila_db)
    rows = con.execute(
        "SELECT prob_model, result FROM sibila_picks "
        "WHERE sport='tennis' AND result IN ('WIN','LOSS') AND prob_model > 0 AND ts >= ?",
        (since_date,)).fetchall()
    con.close()
    X = [float(p) for p, r in rows]
    y = [1.0 if r == 'WIN' else 0.0 for p, r in rows]
    n = len(X)
    if n < MIN_SAMPLES:
        print('[calib] solo %d muestras (necesita %d) desde %s -- NO fitteo' % (n, MIN_SAMPLES, since_date))
        return None
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.02, y_max=0.98)
    iso.fit(X, y)
    pred = sum(X) / n
    real = sum(y) / n
    cal = sum(iso.predict(X)) / n
    print('[calib] n=%d | modelo_avg=%.3f real_avg=%.3f calibrado_avg=%.3f' % (n, pred, real, cal))
    if not dry_run:
        with open(CALIB_PATH, 'wb') as f:
            pickle.dump(iso, f)
        print('[calib] guardado en %s' % CALIB_PATH)
    return iso


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--fit', action='store_true')
    ap.add_argument('--since', default='2026-06-17')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    db = os.path.join(SCRIPT_DIR, 'sibila.db')
    if a.fit:
        fit_tennis_calibrator(db, a.since, dry_run=a.dry_run)
    else:
        for _p in (0.5, 0.6, 0.7, 0.8, 0.9):
            print('%.2f -> %.3f' % (_p, calibrate_tennis_prob(_p)))
