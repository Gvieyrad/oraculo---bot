#!/usr/bin/env python3
"""
oraculo_peru.py - Peru Liga 1 specific model with altitude advantage.

Altitude is the #1 factor in Peruvian football:
- Juliaca (3,825m): Home teams dominate
- Cusco (3,400m): Significant altitude advantage
- Huancayo (3,271m): Strong home record
- Lima (154m): Sea level, no altitude factor

This model adds altitude features + local knowledge.
"""

import os
import json
import logging
import numpy as np
from collections import defaultdict

log = logging.getLogger('oraculo.peru')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Altitude in meters by city
ALTITUDE = {
    'Juliaca': 3825,
    'Cusco': 3400,
    'Huancayo': 3271,
    'Tarma': 3050,
    'Cajamarca': 2750,
    'Cajabamba': 2650,
    'Andahuaylas': 2926,
    'Cutervo': 2637,
    'Arequipa': 2335,
    'Moyobamba': 860,
    'Tarapoto': 356,
    'Piura': 29,
    'Sullana': 60,
    'Trujillo': 34,
    'Lima': 154,
    'Callao': 7,
    'Huacho': 30,
    'Humay': 460,
}

# Team -> home city mapping
TEAM_CITY = {
    'Binacional': 'Juliaca',
    'Cienciano': 'Cusco',
    'Cusco FC': 'Cusco',
    'Deportivo Garcilaso': 'Cusco',
    'Sport Huancayo': 'Huancayo',
    'ADT': 'Tarma',
    'UTC Cajamarca': 'Cajamarca',
    'Comerciantes Unidos': 'Cutervo',
    'FBC Melgar': 'Arequipa',
    'Sporting Cristal': 'Lima',
    'Alianza Lima': 'Lima',
    'Universitario': 'Lima',
    'Sport Boys': 'Callao',
    'Alianza Atletico': 'Sullana',
    'Carlos Mannucci': 'Trujillo',
    'Cesar Vallejo': 'Trujillo',
    'Atletico Grau': 'Piura',
    'Juan Aurich': 'Piura',
    'Univ San Martin': 'Lima',
    'Deportivo Municipal': 'Lima',
    'Los Chankas': 'Andahuaylas',
}


def get_altitude(city):
    """Get altitude for a city. Returns 0 if unknown."""
    if not city:
        return 0
    # Try exact match
    if city in ALTITUDE:
        return ALTITUDE[city]
    # Try partial match
    city_lower = city.lower()
    for k, v in ALTITUDE.items():
        if k.lower() in city_lower or city_lower in k.lower():
            return v
    return 0


def get_team_altitude(team_name):
    """Get home altitude for a team."""
    for team, city in TEAM_CITY.items():
        if team.lower() in team_name.lower() or team_name.lower() in team.lower():
            return ALTITUDE.get(city, 0)
    return 0


def load_peru_matches():
    """Load cached Peru matches."""
    cache_file = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv', 'PER_all.json')
    if not os.path.exists(cache_file):
        log.warning('No Peru data cached. Run API-Football download first.')
        return []
    with open(cache_file, 'r') as f:
        return json.load(f)


def build_peru_features(match, context):
    """
    Build Peru-specific features including altitude.

    Returns:
        dict of features
    """
    ht = match.get('home_team', '')
    at = match.get('away_team', '')
    venue_city = match.get('venue_city', '')

    # Altitude features
    venue_alt = get_altitude(venue_city)
    home_alt = get_team_altitude(ht)
    away_alt = get_team_altitude(at)
    alt_diff = venue_alt - away_alt  # How much higher venue is vs away team's home

    features = {
        'venue_altitude': venue_alt,
        'home_team_altitude': home_alt,
        'away_team_altitude': away_alt,
        'altitude_diff': alt_diff,
        'is_high_altitude': 1.0 if venue_alt > 2500 else 0.0,
        'is_extreme_altitude': 1.0 if venue_alt > 3500 else 0.0,
        'altitude_advantage': min(alt_diff / 1000, 3.0),  # Normalized 0-3
    }

    # Form features from context
    home_recent = [m for m in context
                   if m.get('home_team') == ht or m.get('away_team') == ht][-10:]
    away_recent = [m for m in context
                   if m.get('home_team') == at or m.get('away_team') == at][-10:]

    # Home form
    h_wins = h_goals = h_conceded = h_matches = 0
    for m in home_recent:
        hs = m.get('home_score', 0) or 0
        as_ = m.get('away_score', 0) or 0
        if m.get('home_team') == ht:
            h_goals += hs
            h_conceded += as_
            if hs > as_:
                h_wins += 1
        else:
            h_goals += as_
            h_conceded += hs
            if as_ > hs:
                h_wins += 1
        h_matches += 1

    # Away form
    a_wins = a_goals = a_conceded = a_matches = 0
    for m in away_recent:
        hs = m.get('home_score', 0) or 0
        as_ = m.get('away_score', 0) or 0
        if m.get('home_team') == at:
            a_goals += hs
            a_conceded += as_
            if hs > as_:
                a_wins += 1
        else:
            a_goals += as_
            a_conceded += hs
            if as_ > hs:
                a_wins += 1
        a_matches += 1

    features['home_win_rate'] = h_wins / max(h_matches, 1)
    features['home_goals_avg'] = h_goals / max(h_matches, 1)
    features['home_conceded_avg'] = h_conceded / max(h_matches, 1)
    features['away_win_rate'] = a_wins / max(a_matches, 1)
    features['away_goals_avg'] = a_goals / max(a_matches, 1)
    features['away_conceded_avg'] = a_conceded / max(a_matches, 1)

    # Goal totals
    features['combined_goals_avg'] = (features['home_goals_avg'] +
                                      features['away_goals_avg'])
    features['over25_estimate'] = 1.0 if features['combined_goals_avg'] > 2.5 else 0.0

    # Altitude impact on goals (high altitude = more goals historically)
    features['altitude_goal_boost'] = venue_alt / 5000  # 0-0.8 range

    # Home advantage at altitude
    if venue_alt > 2500:
        # Teams from altitude have advantage at home
        if home_alt > 2500:
            features['altitude_home_boost'] = 0.3  # They're used to it
        else:
            features['altitude_home_boost'] = 0.1  # Coast team at altitude = slight advantage
    else:
        features['altitude_home_boost'] = 0.0

    return features


def train_peru_model(matches=None):
    """Train a model specific to Peru Liga 1."""
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV
    import pickle

    if matches is None:
        matches = load_peru_matches()

    if len(matches) < 50:
        log.warning('Not enough Peru matches for training: %d', len(matches))
        return None

    matches.sort(key=lambda m: m.get('utc_date', ''))

    feature_names = [
        'venue_altitude', 'home_team_altitude', 'away_team_altitude',
        'altitude_diff', 'is_high_altitude', 'is_extreme_altitude',
        'altitude_advantage', 'home_win_rate', 'home_goals_avg',
        'home_conceded_avg', 'away_win_rate', 'away_goals_avg',
        'away_conceded_avg', 'combined_goals_avg', 'over25_estimate',
        'altitude_goal_boost', 'altitude_home_boost',
    ]

    X = []
    y_ou = []
    y_btts = []
    y_1x2 = []

    for i, m in enumerate(matches):
        context = matches[max(0, i - 100):i]
        if len(context) < 5:
            continue

        features = build_peru_features(m, context)
        vec = [features.get(f, 0.0) for f in feature_names]
        X.append(vec)

        hs = m.get('home_score', 0) or 0
        as_ = m.get('away_score', 0) or 0
        y_ou.append(1 if hs + as_ > 2.5 else 0)
        y_btts.append(1 if hs > 0 and as_ > 0 else 0)
        y_1x2.append(0 if hs > as_ else (1 if hs == as_ else 2))

    X = np.array(X, dtype=np.float32)
    y_ou = np.array(y_ou)
    y_btts = np.array(y_btts)

    print(f'Peru training: {len(X)} samples')
    print(f'  Over 2.5 rate: {np.mean(y_ou)*100:.1f}%')
    print(f'  BTTS rate: {np.mean(y_btts)*100:.1f}%')

    # Train models
    models = {}

    # Over/Under 2.5
    rf_ou = RandomForestClassifier(n_estimators=200, max_depth=8, random_state=42)
    rf_ou.fit(X, y_ou)
    gbc_ou = GradientBoostingClassifier(n_estimators=150, max_depth=5, random_state=42)
    gbc_ou.fit(X, y_ou)
    models['over25'] = {'rf': rf_ou, 'gbc': gbc_ou}

    # BTTS
    rf_btts = RandomForestClassifier(n_estimators=200, max_depth=8, random_state=42)
    rf_btts.fit(X, y_btts)
    gbc_btts = GradientBoostingClassifier(n_estimators=150, max_depth=5, random_state=42)
    gbc_btts.fit(X, y_btts)
    models['btts'] = {'rf': rf_btts, 'gbc': gbc_btts}

    # Feature importances
    print(f'\n  Top features (Over 2.5):')
    imp = sorted(zip(feature_names, rf_ou.feature_importances_),
                 key=lambda x: x[1], reverse=True)
    for name, val in imp[:7]:
        print(f'    {name:30s} {val:.4f}')

    # Save
    models_dir = os.path.join(SCRIPT_DIR, 'models')
    os.makedirs(models_dir, exist_ok=True)
    state = {
        'models': models,
        'feature_names': feature_names,
        'n_samples': len(X),
    }
    with open(os.path.join(models_dir, 'peru_v1.pkl'), 'wb') as f:
        pickle.dump(state, f)

    print(f'  Peru model saved')
    return models


def predict_peru(match, context):
    """Predict Over/Under and BTTS for a Peru match."""
    import pickle

    model_path = os.path.join(SCRIPT_DIR, 'models', 'peru_v1.pkl')
    if not os.path.exists(model_path):
        return None

    with open(model_path, 'rb') as f:
        state = pickle.load(f)

    features = build_peru_features(match, context)
    feature_names = state['feature_names']
    vec = np.array([[features.get(f, 0.0) for f in feature_names]], dtype=np.float32)

    results = {}
    for mkt in ['over25', 'btts']:
        if mkt not in state['models']:
            continue
        rf = state['models'][mkt]['rf']
        gbc = state['models'][mkt]['gbc']

        prob_rf = rf.predict_proba(vec)[0][1]
        prob_gbc = gbc.predict_proba(vec)[0][1]
        prob = 0.45 * prob_rf + 0.55 * prob_gbc

        results[mkt] = {
            'prob_yes': round(prob, 4),
            'prob_no': round(1 - prob, 4),
            'predicted': 'Over 2.5' if (mkt == 'over25' and prob > 0.5) else
                        'Under 2.5' if (mkt == 'over25') else
                        'BTTS Yes' if prob > 0.5 else 'BTTS No',
            'confidence': round(max(prob, 1 - prob), 4),
        }

    return results


def backtest_peru():
    """Backtest Peru model with walk-forward validation."""
    matches = load_peru_matches()
    if len(matches) < 100:
        print('Not enough data')
        return

    matches.sort(key=lambda m: m.get('utc_date', ''))
    split = int(len(matches) * 0.7)
    train = matches[:split]
    test = matches[split:]

    print(f'Peru Backtest: train={len(train)}, test={len(test)}')

    # Train
    train_peru_model(train)

    # Test
    correct_ou = total_ou = 0
    correct_btts = total_btts = 0

    for i, m in enumerate(test):
        context = (train + test[:i])[-100:]
        preds = predict_peru(m, context)
        if not preds:
            continue

        hs = m.get('home_score', 0) or 0
        as_ = m.get('away_score', 0) or 0
        actual_ou = 1 if hs + as_ > 2.5 else 0
        actual_btts = 1 if hs > 0 and as_ > 0 else 0

        if 'over25' in preds:
            total_ou += 1
            pred_ou = 1 if preds['over25']['prob_yes'] > 0.5 else 0
            if pred_ou == actual_ou:
                correct_ou += 1

        if 'btts' in preds:
            total_btts += 1
            pred_btts = 1 if preds['btts']['prob_yes'] > 0.5 else 0
            if pred_btts == actual_btts:
                correct_btts += 1

    ou_acc = correct_ou / max(total_ou, 1) * 100
    btts_acc = correct_btts / max(total_btts, 1) * 100
    ou_base = max(sum(1 for m in test if (m.get('home_score',0) or 0) + (m.get('away_score',0) or 0) > 2.5) / len(test) * 100,
                  100 - sum(1 for m in test if (m.get('home_score',0) or 0) + (m.get('away_score',0) or 0) > 2.5) / len(test) * 100)

    print(f'\nResults:')
    print(f'  Over 2.5:  {ou_acc:.1f}% (base: {ou_base:.1f}%, edge: {ou_acc - ou_base:+.1f}%)')
    print(f'  BTTS:      {btts_acc:.1f}%')

    # Altitude breakdown
    high_alt = [m for m in test if get_altitude(m.get('venue_city', '')) > 2500]
    low_alt = [m for m in test if get_altitude(m.get('venue_city', '')) <= 2500]
    print(f'\n  High altitude matches: {len(high_alt)}')
    print(f'  Low altitude matches: {len(low_alt)}')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print('='*60)
    print('  ORACULO PERU - Liga 1 con Altitud')
    print('='*60)
    backtest_peru()
