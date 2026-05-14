"""Portfolio Kelly — optimize bet sizing considering correlation between bets."""
import os, json, logging, math
from collections import defaultdict
from datetime import datetime

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class PortfolioKelly:
    """Multi-variable Kelly criterion that accounts for bet correlations.

    Standard Kelly treats each bet independently. Portfolio Kelly considers:
    1. Correlated bets (same match, same league, same team)
    2. Total portfolio exposure
    3. Diminishing marginal utility of additional bets
    """

    def __init__(self, bankroll, max_total=0.80, max_per_match=0.10, kelly_frac=0.25):
        self.bankroll = bankroll
        self.max_total = max_total
        self.max_per_match = max_per_match
        self.kelly_frac = kelly_frac

    def _correlation(self, bet_a, bet_b):
        """Estimate correlation between two bets (0=independent, 1=identical).
        Same match = 0.9, same league same day = 0.3, same sport = 0.1, else 0."""
        if bet_a.get('event_id') == bet_b.get('event_id'):
            return 0.9
        if bet_a.get('match') == bet_b.get('match'):
            return 0.8
        # Same league, check if teams overlap
        if bet_a.get('league') == bet_b.get('league'):
            teams_a = set(bet_a.get('match', '').lower().split(' vs '))
            teams_b = set(bet_b.get('match', '').lower().split(' vs '))
            if teams_a & teams_b:
                return 0.6  # Shared team
            return 0.2  # Same league, different teams
        if bet_a.get('sport') == bet_b.get('sport'):
            return 0.05
        return 0.0

    def _single_kelly(self, prob, odds):
        """Standard Kelly fraction for a single bet."""
        b = odds - 1
        if b <= 0 or prob <= 0:
            return 0
        f = (prob * b - (1 - prob)) / b
        return max(0, f * self.kelly_frac)

    def optimize(self, picks, active_bets=None):
        """Optimize stake allocation for a set of picks considering correlations.

        picks: list of dicts with model_prob, price, match, league, sport, event_id
        active_bets: currently active bets (for exposure tracking)

        Returns: list of (pick, optimal_stake) tuples
        """
        if not picks:
            return []

        active_bets = active_bets or []

        # Calculate individual Kelly stakes
        kelly_stakes = []
        for p in picks:
            f = self._single_kelly(p['model_prob'], p['price'])
            kelly_stakes.append(f * self.bankroll)

        # Apply correlation discount
        # For each pair of bets, reduce stakes proportional to correlation
        n = len(picks)
        adjusted = list(kelly_stakes)

        for i in range(n):
            total_corr = 0
            # Correlation with other new picks
            for j in range(n):
                if i != j:
                    corr = self._correlation(picks[i], picks[j])
                    total_corr += corr
            # Correlation with active bets
            for ab in active_bets:
                corr = self._correlation(picks[i], ab)
                total_corr += corr

            # Discount: more correlated bets = smaller individual stakes
            # Each unit of correlation reduces stake by ~15%
            discount = max(0.3, 1 - total_corr * 0.15)
            adjusted[i] *= discount

        # Enforce constraints
        total_active = sum(b.get('stake', 0) for b in active_bets)
        remaining_budget = self.bankroll * self.max_total - total_active

        if remaining_budget <= 0:
            return [(p, 0) for p in picks]

        # Match exposure tracking
        match_exposure = defaultdict(float)
        for ab in active_bets:
            match_exposure[ab.get('match', '')] += ab.get('stake', 0)

        # Allocate
        result = []
        total_allocated = 0
        for i, p in enumerate(picks):
            stake = adjusted[i]

            # Cap per match
            match_key = p.get('match', '')
            current_match_exp = match_exposure.get(match_key, 0)
            max_match = self.bankroll * self.max_per_match
            stake = min(stake, max_match - current_match_exp)

            # Cap total
            stake = min(stake, remaining_budget - total_allocated)

            # Minimum stake
            stake = round(max(0, stake), 2)
            if stake < 0.50:
                stake = 0

            result.append((p, stake))
            total_allocated += stake
            match_exposure[match_key] += stake

        # Log portfolio summary
        allocated = [(p, s) for p, s in result if s > 0]
        if allocated:
            total_s = sum(s for _, s in allocated)
            avg_edge = sum(p['edge'] * s for p, s in allocated) / total_s if total_s else 0
            log.info('[Portfolio] %d/%d picks allocated, $%.2f total, avg edge %.1f%%',
                     len(allocated), len(picks), total_s, avg_edge * 100)

        return result

class PortfolioManager:
    """Wrapper compatible con el runner: get_adjusted_stake() por pick individual."""

    def __init__(self, predictions_file=None, bankroll=1000.0,
                 max_total=0.50, max_per_match=0.10, kelly_frac=0.25):
        self._kelly = PortfolioKelly(
            bankroll=bankroll,
            max_total=max_total,
            max_per_match=max_per_match,
            kelly_frac=kelly_frac,
        )
        self._active_bets = []
        self._cache = {}

    def update_bankroll(self, bankroll):
        self._kelly.bankroll = bankroll

    def set_active_bets(self, active_bets):
        self._active_bets = list(active_bets or [])
        self._cache = {}

    def invalidate_cache(self):
        self._cache = {}

    def get_adjusted_stake(self, pick, base_stake=None):
        """
        Ajusta el stake de un pick considerando correlacion con bets activas.
        Retorna dict: {stake_adj, skip, capped, reason, corr_penalty}
        """
        try:
            if base_stake is None or base_stake <= 0:
                return {'stake_adj': 0, 'skip': True, 'capped': False,
                        'reason': 'base_stake=0', 'corr_penalty': 0}

            kelly = self._kelly
            bankroll = kelly.bankroll

            # Correlacion total con bets activas
            total_corr = sum(
                kelly._correlation(pick, ab) for ab in self._active_bets
            )
            discount = max(0.3, 1.0 - total_corr * 0.15)
            stake = base_stake * discount

            # Cap por match
            match_key = pick.get('match', '')
            match_exp = sum(
                b.get('stake', 0) for b in self._active_bets
                if b.get('match', '') == match_key
            )
            max_match = bankroll * kelly.max_per_match
            if match_exp >= max_match:
                return {'stake_adj': 0, 'skip': True, 'capped': True,
                        'reason': 'match cap %.0f%%' % (kelly.max_per_match * 100),
                        'corr_penalty': round(1 - discount, 3)}

            stake = min(stake, max_match - match_exp)

            # Cap total
            total_exp = sum(b.get('stake', 0) for b in self._active_bets)
            max_total = bankroll * kelly.max_total
            if total_exp >= max_total:
                return {'stake_adj': 0, 'skip': True, 'capped': True,
                        'reason': 'exposure cap %.0f%%' % (kelly.max_total * 100),
                        'corr_penalty': round(1 - discount, 3)}

            stake = min(stake, max_total - total_exp)
            stake = round(max(0, stake), 2)

            return {
                'stake_adj':    stake,
                'skip':         stake < 0.25,
                'capped':       stake < base_stake * 0.9,
                'reason':       ('corr discount %.0f%%' % ((1 - discount) * 100)) if discount < 1 else '',
                'corr_penalty': round(1 - discount, 3),
            }
        except Exception as ex:
            log.debug('PortfolioManager.get_adjusted_stake error: %s', ex)
            return {'stake_adj': base_stake or 0, 'skip': False,
                    'capped': False, 'reason': '', 'corr_penalty': 0}

    def format_status(self):
        active = len(self._active_bets)
        exp = sum(b.get('stake', 0) for b in self._active_bets)
        return 'Portfolio: %d bets activos | $%.2f expuesto' % (active, exp)
