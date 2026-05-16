#!/usr/bin/env python3
"""
oraculo_market_predictor.py - Multi-Market Betting Predictor

Predicts Over/Under 2.5, BTTS, Handicap lines, and builds Parlays.
Uses binary ensemble (RF + GBC/XGB + ExtraTrees/LGB) per market.
"""

import os
import json
import pickle
import logging
import warnings
from datetime import datetime

import numpy as np

log = logging.getLogger('oraculo.markets')
warnings.filterwarnings('ignore', category=UserWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, 'models')

# Handicap lines to model
HANDICAP_LINES = [-1, -1.5, -2, -2.5]


class MarketPredictor:
    """Multi-market binary prediction engine."""

    MARKETS = {
        'over25': 'Over 2.5 Goals',
        'btts': 'Both Teams To Score',
        'corners_o95': 'Corners Over 9.5',
        'corners_o105': 'Corners Over 10.5',
        'cards_o35': 'Cards Over 3.5',
        'cards_o45': 'Cards Over 4.5',
        'shots_target_o85': 'Shots on Target Over 8.5',
    }
    # Handicap markets added dynamically per line

    def __init__(self, model_name='markets_v1'):
        self.model_name = model_name
        self._predictors = {}  # market_key -> {'models': {...}, 'trained': bool}
        self._feature_names = []
        self._train_meta = {}

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train_all(self, X, y_dict, feature_names=None):
        """
        Train all markets at once.

        Args:
            X: numpy array (n_samples, n_features)
            y_dict: dict of market_key -> labels array
                    e.g. {'over25': [1,0,1,...], 'btts': [0,1,0,...],
                          'handicap_m1': [1,0,...], ...}
            feature_names: list of feature names
        """
        X = np.array(X, dtype=np.float32)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        if feature_names:
            self._feature_names = list(feature_names)

        self._train_meta = {
            'n_samples': len(X),
            'n_features': X.shape[1] if X.ndim == 2 else 0,
            'trained_at': datetime.now().isoformat(),
            'markets': [],
        }

        for market_key, y in y_dict.items():
            y = np.array(y, dtype=np.int32)
            if len(y) != len(X):
                log.warning('Skipping %s: label count mismatch', market_key)
                continue
            # Need at least both classes
            if len(np.unique(y)) < 2:
                log.warning('Skipping %s: only one class present', market_key)
                continue
            self._train_market(market_key, X, y)
            self._train_meta['markets'].append(market_key)

        log.info('Trained %d markets: %s', len(self._train_meta['markets']),
                 self._train_meta['markets'])

    def _train_market(self, market_key, X, y):
        """Train binary ensemble for one market."""
        from sklearn.ensemble import (RandomForestClassifier,
                                      GradientBoostingClassifier,
                                      ExtraTreesClassifier)
        import numpy as _np

        models = {}
        # Class balance for sample weighting
        pos_count = _np.sum(y)
        neg_count = len(y) - pos_count
        scale_pos = neg_count / max(pos_count, 1)
        # Balanced class weight for sklearn models (value >2 or <0.5 means imbalanced)
        use_balanced = scale_pos > 2.0 or scale_pos < 0.5

        # RF — balanced class weight when imbalanced
        models['rf'] = RandomForestClassifier(
            n_estimators=200, max_depth=10, min_samples_split=10,
            min_samples_leaf=5, random_state=42, n_jobs=-1,
            class_weight='balanced' if use_balanced else None
        )
        models['rf'].fit(X, y)

        # XGBoost with scale_pos_weight for class balance
        try:
            from xgboost import XGBClassifier
            models['xgb'] = XGBClassifier(
                n_estimators=200, max_depth=6, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
                objective='binary:logistic', eval_metric='logloss',
                scale_pos_weight=float(scale_pos),
                use_label_encoder=False, random_state=42, n_jobs=-1, verbosity=0
            )
            models['xgb'].fit(X, y)
        except ImportError:
            models['gbc'] = GradientBoostingClassifier(
                n_estimators=150, max_depth=5, learning_rate=0.05,
                subsample=0.8, random_state=42
            )
            models['gbc'].fit(X, y)

        # LGB with class balance
        try:
            from lightgbm import LGBMClassifier
            models['lgb'] = LGBMClassifier(
                n_estimators=200, max_depth=6, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, min_child_samples=10,
                objective='binary', metric='binary_logloss',
                is_unbalance=bool(use_balanced),
                random_state=42, n_jobs=-1, verbose=-1
            )
            models['lgb'].fit(X, y)
        except ImportError:
            models['et'] = ExtraTreesClassifier(
                n_estimators=200, max_depth=10, min_samples_split=10,
                random_state=43, n_jobs=-1
            )
            models['et'].fit(X, y)

        # CatBoost — thread_count=1 prevents thread pool crash on repeated fit() calls
        try:
            from catboost import CatBoostClassifier
            models['cat'] = CatBoostClassifier(
                iterations=150, depth=5, learning_rate=0.05,
                loss_function='Logloss', random_seed=42, verbose=0,
                thread_count=1, allow_writing_files=False
            )
            models['cat'].fit(X, y)
        except Exception:
            pass

        # AdaBoost
        try:
            from sklearn.ensemble import AdaBoostClassifier
            from sklearn.tree import DecisionTreeClassifier
            models['ada'] = AdaBoostClassifier(
                estimator=DecisionTreeClassifier(max_depth=3),
                n_estimators=100, learning_rate=0.1, random_state=44, algorithm='SAMME'
            )
            models['ada'].fit(X, y)
        except Exception:
            pass

        pos_rate = np.mean(y)
        log.info('Market %s trained: %d samples, %.1f%% positive',
                 market_key, len(y), pos_rate * 100)

        self._predictors[market_key] = {
            'models': models,
            'weights': {k: 1.0 / len(models) for k in models},
            'trained': True,
            'pos_rate': float(pos_rate),
        }

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_market(self, market_key, features_vector):
        """
        Predict a single market.

        Returns:
            dict: {prob_yes, prob_no, predicted, confidence, market}
        """
        pred_info = self._predictors.get(market_key)
        if not pred_info or not pred_info.get('trained'):
            return None

        X = np.array([features_vector], dtype=np.float32)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        probs = []
        weights = pred_info['weights']
        for name, model in pred_info['models'].items():
            try:
                p = model.predict_proba(X)[0]
                if len(p) == 2:
                    probs.append(weights[name] * p[1])  # prob of positive class
            except Exception as e:
                log.debug('Predict failed %s/%s: %s', market_key, name, e)

        if not probs:
            return None

        prob_yes = sum(probs) / sum(weights.values())
        prob_no = 1.0 - prob_yes

        # Market-specific labels
        labels = _market_labels(market_key)

        predicted = labels[0] if prob_yes >= 0.5 else labels[1]
        confidence = max(prob_yes, prob_no)

        return {
            'market': market_key,
            'market_label': self.MARKETS.get(market_key, market_key),
            'prob_yes': round(float(prob_yes), 4),
            'prob_no': round(float(prob_no), 4),
            'predicted': predicted,
            'confidence': round(float(confidence), 4),
        }

    def predict_all(self, features_vector):
        """Predict all trained markets. Returns dict of market_key -> prediction."""
        results = {}
        for market_key in self._predictors:
            pred = self.predict_market(market_key, features_vector)
            if pred:
                results[market_key] = pred
        return results

    # ------------------------------------------------------------------
    # Save / Load
    # ------------------------------------------------------------------

    def save(self, path=None):
        """Save all market models."""
        if path is None:
            if not os.path.exists(MODELS_DIR):
                os.makedirs(MODELS_DIR)
            path = os.path.join(MODELS_DIR, '%s.pkl' % self.model_name)

        state = {
            'model_name': self.model_name,
            'predictors': self._predictors,
            'feature_names': self._feature_names,
            'train_meta': self._train_meta,
        }
        with open(path, 'wb') as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        log.info('MarketPredictor saved to %s (%.1f KB)',
                 path, os.path.getsize(path) / 1024.0)

    def load(self, path=None):
        """Load market models."""
        if path is None:
            path = os.path.join(MODELS_DIR, '%s.pkl' % self.model_name)
        if not os.path.exists(path):
            log.warning('Market model not found: %s', path)
            return False
        try:
            with open(path, 'rb') as f:
                state = pickle.load(f)
            self.model_name = state.get('model_name', self.model_name)
            self._predictors = state.get('predictors', {})
            self._feature_names = state.get('feature_names', [])
            self._train_meta = state.get('train_meta', {})
            log.info('MarketPredictor loaded: %d markets', len(self._predictors))
            return True
        except Exception as e:
            log.error('Failed to load market models: %s', e)
            return False

    @property
    def trained_markets(self):
        return [k for k, v in self._predictors.items() if v.get('trained')]


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------

def build_market_labels(match):
    """
    Generate labels for all markets from a finished match.

    Args:
        match: dict with home_score, away_score, and optionally
               home_corners, away_corners, home_yellow, away_yellow,
               home_shots_target, away_shots_target (from CSV data)

    Returns:
        dict of market_key -> label (0 or 1)
    """
    hs = match.get('home_score')
    as_ = match.get('away_score')
    if hs is None or as_ is None:
        return {}

    labels = {}
    total = hs + as_

    # Over/Under 2.5
    labels['over25'] = 1 if total > 2.5 else 0

    # BTTS
    labels['btts'] = 1 if (hs > 0 and as_ > 0) else 0

    # Handicap lines (home team perspective)
    for line in HANDICAP_LINES:
        key = 'handicap_h%s' % str(line).replace('-', 'm').replace('.', '_')
        labels[key] = 1 if (hs + line) > as_ else 0

    # Corners (from CSV stats)
    hc = match.get('home_corners')
    ac = match.get('away_corners')
    if hc is not None and ac is not None:
        total_corners = hc + ac
        labels['corners_o95'] = 1 if total_corners > 9.5 else 0
        labels['corners_o105'] = 1 if total_corners > 10.5 else 0

    # Cards (from CSV stats)
    hy = match.get('home_yellow', 0) or 0
    ay = match.get('away_yellow', 0) or 0
    hr = match.get('home_red', 0) or 0
    ar = match.get('away_red', 0) or 0
    if match.get('home_yellow') is not None:
        total_cards = hy + ay + hr + ar
        labels['cards_o35'] = 1 if total_cards > 3.5 else 0
        labels['cards_o45'] = 1 if total_cards > 4.5 else 0

    # Shots on target (from CSV stats)
    hst = match.get('home_shots_target')
    ast_ = match.get('away_shots_target')
    if hst is not None and ast_ is not None:
        total_sot = hst + ast_
        labels['shots_target_o85'] = 1 if total_sot > 8.5 else 0

    return labels


def _market_labels(market_key):
    """Return (positive_label, negative_label) for display."""
    if market_key == 'over25':
        return ('Over 2.5', 'Under 2.5')
    elif market_key == 'btts':
        return ('BTTS Yes', 'BTTS No')
    elif market_key.startswith('handicap'):
        return ('Covers', 'No Cover')
    elif market_key.startswith('corners'):
        line = market_key.replace('corners_o', '')
        return (f'Over {line[:-1]}.{line[-1]}', f'Under {line[:-1]}.{line[-1]}')
    elif market_key.startswith('cards'):
        line = market_key.replace('cards_o', '')
        return (f'Over {line[:-1]}.{line[-1]}', f'Under {line[:-1]}.{line[-1]}')
    elif market_key.startswith('shots'):
        return ('Over', 'Under')
    return ('Yes', 'No')


# ---------------------------------------------------------------------------
# Parlays / Combinadas
# ---------------------------------------------------------------------------

def build_parlay(selections, correlation_penalty=0.95):
    """
    Combine independent predictions into a parlay.

    Args:
        selections: list of dicts:
            [{'market': 'over25', 'prob': 0.72, 'odds': 1.85},
             {'market': '1x2', 'prob': 0.65, 'odds': 2.10}, ...]
        correlation_penalty: multiplier per additional leg (default 0.95)
            accounts for inter-market correlation

    Returns:
        dict with combined_prob, combined_odds, edge, kelly, n_legs
    """
    if not selections or len(selections) < 2:
        return None

    combined_prob = 1.0
    combined_odds = 1.0

    for s in selections:
        combined_prob *= s['prob']
        combined_odds *= s['odds']

    # Apply correlation penalty for legs > 1
    n_extra = len(selections) - 1
    combined_prob *= correlation_penalty ** n_extra

    implied = 1.0 / combined_odds if combined_odds > 0 else 1.0
    edge = combined_prob - implied

    # Kelly for parlay
    b = combined_odds - 1
    kelly = (b * combined_prob - (1 - combined_prob)) / b if b > 0 else 0
    kelly = max(0, kelly)

    return {
        'n_legs': len(selections),
        'selections': [{'market': s['market'], 'prob': s['prob'],
                        'odds': s['odds']} for s in selections],
        'combined_prob': round(combined_prob, 4),
        'combined_odds': round(combined_odds, 2),
        'implied_prob': round(implied, 4),
        'edge': round(edge, 4),
        'kelly_full': round(kelly, 4),
        'kelly_quarter': round(kelly * 0.25, 4),
        'is_value': edge > 0.03,
    }
