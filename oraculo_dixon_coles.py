"""Dixon-Coles model for football match prediction.
Improves on basic Poisson by correcting low-scoring bias and using
attack/defense strength parameters with time decay.
"""
import os, json, math, logging
from collections import defaultdict
from datetime import datetime
from math import exp, factorial

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DC_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'dixon_coles.json')


def _parse_date(d):
    """Parse DD/MM/YYYY, YYYY-MM-DD, and ISO YYYY-MM-DDTHH:MM:SSZ."""
    s = (d or '')[:10]
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None


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
        self.team_league = {}
        self.league_avgs = {}
        self._trained = False

    # Per-day decay: calibrated via grid search with correct date parsing (2026-05-21).
    # EUR 0.0008 / LATAM 0.0015 achieved 47.2% backtest accuracy (5051 matches ≥2024).
    _LEAGUE_DECAY = {
        'PL': 0.0008, 'E0': 0.0008, 'E1': 0.0008,
        'PD': 0.0008, 'SP1': 0.0008,
        'SA': 0.0008, 'I1': 0.0008,
        'BL1': 0.0008, 'D1': 0.0008,
        'FL1': 0.0008, 'F1': 0.0008,
        'DED': 0.0008, 'N1': 0.0008,
        'PPL': 0.0008, 'P1': 0.0008,
        'TUR': 0.0010, 'T1': 0.0010,
        'ARG': 0.0015,
        'BRA': 0.0015,
        'CHI': 0.0015,
        'URU': 0.0015,
        'RUS': 0.0012,
        'JPN': 0.0010,
    }

    # Form factor: last N matches per team get this weight multiplier
    _FORM_N      = 5
    _FORM_BOOST  = 2.0

    @staticmethod
    def _form_boost_set(matches, n, boost):
        """Build set of match keys that qualify for form boost.
        A match key (date, home, away) is in the set if it is among
        the last N matches for at least one of the two teams involved.
        Assumes matches are already sorted by date ascending.
        """
        team_keys = defaultdict(list)
        for m in matches:
            key = (m.get('date', ''), m['home'], m['away'])
            team_keys[m['home']].append(key)
            team_keys[m['away']].append(key)
        boosted = set()
        for keys in team_keys.values():
            for k in keys[-n:]:
                boosted.add(k)
        return boosted

    def train(self, matches, decay=0.0035, per_league_decay=None):
        if not matches:
            return
        _decay = per_league_decay or self._LEAGUE_DECAY
        _now = datetime.now()
        matches = sorted(matches, key=lambda m: m.get('date', ''))

        total_goals = sum(m.get('home_goals', 0) + m.get('away_goals', 0) for m in matches)
        if matches:
            self.league_avg_goals = total_goals / len(matches)

        # ── Pass 1: build team_league and league_avgs ─────────────────────────
        # Must come before attack/defense computation so we can normalize per-league.
        league_goals  = defaultdict(float)
        league_counts = defaultdict(int)
        for m in matches:
            lg = m.get('league') or m.get('competition_code', 'UNK')
            self.team_league[m['home']] = lg
            self.team_league[m['away']] = lg
            league_goals[lg]  += m.get('home_goals', 0) + m.get('away_goals', 0)
            league_counts[lg] += 1
        self.league_avgs = {lg: league_goals[lg] / league_counts[lg] for lg in league_goals}

        # ── Form boost set ────────────────────────────────────────────────────
        form_keys = self._form_boost_set(matches, self._FORM_N, self._FORM_BOOST)

        # ── Pass 2: collect weighted goals per team ───────────────────────────
        team_scored   = defaultdict(list)
        team_conceded = defaultdict(list)
        for m in matches:
            lg = m.get('league') or m.get('competition_code', 'UNK')
            d  = _decay.get(lg, decay)
            dt = _parse_date(m.get('date', ''))
            days_ago = max(0, (_now - dt).days) if dt else 730
            mk         = (m.get('date', ''), m['home'], m['away'])
            form_mult  = self._FORM_BOOST if mk in form_keys else 1.0
            w          = exp(-d * days_ago) * form_mult
            hg, ag     = m.get('home_goals', 0), m.get('away_goals', 0)
            team_scored[m['home']].append((hg, w))
            team_scored[m['away']].append((ag, w))
            team_conceded[m['home']].append((ag, w))
            team_conceded[m['away']].append((hg, w))

        # ── Pass 3: compute attack / defense normalized per league ────────────
        for team in set(list(team_scored.keys()) + list(team_conceded.keys())):
            lg   = self.team_league.get(team, '')
            base = self.league_avgs.get(lg, self.league_avg_goals) / 2  # per-league base

            sc = team_scored.get(team, [])
            tw = sum(w for _, w in sc)
            if tw > 0:
                self.attack[team] = max(0.3, sum(g * w for g, w in sc) / tw / base)

            co = team_conceded.get(team, [])
            tw = sum(w for _, w in co)
            if tw > 0:
                self.defense[team] = max(0.3, sum(g * w for g, w in co) / tw / base)

        # rho=-0.13 (Dixon-Coles literature value; in-sample grid search was unstable)
        self.rho = -0.13
        self._trained = True
        _sample = {lg: round(_decay.get(lg, decay), 4) for lg in ['ARG', 'CHI', 'URU', 'BRA', 'PL', 'BL1']}
        log.info('Dixon-Coles: %d teams, %d matches, rho=%.2f', len(self.attack), len(matches), self.rho)
        log.info('Decay rates (per day): %s', _sample)
        log.info('Form factor: last %d games x%.1f | normalization: per-league', self._FORM_N, self._FORM_BOOST)

    def train_mle(self, matches, decay=0.0035, per_league_decay=None):
        """Maximum-likelihood Dixon-Coles via L-BFGS-B (numpy vectorized).
        Falls back to train() if scipy is unavailable.
        """
        try:
            from scipy.optimize import minimize
            import numpy as np
        except ImportError:
            log.warning('scipy not available; falling back to train()')
            return self.train(matches, decay, per_league_decay)

        if not matches:
            return
        _decay = per_league_decay or self._LEAGUE_DECAY
        _now = datetime.now()
        matches = sorted(matches, key=lambda m: m.get('date', ''))

        # Pass 1: league_avgs + team_league (identical to train())
        league_goals  = defaultdict(float)
        league_counts = defaultdict(int)
        for m in matches:
            lg = m.get('league') or m.get('competition_code', 'UNK')
            self.team_league[m['home']] = lg
            self.team_league[m['away']] = lg
            league_goals[lg]  += m.get('home_goals', 0) + m.get('away_goals', 0)
            league_counts[lg] += 1
        self.league_avgs = {lg: league_goals[lg] / league_counts[lg] for lg in league_goals}
        total_goals = sum(m.get('home_goals', 0) + m.get('away_goals', 0) for m in matches)
        self.league_avg_goals = total_goals / len(matches) if matches else 2.65

        # Form boost set
        form_keys = self._form_boost_set(matches, self._FORM_N, self._FORM_BOOST)

        # Team index
        teams = sorted(set(m['home'] for m in matches) | set(m['away'] for m in matches))
        tidx = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        # Precompute arrays for vectorized objective
        rows = []
        for m in matches:
            h, a = m['home'], m['away']
            if h not in tidx or a not in tidx:
                continue
            lg = m.get('league') or m.get('competition_code', 'UNK')
            d  = _decay.get(lg, decay)
            dt = _parse_date(m.get('date', ''))
            days_ago = max(0, (_now - dt).days) if dt else 730
            mk = (m.get('date', ''), h, a)
            form_mult = self._FORM_BOOST if mk in form_keys else 1.0
            w = exp(-d * days_ago) * form_mult
            if w < 1e-9:
                continue
            base = self.league_avgs.get(lg, self.league_avg_goals) / 2
            rows.append((tidx[h], tidx[a], int(m.get('home_goals', 0)),
                         int(m.get('away_goals', 0)), w, base))

        import numpy as np
        h_idx  = np.array([r[0] for r in rows], dtype=np.int32)
        a_idx  = np.array([r[1] for r in rows], dtype=np.int32)
        hg_arr = np.array([r[2] for r in rows], dtype=np.float64)
        ag_arr = np.array([r[3] for r in rows], dtype=np.float64)
        w_arr  = np.array([r[4] for r in rows], dtype=np.float64)
        base_arr = np.array([r[5] for r in rows], dtype=np.float64)

        def neg_ll(params):
            la = params[:n]           # log(attack)
            ld = params[n:2*n]        # log(defense)
            home_adv = float(np.exp(params[-2]))
            rho      = float(np.clip(params[-1], -0.5, 0.0))

            lam = np.exp(la[h_idx] + ld[a_idx]) * base_arr * home_adv
            mu  = np.exp(la[a_idx] + ld[h_idx]) * base_arr
            lam = np.maximum(lam, 1e-9)
            mu  = np.maximum(mu,  1e-9)

            tau = np.ones(len(rows))
            m00 = (hg_arr == 0) & (ag_arr == 0)
            m01 = (hg_arr == 0) & (ag_arr == 1)
            m10 = (hg_arr == 1) & (ag_arr == 0)
            m11 = (hg_arr == 1) & (ag_arr == 1)
            tau[m00] = 1 - lam[m00] * mu[m00] * rho
            tau[m01] = 1 + lam[m01] * rho
            tau[m10] = 1 + mu[m10]  * rho
            tau[m11] = 1 - rho
            tau = np.maximum(tau, 1e-10)

            ll = np.sum(w_arr * (
                np.log(tau)
                + hg_arr * np.log(lam) - lam
                + ag_arr * np.log(mu)  - mu
            ))
            # Soft L2 identifiability: keep mean(log_atk) near 0
            ll -= 1e-4 * float(np.sum(la) ** 2)
            return -float(ll)

        x0 = np.zeros(2 * n + 2)
        x0[-2] = np.log(1.20)
        x0[-1] = -0.13
        log.info('MLE: %d teams, %d matches (effective weight)', n, len(rows))
        res = minimize(neg_ll, x0, method='L-BFGS-B',
                       options={'maxiter': 1000, 'ftol': 1e-9, 'gtol': 1e-6})

        atk = np.exp(res.x[:n])
        def_ = np.exp(res.x[n:2*n])
        # Re-normalize: geometric mean of attacks = 1
        geo_mean_atk = float(np.exp(np.mean(np.log(np.maximum(atk, 1e-9)))))
        atk  /= geo_mean_atk
        def_ *= geo_mean_atk

        for t, i in tidx.items():
            self.attack[t]  = float(max(0.3, atk[i]))
            self.defense[t] = float(max(0.3, def_[i]))
        self.home_adv = float(np.clip(np.exp(res.x[-2]), 1.0, 1.6))
        self.rho      = float(np.clip(res.x[-1], -0.5, 0.0))
        self._trained = True
        log.info('MLE done: rho=%.4f, home_adv=%.4f, converged=%s', self.rho, self.home_adv, res.success)

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
                       'league_avg': self.league_avg_goals,
                       'team_league': dict(self.team_league),
                       'league_avgs': dict(self.league_avgs)}, f)

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
            self.team_league = data.get('team_league', {})
            self.league_avgs = data.get('league_avgs', {})
            self._trained = True
            return True
        except Exception:
            return False
