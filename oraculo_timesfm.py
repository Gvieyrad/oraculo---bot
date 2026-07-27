"""
oraculo_timesfm.py -- TimesFM-ready xG trend scorer for soccer goals

To enable in August:
  1. Set TIMESFM_ENABLED = True
  2. Run: python3 build_timesfm_cache.py  (populates xg_cache from FBref)
  3. Optional: pip install timesfm[cpu] + set USE_REAL_TIMESFM = True

WC Shadow mode (active when TIMESFM_WC_SHADOW = True):
  - Uses timesfm_wc_cache.json populated with 2H goals from propline
  - MIN_SERIES_LEN = 1 (WC teams have 1-2 matches in knockout phase)
  - NEVER blocks bets -- logs shadow predictions only
"""

import json
import math
import os
import logging

log = logging.getLogger(__name__)

# --- Main gate (domestic leagues, August+) ---
TIMESFM_ENABLED   = False
USE_REAL_TIMESFM  = False

CACHE_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timesfm_xg_cache.json')
MAX_SERIES_LEN    = 15
MIN_SERIES_LEN    = 5
EWM_ALPHA         = 0.3
UNDER_THRESHOLD   = 0.55
GRAY_ZONE_CONF_LO = 0.65
GRAY_ZONE_CONF_HI = 0.72

# --- WC Shadow gate (active now, logs only, never blocks) ---
TIMESFM_WC_SHADOW   = True
WC_CACHE_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timesfm_wc_cache.json')
WC_MIN_SERIES_LEN   = 1   # WC teams have 1 R16 data point
WC_UNDER_THRESHOLD  = 0.55

_cache    = None
_wc_cache = None


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            _cache = json.load(f)
    else:
        _cache = {}
    return _cache


def _load_wc_cache():
    global _wc_cache
    if _wc_cache is not None:
        return _wc_cache
    if os.path.exists(WC_CACHE_PATH):
        with open(WC_CACHE_PATH) as f:
            _wc_cache = json.load(f)
    else:
        _wc_cache = {}
    return _wc_cache


def _save_cache():
    with open(CACHE_PATH, 'w') as f:
        json.dump(_cache or {}, f, indent=2)


def update_team_xg(team_name, xg_2h):
    cache = _load_cache()
    key = team_name.lower().strip()
    series = cache.get(key, [])
    series.append(round(float(xg_2h), 3))
    cache[key] = series[-MAX_SERIES_LEN:]
    _save_cache()
    log.debug('[TimesFM] updated %s: series=%s', key, cache[key])


def update_wc_team_goals(team_name, goals_2h):
    """Call after each WC match settles to grow the WC shadow cache."""
    wc_cache = _load_wc_cache()
    key = team_name.lower().strip()
    series = wc_cache.get(key, [])
    series.append(int(goals_2h))
    wc_cache[key] = series[-10:]
    with open(WC_CACHE_PATH, 'w') as f:
        json.dump(wc_cache, f, indent=2)
    log.info('[TimesFM-WC] updated %s: series=%s', key, wc_cache[key])


def _ewm_predict(series):
    result = series[0]
    for v in series[1:]:
        result = EWM_ALPHA * v + (1 - EWM_ALPHA) * result
    return result


def _timesfm_predict(series):
    try:
        import timesfm
        tfm = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(backend='cpu', per_core_batch_size=32, horizon_len=1),
            checkpoint=timesfm.TimesFmCheckpoint(huggingface_repo_id='google/timesfm-1.0-200m-pytorch'),
        )
        forecast, _ = tfm.forecast([series], freq=[0])
        return float(forecast[0][0])
    except Exception as e:
        log.warning('[TimesFM] model failed (%s), falling back to EWM', e)
        return _ewm_predict(series)


def predict_xg_2h(team_name):
    cache = _load_cache()
    series = cache.get(team_name.lower().strip(), [])
    if len(series) < MIN_SERIES_LEN:
        return None
    return _timesfm_predict(series) if USE_REAL_TIMESFM else _ewm_predict(series)


def _predict_wc_xg(team_name):
    wc_cache = _load_wc_cache()
    series = wc_cache.get(team_name.lower().strip(), [])
    if len(series) < WC_MIN_SERIES_LEN:
        return None
    return float(_ewm_predict([float(v) for v in series]))


def _poisson_under(lam, line):
    k_max = int(line)
    prob = 0.0
    for k in range(k_max + 1):
        prob += math.exp(-lam) * (lam ** k) / math.factorial(k)
    return prob


def match_under_score(home_team, away_team, line=1.5):
    xg_home = predict_xg_2h(home_team)
    xg_away = predict_xg_2h(away_team)
    if xg_home is None or xg_away is None:
        return None
    total_xg = xg_home + xg_away
    score = _poisson_under(total_xg, line)
    log.debug('[TimesFM] %s vs %s: xG=%.2f+%.2f=%.2f -> P(U%.1f)=%.3f',
              home_team, away_team, xg_home, xg_away, total_xg, line, score)
    return score


def _wc_shadow_log(pick):
    """Shadow-only: compute WC prediction and log. Never blocks."""
    if not TIMESFM_WC_SHADOW:
        return
    lbl = str(pick.get('label', '') or pick.get('side', '')).lower()
    if 'under 1.5' in lbl:
        line = 1.5
    elif 'under 2.5' in lbl:
        line = 2.5
    else:
        return
    match = pick.get('match', '')
    parts = [p.strip() for p in match.split(' vs ')]
    if len(parts) != 2:
        return
    home, away = parts[0], parts[1]
    xg_h = _predict_wc_xg(home)
    xg_a = _predict_wc_xg(away)
    if xg_h is None and xg_a is None:
        log.info('[TimesFM-WC SHADOW] %s: no WC data for either team -- PASS', match)
        return
    if xg_h is None or xg_a is None:
        known = '%s=%.1f' % ((home, xg_h) if xg_h is not None else (away, xg_a))
        missing = away if xg_h is not None else home
        log.info('[TimesFM-WC SHADOW] %s: partial data (%s, %s=unknown) -- PASS', match, known, missing)
        return
    total_xg = xg_h + xg_a
    prob = _poisson_under(total_xg, line)
    verdict = 'would BLOCK' if prob < WC_UNDER_THRESHOLD else 'would PASS'
    log.info('[TimesFM-WC SHADOW] %s | xG=%.1f+%.1f=%.1f | P(U%.1f)=%.3f | %s',
             match, xg_h, xg_a, total_xg, line, prob, verdict)


def should_place(pick):
    # WC shadow: always runs first, never blocks, logs only
    _wc_shadow_log(pick)

    if not TIMESFM_ENABLED:
        return True
    conf = float(pick.get('conf', pick.get('confidence', 1.0)) or 1.0)
    if not (GRAY_ZONE_CONF_LO <= conf < GRAY_ZONE_CONF_HI):
        return True
    lbl = str(pick.get('label', '') or pick.get('side', '')).lower()
    if 'under 1.5' in lbl:
        line = 1.5
    elif 'under 2.5' in lbl:
        line = 2.5
    else:
        return True
    match = pick.get('match', '')
    parts = [p.strip() for p in match.split(' vs ')]
    if len(parts) != 2:
        return True
    score = match_under_score(parts[0], parts[1], line)
    if score is None:
        log.info('[TimesFM] no data for %s -- allowing pick', match)
        return True
    if score < UNDER_THRESHOLD:
        log.info('[TimesFM] BLOCK %s | P(U%.1f)=%.3f < %.2f', match, line, score, UNDER_THRESHOLD)
        return False
    log.info('[TimesFM] OK %s | P(U%.1f)=%.3f >= %.2f', match, line, score, UNDER_THRESHOLD)
    return True
