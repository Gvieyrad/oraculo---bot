#!/usr/bin/env python3
"""
oraculo_ml_predictor.py - Football Match Prediction ML Pipeline

Ensemble model combining RandomForest, XGBoost, and LightGBM.
Trains on historical match data, predicts home/draw/away probabilities,
tracks feature importance, and supports incremental retraining.
"""

import os
import sys
import json
import time
import pickle
import logging
import warnings
from datetime import datetime

import numpy as np

log = logging.getLogger('oraculo.ml')
warnings.filterwarnings('ignore', category=UserWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, 'models')

# ---------------------------------------------------------------------------
# Model wrapper
# ---------------------------------------------------------------------------

class FootballPredictor:
    """
    Ensemble predictor: RandomForest + XGBoost + LightGBM.
    Outputs calibrated probabilities for home win, draw, away win.
    """

    def __init__(self, model_name='football_v1'):
        self.model_name = model_name
        self.models = {}        # {'rf': model, 'xgb': model, 'lgb': model}
        self.weights = {'rf': 0.30, 'xgb': 0.40, 'lgb': 0.30}
        self.feature_names = []
        self.feature_importance = {}
        self.train_meta = {}    # training metadata
        self._trained = False

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, X, y, feature_names=None):
        """
        Train all ensemble models.

        Args:
            X: numpy array (n_samples, n_features) or list of lists
            y: numpy array of labels (0=home, 1=draw, 2=away)
            feature_names: list of feature name strings
        """
        X = np.array(X, dtype=np.float32)
        y = np.array(y, dtype=np.int32)

        if feature_names:
            self.feature_names = list(feature_names)

        n_samples, n_features = X.shape
        log.info('Training on %d samples, %d features', n_samples, n_features)

        # Validate training data
        warnings_list = self._validate_training_data(X, y)
        for w in warnings_list:
            log.warning('Training data: %s', w)

        # Handle NaN/Inf
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # Store feature stats for drift detection
        self._feature_stats = {
            'mean': X.mean(axis=0).tolist(),
            'std': np.maximum(X.std(axis=0), 1e-8).tolist(),
        }

        # -- RandomForest --
        self.models['rf'] = self._train_rf(X, y)

        # -- XGBoost --
        self.models['xgb'] = self._train_xgb(X, y)

        # -- LightGBM --
        self.models['lgb'] = self._train_lgb(X, y)

        # Compute feature importance (averaged across models)
        self._compute_feature_importance()

        self.train_meta = {
            'n_samples': n_samples,
            'n_features': n_features,
            'trained_at': datetime.now(tz=None).isoformat(),
            'class_distribution': {
                'home': int(np.sum(y == 0)),
                'draw': int(np.sum(y == 1)),
                'away': int(np.sum(y == 2)),
            },
            'validation_warnings': warnings_list,
        }
        self._trained = True
        log.info('Training complete. Classes: %s', self.train_meta['class_distribution'])

    def _train_rf(self, X, y):
        from sklearn.ensemble import RandomForestClassifier
        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=12,
            min_samples_split=10,
            min_samples_leaf=5,
            class_weight='balanced',
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X, y)
        log.info('RF trained. OOB-like train acc: %.3f', model.score(X, y))
        return model

    def _train_xgb(self, X, y):
        try:
            from xgboost import XGBClassifier
            model = XGBClassifier(
                n_estimators=200,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                reg_alpha=0.1,
                reg_lambda=1.0,
                objective='multi:softprob',
                num_class=3,
                eval_metric='mlogloss',
                use_label_encoder=False,
                random_state=42,
                n_jobs=-1,
                verbosity=0,
            )
            model.fit(X, y)
            log.info('XGB trained. Train acc: %.3f', model.score(X, y))
            return model
        except ImportError:
            log.warning('XGBoost not available, using extra RF')
            from sklearn.ensemble import GradientBoostingClassifier
            model = GradientBoostingClassifier(
                n_estimators=150,
                max_depth=6,
                learning_rate=0.05,
                subsample=0.8,
                random_state=42,
            )
            model.fit(X, y)
            return model

    def _train_lgb(self, X, y):
        try:
            from lightgbm import LGBMClassifier
            model = LGBMClassifier(
                n_estimators=200,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_samples=10,
                reg_alpha=0.1,
                reg_lambda=1.0,
                num_class=3,
                objective='multiclass',
                metric='multi_logloss',
                random_state=42,
                n_jobs=-1,
                verbose=-1,
            )
            model.fit(X, y)
            log.info('LGB trained. Train acc: %.3f', model.score(X, y))
            return model
        except ImportError:
            log.warning('LightGBM not available, using extra trees')
            from sklearn.ensemble import ExtraTreesClassifier
            model = ExtraTreesClassifier(
                n_estimators=200,
                max_depth=12,
                min_samples_split=10,
                class_weight='balanced',
                random_state=43,
                n_jobs=-1,
            )
            model.fit(X, y)
            return model

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, features_vector):
        """
        Predict match outcome probabilities.

        Args:
            features_vector: list or 1D array of feature values

        Returns:
            dict: {home_prob, draw_prob, away_prob, predicted, confidence,
                   model_probs: {rf: [...], xgb: [...], lgb: [...]}}
        """
        if not self._trained:
            log.error('Model not trained')
            return {
                'home_prob': 0.33, 'draw_prob': 0.34, 'away_prob': 0.33,
                'predicted': 'draw', 'confidence': 0.0,
                'model_probs': {},
            }

        X = np.array([features_vector], dtype=np.float32)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        model_probs = {}
        ensemble_prob = np.zeros(3)

        for name, model in self.models.items():
            try:
                proba = model.predict_proba(X)[0]
                # Ensure 3 classes
                if len(proba) == 3:
                    model_probs[name] = proba.tolist()
                    ensemble_prob += self.weights.get(name, 0.33) * proba
                else:
                    log.warning('Model %s returned %d classes', name, len(proba))
            except Exception as e:
                log.warning('Prediction failed for %s: %s', name, e)

        # Normalize
        total = ensemble_prob.sum()
        if total > 0:
            ensemble_prob /= total

        labels = ['home', 'draw', 'away']
        predicted_idx = int(np.argmax(ensemble_prob))
        confidence = float(ensemble_prob[predicted_idx])

        return {
            'home_prob': float(ensemble_prob[0]),
            'draw_prob': float(ensemble_prob[1]),
            'away_prob': float(ensemble_prob[2]),
            'predicted': labels[predicted_idx],
            'confidence': confidence,
            'model_probs': model_probs,
        }

    def predict_batch(self, features_list):
        """Predict for multiple matches. Returns list of prediction dicts."""
        return [self.predict(f) for f in features_list]

    # ------------------------------------------------------------------
    # Feature importance
    # ------------------------------------------------------------------

    def _compute_feature_importance(self):
        """Average feature importance across all trained models."""
        if not self.feature_names:
            return

        n_features = len(self.feature_names)
        avg_importance = np.zeros(n_features)
        count = 0

        for name, model in self.models.items():
            imp = None
            if hasattr(model, 'feature_importances_'):
                imp = model.feature_importances_
            if imp is not None and len(imp) == n_features:
                avg_importance += imp
                count += 1

        if count > 0:
            avg_importance /= count

        self.feature_importance = {}
        for i, fname in enumerate(self.feature_names):
            self.feature_importance[fname] = float(avg_importance[i])

    def get_top_features(self, n=15):
        """Return top N most important features."""
        sorted_feats = sorted(self.feature_importance.items(),
                              key=lambda x: x[1], reverse=True)
        return sorted_feats[:n]

    # ------------------------------------------------------------------
    # Training data validation (5.2)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_training_data(X, y):
        """Check training data quality. Returns list of warning strings."""
        warnings_out = []
        n = len(y)
        if n < 30:
            warnings_out.append('Very small dataset: %d samples (recommend 100+)' % n)

        # Class balance
        for cls, label in [(0, 'home'), (1, 'draw'), (2, 'away')]:
            count = int(np.sum(y == cls))
            pct = count / max(n, 1) * 100
            if pct < 10:
                warnings_out.append('Class %s underrepresented: %.1f%% (%d samples)' % (
                    label, pct, count))

        # Feature ranges
        col_std = np.nanstd(X, axis=0)
        zero_var = int(np.sum(col_std < 1e-8))
        if zero_var > 0:
            warnings_out.append('%d features have zero variance' % zero_var)

        nan_count = int(np.sum(np.isnan(X)))
        if nan_count > 0:
            warnings_out.append('%d NaN values in features' % nan_count)

        inf_count = int(np.sum(np.isinf(X)))
        if inf_count > 0:
            warnings_out.append('%d Inf values in features' % inf_count)

        return warnings_out

    # ------------------------------------------------------------------
    # Feature drift detection (5.5)
    # ------------------------------------------------------------------

    def check_feature_drift(self, X_new):
        """
        Compare new features vs training distribution.
        Returns list of drifted feature names (>2 sigma from training mean).
        """
        if not hasattr(self, '_feature_stats') or not self._feature_stats:
            return []

        X_new = np.array(X_new, dtype=np.float32)
        if X_new.ndim == 1:
            X_new = X_new.reshape(1, -1)

        means = np.array(self._feature_stats['mean'])
        stds = np.array(self._feature_stats['std'])

        new_means = X_new.mean(axis=0)
        drift_scores = np.abs(new_means - means) / stds

        drifted = []
        for i, score in enumerate(drift_scores):
            if score > 2.0:
                name = self.feature_names[i] if i < len(self.feature_names) else f'f{i}'
                drifted.append((name, float(score)))

        if drifted:
            log.warning('Feature drift detected in %d features: %s',
                        len(drifted), [d[0] for d in drifted[:5]])
        return drifted

    # ------------------------------------------------------------------
    # Incremental retraining
    # ------------------------------------------------------------------

    def retrain_incremental(self, X_new, y_new, X_old=None, y_old=None,
                            max_old=2000):
        """
        Incremental retraining with new data.

        If old data is provided, combines with new data (keeping most recent).
        Otherwise just trains on new data.

        Args:
            X_new: new feature arrays
            y_new: new labels
            X_old: previous training features (optional)
            y_old: previous training labels (optional)
            max_old: max samples to keep from old data
        """
        X_new = np.array(X_new, dtype=np.float32)
        y_new = np.array(y_new, dtype=np.int32)

        if X_old is not None and y_old is not None:
            X_old = np.array(X_old, dtype=np.float32)
            y_old = np.array(y_old, dtype=np.int32)
            # Keep most recent old samples
            if len(X_old) > max_old:
                X_old = X_old[-max_old:]
                y_old = y_old[-max_old:]
            X = np.vstack([X_old, X_new])
            y = np.concatenate([y_old, y_new])
        else:
            X = X_new
            y = y_new

        log.info('Incremental retrain: %d new + %d old = %d total',
                 len(X_new), len(X) - len(X_new), len(X))

        self.train(X, y, self.feature_names)

    # ------------------------------------------------------------------
    # Save / Load
    # ------------------------------------------------------------------

    def save(self, path=None):
        """Save model to pickle file and update manifest."""
        if path is None:
            if not os.path.exists(MODELS_DIR):
                os.makedirs(MODELS_DIR)
            path = os.path.join(MODELS_DIR, '%s.pkl' % self.model_name)

        saved_at = datetime.now(tz=None).isoformat()

        state = {
            'model_name': self.model_name,
            'models': self.models,
            'weights': self.weights,
            'feature_names': self.feature_names,
            'feature_importance': self.feature_importance,
            'feature_stats': getattr(self, '_feature_stats', {}),
            'train_meta': self.train_meta,
            'trained': self._trained,
            'saved_at': saved_at,
        }

        with open(path, 'wb') as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)

        size_kb = os.path.getsize(path) / 1024.0
        log.info('Model saved to %s (%.1f KB)', path, size_kb)

        # Update manifest
        self._update_manifest(path, saved_at, size_kb)

    def load(self, path=None):
        """Load model from pickle file."""
        if path is None:
            path = os.path.join(MODELS_DIR, '%s.pkl' % self.model_name)

        if not os.path.exists(path):
            log.warning('Model file not found: %s', path)
            return False

        try:
            with open(path, 'rb') as f:
                state = pickle.load(f)
            self.model_name = state.get('model_name', self.model_name)
            self.models = state.get('models', {})
            self.weights = state.get('weights', self.weights)
            self.feature_names = state.get('feature_names', [])
            self.feature_importance = state.get('feature_importance', {})
            self._feature_stats = state.get('feature_stats', {})
            self.train_meta = state.get('train_meta', {})
            self._trained = state.get('trained', False)
            log.info('Model loaded from %s (trained: %s, meta: %s)',
                     path, self._trained, self.train_meta.get('trained_at', '?'))
            return True
        except Exception as e:
            log.error('Failed to load model: %s', e)
            return False

    def load_latest(self):
        """Load the most recently saved model from the manifest."""
        manifest = self._read_manifest()
        if not manifest.get('models'):
            log.warning('No models in manifest')
            return False
        latest = max(manifest['models'], key=lambda m: m.get('saved_at', ''))
        path = latest.get('path', '')
        if os.path.exists(path):
            log.info('Loading latest model: %s', path)
            return self.load(path)
        log.warning('Latest model file missing: %s', path)
        return False

    # ------------------------------------------------------------------
    # Model manifest (5.1)
    # ------------------------------------------------------------------

    def _update_manifest(self, path, saved_at, size_kb):
        """Update model_manifest.json with this model's info."""
        manifest = self._read_manifest()
        entry = {
            'model_name': self.model_name,
            'path': os.path.abspath(path),
            'saved_at': saved_at,
            'size_kb': round(size_kb, 1),
            'n_samples': self.train_meta.get('n_samples', 0),
            'n_features': self.train_meta.get('n_features', 0),
            'class_distribution': self.train_meta.get('class_distribution', {}),
            'warnings': self.train_meta.get('validation_warnings', []),
        }
        # Replace existing entry for same model_name, or append
        models = manifest.get('models', [])
        models = [m for m in models if m.get('model_name') != self.model_name]
        models.append(entry)
        manifest['models'] = models
        manifest['last_updated'] = saved_at

        manifest_path = os.path.join(MODELS_DIR, 'model_manifest.json')
        try:
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)
        except Exception as e:
            log.warning('Failed to update manifest: %s', e)

    @staticmethod
    def _read_manifest():
        """Read model_manifest.json."""
        manifest_path = os.path.join(MODELS_DIR, 'model_manifest.json')
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {'models': []}

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, X, y):
        """
        Evaluate model on test data.

        Returns:
            dict: {accuracy, log_loss, classification_report, confusion_matrix}
        """
        if not self._trained:
            return {'error': 'model not trained'}

        from sklearn.metrics import (accuracy_score, log_loss as sk_log_loss,
                                     classification_report, confusion_matrix)

        X = np.array(X, dtype=np.float32)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        y = np.array(y, dtype=np.int32)

        preds = []
        probas = []
        for row in X:
            result = self.predict(row.tolist())
            label_map = {'home': 0, 'draw': 1, 'away': 2}
            preds.append(label_map[result['predicted']])
            probas.append([result['home_prob'], result['draw_prob'], result['away_prob']])

        preds = np.array(preds)
        probas = np.array(probas)

        acc = accuracy_score(y, preds)
        try:
            ll = sk_log_loss(y, probas, labels=[0, 1, 2])
        except Exception:
            ll = -1.0

        report = classification_report(y, preds,
                                        target_names=['home', 'draw', 'away'],
                                        output_dict=True, zero_division=0)
        cm = confusion_matrix(y, preds, labels=[0, 1, 2]).tolist()

        # Brier score for calibration (5.4)
        brier = -1.0
        try:
            from sklearn.metrics import brier_score_loss
            # One-vs-rest average Brier score
            brier_scores = []
            for cls in range(3):
                y_bin = (y == cls).astype(int)
                brier_scores.append(brier_score_loss(y_bin, probas[:, cls]))
            brier = float(np.mean(brier_scores))
        except Exception:
            pass

        result = {
            'accuracy': float(acc),
            'log_loss': float(ll),
            'brier_score': brier,
            'classification_report': report,
            'confusion_matrix': cm,
            'n_samples': len(y),
        }

        # Log to performance history (5.3)
        self._log_evaluation(result)

        return result

    def _log_evaluation(self, eval_result):
        """Append evaluation to performance log JSON."""
        log_path = os.path.join(MODELS_DIR, 'performance_log.json')
        entry = {
            'model_name': self.model_name,
            'timestamp': datetime.now(tz=None).isoformat(),
            'accuracy': eval_result.get('accuracy'),
            'log_loss': eval_result.get('log_loss'),
            'brier_score': eval_result.get('brier_score'),
            'n_samples': eval_result.get('n_samples'),
        }
        try:
            if not os.path.exists(MODELS_DIR):
                os.makedirs(MODELS_DIR)
            history = []
            if os.path.exists(log_path):
                with open(log_path, 'r') as f:
                    history = json.load(f)
            history.append(entry)
            # Keep last 100 entries
            history = history[-100:]
            with open(log_path, 'w') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log.debug('Failed to log evaluation: %s', e)


# ---------------------------------------------------------------------------
# Training data builder (from finished matches + features)
# ---------------------------------------------------------------------------

def build_training_data(finished_matches, feature_builder_fn):
    """
    Build X, y arrays from finished matches.

    Args:
        finished_matches: list of normalized match dicts (with scores)
        feature_builder_fn: callable(match) -> feature_vector or None

    Returns:
        X (list of lists), y (list of ints), skipped (int)
    """
    X = []
    y = []
    skipped = 0

    for match in finished_matches:
        hs = match.get('home_score')
        as_ = match.get('away_score')
        if hs is None or as_ is None:
            skipped += 1
            continue

        # Label
        if hs > as_:
            label = 0  # home win
        elif hs == as_:
            label = 1  # draw
        else:
            label = 2  # away win

        features = feature_builder_fn(match)
        if features is None:
            skipped += 1
            continue

        X.append(features)
        y.append(label)

    log.info('Training data: %d samples, %d skipped', len(X), skipped)
    return X, y, skipped


# ---------------------------------------------------------------------------
# Value bet detection
# ---------------------------------------------------------------------------

def find_value_bets(prediction, odds, min_edge=0.05):
    """
    Compare model probabilities vs bookmaker odds to find value.

    Args:
        prediction: dict from predict() with home_prob, draw_prob, away_prob
        odds: dict with decimal odds {home: 2.5, draw: 3.2, away: 3.0}
        min_edge: minimum edge to flag as value (default 5%)

    Returns:
        list of value bet dicts: [{outcome, model_prob, implied_prob, edge, odds}]
    """
    if not odds:
        return []

    values = []
    outcomes = [
        ('home', prediction.get('home_prob', 0), odds.get('home', 0)),
        ('draw', prediction.get('draw_prob', 0), odds.get('draw', 0)),
        ('away', prediction.get('away_prob', 0), odds.get('away', 0)),
    ]

    for outcome, model_prob, dec_odds in outcomes:
        if dec_odds <= 1.0:
            continue
        implied_prob = 1.0 / dec_odds
        edge = model_prob - implied_prob
        if edge >= min_edge:
            values.append({
                'outcome': outcome,
                'model_prob': round(model_prob, 4),
                'implied_prob': round(implied_prob, 4),
                'edge': round(edge, 4),
                'odds': dec_odds,
                'kelly': round(edge / (dec_odds - 1), 4) if dec_odds > 1 else 0,
            })

    values.sort(key=lambda x: x['edge'], reverse=True)
    return values


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    # Quick test with synthetic data
    np.random.seed(42)
    n = 300
    n_feat = 48
    X = np.random.randn(n, n_feat).astype(np.float32)
    y = np.random.randint(0, 3, n).astype(np.int32)

    predictor = FootballPredictor('test_model')
    predictor.train(X, y, feature_names=['f%d' % i for i in range(n_feat)])

    # Predict
    sample = X[0].tolist()
    result = predictor.predict(sample)
    print('Prediction: %s (conf: %.2f)' % (result['predicted'], result['confidence']))
    print('Probs: H=%.3f D=%.3f A=%.3f' % (
        result['home_prob'], result['draw_prob'], result['away_prob']))

    # Top features
    print('\nTop 10 features:')
    for fname, imp in predictor.get_top_features(10):
        print('  %-30s %.4f' % (fname, imp))

    # Evaluate
    ev = predictor.evaluate(X[:50], y[:50])
    print('\nEval accuracy: %.3f, log_loss: %.3f' % (ev['accuracy'], ev['log_loss']))

    # Save/load roundtrip
    predictor.save()
    p2 = FootballPredictor('test_model')
    p2.load()
    r2 = p2.predict(sample)
    print('\nReloaded prediction matches: %s' % (
        abs(r2['home_prob'] - result['home_prob']) < 0.001))

    # Value bet test
    odds = {'home': 2.5, 'draw': 3.2, 'away': 3.0}
    vb = find_value_bets(result, odds)
    print('\nValue bets: %s' % vb)
