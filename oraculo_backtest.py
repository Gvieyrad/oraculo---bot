"""Backtesting framework — simulate strategies on historical data."""
import os, json, logging, math
from collections import defaultdict
from datetime import datetime

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_FILE = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
BACKTEST_RESULTS = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'backtest_results.json')


class Backtester:
    """Run strategy simulations on historical prediction data."""

    def __init__(self, initial_bankroll=11.37):
        self.initial_bankroll = initial_bankroll

    def load_settled_bets(self):
        """Load all settled bets from predictions log."""
        if not os.path.exists(PREDICTIONS_FILE):
            return []
        bets = []
        with open(PREDICTIONS_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    if e.get('result') in ('WIN', 'LOSS'):
                        bets.append(e)
                except Exception:
                    pass
        return sorted(bets, key=lambda x: x.get('ts', ''))

    def simulate(self, bets, params=None):
        """Simulate a strategy on historical bets.

        params: dict with strategy parameters to override defaults:
            min_edge, min_conf, kelly_frac, max_daily_pct, max_per_bet
        """
        if not bets:
            return None

        p = params or {}
        min_edge = p.get('min_edge', 0.05)
        min_conf = p.get('min_conf', 0.60)
        kelly_frac = p.get('kelly_frac', 0.25)
        max_per_bet = p.get('max_per_bet', 0.05)

        bankroll = self.initial_bankroll
        peak = bankroll
        max_drawdown = 0
        wins, losses = 0, 0
        total_staked = 0
        total_pnl = 0
        bets_taken = 0
        daily_pnl = defaultdict(float)
        by_market = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0})
        by_sport = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0})
        equity_curve = [bankroll]

        for bet in bets:
            edge = bet.get('edge', 0)
            prob = bet.get('model_prob', 0)
            odds = bet.get('odds', 0)
            result = bet.get('result', '')
            original_stake = bet.get('stake', 0)
            market = bet.get('market_type', 'unknown')
            sport = bet.get('sport', 'unknown')
            day = bet.get('ts', '')[:10]

            # Filter: would this strategy take this bet?
            if edge < min_edge or prob < min_conf:
                continue

            # Size: recalculate Kelly with current bankroll
            b = odds - 1
            if b <= 0:
                continue
            kelly = (prob * b - (1 - prob)) / b
            stake = bankroll * kelly * kelly_frac
            stake = min(stake, bankroll * max_per_bet)
            stake = max(0, round(stake, 2))

            if stake < 0.50 or bankroll < 10:
                continue

            bets_taken += 1
            total_staked += stake

            if result == 'WIN':
                pnl = stake * (odds - 1)
                bankroll += pnl
                total_pnl += pnl
                wins += 1
                by_market[market]['wins'] += 1
                by_market[market]['pnl'] += pnl
                by_sport[sport]['wins'] += 1
                by_sport[sport]['pnl'] += pnl
            elif result == 'LOSS':
                bankroll -= stake
                total_pnl -= stake
                losses += 1
                by_market[market]['losses'] += 1
                by_market[market]['pnl'] -= stake
                by_sport[sport]['losses'] += 1
                by_sport[sport]['pnl'] -= stake

            daily_pnl[day] += (pnl if result == 'WIN' else -stake)
            equity_curve.append(bankroll)
            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak if peak > 0 else 0
            max_drawdown = max(max_drawdown, dd)

        if bets_taken == 0:
            return None

        # Calculate Sharpe ratio (daily)
        daily_returns = list(daily_pnl.values())
        if len(daily_returns) > 1:
            import statistics
            mean_r = statistics.mean(daily_returns)
            std_r = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 1
            sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0
        else:
            sharpe = 0

        return {
            'params': p,
            'bets_taken': bets_taken,
            'bets_filtered': len(bets) - bets_taken,
            'wins': wins,
            'losses': losses,
            'win_rate': wins / bets_taken if bets_taken else 0,
            'total_staked': round(total_staked, 2),
            'total_pnl': round(total_pnl, 2),
            'roi': round(total_pnl / total_staked, 4) if total_staked else 0,
            'final_bankroll': round(bankroll, 2),
            'peak_bankroll': round(peak, 2),
            'max_drawdown': round(max_drawdown, 4),
            'sharpe_ratio': round(sharpe, 3),
            'by_market': dict(by_market),
            'by_sport': dict(by_sport),
            'equity_curve': equity_curve,
        }

    def grid_search(self, bets, param_grid=None):
        """Test multiple strategy variants, find optimal parameters."""
        if param_grid is None:
            param_grid = {
                'min_edge': [0.03, 0.05, 0.07, 0.10],
                'min_conf': [0.55, 0.60, 0.65, 0.70],
                'kelly_frac': [0.15, 0.20, 0.25, 0.30, 0.35],
                'max_per_bet': [0.03, 0.05, 0.08],
            }

        # Generate all combinations
        from itertools import product
        keys = list(param_grid.keys())
        values = [param_grid[k] for k in keys]
        combos = list(product(*values))

        results = []
        for combo in combos:
            params = dict(zip(keys, combo))
            result = self.simulate(bets, params)
            if result and result['bets_taken'] >= 5:
                results.append(result)

        # Sort by Sharpe ratio (risk-adjusted return)
        results.sort(key=lambda r: r.get('sharpe_ratio', 0), reverse=True)

        if results:
            best = results[0]
            log.info('[Backtest] Best params: edge=%.0f%% conf=%.0f%% kelly=%.2f -> '
                     'ROI=%.1f%% Sharpe=%.2f DD=%.1f%% (%d bets)',
                     best['params']['min_edge'] * 100,
                     best['params']['min_conf'] * 100,
                     best['params']['kelly_frac'],
                     best['roi'] * 100, best['sharpe_ratio'],
                     best['max_drawdown'] * 100, best['bets_taken'])

            # Save results
            os.makedirs(os.path.dirname(BACKTEST_RESULTS), exist_ok=True)
            with open(BACKTEST_RESULTS, 'w') as f:
                json.dump({
                    'best': {k: v for k, v in best.items() if k != 'equity_curve'},
                    'top_5': [{k: v for k, v in r.items() if k != 'equity_curve'}
                              for r in results[:5]],
                    'total_tested': len(combos),
                    'valid_results': len(results),
                    'timestamp': datetime.utcnow().isoformat(),
                }, f, indent=2)

        return results

    def recommend_params(self, bets):
        """Run grid search and return recommended parameters."""
        results = self.grid_search(bets)
        if not results:
            return None

        best = results[0]
        return best['params']


class ABTester:
    """A/B test two strategy variants side by side.

    Model A = live (places real bets)
    Model B = shadow (paper trades, logged for comparison)
    """

    def __init__(self):
        self.ab_log_path = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'ab_test.jsonl')

    def log_pick(self, pick, model_name, would_place, reason=''):
        """Log a pick decision for A/B comparison."""
        try:
            entry = {
                'ts': datetime.utcnow().isoformat(),
                'model': model_name,
                'match': pick.get('match', ''),
                'label': pick.get('label', ''),
                'odds': pick.get('price', 0),
                'model_prob': pick.get('model_prob', 0),
                'edge': pick.get('edge', 0),
                'would_place': would_place,
                'reason': reason,
                'result': None,
            }
            with open(self.ab_log_path, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception:
            pass

    def compare(self):
        """Compare A vs B model performance."""
        if not os.path.exists(self.ab_log_path):
            return None

        models = defaultdict(lambda: {'placed': 0, 'wins': 0, 'losses': 0, 'pnl': 0})
        with open(self.ab_log_path) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    if not e.get('result'):
                        continue
                    m = e['model']
                    if e['would_place']:
                        models[m]['placed'] += 1
                        if e['result'] == 'WIN':
                            models[m]['wins'] += 1
                            models[m]['pnl'] += e.get('win_loss', 0)
                        elif e['result'] == 'LOSS':
                            models[m]['losses'] += 1
                            models[m]['pnl'] -= e.get('stake', 0)
                except Exception:
                    pass

        return dict(models)
