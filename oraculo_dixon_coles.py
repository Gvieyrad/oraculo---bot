"""Dixon-Coles model for football match prediction.
Improves on basic Poisson by correcting low-scoring bias and using
attack/defense strength parameters with time decay.
"""
import os, json, math, logging
from collections import defaultdict
from math import exp, factorial

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DC_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'dixon_coles.json')


def _tau(x, y, lam, mu, rho):
    """Dixon-Coles correction factor for low scores."""
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    elif x == 0 and y == 1:
        return 1 + lam * rho
    elif x == 1 and y == 0:
        return 1 + mu * rho
    elif x == 1 and y == 1:
        return 1 - rho
    return 1.0


def _poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return exp(-lam) * (lam ** k) / factorial(k)


class DixonColesModel:
    def __init__(self):
        self.attack = defaultdict(lambda: 1.0)
        self.defense = defaultdict(lambda: 1.0)
        self.home_adv = 1.20
        self.rho = -0.13
        self.league_avg_goals = 2.65
        self._trained = False

    def train(self, matches, decay=0.005):
        if not matches:
            return
        matches = sorted(matches, key=lambda m: m.get('date', ''))
        total_goals = sum(m.get('home_goals', 0) + m.get('away_goals', 0) for m in matches)
        if len(matches) > 0:
            self.league_avg_goals = total_goals / len(matches)

        team_scored = defaultdict(list)
        team_conceded = defaultdict(list)
        now_idx = len(matches)
        for i, m in enumerate(matches):
            w = exp(-decay * (now_idx - i))
            hg, ag = m.get('home_goals', 0), m.get('away_goals', 0)
            team_scored[m['home']].append((hg, w))
            team_scored[m['away']].append((ag, w))
            team_conceded[m['home']].append((ag, w))
            team_conceded[m['away']].append((hg, w))

        for team in set(list(team_scored.keys()) + list(team_conceded.keys())):
            sc = team_scored.get(team, [])
            tw = sum(w for _, w in sc)
            if tw > 0:
                self.attack[team] = max(0.3, sum(g * w for g, w in sc) / tw / (self.league_avg_goals / 2))
            co = team_conceded.get(team, [])
            tw = sum(w for _, w in co)
            if tw > 0:
                self.defense[team] = max(0.3, sum(g * w for g, w in co) / tw / (self.league_avg_goals / 2))

        # Optimize rho
        recent = matches[-50:]
        best_rho, best_ll = -0.13, float('-inf')
        for rho_c in [-0.20, -0.15, -0.13, -0.10, -0.05, 0.0]:
            ll = 0
            for m in recent:
                lam = self._lambda(m['home'], m['away'], True)
                mu = self._lambda(m['away'], m['home'], False)
                p = _poisson_pmf(m.get('home_goals',0), lam) * _poisson_pmf(m.get('away_goals',0), mu) * \
                    _tau(m.get('home_goals',0), m.get('away_goals',0), lam, mu, rho_c)
                if p > 0:
                    ll += math.log(p)
            if ll > best_ll:
                best_ll, best_rho = ll, rho_c
        self.rho = best_rho
        self._trained = True
        log.info('Dixon-Coles: %d teams, %d matches, rho=%.2f', len(self.attack), len(matches), self.rho)

    def _lambda(self, team_a, team_b, is_home):
        lam = self.attack[team_a] * self.defense[team_b] * (self.league_avg_goals / 2)
        if is_home:
            lam *= self.home_adv
        return max(0.1, lam)

    def predict(self, home, away, max_goals=8):
        lam = self._lambda(home, away, True)
        mu = self._lambda(away, home, False)
        ph, pd, pa, p_o25, p_o15, p_o35 = 0, 0, 0, 0, 0, 0
        for i in range(max_goals):
            for j in range(max_goals):
                p = _poisson_pmf(i, lam) * _poisson_pmf(j, mu) * _tau(i, j, lam, mu, self.rho)
                if i > j: ph += p
                elif i == j: pd += p
                else: pa += p
                if i + j > 2: p_o25 += p
                if i + j > 1: p_o15 += p
                if i + j > 3: p_o35 += p
        total = ph + pd + pa
        if total > 0:
            ph, pd, pa = ph/total, pd/total, pa/total
        return {'home_win': ph, 'draw': pd, 'away_win': pa,
                'over25': p_o25, 'under25': 1-p_o25,
                'over15': p_o15, 'under15': 1-p_o15,
                'over35': p_o35, 'under35': 1-p_o35,
                'exp_home': lam, 'exp_away': mu}

    def save(self, path=None):
        path = path or DC_CACHE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'attack': dict(self.attack), 'defense': dict(self.defense),
                       'home_adv': self.home_adv, 'rho': self.rho,
                       'league_avg': self.league_avg_goals}, f)

    def load(self, path=None):
        path = path or DC_CACHE
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.get('attack', {}).items(): self.attack[k] = v
            for k, v in data.get('defense', {}).items(): self.defense[k] = v
            self.home_adv = data.get('home_adv', 1.20)
            self.rho = data.get('rho', -0.13)
            self.league_avg_goals = data.get('league_avg', 2.65)
            self._trained = True
            return True
        except Exception:
            return False
