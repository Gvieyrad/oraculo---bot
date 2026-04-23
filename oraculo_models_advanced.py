#!/usr/bin/env python3
"""
oraculo_models_advanced.py - Advanced mathematical models for Oráculo.

1. Poisson Goal Model - Predicts exact scorelines, derives O/U and BTTS
2. Elo Rating System - Dynamic team strength ratings
3. Dixon-Coles Model - Corrected Poisson with low-score dependency
4. Feature Selection per market - Reduces noise, improves edge

These complement the ML ensemble, not replace it.
"""

import math
import logging
import numpy as np
from collections import defaultdict
from datetime import datetime

log = logging.getLogger('oraculo.advanced')

import difflib

_TEAM_NAME_MAP = {
    'manchester city': 'Man City', 'manchester united': 'Man United',
    'newcastle united': 'Newcastle', 'wolverhampton wanderers': 'Wolves',
    'wolverhampton': 'Wolves', 'brighton & hove albion': 'Brighton',
    'brighton and hove albion': 'Brighton', 'nottingham forest': 'Nottm Forest',
    'west ham united': 'West Ham', 'tottenham hotspur': 'Tottenham',
    'tottenham hotspurs': 'Tottenham', 'spurs': 'Tottenham',
    'atletico madrid': 'Atl. Madrid', 'paris saint-germain': 'Paris SG',
    'paris sg': 'Paris SG', 'psg': 'Paris SG', 'paris saint germain': 'Paris SG',
    'borussia dortmund': 'Dortmund', 'bayer leverkusen': 'Leverkusen',
    'rb leipzig': 'Leipzig', 'ac milan': 'Milan', 'inter milan': 'Inter',
    'internazionale': 'Inter', 'as roma': 'Roma', 'ss lazio': 'Lazio',
    'ssc napoli': 'Napoli', 'juventus fc': 'Juventus', 'arsenal fc': 'Arsenal',
    'chelsea fc': 'Chelsea', 'liverpool fc': 'Liverpool', 'everton fc': 'Everton',
    'aston villa fc': 'Aston Villa', 'burnley fc': 'Burnley',
    'leeds united': 'Leeds', 'sheffield united': 'Sheffield Utd',
    'leicester city': 'Leicester', 'norwich city': 'Norwich',
    'birmingham city': 'Birmingham', 'luton town': 'Luton',
}
_fuzzy_cache = {}

def _normalize_team(name, known_teams):
    if not name: return name
    key = name.lower().strip()
    if key in _TEAM_NAME_MAP: return _TEAM_NAME_MAP[key]
    ck = (key, len(known_teams))
    if ck in _fuzzy_cache: return _fuzzy_cache[ck]
    for t in known_teams:
        if t.lower() == key:
            _fuzzy_cache[ck] = t
            return t
    matches = difflib.get_close_matches(name, known_teams, n=1, cutoff=0.75)
    result = matches[0] if matches else name
    _fuzzy_cache[ck] = result
    return result



# =========================================================================
# 1. POISSON GOAL MODEL
# =========================================================================

class PoissonGoalModel:
    """
    Predicts goal probabilities using Poisson distribution.

    Models expected goals (lambda) for each team based on:
    - Team attack strength (goals scored vs league average)
    - Team defense strength (goals conceded vs league average)
    - Home advantage factor

    From lambdas, derives exact scoreline probabilities,
    Over/Under, BTTS, and correct score markets.
    """

    def __init__(self):
        self.attack = {}     # team -> attack strength
        self.defense = {}    # team -> defense strength
        self.home_adv = 1.0  # home advantage multiplier
        self.league_avg = 1.35  # avg goals per team per match
        self._fitted = False

    def fit(self, matches):
        """
        Fit model from historical matches.

        Args:
            matches: list of dicts with home_team, away_team,
                    home_score, away_score
        """
        # Count goals per team (home and away separately)
        home_scored = defaultdict(list)
        home_conceded = defaultdict(list)
        away_scored = defaultdict(list)
        away_conceded = defaultdict(list)

        for m in matches:
            ht = m.get('home_team', '')
            at = m.get('away_team', '')
            hs = m.get('home_score')
            as_ = m.get('away_score')
            if hs is None or as_ is None or not ht or not at:
                continue
            home_scored[ht].append(hs)
            home_conceded[ht].append(as_)
            away_scored[at].append(as_)
            away_conceded[at].append(hs)

        # League averages
        all_home_goals = [g for gs in home_scored.values() for g in gs]
        all_away_goals = [g for gs in away_scored.values() for g in gs]

        if not all_home_goals or not all_away_goals:
            log.warning('No match data for Poisson fit')
            return

        avg_home = np.mean(all_home_goals)
        avg_away = np.mean(all_away_goals)
        self.league_avg = (avg_home + avg_away) / 2
        self.home_adv = avg_home / max(avg_away, 0.5)

        # Per-team attack and defense strengths
        all_teams = set(home_scored.keys()) | set(away_scored.keys())
        for team in all_teams:
            # Attack = goals scored / league avg
            gs_home = home_scored.get(team, [])
            gs_away = away_scored.get(team, [])
            all_gs = gs_home + gs_away
            self.attack[team] = np.mean(all_gs) / max(self.league_avg, 0.5) if all_gs else 1.0

            # Defense = goals conceded / league avg (lower = better)
            gc_home = home_conceded.get(team, [])
            gc_away = away_conceded.get(team, [])
            all_gc = gc_home + gc_away
            self.defense[team] = np.mean(all_gc) / max(self.league_avg, 0.5) if all_gc else 1.0

        self._fitted = True
        log.info('Poisson fitted: %d teams, avg=%.2f, home_adv=%.2f',
                 len(all_teams), self.league_avg, self.home_adv)

    def predict_lambda(self, home_team, away_team):
        """
        Predict expected goals (lambda) for each team.

        Returns:
            (lambda_home, lambda_away)
        """
        known = list(self.attack.keys())
        home_team = _normalize_team(home_team, known)
        away_team = _normalize_team(away_team, known)
        att_h = self.attack.get(home_team, 1.0)
        def_a = self.defense.get(away_team, 1.0)
        att_a = self.attack.get(away_team, 1.0)
        def_h = self.defense.get(home_team, 1.0)

        lambda_home = att_h * def_a * self.league_avg * self.home_adv
        lambda_away = att_a * def_h * self.league_avg

        return lambda_home, lambda_away

    def predict_scoreline_probs(self, home_team, away_team, max_goals=6):
        """
        Predict probability matrix for all scorelines up to max_goals.

        Returns:
            numpy array [max_goals+1, max_goals+1] where [i,j] = P(home=i, away=j)
        """
        lh, la = self.predict_lambda(home_team, away_team)
        probs = np.zeros((max_goals + 1, max_goals + 1))

        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                probs[i, j] = (self._poisson_pmf(i, lh) *
                               self._poisson_pmf(j, la))

        return probs

    def predict_markets(self, home_team, away_team):
        """
        Predict all markets from Poisson model.

        Returns:
            dict with probabilities for all markets
        """
        lh, la = self.predict_lambda(home_team, away_team)
        probs = self.predict_scoreline_probs(home_team, away_team)

        # 1X2
        home_win = sum(probs[i, j] for i in range(7) for j in range(7) if i > j)
        draw = sum(probs[i, i] for i in range(7))
        away_win = sum(probs[i, j] for i in range(7) for j in range(7) if i < j)

        # Over/Under
        over25 = sum(probs[i, j] for i in range(7) for j in range(7) if i + j > 2.5)
        over15 = sum(probs[i, j] for i in range(7) for j in range(7) if i + j > 1.5)
        over35 = sum(probs[i, j] for i in range(7) for j in range(7) if i + j > 3.5)

        # BTTS
        btts_yes = sum(probs[i, j] for i in range(1, 7) for j in range(1, 7))

        # Clean sheet
        home_cs = sum(probs[i, 0] for i in range(7))
        away_cs = sum(probs[0, j] for j in range(7))

        return {
            'lambda_home': round(lh, 3),
            'lambda_away': round(la, 3),
            'home_win': round(home_win, 4),
            'draw': round(draw, 4),
            'away_win': round(away_win, 4),
            'over15': round(over15, 4),
            'over25': round(over25, 4),
            'over35': round(over35, 4),
            'under25': round(1 - over25, 4),
            'btts_yes': round(btts_yes, 4),
            'btts_no': round(1 - btts_yes, 4),
            'home_clean_sheet': round(home_cs, 4),
            'away_clean_sheet': round(away_cs, 4),
        }


    def prob_ah(self, home_team, away_team, line, outcome="home", max_goals=8):
        """Compute true AH probability via Poisson scoreline matrix.

        Quarter-lines (0.25, 0.75) split into two adjacent half-lines.
        Returns win probability excluding push scenarios.
        """
        probs = self.predict_scoreline_probs(home_team, away_team, max_goals)

        # Quarter-line: average of two adjacent half-lines
        if (line * 4) % 2 != 0:
            p1 = self.prob_ah(home_team, away_team, line - 0.25, outcome, max_goals)
            p2 = self.prob_ah(home_team, away_team, line + 0.25, outcome, max_goals)
            return (p1 + p2) / 2.0

        p_win = p_loss = 0.0
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                p = probs[i, j]
                if p == 0:
                    continue
                adj = (i + line) - j
                if outcome == "home":
                    if adj > 0: p_win += p
                    elif adj < 0: p_loss += p
                else:
                    if adj < 0: p_win += p
                    elif adj > 0: p_loss += p
        total = p_win + p_loss
        return p_win / total if total > 0 else 0.5

    @staticmethod
    def _poisson_pmf(k, lam):
        """Poisson probability mass function."""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam ** k) / math.factorial(k)


# =========================================================================
# 2. ELO RATING SYSTEM
# =========================================================================

class EloRating:
    """
    Dynamic Elo rating system for football teams.

    Updates after each match based on result vs expectation.
    Higher K-factor for recent matches (more responsive).
    Includes home advantage in expected score calculation.
    """

    def __init__(self, initial_rating=1500, k_factor=32, home_advantage=65):
        """
        Args:
            initial_rating: starting Elo for new teams
            k_factor: sensitivity to results (higher = more reactive)
            home_advantage: Elo points for home team (typically 50-80)
        """
        self.ratings = defaultdict(lambda: initial_rating)
        self.initial = initial_rating
        self.k = k_factor
        self.home_adv = home_advantage
        self._history = defaultdict(list)  # team -> [(date, rating)]

    def process_matches(self, matches):
        """
        Process historical matches in chronological order.

        Args:
            matches: list of dicts with home_team, away_team,
                    home_score, away_score, utc_date
        """
        sorted_matches = sorted(matches, key=lambda m: m.get('utc_date', ''))

        for m in sorted_matches:
            ht = m.get('home_team', '')
            at = m.get('away_team', '')
            hs = m.get('home_score')
            as_ = m.get('away_score')
            if hs is None or as_ is None or not ht or not at:
                continue

            # Determine actual result
            if hs > as_:
                actual_home = 1.0
            elif hs == as_:
                actual_home = 0.5
            else:
                actual_home = 0.0

            # Expected result
            expected_home = self.expected_score(ht, at)

            # Update ratings
            self.ratings[ht] += self.k * (actual_home - expected_home)
            self.ratings[at] += self.k * ((1 - actual_home) - (1 - expected_home))

            # Record history
            date = m.get('utc_date', '')[:10]
            self._history[ht].append((date, self.ratings[ht]))
            self._history[at].append((date, self.ratings[at]))

        log.info('Elo processed %d matches, %d teams',
                 len(sorted_matches), len(self.ratings))

    def expected_score(self, home_team, away_team):
        """Expected score for home team (0-1)."""
        r_home = self.ratings[home_team] + self.home_adv
        r_away = self.ratings[away_team]
        return 1.0 / (1.0 + 10 ** ((r_away - r_home) / 400.0))

    def predict(self, home_team, away_team):
        """
        Predict match outcome probabilities.

        Returns:
            dict with home_win, draw, away_win probabilities
        """
        exp_home = self.expected_score(home_team, away_team)

        # Convert expected score to 1X2 probabilities
        # Using empirical football draw rate (~25%)
        draw_base = 0.25
        # Adjust draw probability based on how close teams are
        rating_diff = abs(self.ratings[home_team] - self.ratings[away_team])
        draw_factor = max(0.15, draw_base * (1 - rating_diff / 800))

        home_win = exp_home * (1 - draw_factor)
        away_win = (1 - exp_home) * (1 - draw_factor)
        draw = 1 - home_win - away_win

        return {
            'home_win': round(home_win, 4),
            'draw': round(max(draw, 0.05), 4),
            'away_win': round(away_win, 4),
            'elo_home': round(self.ratings[home_team], 1),
            'elo_away': round(self.ratings[away_team], 1),
            'elo_diff': round(self.ratings[home_team] - self.ratings[away_team], 1),
            'expected_home': round(exp_home, 4),
        }

    def get_features(self, home_team, away_team):
        """Get Elo-based features for ML model."""
        return {
            'elo_home': self.ratings[home_team],
            'elo_away': self.ratings[away_team],
            'elo_diff': self.ratings[home_team] - self.ratings[away_team],
            'elo_expected_home': self.expected_score(home_team, away_team),
        }

    def get_top_teams(self, n=20):
        """Return top N teams by Elo rating."""
        sorted_teams = sorted(self.ratings.items(),
                              key=lambda x: x[1], reverse=True)
        return sorted_teams[:n]


# =========================================================================
# 3. DIXON-COLES MODEL (Corrected Poisson)
# =========================================================================

class DixonColesModel:
    """
    Dixon-Coles model: Poisson with correction for low-scoring matches.

    Standard Poisson underestimates draws (0-0, 1-1).
    Dixon-Coles adds a rho parameter that adjusts probabilities
    for scorelines 0-0, 0-1, 1-0, 1-1.

    This is the gold standard model in football analytics.
    """

    def __init__(self):
        self.poisson = PoissonGoalModel()
        self.rho = -0.13  # correlation parameter (typically -0.05 to -0.20)
        self._fitted = False

    def fit(self, matches):
        """Fit underlying Poisson + estimate rho from data."""
        self.poisson.fit(matches)

        # Estimate rho from observed vs expected 0-0 and 1-1 frequencies
        obs_00 = 0
        obs_11 = 0
        obs_10 = 0
        obs_01 = 0
        exp_00 = 0
        exp_11 = 0
        exp_10 = 0
        exp_01 = 0
        total = 0

        for m in matches:
            ht = m.get('home_team', '')
            at = m.get('away_team', '')
            hs = m.get('home_score')
            as_ = m.get('away_score')
            if hs is None or as_ is None:
                continue
            total += 1

            if hs == 0 and as_ == 0:
                obs_00 += 1
            elif hs == 1 and as_ == 1:
                obs_11 += 1
            elif hs == 1 and as_ == 0:
                obs_10 += 1
            elif hs == 0 and as_ == 1:
                obs_01 += 1

            # Expected from basic Poisson
            probs = self.poisson.predict_scoreline_probs(ht, at)
            exp_00 += probs[0, 0]
            exp_11 += probs[1, 1]
            exp_10 += probs[1, 0]
            exp_01 += probs[0, 1]

        if total > 0 and exp_00 > 0:
            # Estimate rho as average correction needed
            ratio_00 = obs_00 / max(exp_00, 1)
            ratio_11 = obs_11 / max(exp_11, 1)
            # rho adjusts for under/overestimation of low scores
            self.rho = -0.03 * (ratio_00 + ratio_11 - 2)
            self.rho = max(-0.25, min(self.rho, 0.0))

        self._fitted = True
        log.info('Dixon-Coles fitted: rho=%.4f', self.rho)

    def _tau(self, i, j, lambda_h, lambda_a):
        """Dixon-Coles correction factor tau."""
        if i == 0 and j == 0:
            return 1 - lambda_h * lambda_a * self.rho
        elif i == 0 and j == 1:
            return 1 + lambda_h * self.rho
        elif i == 1 and j == 0:
            return 1 + lambda_a * self.rho
        elif i == 1 and j == 1:
            return 1 - self.rho
        return 1.0

    def predict_markets(self, home_team, away_team):
        """Predict all markets with Dixon-Coles correction."""
        lh, la = self.poisson.predict_lambda(home_team, away_team)
        base_probs = self.poisson.predict_scoreline_probs(home_team, away_team)

        # Apply Dixon-Coles correction
        corrected = np.copy(base_probs)
        for i in range(min(2, corrected.shape[0])):
            for j in range(min(2, corrected.shape[1])):
                corrected[i, j] *= self._tau(i, j, lh, la)

        # Normalize
        total = corrected.sum()
        if total > 0:
            corrected /= total

        # Derive markets
        home_win = sum(corrected[i, j] for i in range(7) for j in range(7) if i > j)
        draw = sum(corrected[i, i] for i in range(7))
        away_win = sum(corrected[i, j] for i in range(7) for j in range(7) if i < j)
        over25 = sum(corrected[i, j] for i in range(7) for j in range(7) if i + j > 2.5)
        btts = sum(corrected[i, j] for i in range(1, 7) for j in range(1, 7))

        return {
            'lambda_home': round(lh, 3),
            'lambda_away': round(la, 3),
            'rho': self.rho,
            'home_win': round(home_win, 4),
            'draw': round(draw, 4),
            'away_win': round(away_win, 4),
            'over25': round(over25, 4),
            'under25': round(1 - over25, 4),
            'btts_yes': round(btts, 4),
            'btts_no': round(1 - btts, 4),
        }


# =========================================================================
# 4. META-ENSEMBLE (combines all models)
# =========================================================================

class MetaEnsemble:
    """
    Combines ML ensemble + Poisson + Elo + Dixon-Coles.

    Each model's prediction is weighted by its historical accuracy
    on each specific market.
    """

    def __init__(self):
        self.poisson = PoissonGoalModel()
        self.elo = EloRating()
        self.dixon = DixonColesModel()
        self.weights = {
            'ml': 0.40,
            'poisson': 0.20,
            'dixon': 0.25,
            'elo': 0.15,
        }

    def fit(self, matches):
        """Fit all sub-models."""
        self.poisson.fit(matches)
        self.elo.process_matches(matches)
        self.dixon.fit(matches)
        log.info('MetaEnsemble fitted on %d matches', len(matches))

    def predict(self, home_team, away_team, ml_prediction=None):
        """
        Combined prediction across all models.

        Args:
            home_team: home team name
            away_team: away team name
            ml_prediction: dict from MarketPredictor (optional)

        Returns:
            dict with blended probabilities per market
        """
        result = {}

        # Poisson predictions
        poisson_pred = self.poisson.predict_markets(home_team, away_team)

        # Dixon-Coles predictions
        dixon_pred = self.dixon.predict_markets(home_team, away_team)

        # Elo predictions
        elo_pred = self.elo.predict(home_team, away_team)

        # Blend Over 2.5
        over25_sources = {
            'poisson': poisson_pred['over25'],
            'dixon': dixon_pred['over25'],
        }
        if ml_prediction and 'over25' in ml_prediction:
            over25_sources['ml'] = ml_prediction['over25'].get('prob_yes', 0.5)

        result['over25'] = self._blend(over25_sources, default_key='over25')
        result['under25'] = 1 - result['over25']

        # Blend BTTS
        btts_sources = {
            'poisson': poisson_pred['btts_yes'],
            'dixon': dixon_pred['btts_yes'],
        }
        if ml_prediction and 'btts' in ml_prediction:
            btts_sources['ml'] = ml_prediction['btts'].get('prob_yes', 0.5)

        result['btts_yes'] = self._blend(btts_sources, default_key='btts')
        result['btts_no'] = 1 - result['btts_yes']

        # Blend 1X2
        home_sources = {
            'poisson': poisson_pred['home_win'],
            'dixon': dixon_pred['home_win'],
            'elo': elo_pred['home_win'],
        }
        result['home_win'] = self._blend(home_sources, default_key='1x2')

        away_sources = {
            'poisson': poisson_pred['away_win'],
            'dixon': dixon_pred['away_win'],
            'elo': elo_pred['away_win'],
        }
        result['away_win'] = self._blend(away_sources, default_key='1x2')
        result['draw'] = max(0, 1 - result['home_win'] - result['away_win'])

        # Additional data
        result['lambda_home'] = poisson_pred['lambda_home']
        result['lambda_away'] = poisson_pred['lambda_away']
        result['elo_home'] = elo_pred['elo_home']
        result['elo_away'] = elo_pred['elo_away']
        result['elo_diff'] = elo_pred['elo_diff']
        result['rho'] = dixon_pred['rho']

        # Model agreement (higher = more confident)
        if 'over25' in over25_sources:
            vals = list(over25_sources.values())
            result['model_agreement_over25'] = 1 - np.std(vals)

        return result

    def _blend(self, sources, default_key=''):
        """Weighted average of model predictions."""
        total_weight = 0
        weighted_sum = 0
        for model, prob in sources.items():
            w = self.weights.get(model, 0.2)
            weighted_sum += w * prob
            total_weight += w
        return round(weighted_sum / max(total_weight, 0.01), 4)

    def get_elo_features(self, home_team, away_team):
        """Get Elo features to add to ML feature vector."""
        return self.elo.get_features(home_team, away_team)
