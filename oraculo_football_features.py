#!/usr/bin/env python3
"""
oraculo_football_features.py - Football Feature Engineering

Builds ML-ready feature vectors from football-data.org match data.
Features include form, home/away splits, H2H, table position,
rest days, goal stats, and clean sheet rates.
"""

import logging
from datetime import datetime

log = logging.getLogger('oraculo.features')

# ---------------------------------------------------------------------------
# Main feature builder
# ---------------------------------------------------------------------------

def build_match_features(match, home_matches, away_matches,
                         home_standings_row, away_standings_row,
                         h2h_data=None, csv_stats=None):
    """
    Build a feature dict for a single upcoming match.

    Args:
        match: normalized match dict from oraculo_football
        home_matches: list of recent finished matches for home team
        away_matches: list of recent finished matches for away team
        home_standings_row: standings dict for home team (or None)
        away_standings_row: standings dict for away team (or None)
        h2h_data: head-to-head dict (or None)

    Returns:
        Dict of features ready for ML model input.
    """
    features = {}

    home_id = match.get('home_id', 0)
    away_id = match.get('away_id', 0)

    # ---- Form features (last 5 and last 10) ----
    h_form5 = _compute_form(home_matches, home_id, n=5)
    h_form10 = _compute_form(home_matches, home_id, n=10)
    a_form5 = _compute_form(away_matches, away_id, n=5)
    a_form10 = _compute_form(away_matches, away_id, n=10)

    features['home_win_pct_5'] = h_form5['win_pct']
    features['home_win_pct_10'] = h_form10['win_pct']
    features['home_draw_pct_5'] = h_form5['draw_pct']
    features['home_goals_scored_avg_5'] = h_form5['goals_scored_avg']
    features['home_goals_scored_avg_10'] = h_form10['goals_scored_avg']
    features['home_goals_conceded_avg_5'] = h_form5['goals_conceded_avg']
    features['home_goals_conceded_avg_10'] = h_form10['goals_conceded_avg']

    features['away_win_pct_5'] = a_form5['win_pct']
    features['away_win_pct_10'] = a_form10['win_pct']
    features['away_draw_pct_5'] = a_form5['draw_pct']
    features['away_goals_scored_avg_5'] = a_form5['goals_scored_avg']
    features['away_goals_scored_avg_10'] = a_form10['goals_scored_avg']
    features['away_goals_conceded_avg_5'] = a_form5['goals_conceded_avg']
    features['away_goals_conceded_avg_10'] = a_form10['goals_conceded_avg']

    # ---- Home/Away split performance ----
    h_home = _compute_venue_form(home_matches, home_id, venue='home')
    a_away = _compute_venue_form(away_matches, away_id, venue='away')

    features['home_home_win_pct'] = h_home['win_pct']
    features['home_home_goals_avg'] = h_home['goals_scored_avg']
    features['home_home_conceded_avg'] = h_home['goals_conceded_avg']
    features['away_away_win_pct'] = a_away['win_pct']
    features['away_away_goals_avg'] = a_away['goals_scored_avg']
    features['away_away_conceded_avg'] = a_away['goals_conceded_avg']

    # ---- Table position and points ----
    if home_standings_row:
        features['home_position'] = home_standings_row.get('position', 0)
        features['home_points'] = home_standings_row.get('points', 0)
        features['home_goal_diff'] = home_standings_row.get('goal_difference', 0)
        features['home_played'] = home_standings_row.get('played', 0)
        features['home_ppg'] = (home_standings_row.get('points', 0) /
                                max(home_standings_row.get('played', 1), 1))
    else:
        features['home_position'] = 0
        features['home_points'] = 0
        features['home_goal_diff'] = 0
        features['home_played'] = 0
        features['home_ppg'] = 0.0

    if away_standings_row:
        features['away_position'] = away_standings_row.get('position', 0)
        features['away_points'] = away_standings_row.get('points', 0)
        features['away_goal_diff'] = away_standings_row.get('goal_difference', 0)
        features['away_played'] = away_standings_row.get('played', 0)
        features['away_ppg'] = (away_standings_row.get('points', 0) /
                                max(away_standings_row.get('played', 1), 1))
    else:
        features['away_position'] = 0
        features['away_points'] = 0
        features['away_goal_diff'] = 0
        features['away_played'] = 0
        features['away_ppg'] = 0.0

    features['position_diff'] = features['home_position'] - features['away_position']
    features['points_diff'] = features['home_points'] - features['away_points']

    # ---- Rest days ----
    features['home_rest_days'] = _days_since_last(home_matches, match.get('utc_date', ''))
    features['away_rest_days'] = _days_since_last(away_matches, match.get('utc_date', ''))
    features['rest_diff'] = features['home_rest_days'] - features['away_rest_days']

    # ---- Clean sheet percentage ----
    features['home_clean_sheet_pct'] = h_form10['clean_sheet_pct']
    features['away_clean_sheet_pct'] = a_form10['clean_sheet_pct']

    # ---- Goal difference trend ----
    features['home_gd_per_game_5'] = h_form5['gd_per_game']
    features['away_gd_per_game_5'] = a_form5['gd_per_game']

    # ---- Head to head ----
    if h2h_data and h2h_data.get('total_matches', 0) > 0:
        total = h2h_data['total_matches']
        features['h2h_total'] = total
        features['h2h_home_win_pct'] = h2h_data.get('home_wins', 0) / total
        features['h2h_away_win_pct'] = h2h_data.get('away_wins', 0) / total
        features['h2h_draw_pct'] = h2h_data.get('draws', 0) / total
        # Recent H2H form (last 5 meetings)
        h2h_recent = _h2h_recent_form(h2h_data.get('matches', []),
                                       home_id, away_id, n=5)
        features['h2h_home_win_pct_recent'] = h2h_recent['home_win_pct']
        features['h2h_away_win_pct_recent'] = h2h_recent['away_win_pct']
        features['h2h_goals_avg'] = h2h_recent['total_goals_avg']
    else:
        features['h2h_total'] = 0
        features['h2h_home_win_pct'] = 0.33
        features['h2h_away_win_pct'] = 0.33
        features['h2h_draw_pct'] = 0.34
        features['h2h_home_win_pct_recent'] = 0.33
        features['h2h_away_win_pct_recent'] = 0.33
        features['h2h_goals_avg'] = 2.5

    # ---- Derived / interaction features ----
    features['form_diff_5'] = features['home_win_pct_5'] - features['away_win_pct_5']
    features['attack_vs_defense'] = (features['home_goals_scored_avg_5'] -
                                      features['away_goals_conceded_avg_5'])
    features['defense_vs_attack'] = (features['away_goals_scored_avg_5'] -
                                      features['home_goals_conceded_avg_5'])
    features['expected_goals'] = (features['home_goals_scored_avg_5'] +
                                   features['away_goals_scored_avg_5']) / 2.0

    # ---- Market-specific features (Over/Under, BTTS, Handicap) ----
    h_mkt = _compute_market_stats(home_matches, home_id, n=10)
    a_mkt = _compute_market_stats(away_matches, away_id, n=10)

    features['home_over25_rate'] = h_mkt['over25_rate']
    features['away_over25_rate'] = a_mkt['over25_rate']
    features['home_total_goals_avg'] = h_mkt['total_goals_avg']
    features['away_total_goals_avg'] = a_mkt['total_goals_avg']
    features['combined_goals_predict'] = (h_form5['goals_scored_avg'] +
                                          a_form5['goals_scored_avg'])
    features['defensive_solidity'] = (h_form5['goals_conceded_avg'] +
                                      a_form5['goals_conceded_avg']) / 2.0

    features['home_btts_rate'] = h_mkt['btts_rate']
    features['away_btts_rate'] = a_mkt['btts_rate']
    features['home_scored_pct'] = h_mkt['scored_pct']
    features['away_scored_pct'] = a_mkt['scored_pct']
    features['home_conceded_pct'] = h_mkt['conceded_pct']
    features['away_conceded_pct'] = a_mkt['conceded_pct']

    features['home_win_margin_avg'] = h_mkt['win_margin_avg']
    features['away_win_margin_avg'] = a_mkt['win_margin_avg']

    # ---- CSV stats features (corners, cards, shots) ----
    # These come from football-data.co.uk via csv_stats param
    csv_home = csv_stats.get('home', {}) if csv_stats else {}
    csv_away = csv_stats.get('away', {}) if csv_stats else {}

    features['home_corners_avg'] = csv_home.get('corners_avg', 5.0)
    features['away_corners_avg'] = csv_away.get('corners_avg', 5.0)
    features['home_corners_conceded_avg'] = csv_home.get('corners_conceded_avg', 5.0)
    features['away_corners_conceded_avg'] = csv_away.get('corners_conceded_avg', 5.0)
    features['total_corners_predict'] = (features['home_corners_avg'] +
                                         features['away_corners_avg'])
    features['corners_diff'] = (features['home_corners_avg'] -
                                features['away_corners_avg'])

    features['home_yellow_avg'] = csv_home.get('yellow_avg', 1.5)
    features['away_yellow_avg'] = csv_away.get('yellow_avg', 1.5)
    features['total_cards_predict'] = (features['home_yellow_avg'] +
                                       features['away_yellow_avg'])

    features['home_shots_avg'] = csv_home.get('shots_avg', 12.0)
    features['away_shots_avg'] = csv_away.get('shots_avg', 12.0)
    features['home_shots_target_avg'] = csv_home.get('shots_target_avg', 4.0)
    features['away_shots_target_avg'] = csv_away.get('shots_target_avg', 4.0)
    features['shots_target_diff'] = (features['home_shots_target_avg'] -
                                     features['away_shots_target_avg'])

    # ---- Metadata (not features, but useful for tracking) ----
    features['_match_id'] = match.get('id', 0)
    features['_home_team'] = match.get('home_team', '')
    features['_away_team'] = match.get('away_team', '')
    features['_competition'] = match.get('competition_code', '')
    features['_utc_date'] = match.get('utc_date', '')

    return features


def get_feature_names():
    """Return ordered list of feature names used by the ML model (no metadata)."""
    return [
        'home_win_pct_5', 'home_win_pct_10', 'home_draw_pct_5',
        'home_goals_scored_avg_5', 'home_goals_scored_avg_10',
        'home_goals_conceded_avg_5', 'home_goals_conceded_avg_10',
        'away_win_pct_5', 'away_win_pct_10', 'away_draw_pct_5',
        'away_goals_scored_avg_5', 'away_goals_scored_avg_10',
        'away_goals_conceded_avg_5', 'away_goals_conceded_avg_10',
        'home_home_win_pct', 'home_home_goals_avg', 'home_home_conceded_avg',
        'away_away_win_pct', 'away_away_goals_avg', 'away_away_conceded_avg',
        'home_position', 'home_points', 'home_goal_diff', 'home_played', 'home_ppg',
        'away_position', 'away_points', 'away_goal_diff', 'away_played', 'away_ppg',
        'position_diff', 'points_diff',
        'home_rest_days', 'away_rest_days', 'rest_diff',
        'home_clean_sheet_pct', 'away_clean_sheet_pct',
        'home_gd_per_game_5', 'away_gd_per_game_5',
        'h2h_total', 'h2h_home_win_pct', 'h2h_away_win_pct', 'h2h_draw_pct',
        'h2h_home_win_pct_recent', 'h2h_away_win_pct_recent', 'h2h_goals_avg',
        'form_diff_5', 'attack_vs_defense', 'defense_vs_attack', 'expected_goals',
        # Market features (Over/Under, BTTS, Handicap)
        'home_over25_rate', 'away_over25_rate',
        'home_total_goals_avg', 'away_total_goals_avg',
        'combined_goals_predict', 'defensive_solidity',
        'home_btts_rate', 'away_btts_rate',
        'home_scored_pct', 'away_scored_pct',
        'home_conceded_pct', 'away_conceded_pct',
        'home_win_margin_avg', 'away_win_margin_avg',
        # CSV stats features (corners, cards, shots)
        'home_corners_avg', 'away_corners_avg',
        'home_corners_conceded_avg', 'away_corners_conceded_avg',
        'total_corners_predict', 'corners_diff',
        'home_yellow_avg', 'away_yellow_avg', 'total_cards_predict',
        'home_shots_avg', 'away_shots_avg',
        'home_shots_target_avg', 'away_shots_target_avg', 'shots_target_diff',
        # Elo features
        'elo_home', 'elo_away', 'elo_diff', 'elo_expected_home',
        # Poisson features
        'poisson_lambda_home', 'poisson_lambda_away',
        'poisson_over25', 'poisson_btts',
        # xG features (from Understat)
        'home_xg_for_avg', 'home_xg_against_avg', 'home_xg_diff',
        'home_xg_over25_rate', 'home_overperform',
        'away_xg_for_avg', 'away_xg_against_avg', 'away_xg_diff',
        'away_xg_over25_rate', 'away_overperform',
        'xg_total_predict', 'xg_diff',
        # Weather features (from Open-Meteo)
        'weather_temp', 'weather_rain', 'weather_wind', 'weather_humidity',
        'weather_is_rainy', 'weather_is_windy', 'weather_is_cold',
    ]


def features_to_vector(features):
    """Convert feature dict to ordered list for model input."""
    names = get_feature_names()
    return [features.get(n, 0.0) for n in names]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_form(matches, team_id, n=5):
    """Compute form stats for a team over their last n matches."""
    recent = matches[:n] if len(matches) >= n else matches
    if not recent:
        return {
            'win_pct': 0.0, 'draw_pct': 0.0, 'loss_pct': 0.0,
            'goals_scored_avg': 0.0, 'goals_conceded_avg': 0.0,
            'clean_sheet_pct': 0.0, 'gd_per_game': 0.0,
        }

    wins = draws = losses = 0
    scored = conceded = 0
    clean_sheets = 0

    for m in recent:
        gs, gc = _get_team_goals(m, team_id)
        scored += gs
        conceded += gc
        if gc == 0:
            clean_sheets += 1
        if gs > gc:
            wins += 1
        elif gs == gc:
            draws += 1
        else:
            losses += 1

    total = len(recent)
    return {
        'win_pct': wins / total,
        'draw_pct': draws / total,
        'loss_pct': losses / total,
        'goals_scored_avg': scored / total,
        'goals_conceded_avg': conceded / total,
        'clean_sheet_pct': clean_sheets / total,
        'gd_per_game': (scored - conceded) / total,
    }


def _compute_venue_form(matches, team_id, venue='home'):
    """Compute form stats only for home or away matches."""
    filtered = []
    for m in matches:
        if venue == 'home' and m.get('home_id') == team_id:
            filtered.append(m)
        elif venue == 'away' and m.get('away_id') == team_id:
            filtered.append(m)

    return _compute_form(filtered, team_id, n=len(filtered))


def _get_team_goals(match, team_id):
    """Return (goals_scored, goals_conceded) for a team in a match."""
    hs = match.get('home_score')
    as_ = match.get('away_score')
    if hs is None or as_ is None:
        return 0, 0
    if match.get('home_id') == team_id:
        return hs, as_
    elif match.get('away_id') == team_id:
        return as_, hs
    return 0, 0


def _days_since_last(matches, ref_date_str):
    """Calculate days between ref_date and most recent match."""
    if not matches or not ref_date_str:
        return 7  # default assumption

    try:
        ref = datetime.fromisoformat(ref_date_str.replace('Z', '+00:00'))
    except Exception:
        try:
            ref = datetime.strptime(ref_date_str[:19], '%Y-%m-%dT%H:%M:%S')
        except Exception:
            return 7

    for m in matches:
        d = m.get('utc_date', '')
        if not d:
            continue
        try:
            md = datetime.fromisoformat(d.replace('Z', '+00:00'))
        except Exception:
            try:
                md = datetime.strptime(d[:19], '%Y-%m-%dT%H:%M:%S')
            except Exception:
                continue
        delta = (ref - md).days
        if delta >= 0:
            return delta

    return 7


def _h2h_recent_form(h2h_matches, home_id, away_id, n=5):
    """Compute H2H form from recent meetings."""
    recent = h2h_matches[:n] if len(h2h_matches) >= n else h2h_matches
    if not recent:
        return {'home_win_pct': 0.33, 'away_win_pct': 0.33, 'total_goals_avg': 2.5}

    home_wins = away_wins = draws = 0
    total_goals = 0

    for m in recent:
        hs = m.get('home_score')
        as_ = m.get('away_score')
        if hs is None or as_ is None:
            continue
        total_goals += hs + as_

        # Determine which side is "home" team in this H2H match
        if m.get('home_id') == home_id:
            if hs > as_:
                home_wins += 1
            elif hs < as_:
                away_wins += 1
            else:
                draws += 1
        elif m.get('away_id') == home_id:
            if as_ > hs:
                home_wins += 1
            elif as_ < hs:
                away_wins += 1
            else:
                draws += 1

    total = len(recent)
    return {
        'home_win_pct': home_wins / total,
        'away_win_pct': away_wins / total,
        'total_goals_avg': total_goals / total,
    }


def _compute_market_stats(matches, team_id, n=10):
    """Compute goal-market stats for Over/Under, BTTS, and Handicap."""
    recent = matches[:n] if len(matches) >= n else matches
    if not recent:
        return {
            'over25_rate': 0.5, 'btts_rate': 0.5, 'total_goals_avg': 2.5,
            'scored_pct': 0.5, 'conceded_pct': 0.5, 'win_margin_avg': 0.0,
        }

    over25 = 0
    btts = 0
    total_goals = 0
    scored_count = 0
    conceded_count = 0
    win_margins = []

    for m in recent:
        gs, gc = _get_team_goals(m, team_id)
        total = gs + gc
        total_goals += total

        if total > 2:
            over25 += 1
        if gs > 0 and gc > 0:
            btts += 1
        if gs > 0:
            scored_count += 1
        if gc > 0:
            conceded_count += 1
        if gs > gc:
            win_margins.append(gs - gc)

    count = len(recent)
    return {
        'over25_rate': over25 / count,
        'btts_rate': btts / count,
        'total_goals_avg': total_goals / count,
        'scored_pct': scored_count / count,
        'conceded_pct': conceded_count / count,
        'win_margin_avg': sum(win_margins) / len(win_margins) if win_margins else 0.0,
    }


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import json as _json
    logging.basicConfig(level=logging.DEBUG)

    # Quick test with dummy data
    match = {
        'id': 1, 'home_team': 'Team A', 'away_team': 'Team B',
        'home_id': 100, 'away_id': 200, 'competition_code': 'PL',
        'utc_date': '2026-03-20T15:00:00Z',
    }
    dummy_matches = [
        {'home_id': 100, 'away_id': 300, 'home_score': 2, 'away_score': 1,
         'utc_date': '2026-03-15T15:00:00Z', 'status': 'FINISHED'},
        {'home_id': 400, 'away_id': 100, 'home_score': 0, 'away_score': 3,
         'utc_date': '2026-03-10T15:00:00Z', 'status': 'FINISHED'},
    ]

    features = build_match_features(match, dummy_matches, [], None, None)
    print('Feature count: %d' % len(get_feature_names()))
    for k, v in sorted(features.items()):
        if not k.startswith('_'):
            print('  %-35s = %s' % (k, v))
