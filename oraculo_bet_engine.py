#!/usr/bin/env python3
"""
oraculo_bet_engine.py - Smart bet placement + tracking + auto-run.

Features:
1. Smart placement - retries with current price on PRICE_ABOVE_MARKET
2. Bet tracker - checks results, updates bankroll, logs P&L
3. Per-league model selection
4. Correct score market (Poisson-derived)
5. Auto-run scheduler
"""

import os
import sys
import json
import time
import uuid
import logging
import requests
import pickle
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

log = logging.getLogger('oraculo.engine')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, 'models')
PICKS_DIR = os.path.join(SCRIPT_DIR, 'picks')
STATE_FILE = os.path.join(PICKS_DIR, 'engine_state.json')

CB_BASE = 'https://sports-api.cloudbet.com'

# League-specific market config (V3: AH + Over/Under prioritized, BTTS removed)
# Strategy: Asian Handicap (85% acc) + Over/Under (82% acc) + Corners top leagues only
# BTTS removed: 60% win rate, negative ROI on Day 1 (-$1.24), too many loss modes
LEAGUE_MARKETS = {
    'PL':  ['asian_handicap', 'over25', 'corners_o95'],
    'PD':  ['asian_handicap', 'over25', 'corners_o95'],
    'SA':  ['asian_handicap', 'over25', 'corners_o95'],
    'BL1': ['asian_handicap', 'over25'],
    'FL1': ['asian_handicap', 'over25'],
    'ELC': ['asian_handicap', 'over25'],
    'DED': ['asian_handicap', 'over25'],
    'PPL': ['asian_handicap', 'over25'],
    'TUR': ['asian_handicap', 'over25'],
    'BEL': ['asian_handicap', 'over25'],
    'SWE': ['asian_handicap', 'over25'],
    'NOR': ['asian_handicap', 'over25'],
    'SWZ': ['asian_handicap', 'over25'],
    'BL2': ['asian_handicap', 'over25'],
    'SB':  ['asian_handicap', 'over25'],
    'FL2': ['asian_handicap', 'over25'],
    'PER': ['asian_handicap', 'over25'],
}

# Cloudbet market mapping
CB_MARKETS = {
    'over25':          ('soccer.total_goals', [('over', 'total=2.5'), ('under', 'total=2.5')]),
    'btts':            ('soccer.both_teams_to_score', [('yes', ''), ('no', '')]),
    'corners_o95':     ('soccer.total_corners', [('over', 'total=9.5'), ('under', 'total=9.5')]),
    'cards_o35':       ('soccer.total_bookings', [('over', 'total=3.5'), ('under', 'total=3.5')]),
    'shots_target_o85': ('soccer.total_goals', []),
    'asian_handicap':  ('soccer.asian_handicap', []),  # Dynamic lines
}
# Cloudbet -> Model team name mapping (fixes name mismatch)
_CB_TEAM_MAP = {'Bayern Munich': 'FC Bayern Munchen', 'Bayer Leverkusen': 'Bayer 04 Leverkusen', 'FC St. Pauli': 'FC St. Pauli 1910', 'Freiburg': 'Sport-Club Freiburg', 'Eintracht Frankfurt': 'Ein Frankfurt', 'FSV Mainz': '1. FSV Mainz 05', 'Stuttgart': 'VfB Stuttgart', 'TSG Hoffenheim': 'TSG 1899 Hoffenheim', 'Union Berlin': '1. FC Union Berlin', 'Werder Bremen': 'SV Werder Bremen', 'Wolfsburg': 'VfL Wolfsburg', 'Hamburger SV': 'Hamburg', 'Arsenal': 'Arsenal FC', 'Aston Villa': 'Aston Villa FC', 'Brentford': 'Brentford FC', 'Brighton & Hove Albion': 'Brighton & Hove Albion FC', 'Chelsea': 'Chelsea FC', 'Crystal Palace': 'Crystal Palace FC', 'Everton': 'Everton FC', 'Fulham': 'Fulham FC', 'Leeds United': 'Leeds', 'Liverpool': 'Liverpool FC', 'Manchester City': 'Manchester City FC', 'Manchester United': 'Manchester United FC', 'Newcastle United': 'Newcastle United FC', 'Nottingham Forest': 'Nottingham Forest FC', 'Sunderland AFC': 'Sunderland', 'Tottenham Hotspur': 'Tottenham Hotspur FC', 'West Ham United': 'West Ham United FC', 'Wolverhampton': 'Wolverhampton Wanderers FC', 'AS Monaco': 'AS Monaco FC', 'Brest': 'Stade Brestois 29', 'Lyon': 'Olympique Lyonnais', 'Nantes': 'FC Nantes', 'Nice': 'OGC Nice', 'Olympique Marseille': 'Olympique de Marseille', 'Paris Saint Germain': 'Paris Saint-Germain FC', 'Rennes': 'Stade Rennais FC 1901', 'Strasbourg Alsace': 'RC Strasbourg Alsace', 'AC Milan': 'Milan', 'Hellas Verona': 'Hellas Verona FC', 'Inter Milan': 'FC Internazionale Milano', 'Juventus': 'Juventus FC', 'Parma Calcio': 'Parma Calcio 1913', 'Pisa SC': 'Pisa', 'Sassuolo Calcio': 'Sassuolo', 'US Cremonese': 'Cremonese'}

def _normalize_team(name):
    return _CB_TEAM_MAP.get(name, name)


CB_COMPS = {
    'PL': 'soccer-england-premier-league',
    'PD': 'soccer-spain-la-liga',
    'SA': 'soccer-italy-serie-a',
    'BL1': 'soccer-germany-bundesliga',
    'FL1': 'soccer-france-ligue-1',
    'DED': 'soccer-netherlands-eredivisie',
    'PPL': 'soccer-portugal-primeira-liga',
    'ELC': 'soccer-england-championship',
    'TUR': 'soccer-turkey-super-lig',
    'BEL': 'soccer-belgium-first-division-a',
    'SWE': 'soccer-sweden-allsvenskan',
    'NOR': 'soccer-norway-eliteserien',
    'SWZ': 'soccer-switzerland-super-league',
    'BL2': 'soccer-germany-2-bundesliga',
    'SB':  'soccer-italy-serie-b',
    'FL2': 'soccer-france-ligue-2',
    'PER': 'soccer-peru-primera-division',
}


class BetEngine:
    """Smart bet placement and tracking engine for Cloudbet."""

    def __init__(self, bankroll=67.0, currency='USDC', max_daily_pct=0.40,
                 max_per_bet_pct=0.05, min_edge=0.05, min_conf=0.60):
        self.bankroll = bankroll
        self.currency = currency
        self.max_daily_pct = max_daily_pct
        self.max_per_bet = max_per_bet_pct
        self.min_edge = min_edge
        self.min_conf = min_conf

        self.session = None
        self.active_bets = []
        self.settled_bets = []
        self.daily_staked = 0.0
        self.total_profit = 0.0

        self._load_state()

    # =================================================================
    # 1. SMART PLACEMENT (retry on PRICE_ABOVE_MARKET)
    # =================================================================

    def connect(self):
        """Initialize Cloudbet API session."""
        config = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-Key': config['api_key'],
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        return True

    def place_bet(self, event_id, market_url, initial_price, stake, match_name='',
                  market_label='', max_retries=3):
        """
        Place bet with smart retry on price changes.

        Returns:
            dict with bet result or None
        """
        if not self.session:
            self.connect()

        # Check daily limit
        max_today = self.bankroll * self.max_daily_pct
        if self.daily_staked + stake > max_today:
            log.warning('Daily limit reached: %.2f / %.2f', self.daily_staked, max_today)
            return None

        price = str(initial_price)

        for attempt in range(max_retries):
            ref_id = str(uuid.uuid4())
            payload = {
                'acceptPriceChange': 'all',
                'currency': self.currency,
                'eventId': str(event_id),
                'marketUrl': market_url,
                'price': price,
                'stake': str(round(stake, 2)),
                'referenceId': ref_id,
            }

            try:
                r = self.session.post(f'{CB_BASE}/pub/v3/bets/place',
                                     json=payload, timeout=10)
                resp = r.json() if r.text and r.text.strip() else {"status": "EMPTY_RESPONSE", "http_code": r.status_code}
            except Exception as e:
                log.error('Bet placement error: %s', e)
                return None

            status = resp.get('status', '')
            actual_price = resp.get('price', price)

            if status == 'ACCEPTED':
                bet = {
                    'ref_id': ref_id,
                    'event_id': str(event_id),
                    'match': match_name,
                    'market': market_label,
                    'market_url': market_url,
                    'odds': float(actual_price),
                    'stake': stake,
                    'currency': self.currency,
                    'status': 'ACTIVE',
                    'placed_at': datetime.now().isoformat(),
                }
                self.active_bets.append(bet)
                self.daily_staked += stake
                self._save_state()
                log.info('BET PLACED: %s | %s @ %s | $%.2f',
                         match_name, market_label, actual_price, stake)
                return bet

            elif status == 'PENDING_ACCEPTANCE':
                bet = {
                    'ref_id': ref_id,
                    'event_id': str(event_id),
                    'match': match_name,
                    'market': market_label,
                    'market_url': market_url,
                    'odds': float(actual_price),
                    'stake': stake,
                    'currency': self.currency,
                    'status': 'PENDING',
                    'placed_at': datetime.now().isoformat(),
                }
                self.active_bets.append(bet)
                self.daily_staked += stake
                self._save_state()
                log.info('BET PENDING: %s | %s @ %s | $%.2f',
                         match_name, market_label, actual_price, stake)
                return bet

            elif status == 'PRICE_ABOVE_MARKET':
                # Retry with the returned market price
                new_price = resp.get('price', '')
                if new_price and new_price != price:
                    log.info('Price moved %s -> %s, retrying...', price, new_price)
                    price = new_price
                    time.sleep(0.5)
                    continue
                else:
                    # Try fetching fresh odds
                    fresh = self._get_fresh_price(event_id, market_url)
                    if fresh:
                        price = str(fresh)
                        time.sleep(0.5)
                        continue

            elif status == 'INSUFFICIENT_FUNDS':
                log.warning('Insufficient funds for $%.2f bet', stake)
                return None

            else:
                log.warning('Bet rejected: %s (attempt %d)', status, attempt + 1)
                time.sleep(0.5)

        log.warning('Failed after %d retries: %s', max_retries, match_name)
        return None

    def _get_fresh_price(self, event_id, market_url):
        """Fetch current price for a specific selection."""
        # Parse market_url: e.g. "soccer.total_goals/over?total=2.5"
        parts = market_url.split('/')
        if len(parts) < 2:
            return None

        mkt_key = parts[0]  # soccer.total_goals
        rest = parts[1]     # over?total=2.5
        outcome = rest.split('?')[0]

        try:
            # We need the competition to fetch, try all
            for comp_key in CB_COMPS.values():
                r = self.session.get(
                    f'{CB_BASE}/pub/v2/odds/competitions/{comp_key}', timeout=10)
                if r.status_code != 200:
                    continue
                for ev in r.json().get('events', []):
                    if str(ev.get('id', '')) != str(event_id):
                        continue
                    mkt = ev.get('markets', {}).get(mkt_key, {})
                    for sub in mkt.get('submarkets', {}).values():
                        for sel in sub.get('selections', []):
                            if sel.get('marketUrl') == market_url:
                                return sel.get('price')
                    return None
        except Exception:
            pass
        return None

    # =================================================================
    # 2. BET TRACKER
    # =================================================================

    def check_results(self):
        """Check results for active bets using match scores."""
        from oraculo_football_csv import download_league_csv, LEAGUE_MAP
        from oraculo_market_predictor import build_market_labels

        LEAGUE_MAP['ELC'] = 'E1'
        LEAGUE_MAP['DED'] = 'N1'
        LEAGUE_MAP['PPL'] = 'P1'

        # Load recent results
        recent = []
        for league in ['PL', 'PD', 'SA', 'BL1', 'FL1', 'ELC', 'DED', 'PPL']:
            recent.extend(download_league_csv(league, 2025, force=True))

        settled_count = 0
        for bet in self.active_bets[:]:
            if bet['status'] not in ('ACTIVE', 'PENDING'):
                continue

            # Try to find match result
            match_name = bet.get('match', '')
            for m in recent:
                home = m.get('home_team', m.get('home_team_csv', ''))
                away = m.get('away_team', m.get('away_team_csv', ''))

                if not self._match_names(match_name, home, away):
                    continue

                hs = m.get('home_score')
                as_ = m.get('away_score')
                if hs is None or as_ is None:
                    continue

                # Determine if bet won
                labels = build_market_labels(m)
                market = bet.get('market', '')
                won = self._check_bet_won(bet, labels, hs, as_)

                if won is not None:
                    bet['status'] = 'WON' if won else 'LOST'
                    bet['result_score'] = f'{hs}-{as_}'
                    bet['settled_at'] = datetime.now().isoformat()

                    if won:
                        profit = bet['stake'] * (bet['odds'] - 1)
                        bet['profit'] = profit
                        self.bankroll += bet['stake'] + profit
                        self.total_profit += profit
                    else:
                        bet['profit'] = -bet['stake']
                        self.total_profit -= bet['stake']

                    self.settled_bets.append(bet)
                    self.active_bets.remove(bet)
                    settled_count += 1
                    break

        if settled_count:
            self._save_state()
            self._sync_obsidian()
        return settled_count

    def _match_names(self, bet_match, home, away):
        """Fuzzy match between bet name and CSV team names."""
        bet_lower = bet_match.lower()
        home_words = home.lower().split()[:2]
        away_words = away.lower().split()[:2]
        return (any(w in bet_lower for w in home_words if len(w) > 3) and
                any(w in bet_lower for w in away_words if len(w) > 3))

    def _check_bet_won(self, bet, labels, hs, as_):
        """Check if a bet won based on market labels."""
        market = bet.get('market', '').lower()
        market_url = bet.get('market_url', '')

        total_goals = hs + as_

        if 'over 2.5' in market or ('total_goals/over' in market_url and 'total=2.5' in market_url):
            return total_goals > 2.5
        elif 'under 2.5' in market or ('total_goals/under' in market_url and 'total=2.5' in market_url):
            return total_goals < 2.5
        elif 'btts yes' in market or 'both_teams_to_score/yes' in market_url:
            return hs > 0 and as_ > 0
        elif 'btts no' in market or 'both_teams_to_score/no' in market_url:
            return hs == 0 or as_ == 0
        elif 'corners' in market and 'over' in market:
            return None  # Can't verify corners from score alone
        elif 'cards' in market and 'over' in market:
            return None  # Can't verify cards from score alone

        return None

    def get_report(self):
        """Generate P&L report."""
        won = [b for b in self.settled_bets if b['status'] == 'WON']
        lost = [b for b in self.settled_bets if b['status'] == 'LOST']
        total_settled = len(won) + len(lost)
        wr = len(won) / max(total_settled, 1) * 100

        total_staked = sum(b['stake'] for b in self.settled_bets)
        total_profit = sum(b.get('profit', 0) for b in self.settled_bets)
        roi = total_profit / max(total_staked, 1) * 100

        return {
            'bankroll': round(self.bankroll, 2),
            'active_bets': len(self.active_bets),
            'settled': total_settled,
            'won': len(won),
            'lost': len(lost),
            'win_rate': round(wr, 1),
            'total_staked': round(total_staked, 2),
            'total_profit': round(total_profit, 2),
            'roi': round(roi, 1),
        }

    # =================================================================
    # 3. CORRECT SCORE MARKET (Poisson)
    # =================================================================

    def get_correct_score_picks(self, home_team, away_team, max_picks=3):
        """
        Generate correct score picks from Poisson model.
        High odds (6.0-15.0) = high reward if correct.
        """
        from oraculo_models_advanced import PoissonGoalModel

        poisson = PoissonGoalModel()
        poisson_path = os.path.join(MODELS_DIR, 'poisson_state.pkl')
        if os.path.exists(poisson_path):
            with open(poisson_path, 'rb') as f:
                state = pickle.load(f)
            poisson.attack = state.get('attack', {})
            poisson.defense = state.get('defense', {})
            poisson.league_avg = state.get('league_avg', 1.35)
            poisson.home_adv = state.get('home_adv', 1.0)
            poisson._fitted = True

        if not poisson._fitted:
            return []

        probs = poisson.predict_scoreline_probs(_normalize_team(home_team), _normalize_team(away_team))
        scores = []
        for i in range(5):
            for j in range(5):
                prob = probs[i, j]
                if prob > 0.03:  # Only scores with >3% probability
                    fair_odds = 1.0 / prob
                    scores.append({
                        'score': f'{i}-{j}',
                        'prob': round(prob, 4),
                        'fair_odds': round(fair_odds, 2),
                    })

        scores.sort(key=lambda s: s['prob'], reverse=True)
        return scores[:max_picks]

    # =================================================================
    # 4. STATE PERSISTENCE
    # =================================================================

    # =================================================================
    # ASIAN HANDICAP SCANNER
    # =================================================================

    def _scan_asian_handicap(self, home, away, league, event_id, markets):
        """
        Scan AH market using Poisson model.
        Compares model-implied handicap line vs Cloudbet offered line.
        Returns list of value picks.
        """
        from oraculo_models_advanced import PoissonGoalModel

        poisson = PoissonGoalModel()
        poisson_path = os.path.join(MODELS_DIR, 'poisson_state.pkl')
        if not os.path.exists(poisson_path):
            return []
        try:
            with open(poisson_path, 'rb') as f:
                state = pickle.load(f)
            poisson.attack = state.get('attack', {})
            poisson.defense = state.get('defense', {})
            poisson.league_avg = state.get('league_avg', 1.35)
            poisson.home_adv = state.get('home_adv', 1.0)
            poisson._fitted = True
        except Exception:
            return []

        # Model expected goals
        lh, la = poisson.predict_lambda(_normalize_team(home), _normalize_team(away))
        model_margin = lh - la  # Positive = home favored

        # Scan Cloudbet AH selections
        ah_data = markets.get('soccer.asian_handicap', {})
        picks = []

        for sub_key, sub in ah_data.get('submarkets', {}).items():
            for sel in sub.get('selections', []):
                if sel.get('status') != 'SELECTION_ENABLED':
                    continue
                price = sel.get('price', 0)
                if price < 1.3 or price > 3.5:
                    continue

                outcome = sel.get('outcome', '')  # 'home' or 'away'
                params = sel.get('params', '')     # 'handicap=-0.75'
                market_url = sel.get('marketUrl', '')

                # Parse handicap value
                try:
                    hcap_str = params.split('handicap=')[1].split('&')[0]
                    handicap = float(hcap_str)
                except (IndexError, ValueError):
                    continue

                # Calculate edge
                # Model says home is model_margin goals better
                # Bookmaker offers handicap line
                # If outcome is 'home' with handicap=-0.75:
                #   Home needs to win by >0.75 goals
                #   Model prob = P(home_goals - away_goals > 0.75)
                if outcome == 'home':
                    # Home needs to overcome negative handicap
                    effective_margin = model_margin + handicap
                elif outcome == 'away':
                    effective_margin = -model_margin - handicap
                else:
                    continue

                # Convert margin to probability using normal approximation
                # Std dev of goal difference is typically ~1.3
                import math
                std_dev = 1.3
                z_score = effective_margin / std_dev
                # Cumulative normal distribution
                model_prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))

                implied_prob = 1.0 / price
                edge = model_prob - implied_prob

                if edge > 0.03 and model_prob > 0.52:
                    picks.append({
                        'match': f'{home} vs {away}',
                        'league': league,
                        'event_id': event_id,
                        'market_url': market_url,
                        'price': price,
                        'label': f'AH {outcome} {handicap:+.2f}',
                        'model_prob': model_prob,
                        'edge': edge,
                    })

        # Sort by edge, take top 2 per match
        picks.sort(key=lambda p: p['edge'], reverse=True)
        return picks[:1]  # Only best AH line per match

    def _sync_obsidian(self):
        """Sync state to Obsidian vault."""
        try:
            from oraculo_sync_obsidian import sync_predictions, sync_bet_results
            sync_bet_results()
            log.info('Synced to Obsidian')
        except Exception as e:
            log.debug('Obsidian sync skipped: %s', e)

    def _save_state(self):
        """Save engine state to JSON."""
        os.makedirs(PICKS_DIR, exist_ok=True)
        state = {
            'bankroll': self.bankroll,
            'currency': self.currency,
            'daily_staked': self.daily_staked,
            'total_profit': self.total_profit,
            'active_bets': self.active_bets,
            'settled_bets': self.settled_bets[-100:],  # Keep last 100
            'updated': datetime.now().isoformat(),
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)

    def _load_state(self):
        """Load engine state from JSON."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.bankroll = state.get('bankroll', self.bankroll)
                self.daily_staked = state.get('daily_staked', 0)
                self.total_profit = state.get('total_profit', 0)
                self.active_bets = state.get('active_bets', [])
                self.settled_bets = state.get('settled_bets', [])

                # Reset daily staked if new day
                updated = state.get('updated', '')[:10]
                if updated != datetime.now().strftime('%Y-%m-%d'):
                    self.daily_staked = 0
            except Exception:
                pass

    # =================================================================
    # 5. AUTO-RUN
    # =================================================================

    def auto_run(self, bankroll=None):
        """
        Full automated pipeline:
        1. Load models
        2. Get Cloudbet odds
        3. Generate picks
        4. Place bets
        5. Check results
        6. Report
        """
        if bankroll:
            self.bankroll = bankroll

        self.connect()

        print(f'Bankroll: ${self.bankroll:.2f} {self.currency}')
        print(f'Daily limit: ${self.bankroll * self.max_daily_pct:.2f}')
        print(f'Already staked today: ${self.daily_staked:.2f}')
        print()

        # Check previous results first
        settled = self.check_results()
        if settled:
            print(f'Settled {settled} previous bets')
            report = self.get_report()
            print(f'  P&L: ${report["total_profit"]:.2f} | WR: {report["win_rate"]}%')
            print()

        # Load ML model
        from oraculo_market_predictor import MarketPredictor
        mp = MarketPredictor('picks_global')
        if not mp.load():
            print('ERROR: No trained model. Run: python oraculo_daily_picks.py --train')
            return

        remaining = (self.bankroll * self.max_daily_pct) - self.daily_staked
        if remaining < 1:
            print('Daily limit reached. No more bets today.')
            return

        # Scan all leagues for value bets
        picks = []
        for league, comp_key in CB_COMPS.items():
            try:
                r = self.session.get(f'{CB_BASE}/pub/v2/odds/competitions/{comp_key}',
                                    timeout=10)
                if r.status_code != 200:
                    continue
                events = r.json().get('events', [])
            except Exception:
                continue

            # Markets to check for this league
            league_mkts = LEAGUE_MARKETS.get(league, ['over25'])

            for ev in events:
                if not ev or not isinstance(ev, dict):
                    continue
                home = (ev.get('home') or {}).get('name', '')
                away = (ev.get('away') or {}).get('name', '')
                eid = str(ev.get('id', ''))
                markets = ev.get('markets', {})

                # Get model predictions
                preds = self._get_predictions(mp, _normalize_team(home), _normalize_team(away), league)
                if not preds:
                    continue

                for mkt_key in league_mkts:
                    # === ASIAN HANDICAP (Poisson-based) ===
                    if mkt_key == 'asian_handicap':
                        ah_picks = self._scan_asian_handicap(
                            home, away, league, eid, markets)
                        picks.extend(ah_picks)
                        continue

                    pred = preds.get(mkt_key)
                    if not pred:
                        continue

                    cb_mkt_info = CB_MARKETS.get(mkt_key)
                    if not cb_mkt_info:
                        continue
                    cb_mkt_key, outcomes = cb_mkt_info
                    if not outcomes:
                        continue

                    mkt_data = markets.get(cb_mkt_key, {})
                    for outcome, params_filter in outcomes:
                        model_prob = (pred['prob_yes'] if outcome in ('over', 'yes')
                                     else pred['prob_no'])

                        # Find best Cloudbet price
                        best_price = None
                        best_url = ''
                        for sub in mkt_data.get('submarkets', {}).values():
                            for sel in sub.get('selections', []):
                                if sel.get('status') != 'SELECTION_ENABLED':
                                    continue
                                if sel.get('outcome') != outcome:
                                    continue
                                if params_filter and params_filter not in sel.get('params', ''):
                                    continue
                                price = sel.get('price', 0)
                                if price > 1.2 and (best_price is None or price > best_price):
                                    best_price = price
                                    best_url = sel.get('marketUrl', '')

                        if not best_price:
                            continue

                        implied = 1.0 / best_price
                        edge = model_prob - implied

                        if edge > self.min_edge and model_prob > self.min_conf:
                            picks.append({
                                'match': f'{home} vs {away}',
                                'league': league,
                                'event_id': eid,
                                'market_url': best_url,
                                'price': best_price,
                                'label': f'{mkt_key} {outcome}',
                                'model_prob': model_prob,
                                'edge': edge,
                            })

        picks.sort(key=lambda p: p['edge'], reverse=True)

        if not picks:
            print('No value bets found.')
            return

        # Calculate stakes (Kelly)
        n_bets = min(len(picks), 8)
        stake_each = min(remaining / n_bets,
                        self.bankroll * self.max_per_bet)
        stake_each = round(max(stake_each, 0.5), 2)

        print(f'Found {len(picks)} value bets, placing top {n_bets}')
        print(f'Stake: ${stake_each} each')
        print()

        placed = 0
        for p in picks[:n_bets]:
            result = self.place_bet(
                event_id=p['event_id'],
                market_url=p['market_url'],
                initial_price=p['price'],
                stake=stake_each,
                match_name=p['match'],
                market_label=p['label'],
            )
            if result:
                placed += 1
                edge_pct = p['edge'] * 100
                print(f'  [OK] {p["match"][:35]:35s} | {p["label"]:20s} '
                      f'@ {result["odds"]:.2f} | ${stake_each} | edge +{edge_pct:.1f}%')
            else:
                print(f'  [XX] {p["match"][:35]:35s} | {p["label"]:20s} | FAILED')
            time.sleep(0.5)

        print(f'\nPlaced: {placed}/{n_bets}')
        print(f'Total staked today: ${self.daily_staked:.2f}')
        print(f'Bankroll: ${self.bankroll:.2f}')

        # Sync to Obsidian
        self._sync_obsidian()

        # Correct score bonus picks
        if picks:
            top = picks[0]
            match_parts = top['match'].split(' vs ')
            if len(match_parts) == 2:
                cs_picks = self.get_correct_score_picks(match_parts[0].strip(),
                                                        match_parts[1].strip())
                if cs_picks:
                    print(f'\nCorrect Score picks for {top["match"]}:')
                    for cs in cs_picks:
                        print(f'  {cs["score"]:5s} prob={cs["prob"]*100:.1f}% '
                              f'fair_odds={cs["fair_odds"]:.1f}')

    def _get_predictions(self, mp, home, away, league):
        """Get ML predictions for a match."""
        from oraculo_football_features import build_match_features, features_to_vector
        from oraculo_football_csv import compute_team_stats, download_league_csv, LEAGUE_MAP
        from oraculo_xg_weather import compute_team_xg_stats, load_xg_data, _ORG_TO_US
        from oraculo_models_advanced import EloRating, PoissonGoalModel

        LEAGUE_MAP['ELC'] = 'E1'
        LEAGUE_MAP['DED'] = 'N1'
        LEAGUE_MAP['PPL'] = 'P1'

        # Quick context from cached CSV
        csv_matches = []
        for season in [2024, 2025]:
            csv_matches.extend(download_league_csv(league, season))
        csv_matches.sort(key=lambda m: m.get('utc_date', ''))
        context = csv_matches[-100:]

        if len(context) < 5:
            return None

        # Load math models
        elo = EloRating()
        poisson = PoissonGoalModel()
        elo_path = os.path.join(MODELS_DIR, 'elo_state.pkl')
        poisson_path = os.path.join(MODELS_DIR, 'poisson_state.pkl')
        if os.path.exists(elo_path):
            with open(elo_path, 'rb') as f:
                state = pickle.load(f)
            elo.ratings.update(state.get('ratings', {}))
        if os.path.exists(poisson_path):
            with open(poisson_path, 'rb') as f:
                state = pickle.load(f)
            poisson.attack = state.get('attack', {})
            poisson.defense = state.get('defense', {})
            poisson.league_avg = state.get('league_avg', 1.35)
            poisson.home_adv = state.get('home_adv', 1.0)
            poisson._fitted = True

        xg_data = load_xg_data(leagues=[league], seasons=['2023', '2024'])

        match = {'home_team': home, 'away_team': away,
                 'competition_code': league, 'utc_date': datetime.now().isoformat()}

        home_recent = [m for m in context
                      if m.get('home_team') == home or m.get('away_team') == home][:10]
        away_recent = [m for m in context
                      if m.get('home_team') == away or m.get('away_team') == away][:10]
        csv_stats = {
            'home': compute_team_stats(context, home, n=10),
            'away': compute_team_stats(context, away, n=10),
        }

        try:
            features = build_match_features(match, home_recent, away_recent,
                                            None, None, None, csv_stats=csv_stats)
            features['elo_home'] = elo.ratings[home]
            features['elo_away'] = elo.ratings[away]
            features['elo_diff'] = elo.ratings[home] - elo.ratings[away]
            features['elo_expected_home'] = elo.expected_score(home, away)

            if poisson._fitted:
                lh, la = poisson.predict_lambda(home, away)
                features['poisson_lambda_home'] = lh
                features['poisson_lambda_away'] = la
                p_mkts = poisson.predict_markets(home, away)
                features['poisson_over25'] = p_mkts['over25']
                features['poisson_btts'] = p_mkts['btts_yes']

            for prefix, xg_name in [('home', _ORG_TO_US.get(home, home)),
                                    ('away', _ORG_TO_US.get(away, away))]:
                xg = compute_team_xg_stats(xg_data, xg_name, n=10)
                features[f'{prefix}_xg_for_avg'] = xg['xg_for_avg']
                features[f'{prefix}_xg_against_avg'] = xg['xg_against_avg']
                features[f'{prefix}_xg_diff'] = xg['xg_diff_avg']
                features[f'{prefix}_xg_over25_rate'] = xg['xg_over25_rate']
                features[f'{prefix}_overperform'] = xg['overperform_avg']
            features['xg_total_predict'] = features['home_xg_for_avg'] + features['away_xg_for_avg']
            features['xg_diff'] = features['home_xg_diff'] - features['away_xg_diff']

            for wf in ['weather_temp', 'weather_rain', 'weather_wind',
                       'weather_humidity', 'weather_is_rainy',
                       'weather_is_windy', 'weather_is_cold']:
                features.setdefault(wf, 0.0)

            vec = features_to_vector(features)
            return mp.predict_all(vec)
        except Exception:
            return None


# =================================================================
# CLI
# =================================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    import argparse
    parser = argparse.ArgumentParser(description='Oraculo Bet Engine')
    parser.add_argument('command', choices=['run', 'results', 'report', 'reset'],
                       help='run=find+place bets, results=check scores, report=P&L')
    parser.add_argument('--bankroll', type=float, default=67.0)
    parser.add_argument('--currency', default='USDC')
    parser.add_argument('--min-edge', type=float, default=0.03)
    args = parser.parse_args()

    engine = BetEngine(bankroll=args.bankroll, currency=args.currency,
                      min_edge=args.min_edge)

    if args.command == 'run':
        engine.auto_run()

    elif args.command == 'results':
        engine.connect()
        settled = engine.check_results()
        print(f'Settled: {settled} bets')
        report = engine.get_report()
        print(f'Active: {report["active_bets"]}')
        print(f'Won: {report["won"]} | Lost: {report["lost"]} | WR: {report["win_rate"]}%')
        print(f'Profit: ${report["total_profit"]:.2f} | ROI: {report["roi"]}%')
        print(f'Bankroll: ${report["bankroll"]:.2f}')

    elif args.command == 'report':
        report = engine.get_report()
        print(json.dumps(report, indent=2))

    elif args.command == 'reset':
        engine.bankroll = args.bankroll
        engine.active_bets = []
        engine.settled_bets = []
        engine.daily_staked = 0
        engine.total_profit = 0
        engine._save_state()
        print(f'Reset to ${args.bankroll}')
