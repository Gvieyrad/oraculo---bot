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
        # Dynamic K-factor: higher for new players (uncertain), lower for established (stable)
        # 250/(5+n) gives ~42 at career start, decays to ~16 at 200+ matches (FiveThirtyEight method)
        k_winner = max(10, min(40, 250.0 / (5 + self._match_count[winner])))
        k_loser  = max(10, min(40, 250.0 / (5 + self._match_count[loser])))
        k = (k_winner + k_loser) / 2.0  # symmetric update

        # Overall Elo
        exp_w = self._expected(self.overall[winner], self.overall[loser])
        self.overall[winner] += k * (1 - exp_w)
        self.overall[loser] += k * (0 - (1 - exp_w))

        # Surface-specific Elo
        if surface in self.by_surface:
            surf = self.by_surface[surface]
            exp_ws = self._expected(surf[winner], surf[loser])
            self.overall[winner] += k * 0.5 * (1 - exp_ws)
            surf[winner] += k * (1 - exp_ws)
            surf[loser] += k * (0 - (1 - exp_ws))

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

    def _norm_player(self, name):
        """Try hyphen->space normalization if name not in overall."""
        if name in self.overall:
            return name
        alt = name.replace('-', ' ')
        if alt in self.overall:
            return alt
        return name  # fallback: use as-is (will get initial Elo)

    def predict(self, player_a, player_b, surface='hard', include_h2h=True):
        """Predict win probability for player_a with Elo + form + H2H."""
        player_a = self._norm_player(player_a)
        player_b = self._norm_player(player_b)
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

        # H2H adjustment (+/- 5% max) — skip when predict_enhanced handles it
        h2h_adj = 0.0
        if include_h2h:
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
    features['spw_a'] = a_serve['spw']
    features['spw_b'] = b_serve['spw']
    features['rpw_a'] = a_serve['rpw']
    features['rpw_b'] = b_serve['rpw']
    features['dominance_a'] = a_serve['dominance_ratio']
    features['dominance_b'] = b_serve['dominance_ratio']
    features['dominance_diff'] = a_serve['dominance_ratio'] - b_serve['dominance_ratio']

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
        'spw_a', 'spw_b', 'rpw_a', 'rpw_b',
        'dominance_a', 'dominance_b', 'dominance_diff',
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
    """Compute serve/return dominance stats from ATP match data.
    SPW = Service Points Won % (how well you hold serve)
    RPW = Return Points Won % (how well you break opponent's serve)
    dominance_ratio: the strongest single tennis predictor per Tennis Abstract research
    """
    aces = svpts = first_won = second_won = 0
    bp_saved = bp_faced = 0
    opp_svpts = opp_first_won = opp_second_won = 0
    count = 0
    for m in reversed(matches):
        if count >= n:
            break
        if m['winner'] == player:
            if m.get('w_svpt') is not None:
                aces       += m.get('w_ace', 0) or 0
                svpts      += m.get('w_svpt', 0) or 0
                first_won  += m.get('w_1stWon', 0) or 0
                second_won += m.get('w_2ndWon', 0) or 0
                bp_saved   += m.get('w_bpSaved', 0) or 0
                bp_faced   += m.get('w_bpFaced', 0) or 0
                opp_svpts      += m.get('l_svpt', 0) or 0
                opp_first_won  += m.get('l_1stWon', 0) or 0
                opp_second_won += m.get('l_2ndWon', 0) or 0
                count += 1
        elif m['loser'] == player:
            if m.get('l_svpt') is not None:
                aces       += m.get('l_ace', 0) or 0
                svpts      += m.get('l_svpt', 0) or 0
                first_won  += m.get('l_1stWon', 0) or 0
                second_won += m.get('l_2ndWon', 0) or 0
                bp_saved   += m.get('l_bpSaved', 0) or 0
                bp_faced   += m.get('l_bpFaced', 0) or 0
                opp_svpts      += m.get('w_svpt', 0) or 0
                opp_first_won  += m.get('w_1stWon', 0) or 0
                opp_second_won += m.get('w_2ndWon', 0) or 0
                count += 1

    spw = (first_won + second_won) / max(svpts, 1)
    opp_spw = (opp_first_won + opp_second_won) / max(opp_svpts, 1)
    rpw = 1.0 - opp_spw
    # dominance_ratio > 1.0 = player dominates; < 1.0 = struggles
    dominance_ratio = spw / max(1.0 - rpw, 0.01)

    return {
        'ace_rate':        aces / max(svpts, 1),
        'bp_saved_rate':   bp_saved / max(bp_faced, 1),
        'spw':             round(spw, 4),
        'rpw':             round(rpw, 4),
        'dominance_ratio': round(dominance_ratio, 4),
    }




# =========================================================================
# SERVE / RETURN ELO  (Newton-Keller model)
# Source: Jeff Sackmann / Tennis Abstract serve model
# SPW = Service Points Won %, RPW = Return Points Won %
# Match win probability computed via Markov chain over games/sets
# =========================================================================

def _p_win_game(p: float) -> float:
    """P(hold serve game) when winning each service point with prob p.
    Uses exact formula with deuce handling.
    """
    if p <= 0: return 0.0
    if p >= 1: return 1.0
    q = 1.0 - p
    # Pre-deuce paths: 4-0, 4-1, 4-2
    pre_deuce = p**4 + 4*(p**4)*q + 10*(p**4)*(q**2)
    # Deuce: P(reach deuce) = 20 * p^3 * q^3
    # P(win from deuce) = p^2 / (p^2 + q^2)
    p_deuce_reached = 20 * (p**3) * (q**3)
    p_win_from_deuce = (p**2) / (p**2 + q**2)
    return pre_deuce + p_deuce_reached * p_win_from_deuce


def _p_win_set(p_hold_server: float, p_hold_returner: float) -> float:
    """P(A wins a set) when A serves first.
    p_hold_server   = P(A wins a game when A is serving)
    p_hold_returner = P(B wins a game when B is serving)
    Uses iterative DP — no recursion, no lru_cache issues.
    Handles: 6-0..6-4, 7-5, 7-6(tiebreak)
    """
    p_a_hold  = p_hold_server
    p_a_break = 1.0 - p_hold_returner  # P(A wins game when B serves)
    # Tiebreak win prob: slight advantage for better hold/break player
    p_a_tb    = max(0.05, min(0.95, 0.5 + (p_a_hold - p_hold_returner) * 0.5))

    # DP table: dp[ga][gb][serves] = P(A wins set from this state)
    # serves: 0=A serves, 1=B serves
    # Scores go from 0-0 up to 7-6
    MAX = 8
    dp = [[[0.0, 0.0] for _ in range(MAX)] for _ in range(MAX)]

    # Fill terminal states first (backward from max score)
    for ga in range(MAX):
        for gb in range(MAX):
            for s in range(2):
                # A wins set
                if (ga == 6 and gb <= 4) or (ga == 7 and gb == 5) or (ga == 7 and gb == 6):
                    dp[ga][gb][s] = 1.0
                # B wins set
                elif (gb == 6 and ga <= 4) or (gb == 7 and ga == 5) or (gb == 7 and ga == 6):
                    dp[ga][gb][s] = 0.0
                # Tiebreak
                elif ga == 6 and gb == 6:
                    dp[ga][gb][s] = p_a_tb

    # Fill non-terminal states iteratively (all reachable scores up to 7-5/6-6)
    # Process in reverse order to ensure dependencies are filled
    all_states = []
    for ga in range(8):
        for gb in range(8):
            is_terminal = ((ga >= 6 and abs(ga - gb) >= 2 and ga <= 7)
                           or (gb >= 6 and abs(gb - ga) >= 2 and gb <= 7)
                           or (ga == 6 and gb == 6))
            if not is_terminal and ga + gb <= 13:
                all_states.append((ga + gb, ga, gb))

    # Sort by total games descending (fill high scores first, then (0,0) last)
    for _, ga, gb in sorted(all_states, reverse=True):
        for s in range(2):
            if s == 0:  # A serves
                p_win_game = p_a_hold
            else:       # B serves
                p_win_game = p_a_break
            next_s = 1 - s
            # Clip transitions to valid range
            next_ga = min(ga + 1, 7)
            next_gb = min(gb + 1, 7)
            p_a = dp[next_ga][gb][next_s]
            p_b = dp[ga][next_gb][next_s]
            dp[ga][gb][s] = p_win_game * p_a + (1.0 - p_win_game) * p_b

    return dp[0][0][0]  # A serves first


def _p_win_match_bo3(p_win_set_serving: float, p_win_set_returning: float) -> float:
    """P(A wins best-of-3 match).
    p_win_set_serving   = P(A wins set when A serves first)
    p_win_set_returning = P(A wins set when B serves first)
    Alternating serve between sets.
    """
    ps = p_win_set_serving
    pr = p_win_set_returning
    # Set 1: A serves first → P(A wins) = ps
    # Set 2: B serves first → P(A wins) = pr
    # Set 3: A serves first (if reached) → P(A wins) = ps
    p_20 = ps * pr                           # A wins sets 1 and 2
    p_21 = ps * (1-pr) * ps                  # A wins 1, loses 2, wins 3
    p_12 = (1-ps) * pr * ps                  # A loses 1, wins 2 and 3 (set3: A serves first)
    return p_20 + p_21 + p_12


class ServeReturnElo:
    """Track rolling SPW (Service Points Won) and RPW (Return Points Won)
    per player per surface. Use Newton-Keller equations to convert
    serve/return stats into match win probability.

    Blended with TennisElo for final prediction:
      final = 0.5 * elo_prob + 0.5 * serve_return_prob

    Falls back gracefully when serve data is sparse.

    Source: Tennis Abstract serve model by Jeff Sackmann
    """

    # ATP tour averages (fallback when player has no data)
    _ATP_AVG_SPW = {'hard': 0.638, 'clay': 0.598, 'grass': 0.660, 'carpet': 0.640}
    # Min matches with serve data before we trust the estimate
    MIN_MATCHES = 10

    def __init__(self, alpha: float = 0.15):
        """alpha: EMA smoothing factor (0.15 = ~12 match memory)"""
        self.alpha = alpha
        # spw[surface][player] = exponential moving avg of service points won
        self.spw = {s: {} for s in ('hard', 'clay', 'grass', 'carpet')}
        self.rpw = {s: {} for s in ('hard', 'clay', 'grass', 'carpet')}
        # Count of matches with serve data per player
        self.serve_count = {}   # player -> int

    def _surface(self, surface: str) -> str:
        return surface if surface in self.spw else 'hard'

    # ATP average 2nd serve win rates by surface (used when w_2ndWon is missing)
    _ATP_2ND_WIN = {'hard': 0.55, 'clay': 0.52, 'grass': 0.57, 'carpet': 0.54}

    def _calc_spw(self, svpt, first_won, second_won, first_in, surface):
        """Calculate SPW handling missing w_2ndWon (common in 2023+ Sackmann data)."""
        if not svpt or svpt < 10:
            return None
        if second_won is not None:
            return (first_won + second_won) / svpt
        # Estimate 2nd serve points won from w_1stIn
        if first_in is not None and first_in > 0:
            second_serves = svpt - first_in
            avg_2nd = self._ATP_2ND_WIN.get(self._surface(surface), 0.54)
            est_second_won = second_serves * avg_2nd
            return (first_won + est_second_won) / svpt
        # Fallback: only 1st serve data
        return None

    def update(self, winner: str, loser: str, surface: str,
               w_svpt: int, w_1stWon: int, w_2ndWon,
               l_svpt: int, l_1stWon: int, l_2ndWon,
               w_1stIn: int = None, l_1stIn: int = None):
        """Update SPW/RPW for both players from a single match."""
        s = self._surface(surface)
        avg = self._ATP_AVG_SPW[s]

        w_spw = self._calc_spw(w_svpt, w_1stWon or 0, w_2ndWon, w_1stIn, surface)
        l_spw = self._calc_spw(l_svpt, l_1stWon or 0, l_2ndWon, l_1stIn, surface)

        if w_spw is not None:
            w_rpw = 1.0 - (l_spw if l_spw is not None else avg)
            self.spw[s][winner] = (self.spw[s].get(winner, avg) * (1 - self.alpha)
                                   + w_spw * self.alpha)
            self.rpw[s][winner] = (self.rpw[s].get(winner, 1 - avg) * (1 - self.alpha)
                                   + w_rpw * self.alpha)
            self.serve_count[winner] = self.serve_count.get(winner, 0) + 1

        if l_spw is not None:
            l_rpw = 1.0 - (w_spw if w_spw is not None else avg)
            self.spw[s][loser] = (self.spw[s].get(loser, avg) * (1 - self.alpha)
                                  + l_spw * self.alpha)
            self.rpw[s][loser] = (self.rpw[s].get(loser, 1 - avg) * (1 - self.alpha)
                                  + l_rpw * self.alpha)
            self.serve_count[loser] = self.serve_count.get(loser, 0) + 1

    def process_matches(self, matches: list):
        """Bulk process match list (from Sackmann CSV data)."""
        for m in sorted(matches, key=lambda x: x.get('date', '')):
            w_svpt   = m.get('w_svpt') or 0
            w_1stWon = m.get('w_1stWon') or 0
            w_2ndWon = m.get('w_2ndWon')   # keep None — _calc_spw handles estimation
            l_svpt   = m.get('l_svpt') or 0
            l_1stWon = m.get('l_1stWon') or 0
            l_2ndWon = m.get('l_2ndWon')   # keep None — _calc_spw handles estimation
            if w_svpt < 10 and l_svpt < 10:
                continue  # No serve data for this match
            self.update(
                m['winner'], m['loser'], m.get('surface', 'hard'),
                w_svpt, w_1stWon, w_2ndWon,
                l_svpt, l_1stWon, l_2ndWon,
                w_1stIn=m.get('w_1stIn'), l_1stIn=m.get('l_1stIn'),
            )

    def get_spw(self, player: str, surface: str) -> float:
        """Get SPW for player on surface. Returns ATP average if unknown."""
        s = self._surface(surface)
        return self.spw[s].get(player, self._ATP_AVG_SPW[s])

    def get_rpw(self, player: str, surface: str) -> float:
        """Get RPW for player on surface. Returns ATP average if unknown."""
        s = self._surface(surface)
        return self.rpw[s].get(player, 1.0 - self._ATP_AVG_SPW[s])

    def has_data(self, player: str) -> bool:
        return self.serve_count.get(player, 0) >= self.MIN_MATCHES

    def predict(self, player_a: str, player_b: str, surface: str = 'hard'):
        """Predict P(A wins match) using serve/return stats.
        Returns float or None if insufficient data for both players.
        """
        has_a = self.has_data(player_a)
        has_b = self.has_data(player_b)
        if not has_a and not has_b:
            return None   # No serve data, defer to Elo

        spw_a = self.get_spw(player_a, surface)
        rpw_a = self.get_rpw(player_a, surface)
        spw_b = self.get_spw(player_b, surface)
        rpw_b = self.get_rpw(player_b, surface)

        # Convert to game-win probabilities (A serving / B serving)
        p_hold_a = _p_win_game(spw_a)                # P(A holds serve)
        p_hold_b = _p_win_game(spw_b)                # P(B holds serve)
        # Note: rpw_a = 1 - spw_b (return points won = opponent fails to win service pts)
        # We blend actual tracked RPW with implied (1-opponent_SPW) for stability
        rpw_a_combined = 0.7 * rpw_a + 0.3 * (1.0 - spw_b)
        rpw_b_combined = 0.7 * rpw_b + 0.3 * (1.0 - spw_a)

        # Set win probabilities
        # When A serves first: _p_win_set(P(A holds), P(B holds))
        # When B serves first: 1 - _p_win_set(P(B holds), P(A holds))
        p_win_set_serving   = _p_win_set(p_hold_a, p_hold_b)
        p_win_set_returning = 1.0 - _p_win_set(p_hold_b, p_hold_a)

        # Match win probability (best of 3)
        prob = _p_win_match_bo3(p_win_set_serving, p_win_set_returning)
        return max(0.05, min(0.95, prob))

    def save_state(self, path: str):
        import pickle
        state = {'spw': {s: dict(v) for s, v in self.spw.items()},
                 'rpw': {s: dict(v) for s, v in self.rpw.items()},
                 'serve_count': dict(self.serve_count),
                 'alpha': self.alpha}
        with open(path, 'wb') as f:
            pickle.dump(state, f)

    def load_state(self, path: str) -> bool:
        import pickle, os
        if not os.path.exists(path):
            return False
        try:
            with open(path, 'rb') as f:
                state = pickle.load(f)
            for s, vals in state.get('spw', {}).items():
                if s in self.spw:
                    self.spw[s].update(vals)
            for s, vals in state.get('rpw', {}).items():
                if s in self.rpw:
                    self.rpw[s].update(vals)
            self.serve_count.update(state.get('serve_count', {}))
            return True
        except Exception:
            return False

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
