#!/usr/bin/env python3
"""
oraculo_tennis.py - Tennis prediction model (ATP/WTA).

Uses surface-specific Elo + form + H2H + fatigue to predict match winners.
Data source: Jeff Sackmann GitHub CSVs (free, 50+ years of ATP data).

Features:
1. Surface Elo (separate ratings for clay/hard/grass)
2. Overall Elo
3. H2H record
4. Recent form (last 10 matches)
5. Surface form (last 10 on this surface)
6. Fatigue (matches in last 7/14 days)
7. Ranking difference
8. Set win rates
9. Tournament round (R128 vs Final)
"""

import os
import csv
import json
import math
import logging
import pickle
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
from io import StringIO

log = logging.getLogger('oraculo.tennis')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis')
MODELS_DIR = os.path.join(SCRIPT_DIR, 'models')

# Jeff Sackmann ATP data URLs
SACKMANN_URLS = {
    'atp_2024': 'https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2024.csv',
    'atp_2023': 'https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2023.csv',
    'atp_2022': 'https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2022.csv',
    'atp_2025': 'https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2025.csv',
}

# Cloudbet tennis competition keys
CB_TENNIS = {
    'atp': [
        'tennis-atp-australian-open',
        'tennis-atp-roland-garros',
        'tennis-atp-wimbledon',
        'tennis-atp-us-open',
        'tennis-atp-miami-open',
        'tennis-atp-indian-wells',
        'tennis-atp-madrid-open',
        'tennis-atp-rome-masters',
        'tennis-atp-montreal',
        'tennis-atp-cincinnati',
        'tennis-atp-shanghai',
    ],
}

SURFACES = {'Hard': 'hard', 'Clay': 'clay', 'Grass': 'grass', 'Carpet': 'hard'}
ROUND_ORDER = {'F': 7, 'SF': 6, 'QF': 5, 'R16': 4, 'R32': 3, 'R64': 2, 'R128': 1, 'RR': 3}


# =========================================================================
# DATA LOADING
# =========================================================================

def download_atp_data(years=None):
    """Download ATP match data from Jeff Sackmann's GitHub."""
    if years is None:
        years = [2022, 2023, 2024, 2025]

    os.makedirs(CACHE_DIR, exist_ok=True)
    all_matches = []

    for year in years:
        cache_file = os.path.join(CACHE_DIR, f'atp_{year}.json')

        # Check cache (7 day TTL)
        import time
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 7 * 86400:
                try:
                    with open(cache_file, 'r') as f:
                        matches = json.load(f)
                    all_matches.extend(matches)
                    log.info('ATP %d cache: %d matches', year, len(matches))
                    continue
                except Exception:
                    pass

        url = SACKMANN_URLS.get(f'atp_{year}')
        if not url:
            continue

        log.info('Downloading ATP %d...', year)
        try:
            from urllib.request import Request, urlopen
            req = Request(url)
            req.add_header('User-Agent', 'Oraculo/1.0')
            resp = urlopen(req, timeout=30)
            raw = resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            log.error('Failed to download ATP %d: %s', year, e)
            continue

        matches = _parse_atp_csv(raw)
        log.info('ATP %d: %d matches', year, len(matches))

        # Cache
        with open(cache_file, 'w') as f:
            json.dump(matches, f)

        all_matches.extend(matches)

    all_matches.sort(key=lambda m: m.get('date', ''))
    return all_matches


def _parse_atp_csv(raw_text):
    """Parse Sackmann CSV into match dicts."""
    matches = []
    reader = csv.DictReader(StringIO(raw_text))

    for row in reader:
        try:
            winner = row.get('winner_name', '').strip()
            loser = row.get('loser_name', '').strip()
            if not winner or not loser:
                continue

            # Parse date
            date_str = row.get('tourney_date', '')
            if len(date_str) == 8:
                date = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'
            else:
                date = date_str

            surface = row.get('surface', 'Hard')
            score = row.get('score', '')

            # Count sets
            sets_w = sets_l = 0
            if score:
                for s in score.split():
                    parts = s.replace('(', '').replace(')', '').split('-')
                    if len(parts) == 2:
                        try:
                            if int(parts[0]) > int(parts[1]):
                                sets_w += 1
                            else:
                                sets_l += 1
                        except ValueError:
                            pass

            matches.append({
                'winner': winner,
                'loser': loser,
                'date': date,
                'surface': SURFACES.get(surface, 'hard'),
                'tourney': row.get('tourney_name', ''),
                'round': row.get('round', ''),
                'winner_rank': _safe_int(row.get('winner_rank')),
                'loser_rank': _safe_int(row.get('loser_rank')),
                'winner_age': _safe_float(row.get('winner_age')),
                'loser_age': _safe_float(row.get('loser_age')),
                'sets_won': sets_w,
                'sets_lost': sets_l,
                'score': score,
                'w_ace': _safe_int(row.get('w_ace')),
                'l_ace': _safe_int(row.get('l_ace')),
                'w_df': _safe_int(row.get('w_df')),
                'l_df': _safe_int(row.get('l_df')),
                'w_svpt': _safe_int(row.get('w_svpt')),
                'w_1stIn': _safe_int(row.get('w_1stIn')),
                'w_1stWon': _safe_int(row.get('w_1stWon')),
                'w_bpSaved': _safe_int(row.get('w_bpSaved')),
                'w_bpFaced': _safe_int(row.get('w_bpFaced')),
            })
        except Exception:
            continue

    return matches


def _safe_int(val):
    try:
        return int(float(val)) if val else None
    except (ValueError, TypeError):
        return None


def _safe_float(val):
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


# =========================================================================
# ELO RATING (Surface-specific)
# =========================================================================

class TennisElo:
    """Surface-specific Elo with form and H2H tracking."""

    def __init__(self, k_factor=32, initial=1500):
        self.overall = defaultdict(lambda: initial)
        self.by_surface = {
            'hard': defaultdict(lambda: initial),
            'clay': defaultdict(lambda: initial),
            'grass': defaultdict(lambda: initial),
        }
        self.k = k_factor
        self.initial = initial
        self._match_count = defaultdict(int)
        self._form = defaultdict(list)      # player -> list of recent results (1=win, 0=loss)
        self._h2h = defaultdict(lambda: [0, 0])  # (p1,p2) sorted -> [p1_wins, p2_wins]

    def process_match(self, winner, loser, surface='hard'):
        """Update Elo, form, and H2H after a match."""
        # Overall Elo
        exp_w = self._expected(self.overall[winner], self.overall[loser])
        self.overall[winner] += self.k * (1 - exp_w)
        self.overall[loser] += self.k * (0 - (1 - exp_w))

        # Surface-specific Elo
        if surface in self.by_surface:
            surf = self.by_surface[surface]
            exp_ws = self._expected(surf[winner], surf[loser])
            self.overall[winner] += self.k * 0.5 * (1 - exp_ws)
            surf[winner] += self.k * (1 - exp_ws)
            surf[loser] += self.k * (0 - (1 - exp_ws))

        self._match_count[winner] += 1
        self._match_count[loser] += 1

        # Form: track last 15 results
        self._form[winner].append(1)
        self._form[loser].append(0)
        if len(self._form[winner]) > 15:
            self._form[winner] = self._form[winner][-15:]
        if len(self._form[loser]) > 15:
            self._form[loser] = self._form[loser][-15:]

        # H2H: use sorted tuple as key
        key = tuple(sorted([winner, loser]))
        if winner == key[0]:
            self._h2h[key][0] += 1
        else:
            self._h2h[key][1] += 1

    def process_matches(self, matches):
        """Process all matches chronologically."""
        for m in sorted(matches, key=lambda x: x.get('date', '')):
            self.process_match(m['winner'], m['loser'], m.get('surface', 'hard'))

    def get_form(self, player, n=10):
        """Get recent form score (0.0 to 1.0). None if insufficient data."""
        results = self._form.get(player, [])
        if len(results) < 3:
            return None
        recent = results[-n:]
        return sum(recent) / len(recent)

    def get_h2h(self, player_a, player_b):
        """Get H2H record: (player_a_wins, player_b_wins)."""
        key = tuple(sorted([player_a, player_b]))
        record = self._h2h.get(key, [0, 0])
        if player_a == key[0]:
            return record[0], record[1]
        return record[1], record[0]

    def predict(self, player_a, player_b, surface='hard'):
        """Predict win probability for player_a with Elo + form + H2H."""
        elo_a = self.overall[player_a]
        elo_b = self.overall[player_b]

        # Blend overall + surface-specific (60/40)
        if surface in self.by_surface:
            surf_a = self.by_surface[surface][player_a]
            surf_b = self.by_surface[surface][player_b]
            elo_a = 0.6 * elo_a + 0.4 * surf_a
            elo_b = 0.6 * elo_b + 0.4 * surf_b

        base_prob = self._expected(elo_a, elo_b)

        # Form adjustment (+/- 5% max)
        form_adj = 0.0
        form_a = self.get_form(player_a)
        form_b = self.get_form(player_b)
        if form_a is not None and form_b is not None:
            if form_a > 0.7 and form_b < 0.4:
                form_adj = 0.05  # Player A in great form, B struggling
            elif form_b > 0.7 and form_a < 0.4:
                form_adj = -0.05  # Opposite

        # H2H adjustment (+/- 5% max)
        h2h_adj = 0.0
        wins_a, wins_b = self.get_h2h(player_a, player_b)
        total_h2h = wins_a + wins_b
        if total_h2h >= 3:
            h2h_ratio = wins_a / total_h2h
            if h2h_ratio > 0.70:
                h2h_adj = 0.05  # Player A dominates H2H
            elif h2h_ratio < 0.30:
                h2h_adj = -0.05  # Player B dominates

        # Combine and clip
        final = base_prob + form_adj + h2h_adj
        return max(0.05, min(0.95, final))

    def _expected(self, rating_a, rating_b):
        return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))

    def get_top(self, n=20, surface=None):
        """Get top N players by Elo."""
        if surface and surface in self.by_surface:
            ratings = self.by_surface[surface]
        else:
            ratings = self.overall
        return sorted(ratings.items(), key=lambda x: x[1], reverse=True)[:n]

    def save_state(self, path):
        """Persist Elo state to pickle file."""
        import pickle
        state = {
            'overall': dict(self.overall),
            'by_surface': {k: dict(v) for k, v in self.by_surface.items()},
            'match_count': dict(self._match_count),
            'form': dict(self._form),
            'h2h': dict(self._h2h),
            'k': self.k,
            'initial': self.initial,
        }
        with open(path, 'wb') as f:
            pickle.dump(state, f)

    def load_state(self, path):
        """Load Elo state from pickle file. Returns True if loaded."""
        import pickle, os
        if not os.path.exists(path):
            return False
        try:
            with open(path, 'rb') as f:
                state = pickle.load(f)
            for k, v in state.get('overall', {}).items():
                self.overall[k] = v
            for surf, ratings in state.get('by_surface', {}).items():
                if surf in self.by_surface:
                    for k, v in ratings.items():
                        self.by_surface[surf][k] = v
            for k, v in state.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in state.get('form', {}).items():
                self._form[k] = v
            for k, v in state.get('h2h', {}).items():
                self._h2h[k] = v
            return True
        except Exception:
            return False

    @property
    def ratings(self):
        """Alias for overall ratings dict (backwards compat)."""
        return self.overall


# =========================================================================
# FEATURE ENGINEERING
# =========================================================================

def build_tennis_features(player_a, player_b, surface, matches, elo):
    """Build feature vector for a tennis match."""
    features = {}

    # 1. Elo features
    features['elo_a'] = elo.overall[player_a]
    features['elo_b'] = elo.overall[player_b]
    features['elo_diff'] = features['elo_a'] - features['elo_b']
    features['elo_expected_a'] = elo.predict(player_a, player_b, surface)

    # Surface Elo
    if surface in elo.by_surface:
        features['surf_elo_a'] = elo.by_surface[surface][player_a]
        features['surf_elo_b'] = elo.by_surface[surface][player_b]
        features['surf_elo_diff'] = features['surf_elo_a'] - features['surf_elo_b']
    else:
        features['surf_elo_a'] = features['elo_a']
        features['surf_elo_b'] = features['elo_b']
        features['surf_elo_diff'] = features['elo_diff']

    # 2. Ranking
    a_rank = _get_latest_rank(matches, player_a)
    b_rank = _get_latest_rank(matches, player_b)
    features['rank_a'] = a_rank or 200
    features['rank_b'] = b_rank or 200
    features['rank_diff'] = features['rank_b'] - features['rank_a']  # Positive = A is better ranked

    # 3. Form (last 10 matches)
    a_form = _compute_form(matches, player_a, n=10)
    b_form = _compute_form(matches, player_b, n=10)
    features['form_a'] = a_form['win_rate']
    features['form_b'] = b_form['win_rate']
    features['form_diff'] = a_form['win_rate'] - b_form['win_rate']

    # 4. Surface form
    a_surf = _compute_form(matches, player_a, n=10, surface=surface)
    b_surf = _compute_form(matches, player_b, n=10, surface=surface)
    features['surf_form_a'] = a_surf['win_rate']
    features['surf_form_b'] = b_surf['win_rate']

    # 5. H2H
    h2h = _compute_h2h(matches, player_a, player_b)
    features['h2h_a_wins'] = h2h['a_wins']
    features['h2h_b_wins'] = h2h['b_wins']
    features['h2h_a_rate'] = h2h['a_rate']

    # 6. Fatigue
    a_fatigue = _compute_fatigue(matches, player_a)
    b_fatigue = _compute_fatigue(matches, player_b)
    features['fatigue_a_7d'] = a_fatigue['matches_7d']
    features['fatigue_b_7d'] = b_fatigue['matches_7d']
    features['fatigue_diff'] = a_fatigue['matches_7d'] - b_fatigue['matches_7d']

    # 7. Serve stats
    a_serve = _compute_serve_stats(matches, player_a)
    b_serve = _compute_serve_stats(matches, player_b)
    features['ace_rate_a'] = a_serve['ace_rate']
    features['ace_rate_b'] = b_serve['ace_rate']
    features['bp_saved_a'] = a_serve['bp_saved_rate']
    features['bp_saved_b'] = b_serve['bp_saved_rate']

    # 8. Surface encoding
    features['is_clay'] = 1.0 if surface == 'clay' else 0.0
    features['is_grass'] = 1.0 if surface == 'grass' else 0.0
    features['is_hard'] = 1.0 if surface == 'hard' else 0.0

    return features


def get_tennis_feature_names():
    """Return ordered feature names."""
    return [
        'elo_a', 'elo_b', 'elo_diff', 'elo_expected_a',
        'surf_elo_a', 'surf_elo_b', 'surf_elo_diff',
        'rank_a', 'rank_b', 'rank_diff',
        'form_a', 'form_b', 'form_diff',
        'surf_form_a', 'surf_form_b',
        'h2h_a_wins', 'h2h_b_wins', 'h2h_a_rate',
        'fatigue_a_7d', 'fatigue_b_7d', 'fatigue_diff',
        'ace_rate_a', 'ace_rate_b', 'bp_saved_a', 'bp_saved_b',
        'is_clay', 'is_grass', 'is_hard',
    ]


def _get_latest_rank(matches, player):
    for m in reversed(matches):
        if m['winner'] == player and m.get('winner_rank'):
            return m['winner_rank']
        if m['loser'] == player and m.get('loser_rank'):
            return m['loser_rank']
    return None


def _compute_form(matches, player, n=10, surface=None):
    recent = []
    for m in reversed(matches):
        if surface and m.get('surface') != surface:
            continue
        if m['winner'] == player or m['loser'] == player:
            recent.append(m)
            if len(recent) >= n:
                break
    if not recent:
        return {'win_rate': 0.5, 'set_rate': 0.5}
    wins = sum(1 for m in recent if m['winner'] == player)
    return {'win_rate': wins / len(recent), 'set_rate': 0.5}


def _compute_h2h(matches, player_a, player_b):
    a_wins = b_wins = 0
    for m in matches:
        if m['winner'] == player_a and m['loser'] == player_b:
            a_wins += 1
        elif m['winner'] == player_b and m['loser'] == player_a:
            b_wins += 1
    total = a_wins + b_wins
    return {
        'a_wins': a_wins, 'b_wins': b_wins,
        'a_rate': a_wins / max(total, 1),
    }


def _compute_fatigue(matches, player):
    if not matches:
        return {'matches_7d': 0, 'matches_14d': 0}
    last_date = matches[-1].get('date', '')
    try:
        ref = datetime.strptime(last_date[:10], '%Y-%m-%d')
    except Exception:
        return {'matches_7d': 0, 'matches_14d': 0}

    m7 = m14 = 0
    for m in reversed(matches):
        if m['winner'] != player and m['loser'] != player:
            continue
        try:
            md = datetime.strptime(m['date'][:10], '%Y-%m-%d')
            days = (ref - md).days
            if days <= 7:
                m7 += 1
            if days <= 14:
                m14 += 1
            if days > 14:
                break
        except Exception:
            continue
    return {'matches_7d': m7, 'matches_14d': m14}


def _compute_serve_stats(matches, player, n=20):
    aces = svpts = bp_saved = bp_faced = 0
    count = 0
    for m in reversed(matches):
        if count >= n:
            break
        if m['winner'] == player:
            if m.get('w_ace') is not None:
                aces += m['w_ace']
                svpts += m.get('w_svpt', 0) or 0
                bp_saved += m.get('w_bpSaved', 0) or 0
                bp_faced += m.get('w_bpFaced', 0) or 0
                count += 1
        elif m['loser'] == player:
            if m.get('l_ace') is not None:
                aces += m['l_ace']
                count += 1

    return {
        'ace_rate': aces / max(svpts, 1),
        'bp_saved_rate': bp_saved / max(bp_faced, 1),
    }


# =========================================================================
# TRAIN + PREDICT + BACKTEST
# =========================================================================

def train_tennis_model(matches=None):
    """Train tennis prediction model."""
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier

    if matches is None:
        matches = download_atp_data()

    if len(matches) < 100:
        print('Not enough tennis data')
        return None

    # Fit Elo
    elo = TennisElo(k_factor=32)
    split = int(len(matches) * 0.7)
    train = matches[:split]
    elo.process_matches(train)

    # Build features
    feature_names = get_tennis_feature_names()
    X = []
    y = []

    for i, m in enumerate(train):
        if i < 50:
            continue
        context = train[:i]
        feats = build_tennis_features(m['winner'], m['loser'],
                                      m.get('surface', 'hard'), context, elo)
        vec = [feats.get(f, 0.0) for f in feature_names]
        X.append(vec)
        y.append(1)  # Winner is always player_a in training

        # Also add reversed (loser as player_a) for balance
        feats_rev = build_tennis_features(m['loser'], m['winner'],
                                          m.get('surface', 'hard'), context, elo)
        vec_rev = [feats_rev.get(f, 0.0) for f in feature_names]
        X.append(vec_rev)
        y.append(0)  # Loser = player_a loses

        # Update Elo
        elo.process_match(m['winner'], m['loser'], m.get('surface', 'hard'))

    X = np.array(X, dtype=np.float32)
    y = np.array(y)
    print(f'Tennis training: {len(X)} samples (balanced)')

    # Train
    rf = RandomForestClassifier(n_estimators=200, max_depth=10, random_state=42, n_jobs=-1)
    rf.fit(X, y)
    gbc = GradientBoostingClassifier(n_estimators=150, max_depth=5, learning_rate=0.05, random_state=42)
    gbc.fit(X, y)

    # Feature importance
    print('\nTop features:')
    imp = sorted(zip(feature_names, rf.feature_importances_),
                 key=lambda x: x[1], reverse=True)
    for name, val in imp[:10]:
        print(f'  {name:20s} {val:.4f}')

    # Backtest on remaining data
    test = matches[split:]
    correct = total = 0

    for m in test:
        feats = build_tennis_features(m['winner'], m['loser'],
                                      m.get('surface', 'hard'), train + test[:total], elo)
        vec = np.array([[feats.get(f, 0.0) for f in feature_names]], dtype=np.float32)

        prob_rf = rf.predict_proba(vec)[0][1]
        prob_gbc = gbc.predict_proba(vec)[0][1]
        prob = 0.45 * prob_rf + 0.55 * prob_gbc

        total += 1
        if prob > 0.5:  # Predict winner (player_a = winner in this case)
            correct += 1

        elo.process_match(m['winner'], m['loser'], m.get('surface', 'hard'))

    acc = correct / max(total, 1) * 100
    print(f'\nBacktest: {acc:.1f}% ({correct}/{total})')
    print(f'Edge vs 50%: {acc - 50:+.1f}%')

    # Save
    os.makedirs(MODELS_DIR, exist_ok=True)
    state = {
        'rf': rf, 'gbc': gbc,
        'feature_names': feature_names,
        'elo': {'overall': dict(elo.overall),
                'hard': dict(elo.by_surface['hard']),
                'clay': dict(elo.by_surface['clay']),
                'grass': dict(elo.by_surface['grass'])},
    }
    with open(os.path.join(MODELS_DIR, 'tennis_v1.pkl'), 'wb') as f:
        pickle.dump(state, f)
    print('Tennis model saved')

    # Top players
    print('\nTop 10 ATP Elo:')
    for name, rating in elo.get_top(10):
        print(f'  {name:25s} {rating:.0f}')

    return {'accuracy': acc, 'total': total}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    print('='*60)
    print('  ORACULO TENNIS - ATP Model')
    print('='*60)
    train_tennis_model()
