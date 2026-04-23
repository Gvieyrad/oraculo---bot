"""GBM ensemble predictor for football matches.
Combines Dixon-Coles, xG, Elo, injuries, and odds movement into a
gradient boosting model for superior predictions.
"""
import os, json, logging, pickle
import numpy as np
from collections import defaultdict

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GBM_MODEL_PATH = os.path.join(SCRIPT_DIR, 'models', 'gbm_football.pkl')
GBM_FEATURES_PATH = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'gbm_features.json')


def build_features(home, away, league, dc_model=None, xg_data=None,
                   injury_data=None, odds_monitor=None, event_id=None):
    """Build feature vector for a match."""
    features = {}

    # 1. Dixon-Coles features
    if dc_model and dc_model._trained:
        pred = dc_model.predict(home, away)
        features['dc_home_win'] = pred['home_win']
        features['dc_draw'] = pred['draw']
        features['dc_away_win'] = pred['away_win']
        features['dc_over25'] = pred['over25']
        features['dc_exp_home'] = pred['exp_home']
        features['dc_exp_away'] = pred['exp_away']
        features['dc_attack_home'] = dc_model.attack[home]
        features['dc_attack_away'] = dc_model.attack[away]
        features['dc_defense_home'] = dc_model.defense[home]
        features['dc_defense_away'] = dc_model.defense[away]
    else:
        for k in ['dc_home_win','dc_draw','dc_away_win','dc_over25',
                   'dc_exp_home','dc_exp_away','dc_attack_home','dc_attack_away',
                   'dc_defense_home','dc_defense_away']:
            features[k] = 0.5 if 'win' in k or 'draw' in k else 1.0

    # 2. xG features
    if xg_data and league in xg_data:
        from oraculo_xg import get_team_xg
        hxg = get_team_xg(home, league, xg_data)
        axg = get_team_xg(away, league, xg_data)
        if hxg:
            features['xg_home'] = hxg['xg']
            features['xga_home'] = hxg['xga']
            features['xg_diff_home'] = hxg['xg'] - hxg['xga']
        else:
            features['xg_home'] = features['xga_home'] = features['xg_diff_home'] = 0
        if axg:
            features['xg_away'] = axg['xg']
            features['xga_away'] = axg['xga']
            features['xg_diff_away'] = axg['xg'] - axg['xga']
        else:
            features['xg_away'] = features['xga_away'] = features['xg_diff_away'] = 0
    else:
        for k in ['xg_home','xga_home','xg_diff_home','xg_away','xga_away','xg_diff_away']:
            features[k] = 0

    # 3. Injury features
    if injury_data:
        from oraculo_injuries import get_team_injuries
        h_count, h_impact = get_team_injuries(home, league, injury_data)
        a_count, a_impact = get_team_injuries(away, league, injury_data)
        features['injuries_home'] = h_count
        features['injury_impact_home'] = h_impact
        features['injuries_away'] = a_count
        features['injury_impact_away'] = a_impact
        features['injury_diff'] = a_count - h_count  # Positive = away more injured
    else:
        for k in ['injuries_home','injury_impact_home','injuries_away',
                   'injury_impact_away','injury_diff']:
            features[k] = 0

    # 4. Form features (from Dixon-Coles match data)
    if dc_model and dc_model._trained:
        # Attack/defense ratio (strength indicator)
        features['attack_ratio'] = dc_model.attack[home] / max(0.1, dc_model.attack[away])
        features['defense_ratio'] = dc_model.defense[away] / max(0.1, dc_model.defense[home])
        # Expected goal difference
        features['exp_goal_diff'] = dc_model._lambda(home, away, True) - dc_model._lambda(away, home, False)
    else:
        features['attack_ratio'] = features['defense_ratio'] = 1.0
        features['exp_goal_diff'] = 0

    # 5. Derived features
    # xG dominance
    if features.get('xg_home', 0) > 0 and features.get('xg_away', 0) > 0:
        features['xg_ratio'] = features['xg_home'] / features['xg_away']
        features['xga_ratio'] = features['xga_away'] / max(0.01, features['xga_home'])
    else:
        features['xg_ratio'] = features['xga_ratio'] = 1.0

    # Combined strength score
    features['home_strength'] = (
        features.get('dc_attack_home', 1) * 0.4 +
        features.get('xg_home', 1.3) * 0.3 +
        (1 - features.get('injury_impact_home', 0)) * 0.3
    )
    features['away_strength'] = (
        features.get('dc_attack_away', 1) * 0.4 +
        features.get('xg_away', 1.3) * 0.3 +
        (1 - features.get('injury_impact_away', 0)) * 0.3
    )
    features['strength_diff'] = features['home_strength'] - features['away_strength']

    # 6. Odds movement features
    if odds_monitor and event_id:
        from oraculo_odds_monitor import detect_steam_moves
        steam = detect_steam_moves(event_id, 'soccer.match_odds/home')
        if steam:
            features['steam_home'] = steam['magnitude'] * (1 if steam['direction'] == 'DROP' else -1)
        else:
            features['steam_home'] = 0
        steam_away = detect_steam_moves(event_id, 'soccer.match_odds/away')
        if steam_away:
            features['steam_away'] = steam_away['magnitude'] * (1 if steam_away['direction'] == 'DROP' else -1)
        else:
            features['steam_away'] = 0
    else:
        features['steam_home'] = features['steam_away'] = 0

    return features


class GBMEnsemble:
    """Gradient Boosting ensemble that blends all model predictions."""

    def __init__(self):
        self.model = None
        self.feature_names = None
        self._trained = False

    def train(self, training_data):
        """Train from historical predictions with outcomes.
        training_data: list of dicts with features + 'outcome' (1=home, 0=draw, -1=away)
        """
        if len(training_data) < 30:
            log.info('GBM: insufficient data (%d < 30), using weighted blend', len(training_data))
            return False

        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError:
            log.warning('GBM: scikit-learn not available, using weighted blend')
            return False

        # Extract features and labels
        feature_names = sorted([k for k in training_data[0].keys() if k != 'outcome'])
        X = np.array([[d.get(f, 0) for f in feature_names] for d in training_data])
        y = np.array([d['outcome'] for d in training_data])

        self.model = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            min_samples_leaf=5, subsample=0.8, random_state=42
        )
        self.model.fit(X, y)
        self.feature_names = feature_names
        self._trained = True

        # Log feature importance
        importances = sorted(zip(feature_names, self.model.feature_importances_),
                           key=lambda x: -x[1])
        log.info('GBM trained on %d samples. Top features:', len(training_data))
        for name, imp in importances[:5]:
            log.info('  %s: %.3f', name, imp)

        # Save model
        os.makedirs(os.path.dirname(GBM_MODEL_PATH), exist_ok=True)
        with open(GBM_MODEL_PATH, 'wb') as f:
            pickle.dump({'model': self.model, 'features': self.feature_names}, f)
        return True

    def load(self):
        if not os.path.exists(GBM_MODEL_PATH):
            return False
        try:
            with open(GBM_MODEL_PATH, 'rb') as f:
                data = pickle.load(f)
            self.model = data['model']
            self.feature_names = data['features']
            self._trained = True
            return True
        except Exception:
            return False

    def predict(self, features):
        """Predict match probabilities. Returns dict with home/draw/away probs.
        If GBM not trained, falls back to weighted blend of available models."""
        if self._trained and self.model is not None:
            X = np.array([[features.get(f, 0) for f in self.feature_names]])
            proba = self.model.predict_proba(X)[0]
            classes = list(self.model.classes_)
            return {
                'home_win': proba[classes.index(1)] if 1 in classes else 0.33,
                'draw': proba[classes.index(0)] if 0 in classes else 0.33,
                'away_win': proba[classes.index(-1)] if -1 in classes else 0.33,
            }

        # Fallback: weighted blend of Dixon-Coles + xG + injury adjustment
        dc_h = features.get('dc_home_win', 0.4)
        dc_d = features.get('dc_draw', 0.25)
        dc_a = features.get('dc_away_win', 0.35)

        # xG adjustment
        xg_diff = features.get('xg_diff_home', 0) - features.get('xg_diff_away', 0)
        xg_adj = xg_diff * 0.05  # 0.05 prob per xG diff unit

        # Injury adjustment
        inj_diff = features.get('injury_diff', 0)
        inj_adj = inj_diff * 0.02  # 0.02 prob per injury difference

        # Steam move adjustment
        steam_h = features.get('steam_home', 0)
        steam_a = features.get('steam_away', 0)
        steam_adj = (steam_h - steam_a) * 0.3  # Steam moves are strong signals

        h = max(0.05, min(0.90, dc_h + xg_adj + inj_adj + steam_adj))
        a = max(0.05, min(0.90, dc_a - xg_adj - inj_adj - steam_adj))
        d = max(0.05, 1 - h - a)

        # Normalize
        total = h + d + a
        return {'home_win': h/total, 'draw': d/total, 'away_win': a/total}
