"""In-play betting + cash out logic for Oraculo."""
import os, json, logging, time
from datetime import datetime

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class InPlayMonitor:
    """Monitor live matches for cash-out and in-play opportunities."""

    def __init__(self, api):
        self.api = api

    def check_active_bets_inplay(self, active_bets):
        """Check if any active bets are in matches currently in-play.

        Returns list of bets that might need cash-out consideration.
        """
        alerts = []
        for bet in active_bets:
            eid = bet.get('event_id', '')
            if not eid:
                continue

            # Check if event is live
            live_data = self._get_live_data(eid)
            if not live_data:
                continue

            status = live_data.get('status', '')
            if status not in ('IN_PROGRESS', 'LIVE', 'live'):
                continue

            score_home = live_data.get('homeScore', 0)
            score_away = live_data.get('awayScore', 0)
            minutes = live_data.get('minutes', 0)
            period = live_data.get('period', '')

            # Evaluate if bet is in danger
            market = bet.get('label', '') or bet.get('market', '')
            danger = self._assess_danger(bet, score_home, score_away, minutes, market)

            if danger:
                alerts.append({
                    'bet': bet,
                    'score': f'{score_home}-{score_away}',
                    'minutes': minutes,
                    'danger_level': danger['level'],
                    'reason': danger['reason'],
                    'recommendation': danger['action'],
                    'current_odds': danger.get('current_odds'),
                })

        return alerts

    def _get_live_data(self, event_id):
        """Get live match data from Cloudbet."""
        try:
            r = self.api.v2.get(
                f'https://sports-api.cloudbet.com/pub/v2/odds/events/{event_id}',
                timeout=10
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def _assess_danger(self, bet, score_h, score_a, minutes, market):
        """Assess if a bet is in danger based on live score."""
        market_lower = market.lower()
        stake = bet.get('stake', 0)
        odds = bet.get('odds', 0)

        # Asian Handicap assessment
        if 'ah' in market_lower or 'handicap' in market_lower:
            # Parse handicap value
            import re
            hcap_match = re.search(r'[+-]?\d+\.?\d*', market_lower.split('handicap')[-1] if 'handicap' in market_lower else market_lower)
            if hcap_match:
                handicap = float(hcap_match.group())
                # If bet is AH home +1.5 and score is 0-3, it's dead
                adjusted = (score_h + handicap) - score_a
                if adjusted <= -2 and minutes > 60:
                    return {'level': 'CRITICAL', 'reason': f'AH{handicap:+.1f} losing by {abs(adjusted):.1f} at {minutes}min',
                            'action': 'CASH_OUT', 'current_odds': None}
                if adjusted <= -1 and minutes > 75:
                    return {'level': 'HIGH', 'reason': f'AH{handicap:+.1f} behind at {minutes}min',
                            'action': 'CONSIDER_CASHOUT', 'current_odds': None}

        # Over/Under assessment
        if 'over' in market_lower:
            total_goals = score_h + score_a
            target_match = re.search(r'(\d+\.?\d*)', market_lower) if 'over' in market_lower else None
            if target_match:
                target = float(target_match.group(1))
                remaining = 90 - minutes
                if total_goals > target:
                    # Already won! But need to wait for settlement
                    return None
                goals_needed = target - total_goals + 1
                if goals_needed > 2 and remaining < 30:
                    return {'level': 'HIGH', 'reason': f'Need {goals_needed:.0f} goals in {remaining:.0f}min for Over {target}',
                            'action': 'LIKELY_LOSS', 'current_odds': None}

        if 'under' in market_lower:
            total_goals = score_h + score_a
            target_match = re.search(r'(\d+\.?\d*)', market_lower)
            if target_match:
                target = float(target_match.group(1))
                if total_goals >= target:
                    return {'level': 'CRITICAL', 'reason': f'Under {target} already busted ({total_goals} goals)',
                            'action': 'LOST', 'current_odds': None}

        return None

    def find_inplay_value(self, events):
        """Scan live events for in-play value bets.

        Key scenarios:
        - Strong team losing at halftime (overreaction)
        - Red card just shown (odds shift too much)
        - Expected goals vs actual divergence
        """
        picks = []
        for ev in events:
            status = ev.get('status', '')
            if status not in ('IN_PROGRESS', 'LIVE', 'live'):
                continue

            home = (ev.get('home') or {}).get('name', '')
            away = (ev.get('away') or {}).get('name', '')
            score_h = ev.get('homeScore', 0)
            score_a = ev.get('awayScore', 0)
            minutes = ev.get('minutes', 0)

            # Look for overreaction: strong favorite losing
            markets = ev.get('markets', {})
            ft = markets.get('soccer.match_odds', {})
            for sv in ft.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    price = float(sel.get('price', 0) or 0)
                    outcome = sel.get('outcome', '')
                    murl = sel.get('marketUrl', '')

                    # Halftime overreaction: if favorite lost first half
                    # and now has odds > 3.0, might be value
                    if minutes >= 45 and minutes <= 55 and price > 3.0:
                        # This is a simplified heuristic
                        # Real in-play needs xG live data
                        pass

        return picks


def check_cashout_opportunities(api, state):
    """Check if any active bets should be cashed out.
    Called every result check cycle (30min).
    """
    try:
        monitor = InPlayMonitor(api)
        alerts = monitor.check_active_bets_inplay(state.get('active_bets', []))
        for alert in alerts:
            level = alert['danger_level']
            match = alert['bet'].get('match', '')[:30]
            log.warning('[InPlay] %s: %s | %s — %s',
                       level, match, alert['score'], alert['reason'])
            # Store alert for Telegram/Obsidian notification
            if '_inplay_alerts' not in state:
                state['_inplay_alerts'] = []
            state['_inplay_alerts'].append({
                'ts': datetime.utcnow().isoformat(),
                'match': match,
                'level': level,
                'reason': alert['reason'],
                'action': alert['recommendation'],
            })
        return alerts
    except Exception as e:
        log.debug('InPlay check error: %s', e)
        return []
