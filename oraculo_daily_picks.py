#!/usr/bin/env python3
"""
oraculo_daily_picks.py - Daily automated pick generator.

Generates betting picks for today's matches across 8 leagues and 5 markets.
Only outputs picks with edge > min_edge (default 2%).
Uses per-league models trained on 3 seasons of data.

Usage:
    python oraculo_daily_picks.py                  # Today's picks
    python oraculo_daily_picks.py --date 2026-03-22
    python oraculo_daily_picks.py --train           # Retrain models first
    python oraculo_daily_picks.py --bankroll 5000   # Set bankroll for Kelly sizing
    python oraculo_daily_picks.py --json            # JSON output
"""

import os
import sys
import json
import logging
import argparse
import numpy as np
from datetime import datetime, timedelta

log = logging.getLogger('oraculo.picks')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, 'models')
PICKS_DIR = os.path.join(SCRIPT_DIR, 'picks')

# Best league+market combos from backtest (edge > 2%)
PROFITABLE_COMBOS = {
    'PD':  ['over25', 'btts', 'corners_o95', 'cards_o35'],
    'SA':  ['corners_o95', 'btts', 'over25', 'shots_target_o85'],
    'BL1': ['over25'],
    'FL1': ['over25'],
    'ELC': ['btts'],
    'TUR': ['over25', 'btts'],
    'BEL': ['over25', 'btts'],
    'SWE': ['over25', 'btts', 'corners_o95'],
    'NOR': ['over25', 'btts', 'corners_o95'],
    'BL2': ['over25', 'btts'],
    'SB':  ['over25', 'btts', 'corners_o95'],
    'FL2': ['over25', 'btts'],
    'DED': ['shots_target_o85', 'cards_o35'],
    'PPL': ['over25', 'btts', 'shots_target_o85'],
    'PL':  [],  # Most efficient market, no edge
}

ALL_LEAGUES = ['PL', 'PD', 'SA', 'BL1', 'FL1', 'ELC', 'DED', 'PPL',
               'TUR', 'BEL', 'SWE', 'NOR', 'BL2', 'SB', 'FL2']
BEST_MARKETS = ['over25', 'btts', 'cards_o35', 'corners_o95', 'shots_target_o85']

MARKET_LABELS = {
    'over25': 'Over 2.5 Goals',
    'btts': 'BTTS Yes',
    'cards_o35': 'Cards Over 3.5',
    'corners_o95': 'Corners Over 9.5',
    'shots_target_o85': 'Shots on Target Over 8.5',
}


def ensure_dirs():
    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(PICKS_DIR, exist_ok=True)


def load_all_data():
    """Load CSV + xG data for all leagues."""
    from oraculo_football_csv import download_league_csv, LEAGUE_MAP
    from oraculo_xg_weather import load_xg_data

    LEAGUE_MAP['ELC'] = 'E1'
    LEAGUE_MAP['DED'] = 'N1'
    LEAGUE_MAP['PPL'] = 'P1'

    all_matches = []
    for league in ALL_LEAGUES:
        for season in [2023, 2024, 2025]:
            all_matches.extend(download_league_csv(league, season))
    all_matches.sort(key=lambda m: m.get('utc_date', ''))

    xg_data = load_xg_data(
        leagues=['PL', 'PD', 'SA', 'BL1', 'FL1'],
        seasons=['2023', '2024']
    )

    return all_matches, xg_data


def build_feature_vector(match, context, xg_data, elo=None, poisson=None):
    """Build full 105-feature vector for a match."""
    from oraculo_football_features import (build_match_features,
                                           features_to_vector)
    from oraculo_football_csv import compute_team_stats
    from oraculo_xg_weather import compute_team_xg_stats, _ORG_TO_US

    ht = match.get('home_team', '')
    at = match.get('away_team', '')
    home_recent = [m for m in context
                   if m.get('home_team') == ht or m.get('away_team') == ht][:10]
    away_recent = [m for m in context
                   if m.get('home_team') == at or m.get('away_team') == at][:10]
    csv_stats = {
        'home': compute_team_stats(context, ht, n=10),
        'away': compute_team_stats(context, at, n=10),
    }

    try:
        features = build_match_features(
            match, home_recent, away_recent, None, None, None,
            csv_stats=csv_stats
        )

        # Elo features
        if elo:
            features['elo_home'] = elo.ratings[ht]
            features['elo_away'] = elo.ratings[at]
            features['elo_diff'] = elo.ratings[ht] - elo.ratings[at]
            features['elo_expected_home'] = elo.expected_score(ht, at)

        # Poisson features
        if poisson and poisson._fitted:
            lh, la = poisson.predict_lambda(ht, at)
            features['poisson_lambda_home'] = lh
            features['poisson_lambda_away'] = la
            p_mkts = poisson.predict_markets(ht, at)
            features['poisson_over25'] = p_mkts['over25']
            features['poisson_btts'] = p_mkts['btts_yes']

        # xG features
        h_us = _ORG_TO_US.get(ht, ht)
        a_us = _ORG_TO_US.get(at, at)
        for prefix, xg_name in [('home', h_us), ('away', a_us)]:
            xg = compute_team_xg_stats(xg_data, xg_name, n=10)
            features[f'{prefix}_xg_for_avg'] = xg['xg_for_avg']
            features[f'{prefix}_xg_against_avg'] = xg['xg_against_avg']
            features[f'{prefix}_xg_diff'] = xg['xg_diff_avg']
            features[f'{prefix}_xg_over25_rate'] = xg['xg_over25_rate']
            features[f'{prefix}_overperform'] = xg['overperform_avg']
        features['xg_total_predict'] = features['home_xg_for_avg'] + features['away_xg_for_avg']
        features['xg_diff'] = features['home_xg_diff'] - features['away_xg_diff']

        # Weather features — only for recent/upcoming matches (skip historical to avoid 16k HTTP calls)
        try:
            from datetime import datetime, timezone, timedelta
            match_date_str = match.get('utc_date', '')
            if match_date_str:
                match_dt = datetime.fromisoformat(match_date_str[:10])
                if (datetime.now() - match_dt).days <= 3:
                    from oraculo_xg_weather import get_weather_features
                    features.update(get_weather_features(ht, match_date_str))
        except Exception:
            pass
        for wf in ['weather_temp', 'weather_rain', 'weather_wind',
                   'weather_humidity', 'weather_is_rainy', 'weather_is_windy',
                   'weather_is_cold']:
            features.setdefault(wf, 0.0)

        return features_to_vector(features)
    except Exception as e:
        log.debug('Feature build failed: %s', e)
        return None


def train_models(all_matches, xg_data):
    """Train per-league + global models with Elo + Poisson features."""
    from oraculo_football_features import get_feature_names
    from oraculo_market_predictor import MarketPredictor, build_market_labels
    from oraculo_models_advanced import EloRating, PoissonGoalModel

    feature_names = get_feature_names()
    ensure_dirs()

    # Fit math models on all data
    print('  Fitting Elo + Poisson...')
    elo = EloRating(k_factor=32, home_advantage=65)
    elo.process_matches(all_matches)
    poisson = PoissonGoalModel()
    poisson.fit(all_matches)

    # Save math models for prediction time
    import pickle
    with open(os.path.join(MODELS_DIR, 'elo_state.pkl'), 'wb') as f:
        pickle.dump({'ratings': dict(elo.ratings), 'home_adv': elo.home_adv}, f)
    with open(os.path.join(MODELS_DIR, 'poisson_state.pkl'), 'wb') as f:
        pickle.dump({'attack': poisson.attack, 'defense': poisson.defense,
                     'league_avg': poisson.league_avg, 'home_adv': poisson.home_adv}, f)

    # Global model with all data
    train_data_all = []
    for i, m in enumerate(all_matches):
        context = all_matches[max(0, i - 200):i]
        if len(context) < 10:
            continue
        vec = build_feature_vector(m, context, xg_data, elo=elo, poisson=poisson)
        if vec is None:
            continue
        labels = build_market_labels(m)
        if labels:
            train_data_all.append((vec, labels))

    print(f'  Training samples: {len(train_data_all)}')

    mp_global = MarketPredictor('picks_global')
    mp_global._feature_names = feature_names
    for mkt in BEST_MARKETS:
        X_m = [v for v, l in train_data_all if mkt in l]
        y_m = [l[mkt] for _, l in train_data_all if mkt in l]
        y_arr = np.array(y_m)
        if len(X_m) < 50 or len(np.unique(y_arr)) < 2:
            continue
        mp_global._train_market(mkt, np.array(X_m, dtype=np.float32), y_arr)

    mp_global.save()
    print(f'  Trained GLOBAL: {len(train_data_all)} samples, '
          f'{len(mp_global.trained_markets)} markets')

    # Per-league models
    for league in ALL_LEAGUES:
        league_matches = [m for m in all_matches
                         if m.get('competition_code') == league]
        if len(league_matches) < 100:
            continue

        train_data = []
        for i, m in enumerate(league_matches):
            context = league_matches[max(0, i - 100):i]
            if len(context) < 10:
                continue
            vec = build_feature_vector(m, context, xg_data, elo=elo, poisson=poisson)
            if vec is None:
                continue
            labels = build_market_labels(m)
            if labels:
                train_data.append((vec, labels))

        if len(train_data) < 50:
            continue

        mp = MarketPredictor(f'picks_{league}')
        mp._feature_names = feature_names
        markets = PROFITABLE_COMBOS.get(league, BEST_MARKETS)
        if not markets:
            markets = BEST_MARKETS
        for mkt in markets:
            X_m = [v for v, l in train_data if mkt in l]
            y_m = [l[mkt] for _, l in train_data if mkt in l]
            y_arr = np.array(y_m)
            if len(X_m) < 30 or len(np.unique(y_arr)) < 2:
                continue
            mp._train_market(mkt, np.array(X_m, dtype=np.float32), y_arr)

        mp.save()
        print(f'  Trained {league}: {len(train_data)} samples, '
              f'{len(mp.trained_markets)} markets')


def get_upcoming_matches(all_matches, target_date):
    """
    Get matches for target_date.
    Since we use CSV data, we look for matches on that date.
    For real-time, this would call the football-data.org API.
    """
    date_str = target_date.strftime('%Y-%m-%d')
    today_matches = [m for m in all_matches
                    if m.get('utc_date', '').startswith(date_str)]

    if not today_matches:
        # Try tomorrow and next few days
        for delta in range(1, 4):
            next_date = (target_date + timedelta(days=delta)).strftime('%Y-%m-%d')
            today_matches = [m for m in all_matches
                            if m.get('utc_date', '').startswith(next_date)]
            if today_matches:
                print(f'  No matches on {date_str}, using {next_date}')
                break

    return today_matches


def generate_picks(all_matches, xg_data, target_date, bankroll=1000.0,
                   min_edge=0.02, min_confidence=0.55):
    """
    Generate daily picks.

    Returns:
        list of pick dicts
    """
    import pickle
    from oraculo_market_predictor import MarketPredictor
    from oraculo_models_advanced import EloRating, PoissonGoalModel

    upcoming = get_upcoming_matches(all_matches, target_date)
    if not upcoming:
        print('  No matches found')
        return []

    print(f'  {len(upcoming)} matches found')

    # Load ML models
    models = {}
    for league in ALL_LEAGUES:
        mp = MarketPredictor(f'picks_{league}')
        if mp.load():
            models[league] = mp

    mp_global = MarketPredictor('picks_global')
    if mp_global.load():
        models['_global'] = mp_global

    if not models:
        print('  ERROR: No trained models found. Run with --train first.')
        return []

    # Load math models
    elo = EloRating()
    poisson = PoissonGoalModel()
    elo_path = os.path.join(MODELS_DIR, 'elo_state.pkl')
    poisson_path = os.path.join(MODELS_DIR, 'poisson_state.pkl')
    if os.path.exists(elo_path):
        with open(elo_path, 'rb') as f:
            state = pickle.load(f)
        elo.ratings.update(state.get('ratings', {}))
    if os.path.exists(poisson_path):
        with open(poisson_path, 'rb') as f:
            state = pickle.load(f)
        poisson.attack = state.get('attack', {})
        poisson.defense = state.get('defense', {})
        poisson.league_avg = state.get('league_avg', 1.35)
        poisson.home_adv = state.get('home_adv', 1.0)
        poisson._fitted = True

    # Context
    date_str = target_date.strftime('%Y-%m-%d')
    context = [m for m in all_matches if m.get('utc_date', '') < date_str][-200:]

    picks = []

    for match in upcoming:
        league = match.get('competition_code', '')
        ht = match.get('home_team', match.get('home_team_csv', ''))
        at = match.get('away_team', match.get('away_team_csv', ''))

        vec = build_feature_vector(match, context, xg_data, elo=elo, poisson=poisson)
        if vec is None:
            continue

        # Use per-league model, fallback to global
        mp = models.get(league, models.get('_global'))
        if not mp:
            continue

        # Which markets to check for this league
        league_markets = PROFITABLE_COMBOS.get(league, BEST_MARKETS)
        if not league_markets:
            league_markets = BEST_MARKETS

        preds = mp.predict_all(vec)

        for mkt in league_markets:
            pred = preds.get(mkt)
            if not pred:
                continue

            conf = pred['confidence']
            if conf < min_confidence:
                continue

            # Estimate edge (confidence - 50% baseline)
            edge_est = conf - 0.50

            if edge_est < min_edge:
                continue

            # Kelly sizing
            odds_est = 1.0 / max(conf - 0.05, 0.3)
            odds_est = max(1.3, min(odds_est, 3.5))
            b = odds_est - 1
            kelly = max(0, (b * conf - (1 - conf)) / b) * 0.25
            kelly = min(kelly, 0.03)
            stake = round(bankroll * kelly, 2)

            if stake < 1:
                continue

            picks.append({
                'match': f'{ht} vs {at}',
                'home_team': ht,
                'away_team': at,
                'league': league,
                'date': match.get('utc_date', '')[:16],
                'market': mkt,
                'market_label': MARKET_LABELS.get(mkt, mkt),
                'pick': pred['predicted'],
                'confidence': round(conf * 100, 1),
                'edge_est': round(edge_est * 100, 1),
                'odds_est': round(odds_est, 2),
                'stake': stake,
                'stake_pct': round(kelly * 100, 2),
                'kelly_fraction': 0.25,
            })

    # Sort by edge descending
    picks.sort(key=lambda p: p['edge_est'], reverse=True)
    return picks


def print_picks(picks, bankroll=1000.0):
    """Print picks in a readable format."""
    if not picks:
        print('\n  No value picks found today.')
        return

    total_stake = sum(p['stake'] for p in picks)
    total_exposure = total_stake / bankroll * 100

    print(f'\n  PICKS: {len(picks)} value bets found')
    print(f'  Total stake: ${total_stake:.2f} ({total_exposure:.1f}% of bankroll)')
    print()
    print(f'  {"#":>2s} {"Match":<35s} {"League":>5s} {"Market":<22s} '
          f'{"Pick":<12s} {"Conf":>5s} {"Edge":>5s} {"Odds":>5s} {"Stake":>7s}')
    print(f'  {"--":>2s} {"-"*35} {"-----":>5s} {"-"*22} '
          f'{"-"*12} {"-----":>5s} {"-----":>5s} {"-----":>5s} {"-------":>7s}')

    for i, p in enumerate(picks, 1):
        match_short = p['match'][:35]
        print(f'  {i:2d} {match_short:<35s} {p["league"]:>5s} {p["market_label"]:<22s} '
              f'{p["pick"]:<12s} {p["confidence"]:4.0f}% {p["edge_est"]:+4.1f}% '
              f'{p["odds_est"]:5.2f} ${p["stake"]:6.2f}')

    print()
    print(f'  Bankroll: ${bankroll:.2f}')
    print(f'  Max drawdown if all lose: -${total_stake:.2f} ({total_exposure:.1f}%)')
    exp_profit = sum(p['stake'] * (p['odds_est'] - 1) * p['confidence'] / 100
                    - p['stake'] * (1 - p['confidence'] / 100) for p in picks)
    print(f'  Expected profit: ${exp_profit:.2f}')


def save_picks(picks, target_date):
    """Save picks to JSON file."""
    ensure_dirs()
    date_str = target_date.strftime('%Y-%m-%d')
    path = os.path.join(PICKS_DIR, f'picks_{date_str}.json')

    output = {
        'date': date_str,
        'generated': datetime.now().isoformat(),
        'n_picks': len(picks),
        'picks': picks,
    }

    with open(path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'  Saved to {path}')
    return path


def main():
    parser = argparse.ArgumentParser(description='Oraculo Daily Picks')
    parser.add_argument('--date', type=str, default=None,
                       help='Target date YYYY-MM-DD (default: today)')
    parser.add_argument('--train', action='store_true',
                       help='Train/retrain models before generating picks')
    parser.add_argument('--bankroll', type=float, default=1000.0,
                       help='Current bankroll (default: $1000)')
    parser.add_argument('--min-edge', type=float, default=0.02,
                       help='Minimum edge to include (default: 2%%)')
    parser.add_argument('--min-conf', type=float, default=0.55,
                       help='Minimum confidence (default: 55%%)')
    parser.add_argument('--json', action='store_true',
                       help='Output as JSON')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level,
                       format='%(asctime)s %(name)s %(levelname)s %(message)s')

    if args.date:
        target = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        target = datetime.now()

    date_str = target.strftime('%Y-%m-%d')

    print('=' * 80)
    print(f'  ORACULO DAILY PICKS - {date_str}')
    print(f'  Bankroll: ${args.bankroll:.2f} | Min edge: {args.min_edge*100:.0f}% | Min conf: {args.min_conf*100:.0f}%')
    print('=' * 80)

    # Load data
    print('\n  Loading data...')
    all_matches, xg_data = load_all_data()
    print(f'  {len(all_matches)} matches, {len(xg_data)} xG records')

    # Train if requested
    if args.train:
        print('\n  Training models...')
        train_models(all_matches, xg_data)

    # Generate picks
    print(f'\n  Generating picks for {date_str}...')
    picks = generate_picks(all_matches, xg_data, target,
                          bankroll=args.bankroll,
                          min_edge=args.min_edge,
                          min_confidence=args.min_conf)

    if args.json:
        output = {
            'date': date_str,
            'generated': datetime.now().isoformat(),
            'bankroll': args.bankroll,
            'n_picks': len(picks),
            'picks': picks,
        }
        print(json.dumps(output, indent=2))
    else:
        print_picks(picks, args.bankroll)

    # Save
    save_picks(picks, target)

    print('\n' + '=' * 80)


if __name__ == '__main__':
    main()
