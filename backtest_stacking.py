#!/usr/bin/env python3
"""
backtest_stacking.py
Compare current MarketPredictor voting ensemble (v1) vs stacking with
Ridge meta-learner (v2) on historical match data from league JSON caches.

Strategy:
  - Load raw match JSON from .oraculo_cache/csv/
  - Rebuild features using rolling window logic (mirrors oraculo_football_features.py)
  - Compute binary labels per market (over25, btts, corners_o95, cards_o35, shots_target_o85)
  - Per league+market with >=50 rows: 80/20 chronological split
  - Train v1 (VotingClassifier: RF+GBC+ET) on train set
  - Train v2 (Stacking: RF+GBC+ET base + Ridge meta, 5-fold OOF) on train set
  - Evaluate both on test set: log-loss, ROC-AUC, Brier score
  - Simulate profit: bet 1 unit when prob > 0.55, avg odds 1.90
  - Print comparison table
"""

import os
import sys
import json
import warnings
import numpy as np
from datetime import datetime
from collections import defaultdict

warnings.filterwarnings('ignore')

CACHE_DIR = '/home/noc/oraculo_v2/.oraculo_cache/csv'
LEAGUE_FILES = {
    'PL':  'E0_2526.json',
    'BL1': 'D1_2526.json',
    'FL1': 'F1_2526.json',
    'SA':  'I1_2526.json',
    'PD':  'SP1_2526.json',
}
AVG_ODDS = 1.90
BET_THRESHOLD = 0.55
MIN_ROWS = 50

# ---------------------------------------------------------------------------
# Feature engineering (self-contained, no import of oraculo modules)
# ---------------------------------------------------------------------------

def _form(matches, team_key, n=5):
    recent = [m for m in matches if m.get('_team') == team_key][:n]
    if not recent:
        return dict(win_pct=0., draw_pct=0., goals_scored_avg=0.,
                    goals_conceded_avg=0., clean_sheet_pct=0., gd_per_game=0.)
    wins = draws = 0
    scored = conceded = cs = 0
    for m in recent:
        gs, gc = m['gs'], m['gc']
        scored += gs; conceded += gc
        if gc == 0: cs += 1
        if gs > gc: wins += 1
        elif gs == gc: draws += 1
    t = len(recent)
    return dict(win_pct=wins/t, draw_pct=draws/t,
                goals_scored_avg=scored/t, goals_conceded_avg=conceded/t,
                clean_sheet_pct=cs/t, gd_per_game=(scored-conceded)/t)

def _mkt_stats(matches, team_key, n=10):
    recent = [m for m in matches if m.get('_team') == team_key][:n]
    if not recent:
        return dict(over25_rate=0.5, btts_rate=0.5, total_goals_avg=2.5,
                    scored_pct=0.5, conceded_pct=0.5, win_margin_avg=0.)
    o25 = btts = sc = cc = 0
    tg = wm = 0.
    wm_cnt = 0
    for m in recent:
        gs, gc = m['gs'], m['gc']
        tg += gs + gc
        if gs + gc > 2: o25 += 1
        if gs > 0 and gc > 0: btts += 1
        if gs > 0: sc += 1
        if gc > 0: cc += 1
        if gs > gc: wm += gs - gc; wm_cnt += 1
    t = len(recent)
    return dict(over25_rate=o25/t, btts_rate=btts/t, total_goals_avg=tg/t,
                scored_pct=sc/t, conceded_pct=cc/t,
                win_margin_avg=wm/wm_cnt if wm_cnt else 0.)

def _venue_form(matches, team_key, venue, n=20):
    filtered = [m for m in matches
                if m.get('_team') == team_key and m.get('_venue') == venue][:n]
    if not filtered:
        return dict(win_pct=0., goals_scored_avg=0., goals_conceded_avg=0.)
    wins = sc = cc = 0
    for m in filtered:
        gs, gc = m['gs'], m['gc']
        sc += gs; cc += gc
        if gs > gc: wins += 1
    t = len(filtered)
    return dict(win_pct=wins/t, goals_scored_avg=sc/t, goals_conceded_avg=cc/t)

def _csv_stats(matches, team_key, n=10):
    recent = [m for m in matches if m.get('_team') == team_key][:n]
    if not recent:
        return dict(corners_avg=5., corners_conceded_avg=5.,
                    yellow_avg=1.5, shots_avg=12., shots_target_avg=4.)
    cf = cc = yc = sh = sht = 0.
    cnt = 0
    for m in recent:
        cf += m.get('corners_for', 5.)
        cc += m.get('corners_against', 5.)
        yc += m.get('yellow', 1.5)
        sh += m.get('shots', 12.)
        sht += m.get('shots_target', 4.)
        cnt += 1
    return dict(corners_avg=cf/cnt, corners_conceded_avg=cc/cnt,
                yellow_avg=yc/cnt, shots_avg=sh/cnt, shots_target_avg=sht/cnt)

FEATURE_NAMES = [
    'home_win_pct_5','home_win_pct_10','home_draw_pct_5',
    'home_goals_scored_avg_5','home_goals_scored_avg_10',
    'home_goals_conceded_avg_5','home_goals_conceded_avg_10',
    'away_win_pct_5','away_win_pct_10','away_draw_pct_5',
    'away_goals_scored_avg_5','away_goals_scored_avg_10',
    'away_goals_conceded_avg_5','away_goals_conceded_avg_10',
    'home_home_win_pct','home_home_goals_avg','home_home_conceded_avg',
    'away_away_win_pct','away_away_goals_avg','away_away_conceded_avg',
    'home_position','home_points','home_goal_diff','home_played','home_ppg',
    'away_position','away_points','away_goal_diff','away_played','away_ppg',
    'position_diff','points_diff',
    'home_rest_days','away_rest_days','rest_diff',
    'home_clean_sheet_pct','away_clean_sheet_pct',
    'home_gd_per_game_5','away_gd_per_game_5',
    'h2h_total','h2h_home_win_pct','h2h_away_win_pct','h2h_draw_pct',
    'h2h_home_win_pct_recent','h2h_away_win_pct_recent','h2h_goals_avg',
    'form_diff_5','attack_vs_defense','defense_vs_attack','expected_goals',
    'home_over25_rate','away_over25_rate',
    'home_total_goals_avg','away_total_goals_avg',
    'combined_goals_predict','defensive_solidity',
    'home_btts_rate','away_btts_rate',
    'home_scored_pct','away_scored_pct',
    'home_conceded_pct','away_conceded_pct',
    'home_win_margin_avg','away_win_margin_avg',
    'home_corners_avg','away_corners_avg',
    'home_corners_conceded_avg','away_corners_conceded_avg',
    'total_corners_predict','corners_diff',
    'home_yellow_avg','away_yellow_avg','total_cards_predict',
    'home_shots_avg','away_shots_avg',
    'home_shots_target_avg','away_shots_target_avg','shots_target_diff',
    # Elo/Poisson/xG/Weather - zeroed out (not available in raw JSON)
    'elo_home','elo_away','elo_diff','elo_expected_home',
    'poisson_lambda_home','poisson_lambda_away','poisson_over25','poisson_btts',
    'home_xg_for_avg','home_xg_against_avg','home_xg_diff','home_xg_over25_rate','home_overperform',
    'away_xg_for_avg','away_xg_against_avg','away_xg_diff','away_xg_over25_rate','away_overperform',
    'xg_total_predict','xg_diff',
    'weather_temp','weather_rain','weather_wind','weather_humidity',
    'weather_is_rainy','weather_is_windy','weather_is_cold',
]

def build_features(match, history, standings):
    """Build 105-dim feature vector for a match using prior history."""
    ht = match['home_team']
    at = match['away_team']
    date = match.get('utc_date', '')

    # Build per-team history with perspective fields
    home_hist = []
    away_hist = []
    for m in history:
        md = m.get('utc_date', '')
        if md >= date:
            continue  # only use matches before this one
        hm = at_m = None
        if m['home_team'] == ht:
            home_hist.append({**m, '_team': ht, '_venue': 'home',
                               'gs': m['home_score'], 'gc': m['away_score'],
                               'corners_for': m.get('home_corners', 5.),
                               'corners_against': m.get('away_corners', 5.),
                               'yellow': m.get('home_yellow', 1.5),
                               'shots': m.get('home_shots', 12.),
                               'shots_target': m.get('home_shots_target', 4.)})
        elif m['away_team'] == ht:
            home_hist.append({**m, '_team': ht, '_venue': 'away',
                               'gs': m['away_score'], 'gc': m['home_score'],
                               'corners_for': m.get('away_corners', 5.),
                               'corners_against': m.get('home_corners', 5.),
                               'yellow': m.get('away_yellow', 1.5),
                               'shots': m.get('away_shots', 12.),
                               'shots_target': m.get('away_shots_target', 4.)})
        if m['home_team'] == at:
            away_hist.append({**m, '_team': at, '_venue': 'home',
                               'gs': m['home_score'], 'gc': m['away_score'],
                               'corners_for': m.get('home_corners', 5.),
                               'corners_against': m.get('away_corners', 5.),
                               'yellow': m.get('home_yellow', 1.5),
                               'shots': m.get('home_shots', 12.),
                               'shots_target': m.get('home_shots_target', 4.)})
        elif m['away_team'] == at:
            away_hist.append({**m, '_team': at, '_venue': 'away',
                               'gs': m['away_score'], 'gc': m['home_score'],
                               'corners_for': m.get('away_corners', 5.),
                               'corners_against': m.get('home_corners', 5.),
                               'yellow': m.get('away_yellow', 1.5),
                               'shots': m.get('away_shots', 12.),
                               'shots_target': m.get('away_shots_target', 4.)})

    hf5  = _form(home_hist, ht, 5)
    hf10 = _form(home_hist, ht, 10)
    af5  = _form(away_hist, at, 5)
    af10 = _form(away_hist, at, 10)
    hv   = _venue_form(home_hist, ht, 'home')
    av   = _venue_form(away_hist, at, 'away')
    hm   = _mkt_stats(home_hist, ht, 10)
    am   = _mkt_stats(away_hist, at, 10)
    hc   = _csv_stats(home_hist, ht, 10)
    ac   = _csv_stats(away_hist, at, 10)

    hs = standings.get(ht, {})
    as_ = standings.get(at, {})

    f = {}
    f['home_win_pct_5']          = hf5['win_pct']
    f['home_win_pct_10']         = hf10['win_pct']
    f['home_draw_pct_5']         = hf5['draw_pct']
    f['home_goals_scored_avg_5'] = hf5['goals_scored_avg']
    f['home_goals_scored_avg_10']= hf10['goals_scored_avg']
    f['home_goals_conceded_avg_5']= hf5['goals_conceded_avg']
    f['home_goals_conceded_avg_10']= hf10['goals_conceded_avg']
    f['away_win_pct_5']          = af5['win_pct']
    f['away_win_pct_10']         = af10['win_pct']
    f['away_draw_pct_5']         = af5['draw_pct']
    f['away_goals_scored_avg_5'] = af5['goals_scored_avg']
    f['away_goals_scored_avg_10']= af10['goals_scored_avg']
    f['away_goals_conceded_avg_5']= af5['goals_conceded_avg']
    f['away_goals_conceded_avg_10']= af10['goals_conceded_avg']
    f['home_home_win_pct']       = hv['win_pct']
    f['home_home_goals_avg']     = hv['goals_scored_avg']
    f['home_home_conceded_avg']  = hv['goals_conceded_avg']
    f['away_away_win_pct']       = av['win_pct']
    f['away_away_goals_avg']     = av['goals_scored_avg']
    f['away_away_conceded_avg']  = av['goals_conceded_avg']
    f['home_position']  = hs.get('position', 0)
    f['home_points']    = hs.get('points', 0)
    f['home_goal_diff'] = hs.get('goal_diff', 0)
    f['home_played']    = hs.get('played', 0)
    f['home_ppg']       = hs.get('points', 0) / max(hs.get('played', 1), 1)
    f['away_position']  = as_.get('position', 0)
    f['away_points']    = as_.get('points', 0)
    f['away_goal_diff'] = as_.get('goal_diff', 0)
    f['away_played']    = as_.get('played', 0)
    f['away_ppg']       = as_.get('points', 0) / max(as_.get('played', 1), 1)
    f['position_diff']  = f['home_position'] - f['away_position']
    f['points_diff']    = f['home_points'] - f['away_points']
    f['home_rest_days'] = 7.
    f['away_rest_days'] = 7.
    f['rest_diff']      = 0.
    f['home_clean_sheet_pct'] = hf10['clean_sheet_pct']
    f['away_clean_sheet_pct'] = af10['clean_sheet_pct']
    f['home_gd_per_game_5']   = hf5['gd_per_game']
    f['away_gd_per_game_5']   = af5['gd_per_game']
    f['h2h_total']             = 0.
    f['h2h_home_win_pct']      = 0.33
    f['h2h_away_win_pct']      = 0.33
    f['h2h_draw_pct']          = 0.34
    f['h2h_home_win_pct_recent']= 0.33
    f['h2h_away_win_pct_recent']= 0.33
    f['h2h_goals_avg']         = 2.5
    f['form_diff_5']           = hf5['win_pct'] - af5['win_pct']
    f['attack_vs_defense']     = hf5['goals_scored_avg'] - af5['goals_conceded_avg']
    f['defense_vs_attack']     = af5['goals_scored_avg'] - hf5['goals_conceded_avg']
    f['expected_goals']        = (hf5['goals_scored_avg'] + af5['goals_scored_avg']) / 2.
    f['home_over25_rate']      = hm['over25_rate']
    f['away_over25_rate']      = am['over25_rate']
    f['home_total_goals_avg']  = hm['total_goals_avg']
    f['away_total_goals_avg']  = am['total_goals_avg']
    f['combined_goals_predict']= hf5['goals_scored_avg'] + af5['goals_scored_avg']
    f['defensive_solidity']    = (hf5['goals_conceded_avg'] + af5['goals_conceded_avg']) / 2.
    f['home_btts_rate']        = hm['btts_rate']
    f['away_btts_rate']        = am['btts_rate']
    f['home_scored_pct']       = hm['scored_pct']
    f['away_scored_pct']       = am['scored_pct']
    f['home_conceded_pct']     = hm['conceded_pct']
    f['away_conceded_pct']     = am['conceded_pct']
    f['home_win_margin_avg']   = hm['win_margin_avg']
    f['away_win_margin_avg']   = am['win_margin_avg']
    f['home_corners_avg']          = hc['corners_avg']
    f['away_corners_avg']          = ac['corners_avg']
    f['home_corners_conceded_avg'] = hc['corners_conceded_avg']
    f['away_corners_conceded_avg'] = ac['corners_conceded_avg']
    f['total_corners_predict']     = hc['corners_avg'] + ac['corners_avg']
    f['corners_diff']              = hc['corners_avg'] - ac['corners_avg']
    f['home_yellow_avg']           = hc['yellow_avg']
    f['away_yellow_avg']           = ac['yellow_avg']
    f['total_cards_predict']       = hc['yellow_avg'] + ac['yellow_avg']
    f['home_shots_avg']            = hc['shots_avg']
    f['away_shots_avg']            = ac['shots_avg']
    f['home_shots_target_avg']     = hc['shots_target_avg']
    f['away_shots_target_avg']     = ac['shots_target_avg']
    f['shots_target_diff']         = hc['shots_target_avg'] - ac['shots_target_avg']
    # zeroed-out features
    for k in ['elo_home','elo_away','elo_diff','elo_expected_home',
              'poisson_lambda_home','poisson_lambda_away','poisson_over25','poisson_btts',
              'home_xg_for_avg','home_xg_against_avg','home_xg_diff','home_xg_over25_rate','home_overperform',
              'away_xg_for_avg','away_xg_against_avg','away_xg_diff','away_xg_over25_rate','away_overperform',
              'xg_total_predict','xg_diff',
              'weather_temp','weather_rain','weather_wind','weather_humidity',
              'weather_is_rainy','weather_is_windy','weather_is_cold']:
        f[k] = 0.

    return np.array([f.get(n, 0.) for n in FEATURE_NAMES], dtype=np.float32)


def make_labels(match):
    hs = match.get('home_score')
    as_ = match.get('away_score')
    if hs is None or as_ is None:
        return {}
    total = hs + as_
    labels = {
        'over25': 1 if total > 2.5 else 0,
        'btts':   1 if (hs > 0 and as_ > 0) else 0,
    }
    hc = match.get('home_corners')
    ac = match.get('away_corners')
    if hc is not None and ac is not None:
        labels['corners_o95'] = 1 if (hc + ac) > 9.5 else 0
    hy = match.get('home_yellow', 0) or 0
    ay = match.get('away_yellow', 0) or 0
    if match.get('home_yellow') is not None:
        labels['cards_o35'] = 1 if (hy + ay) > 3.5 else 0
    hst = match.get('home_shots_target')
    ast_ = match.get('away_shots_target')
    if hst is not None and ast_ is not None:
        labels['shots_target_o85'] = 1 if (hst + ast_) > 8.5 else 0
    return labels


def build_standings(matches_up_to):
    """Compute running standings from matches (points, gd, played)."""
    teams = defaultdict(lambda: dict(points=0, goal_diff=0, played=0))
    for m in matches_up_to:
        hs = m.get('home_score')
        as_ = m.get('away_score')
        if hs is None or as_ is None:
            continue
        ht = m['home_team']
        at = m['away_team']
        teams[ht]['played'] += 1
        teams[at]['played'] += 1
        teams[ht]['goal_diff'] += hs - as_
        teams[at]['goal_diff'] += as_ - hs
        if hs > as_:
            teams[ht]['points'] += 3
        elif hs == as_:
            teams[ht]['points'] += 1
            teams[at]['points'] += 1
        else:
            teams[at]['points'] += 3
    # Assign position by points desc
    ranked = sorted(teams.items(), key=lambda x: -x[1]['points'])
    for i, (name, d) in enumerate(ranked):
        d['position'] = i + 1
    return dict(teams)


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------

def load_league(league_code, fname):
    fpath = os.path.join(CACHE_DIR, fname)
    if not os.path.exists(fpath):
        print('  MISSING:', fpath)
        return []
    with open(fpath) as f:
        raw = json.load(f)
    matches = []
    for m in raw:
        hs = m.get('home_score')
        as_ = m.get('away_score')
        if hs is None or as_ is None:
            continue
        matches.append({
            'home_team':        m.get('home_team', m.get('home_team_csv', '')),
            'away_team':        m.get('away_team', m.get('away_team_csv', '')),
            'home_score':       hs,
            'away_score':       as_,
            'utc_date':         m.get('utc_date', ''),
            'home_corners':     m.get('home_corners'),
            'away_corners':     m.get('away_corners'),
            'home_yellow':      m.get('home_yellow'),
            'away_yellow':      m.get('away_yellow'),
            'home_shots':       m.get('home_shots'),
            'away_shots':       m.get('away_shots'),
            'home_shots_target':m.get('home_shots_target'),
            'away_shots_target':m.get('away_shots_target'),
            'league':           league_code,
        })
    matches.sort(key=lambda m: m.get('utc_date', ''))
    return matches


def build_dataset(matches):
    """For each match (after 10th), build feature vector + labels."""
    rows_X = []
    rows_y = defaultdict(list)
    valid_idx = []

    for i, match in enumerate(matches):
        if i < 10:
            continue  # need history
        history = matches[:i]
        standings = build_standings(history)
        try:
            x = build_features(match, history, standings)
        except Exception as e:
            continue
        x = np.nan_to_num(x, nan=0., posinf=0., neginf=0.)
        labels = make_labels(match)
        if not labels:
            continue
        rows_X.append(x)
        for mkt, lbl in labels.items():
            rows_y[mkt].append(lbl)
        # pad markets that weren't in this match
        valid_idx.append(i)

    X = np.array(rows_X, dtype=np.float32)
    # rows_y may have variable lengths due to optional markets; align
    n = len(rows_X)
    y_dict = {}
    for mkt, vals in rows_y.items():
        if len(vals) == n:
            y_dict[mkt] = np.array(vals, dtype=np.int32)
    return X, y_dict


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def make_base_models():
    from sklearn.ensemble import (RandomForestClassifier,
                                  GradientBoostingClassifier,
                                  ExtraTreesClassifier)
    return {
        'rf': RandomForestClassifier(
            n_estimators=100, max_depth=8, min_samples_split=10,
            min_samples_leaf=5, random_state=42, n_jobs=-1),
        'gbc': GradientBoostingClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.05,
            subsample=0.8, random_state=42),
        'et': ExtraTreesClassifier(
            n_estimators=100, max_depth=8, min_samples_split=10,
            random_state=43, n_jobs=-1),
    }


def train_v1(X_tr, y_tr):
    """Voting ensemble (soft, equal weights) — current architecture."""
    from sklearn.ensemble import VotingClassifier
    base = [(name, clf) for name, clf in make_base_models().items()]
    vc = VotingClassifier(estimators=base, voting='soft', n_jobs=-1)
    vc.fit(X_tr, y_tr)
    return vc


def train_v2(X_tr, y_tr):
    """Stacking: RF+GBC+ET base + Ridge meta-learner via 5-fold OOF."""
    from sklearn.model_selection import StratifiedKFold
    from sklearn.linear_model import RidgeClassifier
    from sklearn.calibration import CalibratedClassifierCV

    base_models = make_base_models()
    n = len(X_tr)
    n_base = len(base_models)
    oof_probs = np.zeros((n, n_base), dtype=np.float32)

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    for fold, (tr_idx, val_idx) in enumerate(skf.split(X_tr, y_tr)):
        Xf_tr, Xf_val = X_tr[tr_idx], X_tr[val_idx]
        yf_tr = y_tr[tr_idx]
        for j, (name, clf) in enumerate(base_models.items()):
            clf_clone = clf.__class__(**clf.get_params())
            clf_clone.fit(Xf_tr, yf_tr)
            oof_probs[val_idx, j] = clf_clone.predict_proba(Xf_val)[:, 1]

    # Train meta-learner on OOF predictions
    meta = CalibratedClassifierCV(RidgeClassifier(), cv=3, method='sigmoid')
    meta.fit(oof_probs, y_tr)

    # Retrain base models on full train set
    trained_base = {}
    for name, clf in base_models.items():
        clf.fit(X_tr, y_tr)
        trained_base[name] = clf

    return trained_base, meta


def predict_v1(model, X):
    return model.predict_proba(X)[:, 1]


def predict_v2(trained_base, meta, X):
    base_preds = np.column_stack([
        clf.predict_proba(X)[:, 1]
        for clf in trained_base.values()
    ])
    return meta.predict_proba(base_preds)[:, 1]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(y_true, probs, name):
    from sklearn.metrics import log_loss, roc_auc_score, brier_score_loss
    if len(np.unique(y_true)) < 2:
        return dict(name=name, logloss=float('nan'), auc=float('nan'),
                    brier=float('nan'), bets=0, profit=0., roi=float('nan'))
    ll  = log_loss(y_true, probs)
    auc = roc_auc_score(y_true, probs)
    bs  = brier_score_loss(y_true, probs)

    # Simulated profit: bet 1 unit when prob > threshold
    bets = 0
    profit = 0.
    for p, actual in zip(probs, y_true):
        if p > BET_THRESHOLD:
            bets += 1
            if actual == 1:
                profit += AVG_ODDS - 1.
            else:
                profit -= 1.
    roi = (profit / bets * 100.) if bets > 0 else float('nan')
    return dict(name=name, logloss=ll, auc=auc, brier=bs,
                bets=bets, profit=profit, roi=roi)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from sklearn.model_selection import train_test_split

    print('=' * 80)
    print('BACKTEST: Voting Ensemble (v1) vs Stacking+Ridge (v2)')
    print('Avg odds: %.2f  |  Bet threshold: %.2f  |  Min rows: %d' %
          (AVG_ODDS, BET_THRESHOLD, MIN_ROWS))
    print('=' * 80)

    all_results = []

    for league_code, fname in sorted(LEAGUE_FILES.items()):
        print('\nLoading %s (%s)...' % (league_code, fname))
        matches = load_league(league_code, fname)
        print('  %d completed matches' % len(matches))
        if len(matches) < 20:
            print('  Skipping — too few matches')
            continue

        X, y_dict = build_dataset(matches)
        print('  Dataset: %d rows, %d features, markets: %s' %
              (len(X), X.shape[1] if len(X) else 0, list(y_dict.keys())))

        for market, y in sorted(y_dict.items()):
            n = len(y)
            if n < MIN_ROWS:
                print('  [%s] Skipping %s — only %d rows' % (league_code, market, n))
                continue
            pos_rate = np.mean(y)
            if pos_rate < 0.05 or pos_rate > 0.95:
                print('  [%s] Skipping %s — degenerate labels (pos=%.2f)' %
                      (league_code, market, pos_rate))
                continue

            # Chronological 80/20 split
            split = int(n * 0.8)
            X_tr, X_te = X[:split], X[split:]
            y_tr, y_te = y[:split], y[split:]

            if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
                print('  [%s/%s] Skipping — single class in split' % (league_code, market))
                continue

            print('  [%s/%s] train=%d test=%d pos=%.1f%%' %
                  (league_code, market, len(y_tr), len(y_te), pos_rate*100))

            try:
                m1 = train_v1(X_tr, y_tr)
                p1 = predict_v1(m1, X_te)
                r1 = evaluate(y_te, p1, 'v1_voting')
            except Exception as e:
                print('    v1 ERROR:', e)
                continue

            try:
                base2, meta2 = train_v2(X_tr, y_tr)
                p2 = predict_v2(base2, meta2, X_te)
                r2 = evaluate(y_te, p2, 'v2_stacking')
            except Exception as e:
                print('    v2 ERROR:', e)
                continue

            all_results.append({
                'league': league_code,
                'market': market,
                'n_train': len(y_tr),
                'n_test':  len(y_te),
                'pos_rate': pos_rate,
                'v1': r1,
                'v2': r2,
            })

    # ------- Print comparison table -------
    print('\n')
    print('=' * 110)
    print('RESULTS COMPARISON')
    print('=' * 110)
    hdr = ('%-6s  %-18s  %5s  %5s  %7s  %7s  %7s  %7s  %7s  %7s  %7s  %7s'
           % ('LEAGUE', 'MARKET', 'TRAIN', 'TEST',
              'v1-AUC', 'v2-AUC', 'v1-LL', 'v2-LL',
              'v1-ROI', 'v2-ROI', 'v1-BETS', 'v2-BETS'))
    print(hdr)
    print('-' * 110)

    auc_wins_v2 = 0
    roi_wins_v2 = 0
    total = len(all_results)

    for r in all_results:
        v1, v2 = r['v1'], r['v2']
        auc_delta = (v2['auc'] - v1['auc']) if not (
            np.isnan(v2['auc']) or np.isnan(v1['auc'])) else 0.
        roi_delta = (v2['roi'] - v1['roi']) if not (
            np.isnan(v2['roi']) or np.isnan(v1['roi'])) else 0.
        if auc_delta > 0: auc_wins_v2 += 1
        if roi_delta > 0: roi_wins_v2 += 1

        marker = ' <--' if (auc_delta > 0.01 or roi_delta > 2.) else ''

        def fmt_f(x, decimals=4):
            return ('%.4f' % x) if not np.isnan(x) else '   N/A'
        def fmt_roi(x):
            return ('%+.1f%%' % x) if not np.isnan(x) else '   N/A'

        print('%-6s  %-18s  %5d  %5d  %s  %s  %s  %s  %s  %s  %7d  %7d%s' % (
            r['league'], r['market'], r['n_train'], r['n_test'],
            fmt_f(v1['auc']), fmt_f(v2['auc']),
            fmt_f(v1['logloss']), fmt_f(v2['logloss']),
            fmt_roi(v1['roi']), fmt_roi(v2['roi']),
            v1['bets'], v2['bets'],
            marker
        ))

    print('-' * 110)
    print('\nSUMMARY (%d market-league pairs):' % total)
    print('  v2 wins on AUC:  %d / %d  (%.0f%%)' % (auc_wins_v2, total,
          100.*auc_wins_v2/total if total else 0))
    print('  v2 wins on ROI:  %d / %d  (%.0f%%)' % (roi_wins_v2, total,
          100.*roi_wins_v2/total if total else 0))

    if all_results:
        v1_aucs = [r['v1']['auc'] for r in all_results if not np.isnan(r['v1']['auc'])]
        v2_aucs = [r['v2']['auc'] for r in all_results if not np.isnan(r['v2']['auc'])]
        v1_rois = [r['v1']['roi'] for r in all_results if not np.isnan(r['v1']['roi'])]
        v2_rois = [r['v2']['roi'] for r in all_results if not np.isnan(r['v2']['roi'])]
        print('  Mean AUC  v1=%.4f  v2=%.4f  delta=%+.4f' % (
            np.mean(v1_aucs), np.mean(v2_aucs), np.mean(v2_aucs)-np.mean(v1_aucs)))
        print('  Mean ROI  v1=%+.1f%%  v2=%+.1f%%  delta=%+.1f%%' % (
            np.mean(v1_rois), np.mean(v2_rois), np.mean(v2_rois)-np.mean(v1_rois)))

    print()
    if total > 0:
        auc_pct = 100.*auc_wins_v2/total
        roi_pct = 100.*roi_wins_v2/total
        if auc_pct >= 60 and roi_pct >= 55:
            verdict = 'RECOMMEND stacking (v2) — consistent improvement on both AUC and ROI'
        elif auc_pct >= 60:
            verdict = 'PARTIAL: stacking improves AUC but not ROI consistently — marginal benefit'
        elif roi_pct >= 60:
            verdict = 'PARTIAL: stacking improves ROI but not AUC — possible overfitting risk'
        else:
            verdict = 'NO CLEAR BENEFIT: voting ensemble (v1) holds up — keep current architecture'
        print('VERDICT:', verdict)
    print('=' * 110)


if __name__ == '__main__':
    main()
