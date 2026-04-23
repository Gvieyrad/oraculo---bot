"""
Pinnacle API adapter for Oraculo betting system.
Drop-in replacement for CloudbetAPI — same interface, translates to Pinnacle's REST API.

Pinnacle docs: https://pinnacleapi.github.io/
Auth: HTTP Basic (username:password)
Markets: SPREAD (asian handicap), TOTAL_POINTS (over/under), MONEYLINE (winner)

Usage:
    from oraculo_pinnacle import PinnacleAPI, PN_COMPS, PN_TENNIS_LEAGUES
    api = PinnacleAPI()   # reads pinnacle_config.json
    events = api.get_odds('soccer-england-premier-league')  # Cloudbet-compatible format
    resp = api.place_straight(event_id, market_url, price, stake)
"""
import json, os, time, uuid, logging, requests
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PN_BASE = 'https://api.pinnacle.com'

# Sport IDs
SPORT_SOCCER = 29
SPORT_TENNIS = 33
SPORT_BASKETBALL = 4

# ---------------------------------------------------------------------------
# Pinnacle league IDs — mapped from Cloudbet comp keys
# These need to be verified/updated via api.get_leagues() on first run
# ---------------------------------------------------------------------------
PN_LEAGUE_IDS = {
    # Soccer
    'PL':   1980,   # England - Premier League
    'PD':   2196,   # Spain - La Liga
    'SA':   2436,   # Italy - Serie A
    'BL1':  1842,   # Germany - Bundesliga
    'FL1':  2036,   # France - Ligue 1
    'DED':  2003,   # Netherlands - Eredivisie
    'PPL':  2462,   # Portugal - Primeira Liga
    'ELC':  1977,   # England - Championship
    'MLS':  2663,   # USA - MLS
    'LMX':  2560,   # Mexico - Liga MX
    'ARG':  2541,   # Argentina - Primera Division
    'BRA':  2551,   # Brazil - Serie A
    'ESP2': 2198,   # Spain - Segunda Division
    'BRA2': 2553,   # Brazil - Serie B
    'COL':  2570,   # Colombia - Primera A
    'CHL':  2576,   # Chile - Primera Division
    'URU':  2580,   # Uruguay - Primera Division
}

# Cloudbet comp_key -> our league key (reverse mapping for get_odds compatibility)
CB_KEY_TO_LEAGUE = {
    'soccer-england-premier-league': 'PL',
    'soccer-spain-la-liga': 'PD',
    'soccer-italy-serie-a': 'SA',
    'soccer-germany-bundesliga': 'BL1',
    'soccer-france-ligue-1': 'FL1',
    'soccer-netherlands-eredivisie': 'DED',
    'soccer-portugal-primeira-liga': 'PPL',
    'soccer-england-championship': 'ELC',
    'soccer-usa-mls': 'MLS',
    'soccer-mexico-liga-mx': 'LMX',
    'soccer-argentina-primera-division': 'ARG',
    'soccer-brazil-serie-a': 'BRA',
    'soccer-spain-segunda-division': 'ESP2',
    'soccer-brazil-serie-b': 'BRA2',
    'soccer-colombia-primera-a': 'COL',
    'soccer-chile-primera-division': 'CHL',
    'soccer-uruguay-primera-division': 'URU',
}

# International competitions — Pinnacle league IDs
PN_INTL_LEAGUES = {
    'FIFA_WC':       7593,   # FIFA World Cup Qualifiers
    'UEFA_NL':       6375,   # UEFA Nations League
    'CHAMPIONS':     1928,   # UEFA Champions League
    'EUROPA_L':      2627,   # UEFA Europa League
    'CONF_L':        9447,   # UEFA Conference League
    'LIBERTADORES':  2585,   # Copa Libertadores
    'SUDAMERICANA':  2586,   # Copa Sudamericana
}


# ---------------------------------------------------------------------------
# Market URL translation: Cloudbet marketUrl <-> Pinnacle betType+params
#
# Cloudbet format:  "soccer.asian_handicap/home?handicap=-0.75"
# Pinnacle format:  betType=SPREAD, team=Team1, handicap=-0.75
#
# We keep using Cloudbet-style marketUrl strings internally so the scanning
# code and pick dicts don't need changes. This class translates at placement.
# ---------------------------------------------------------------------------

def _parse_market_url(market_url):
    """Parse Cloudbet-style marketUrl into Pinnacle params.
    Returns dict with keys: bet_type, team, handicap, total, period
    """
    result = {'bet_type': None, 'team': None, 'handicap': None,
              'total': None, 'period': 0}  # period 0 = full match

    if not market_url:
        return result

    # Parse: "soccer.asian_handicap/home?handicap=-0.75"
    base = market_url.split('?')[0]
    params_str = market_url.split('?')[1] if '?' in market_url else ''
    params = {}
    for p in params_str.split('&'):
        if '=' in p:
            k, v = p.split('=', 1)
            params[k] = v

    if 'asian_handicap' in base:
        result['bet_type'] = 'SPREAD'
        if '/home' in base:
            result['team'] = 'Team1'
        elif '/away' in base:
            result['team'] = 'Team2'
        result['handicap'] = float(params.get('handicap', 0))

    elif 'total_goals' in base or 'total_corners' in base:
        result['bet_type'] = 'TOTAL_POINTS'
        if '/over' in base or base.endswith('/over'):
            result['team'] = 'Over'
        elif '/under' in base or base.endswith('/under'):
            result['team'] = 'Under'
        # Also check outcome in base path
        if 'over' in market_url.split('/')[-1]:
            result['team'] = 'Over'
        elif 'under' in market_url.split('/')[-1]:
            result['team'] = 'Under'
        result['total'] = float(params.get('total', 2.5))
        if 'first_half' in base:
            result['period'] = 1

    elif 'match_odds' in base or 'winner' in base or 'moneyline' in base:
        result['bet_type'] = 'MONEYLINE'
        if '/home' in base:
            result['team'] = 'Team1'
        elif '/away' in base:
            result['team'] = 'Team2'
        elif '/draw' in base:
            result['team'] = 'Draw'

    elif 'total_sets' in base:
        result['bet_type'] = 'TOTAL_POINTS'
        if 'over' in base:
            result['team'] = 'Over'
        elif 'under' in base:
            result['team'] = 'Under'
        result['total'] = float(params.get('total', 2.5))

    return result


def _build_market_url(bet_type, outcome, **kw):
    """Build a Cloudbet-compatible marketUrl from Pinnacle data.
    Used when scanning odds to create pick dicts the runner understands.
    """
    handicap = kw.get('handicap')
    total = kw.get('total')
    sport = kw.get('sport', 'soccer')

    if bet_type == 'SPREAD':
        side = 'home' if outcome in ('Team1', 'home') else 'away'
        return f'{sport}.asian_handicap/{side}?handicap={handicap}'

    elif bet_type == 'TOTAL_POINTS':
        side = 'over' if outcome in ('Over', 'over') else 'under'
        if sport == 'tennis':
            return f'tennis.total_sets/{side}?total={total}'
        return f'{sport}.total_goals/{side}?total={total}'

    elif bet_type == 'MONEYLINE':
        side = 'home' if outcome in ('Team1', 'home') else 'away'
        if sport == 'tennis':
            return f'tennis.winner/{side}'
        return f'{sport}.match_odds/{side}'

    return f'{sport}.unknown/{outcome}'


# ---------------------------------------------------------------------------
# Pinnacle API Client
# ---------------------------------------------------------------------------
class PinnacleAPI:
    """Drop-in replacement for CloudbetAPI.
    Same interface: get_odds(), place_straight(), place_parlay(), get_bets().
    """

    def __init__(self, config_path=None):
        cfg_path = config_path or os.path.join(SCRIPT_DIR, 'pinnacle_config.json')
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.username = cfg['username']
        self.password = cfg['password']
        self.currency = cfg.get('currency', 'USD')

        self.session = requests.Session()
        self.session.auth = (self.username, self.password)
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        self._last_call = 0
        self._league_cache = {}   # league_key -> league_id (auto-discovered)
        self._fixture_cache = {}  # (sport_id, league_id) -> {event_id: fixture}
        self._odds_since = {}     # (sport_id, league_id) -> since_timestamp

    def _throttle(self):
        """Respect Pinnacle's ~1 req/sec rate limit."""
        elapsed = time.time() - self._last_call
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        self._last_call = time.time()

    def _get(self, path, params=None):
        self._throttle()
        try:
            r = self.session.get(f'{PN_BASE}{path}', params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                log.warning('Pinnacle rate limited, backing off 5s')
                time.sleep(5)
                return None
            else:
                log.debug('Pinnacle GET %s -> HTTP %d', path, r.status_code)
                return None
        except Exception as e:
            log.error('Pinnacle GET %s error: %s', path, e)
            return None

    def _post(self, path, data):
        self._throttle()
        try:
            r = self.session.post(f'{PN_BASE}{path}', json=data, timeout=15)
            if r.status_code in (200, 201):
                return r.json()
            log.warning('Pinnacle POST %s -> HTTP %d: %s',
                        path, r.status_code, r.text[:300] if r.text else '')
            return None
        except Exception as e:
            log.error('Pinnacle POST %s error: %s', path, e)
            return None

    # ------------------------------------------------------------------
    # Discovery: leagues and competitions
    # ------------------------------------------------------------------
    def get_leagues(self, sport_id=SPORT_SOCCER):
        """Get all leagues for a sport. Returns list of {id, name}."""
        data = self._get(f'/v2/leagues', params={'sportId': sport_id})
        if data and isinstance(data, list):
            return data
        return data.get('leagues', []) if data else []

    def discover_league_ids(self, sport_id=SPORT_SOCCER):
        """Auto-discover Pinnacle league IDs and cache them.
        Call once on startup to verify hardcoded IDs are still valid.
        """
        leagues = self.get_leagues(sport_id)
        found = {}
        for lg in leagues:
            lid = lg.get('id')
            name = lg.get('name', '').lower()
            found[lid] = lg.get('name', '')
        return found

    def discover_tennis_leagues(self):
        """Discover active tennis leagues/tournaments."""
        leagues = self.get_leagues(SPORT_TENNIS)
        active = []
        for lg in leagues:
            ev_count = lg.get('eventCount', lg.get('events', 0))
            if ev_count and ev_count > 0:
                active.append({
                    'id': lg['id'],
                    'name': lg.get('name', ''),
                    'events': ev_count,
                })
        return active

    # ------------------------------------------------------------------
    # get_odds() — Cloudbet-compatible interface
    # Returns list of event dicts with same structure as Cloudbet:
    #   {id, home: {name}, away: {name}, cutoffTime, markets: {...}}
    # ------------------------------------------------------------------
    def get_odds(self, comp_key):
        """Fetch events+odds for a competition.
        Accepts Cloudbet comp_key (e.g. 'soccer-england-premier-league')
        or our league key (e.g. 'PL').
        Returns Cloudbet-format events list.
        """
        # Resolve league
        league_key = CB_KEY_TO_LEAGUE.get(comp_key, comp_key)
        sport_id = SPORT_SOCCER

        # Check if it's a tennis comp
        if comp_key.startswith('tennis-') or league_key.startswith('tennis-'):
            sport_id = SPORT_TENNIS
            league_id = self._resolve_tennis_league(comp_key)
        elif league_key in PN_INTL_LEAGUES:
            league_id = PN_INTL_LEAGUES[league_key]
        elif league_key in PN_LEAGUE_IDS:
            league_id = PN_LEAGUE_IDS[league_key]
        else:
            log.debug('Unknown comp_key: %s', comp_key)
            return []

        if not league_id:
            return []

        # Fetch fixtures
        fixtures = self._get(f'/v1/fixtures',
                             params={'sportId': sport_id, 'leagueIds': league_id})
        if not fixtures:
            return []

        fixture_list = []
        for lg in fixtures.get('league', []):
            for ev in lg.get('events', []):
                fixture_list.append(ev)

        if not fixture_list:
            return []

        # Fetch odds
        odds_data = self._get(f'/v1/odds',
                              params={'sportId': sport_id, 'leagueIds': league_id})

        # Build odds lookup: event_id -> {periods}
        odds_map = {}
        if odds_data:
            for lg in odds_data.get('leagues', []):
                for ev in lg.get('events', []):
                    odds_map[ev.get('id')] = ev.get('periods', [])

        # Convert to Cloudbet-compatible format
        events = []
        for fix in fixture_list:
            eid = fix.get('id')
            starts = fix.get('starts', '')
            home = fix.get('home', '')
            away = fix.get('away', '')

            if not home or not away:
                continue

            # Build markets dict from Pinnacle odds
            markets = {}
            periods = odds_map.get(eid, [])
            sport_prefix = 'tennis' if sport_id == SPORT_TENNIS else 'soccer'

            for period in periods:
                pnum = period.get('number', 0)  # 0=full match, 1=1st half/set
                if pnum != 0:
                    continue  # Only full match for now

                # SPREAD (Asian Handicap)
                spreads = period.get('spreads', [])
                if spreads:
                    ah_sels = []
                    for sp in spreads:
                        hdp = sp.get('hdp', 0)
                        home_price = sp.get('home', 0)
                        away_price = sp.get('away', 0)
                        if home_price and home_price > 1.0:
                            murl = _build_market_url('SPREAD', 'home',
                                                     handicap=hdp, sport=sport_prefix)
                            ah_sels.append({
                                'outcome': 'home',
                                'price': home_price,
                                'marketUrl': murl,
                                'status': 'SELECTION_ENABLED',
                                'params': f'handicap={hdp}',
                            })
                        if away_price and away_price > 1.0:
                            murl = _build_market_url('SPREAD', 'away',
                                                     handicap=-hdp, sport=sport_prefix)
                            ah_sels.append({
                                'outcome': 'away',
                                'price': away_price,
                                'marketUrl': murl,
                                'status': 'SELECTION_ENABLED',
                                'params': f'handicap={-hdp}',
                            })
                    if ah_sels:
                        markets[f'{sport_prefix}.asian_handicap'] = {
                            'submarkets': {'main': {'selections': ah_sels}}
                        }

                # TOTAL_POINTS (Over/Under)
                totals = period.get('totals', [])
                if totals:
                    ou_sels = []
                    for tot in totals:
                        points = tot.get('points', 0)
                        over_price = tot.get('over', 0)
                        under_price = tot.get('under', 0)
                        if over_price and over_price > 1.0:
                            murl = _build_market_url('TOTAL_POINTS', 'over',
                                                     total=points, sport=sport_prefix)
                            ou_sels.append({
                                'outcome': 'over',
                                'price': over_price,
                                'marketUrl': murl,
                                'status': 'SELECTION_ENABLED',
                                'params': f'total={points}',
                            })
                        if under_price and under_price > 1.0:
                            murl = _build_market_url('TOTAL_POINTS', 'under',
                                                     total=points, sport=sport_prefix)
                            ou_sels.append({
                                'outcome': 'under',
                                'price': under_price,
                                'marketUrl': murl,
                                'status': 'SELECTION_ENABLED',
                                'params': f'total={points}',
                            })
                    if ou_sels:
                        total_key = f'{sport_prefix}.total_goals' if sport_id == SPORT_SOCCER else 'tennis.total_sets'
                        markets[total_key] = {
                            'submarkets': {'main': {'selections': ou_sels}}
                        }

                # MONEYLINE (Match Winner)
                ml = period.get('moneyline', {})
                if ml:
                    ml_sels = []
                    home_price = ml.get('home', 0)
                    away_price = ml.get('away', 0)
                    draw_price = ml.get('draw', 0)
                    if home_price and home_price > 1.0:
                        murl = _build_market_url('MONEYLINE', 'home', sport=sport_prefix)
                        ml_sels.append({
                            'outcome': 'home',
                            'price': home_price,
                            'marketUrl': murl,
                            'status': 'SELECTION_ENABLED',
                        })
                    if away_price and away_price > 1.0:
                        murl = _build_market_url('MONEYLINE', 'away', sport=sport_prefix)
                        ml_sels.append({
                            'outcome': 'away',
                            'price': away_price,
                            'marketUrl': murl,
                            'status': 'SELECTION_ENABLED',
                        })
                    if draw_price and draw_price > 1.0:
                        murl = _build_market_url('MONEYLINE', 'draw', sport=sport_prefix)
                        ml_sels.append({
                            'outcome': 'draw',
                            'price': draw_price,
                            'marketUrl': murl,
                            'status': 'SELECTION_ENABLED',
                        })
                    if ml_sels:
                        winner_key = 'tennis.winner' if sport_id == SPORT_TENNIS else 'soccer.match_odds'
                        # Also add as tennis.match_odds for compatibility
                        markets[winner_key] = {
                            'submarkets': {'main': {'selections': ml_sels}}
                        }
                        if sport_id == SPORT_TENNIS:
                            markets['tennis.match_odds'] = markets[winner_key]

            events.append({
                'id': str(eid),
                'home': {'name': home},
                'away': {'name': away},
                'cutoffTime': starts,
                'markets': markets,
                '_pinnacle_id': eid,  # Keep original ID for placement
            })

        return events

    def _resolve_tennis_league(self, comp_key):
        """Try to find a Pinnacle league ID for a Cloudbet tennis comp key.
        Uses cached discovery or keyword matching.
        """
        # Cache tennis leagues
        if not hasattr(self, '_tennis_leagues_cache'):
            self._tennis_leagues_cache = {}
            try:
                leagues = self.get_leagues(SPORT_TENNIS)
                for lg in leagues:
                    self._tennis_leagues_cache[lg.get('id')] = lg.get('name', '')
            except Exception:
                pass

        # Extract keywords from comp_key
        # e.g. "tennis-atp-french-open-men-singles" -> ["atp", "french", "open", "men", "singles"]
        parts = comp_key.replace('tennis-', '').split('-')
        keywords = [p.lower() for p in parts if len(p) > 2
                    and p not in ('men', 'women', 'singles', 'qual', 'atp', 'wta', 'challenger')]
        tour = 'atp' if 'atp' in comp_key.lower() else ('wta' if 'wta' in comp_key.lower() else '')

        best_id = None
        best_score = 0

        for lid, name in self._tennis_leagues_cache.items():
            name_lower = name.lower()
            # Must match tour (ATP/WTA)
            if tour and tour not in name_lower:
                continue
            score = sum(1 for kw in keywords if kw in name_lower)
            if score > best_score:
                best_score = score
                best_id = lid

        if best_score >= 2:
            return best_id

        log.debug('Could not resolve tennis league for %s (best_score=%d)', comp_key, best_score)
        return None

    # ------------------------------------------------------------------
    # place_straight() — Cloudbet-compatible interface
    # ------------------------------------------------------------------
    def place_straight(self, event_id, market_url, price, stake):
        """Place single bet. Returns Cloudbet-compatible response dict or None."""
        parsed = _parse_market_url(market_url)
        if not parsed['bet_type']:
            log.warning('Cannot parse market_url: %s', market_url)
            return None

        # Step 1: Get current line (required by Pinnacle)
        sport_id = SPORT_TENNIS if 'tennis' in market_url else SPORT_SOCCER
        line_params = {
            'sportId': sport_id,
            'eventId': int(event_id),
            'periodNumber': parsed.get('period', 0),
            'betType': parsed['bet_type'],
        }
        if parsed['bet_type'] == 'SPREAD':
            line_params['team'] = parsed['team']
            line_params['handicap'] = parsed['handicap']
        elif parsed['bet_type'] == 'TOTAL_POINTS':
            line_params['side'] = parsed['team']  # Over/Under
            line_params['total'] = parsed['total']
        elif parsed['bet_type'] == 'MONEYLINE':
            line_params['team'] = parsed['team']

        for attempt in range(3):
            backoff = 2 ** (attempt + 1)

            line = self._get('/v2/line', params=line_params)
            if not line:
                log.warning('  Line fetch failed, attempt %d, backoff %ds', attempt + 1, backoff)
                time.sleep(backoff)
                continue

            if line.get('status') != 'SUCCESS':
                log.warning('  Line not available: %s', line.get('status', '?'))
                return None

            line_id = line.get('lineId')
            current_price = line.get('price', price)
            min_stake = line.get('minRiskStake', 1.0)
            max_stake = line.get('maxRiskStake', 1000.0)

            # Check limits
            if stake < min_stake:
                log.warning('  Stake $%.2f below min $%.2f', stake, min_stake)
                return None
            if stake > max_stake:
                log.info('  Stake $%.2f exceeds max $%.2f, capping', stake, max_stake)
                stake = max_stake

            # Step 2: Place bet
            payload = {
                'uniqueRequestId': str(uuid.uuid4()),
                'acceptBetterLine': True,
                'oddsFormat': 'DECIMAL',
                'stake': round(stake, 2),
                'sportId': sport_id,
                'eventId': int(event_id),
                'periodNumber': parsed.get('period', 0),
                'betType': parsed['bet_type'],
                'lineId': line_id,
            }

            if parsed['bet_type'] == 'SPREAD':
                payload['team'] = parsed['team']
                payload['handicap'] = parsed['handicap']
            elif parsed['bet_type'] == 'TOTAL_POINTS':
                payload['side'] = parsed['team']
                payload['total'] = parsed['total']
            elif parsed['bet_type'] == 'MONEYLINE':
                payload['team'] = parsed['team']

            resp = self._post('/v2/bets/straight', payload)

            if not resp:
                log.warning('  Empty response, attempt %d, backoff %ds', attempt + 1, backoff)
                time.sleep(backoff)
                continue

            status = resp.get('status', resp.get('straightBetStatus', ''))

            if status == 'ACCEPTED':
                bet_id = resp.get('betId', resp.get('uniqueRequestId', str(uuid.uuid4())))
                log.info('  Bet ACCEPTED: %s @%.3f', str(bet_id)[:12], current_price)
                return {
                    'betId': str(bet_id),
                    'state': 'ACCEPTED',
                    'stake': stake,
                    'price': current_price,
                    'status': 'ACCEPTED',
                }

            elif status == 'PROCESSED_WITH_ERROR':
                error_code = resp.get('errorCode', '')
                log.warning('  Bet error: %s', error_code)
                if error_code in ('LINE_CHANGED', 'ODDS_CHANGED'):
                    log.info('  Line changed, retrying...')
                    time.sleep(1.5)
                    continue
                return None

            elif status == 'NOT_ACCEPTED':
                log.warning('  Bet NOT_ACCEPTED: %s', resp.get('errorCode', '?'))
                return None

            else:
                log.warning('  Unexpected status: %s | resp: %s', status, str(resp)[:400])
                time.sleep(backoff)

        log.warning('  All 3 attempts exhausted for %s', market_url[:40])
        return None

    # ------------------------------------------------------------------
    # place_parlay() — Cloudbet-compatible interface
    # ------------------------------------------------------------------
    def place_parlay(self, selections, stake):
        """Place multi-leg parlay.
        selections: [{eventId, marketUrl, price}, ...]
        Returns Cloudbet-compatible response dict or None.
        """
        legs = []
        for sel in selections:
            parsed = _parse_market_url(sel.get('marketUrl', ''))
            if not parsed['bet_type']:
                log.warning('Cannot parse parlay leg: %s', sel.get('marketUrl', ''))
                return None

            sport_id = SPORT_TENNIS if 'tennis' in sel.get('marketUrl', '') else SPORT_SOCCER
            leg = {
                'sportId': sport_id,
                'eventId': int(sel['eventId']),
                'periodNumber': parsed.get('period', 0),
                'betType': parsed['bet_type'],
                'uniqueLegId': str(uuid.uuid4()),
            }
            if parsed['bet_type'] == 'SPREAD':
                leg['team'] = parsed['team']
                leg['handicap'] = parsed['handicap']
                # Need lineId — fetch line for each leg
                line = self._get('/v2/line', params={
                    'sportId': sport_id,
                    'eventId': int(sel['eventId']),
                    'periodNumber': 0,
                    'betType': 'SPREAD',
                    'team': parsed['team'],
                    'handicap': parsed['handicap'],
                })
                if line and line.get('lineId'):
                    leg['lineId'] = line['lineId']
            elif parsed['bet_type'] == 'TOTAL_POINTS':
                leg['side'] = parsed['team']
                leg['total'] = parsed['total']
                line = self._get('/v2/line', params={
                    'sportId': sport_id,
                    'eventId': int(sel['eventId']),
                    'periodNumber': 0,
                    'betType': 'TOTAL_POINTS',
                    'side': parsed['team'],
                    'total': parsed['total'],
                })
                if line and line.get('lineId'):
                    leg['lineId'] = line['lineId']
            elif parsed['bet_type'] == 'MONEYLINE':
                leg['team'] = parsed['team']
                line = self._get('/v2/line', params={
                    'sportId': sport_id,
                    'eventId': int(sel['eventId']),
                    'periodNumber': 0,
                    'betType': 'MONEYLINE',
                    'team': parsed['team'],
                })
                if line and line.get('lineId'):
                    leg['lineId'] = line['lineId']

            legs.append(leg)

        payload = {
            'uniqueRequestId': str(uuid.uuid4()),
            'acceptBetterLine': True,
            'oddsFormat': 'DECIMAL',
            'riskAmount': round(stake, 2),
            'legs': legs,
        }

        resp = self._post('/v2/bets/parlay', payload)
        if not resp:
            return None

        status = resp.get('status', '')
        if status == 'ACCEPTED':
            bet_id = resp.get('betId', str(uuid.uuid4()))
            log.info('  Parlay ACCEPTED: %s', str(bet_id)[:12])
            return {
                'betId': str(bet_id),
                'state': 'ACCEPTED',
                'stake': stake,
                'status': 'ACCEPTED',
            }

        log.warning('Parlay rejected: %s', resp.get('errorCode', status))
        return None

    # ------------------------------------------------------------------
    # get_bets() — Cloudbet-compatible interface
    # ------------------------------------------------------------------
    def get_bets(self, settled_only=False, limit=50, days_back=30):
        """Get bet history. Returns Cloudbet-format list of bets."""
        from_dt = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%dT00:00:00Z')
        to_dt = datetime.utcnow().strftime('%Y-%m-%dT23:59:59Z')

        statuses = 'SETTLED' if settled_only else 'SETTLED,PENDING,ACCEPTED'
        params = {
            'betStatuses': statuses,
            'fromDate': from_dt,
            'toDate': to_dt,
            'pageSize': limit,
        }

        # Try straight bets
        straight = self._get('/v3/bets', params={**params, 'betType': 'STRAIGHT'})
        parlay = self._get('/v3/bets', params={**params, 'betType': 'PARLAY'})

        results = []

        for bets_data in [straight, parlay]:
            if not bets_data:
                continue
            items = bets_data if isinstance(bets_data, list) else bets_data.get('straightBets', bets_data.get('parlayBets', []))
            for b in items:
                # Convert to Cloudbet format
                status = b.get('betStatus', '')
                is_settled = status in ('WON', 'LOST', 'CANCELLED', 'REFUNDED', 'HALF_WON_HALF_PUSHED',
                                        'HALF_LOST_HALF_PUSHED')

                # Map Pinnacle result to Cloudbet result
                result_map = {
                    'WON': 'WIN',
                    'LOST': 'LOSS',
                    'CANCELLED': 'VOID',
                    'REFUNDED': 'VOID',
                    'HALF_WON_HALF_PUSHED': 'HALF_WIN',
                    'HALF_LOST_HALF_PUSHED': 'HALF_LOSS',
                }

                win_amount = float(b.get('win', 0))
                risk_amount = float(b.get('risk', b.get('stake', 0)))

                if status == 'WON':
                    win_loss = win_amount
                elif status == 'LOST':
                    win_loss = 0  # Pinnacle reports 0 for losses
                elif status in ('HALF_WON_HALF_PUSHED',):
                    win_loss = win_amount
                else:
                    win_loss = 0

                results.append({
                    'betId': str(b.get('betId', '')),
                    'isSettled': is_settled,
                    'result': result_map.get(status, status),
                    'stake': risk_amount,
                    'winLoss': win_loss,
                    'price': b.get('price', 0),
                    'eventId': str(b.get('eventId', '')),
                    '_pinnacle_status': status,
                })

        return results[:limit]

    # ------------------------------------------------------------------
    # get_balance() — bonus method
    # ------------------------------------------------------------------
    def get_balance(self):
        """Get account balance."""
        data = self._get('/v1/client/balance')
        if data:
            return {
                'availableBalance': data.get('availableBalance', 0),
                'outstandingTransactions': data.get('outstandingTransactions', 0),
                'givenCredit': data.get('givenCredit', 0),
                'currency': data.get('currency', 'USD'),
            }
        return None


# ---------------------------------------------------------------------------
# Config template generator
# ---------------------------------------------------------------------------
def create_config_template(path=None):
    """Create a pinnacle_config.json template."""
    path = path or os.path.join(SCRIPT_DIR, 'pinnacle_config.json')
    template = {
        'username': 'YOUR_PINNACLE_USERNAME',
        'password': 'YOUR_PINNACLE_PASSWORD',
        'currency': 'USD',
        '_comment': 'Register at pinnacle.com, fund account, then fill in credentials',
        '_setup': [
            '1. Create account at pinnacle.com (Peru is accepted)',
            '2. Complete KYC with Peruvian documents',
            '3. Deposit USDT (or BTC) to fund account',
            '4. Fill in username and password here',
            '5. In oraculo_runner_auto.py, change: api = PinnacleAPI()',
        ],
    }
    with open(path, 'w') as f:
        json.dump(template, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Integration helper: swap Cloudbet -> Pinnacle in runner
# ---------------------------------------------------------------------------
RUNNER_PATCH_INSTRUCTIONS = """
To switch oraculo_runner_auto.py from Cloudbet to Pinnacle:

1. Add import at top:
   from oraculo_pinnacle import PinnacleAPI

2. In main() or wherever CloudbetAPI() is instantiated, change:
   # api = CloudbetAPI()
   api = PinnacleAPI()

3. The rest of the code (scanning, placing, checking results) works unchanged
   because PinnacleAPI returns Cloudbet-compatible data structures.

4. Create pinnacle_config.json with your credentials.

5. Tennis auto-discovery: Pinnacle uses numeric league IDs instead of
   Cloudbet's string keys. The adapter auto-discovers tennis tournaments
   by keyword matching. Verify coverage in logs.

6. CB_BASE is no longer used. The adapter connects to api.pinnacle.com.

7. Rate limit: Pinnacle allows ~1 req/s (vs Cloudbet's ~2 req/s).
   The adapter handles this automatically.

Note: get_bets() returns Cloudbet-format dicts so check_results() and
reconcile_bankroll() work unchanged.
"""


if __name__ == '__main__':
    # Quick test / discovery mode
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'discover':
        api = PinnacleAPI()
        print('=== BALANCE ===')
        print(json.dumps(api.get_balance(), indent=2))
        print('\n=== SOCCER LEAGUES ===')
        leagues = api.get_leagues(SPORT_SOCCER)
        for lg in sorted(leagues, key=lambda x: x.get('name', '')):
            print(f"  {lg.get('id'):>6}  {lg.get('name', '?')}")
        print(f'\n=== TENNIS LEAGUES (active) ===')
        tl = api.discover_tennis_leagues()
        for t in sorted(tl, key=lambda x: -x['events']):
            print(f"  {t['id']:>6}  {t['name']}  ({t['events']} events)")
    elif len(sys.argv) > 1 and sys.argv[1] == 'test-odds':
        api = PinnacleAPI()
        events = api.get_odds('soccer-england-premier-league')
        print(f'Premier League: {len(events)} events')
        for ev in events[:3]:
            h = ev['home']['name']
            a = ev['away']['name']
            mkts = list(ev['markets'].keys())
            print(f'  {h} vs {a} | markets: {mkts}')
            for mk, md in ev['markets'].items():
                for sub in md.get('submarkets', {}).values():
                    for sel in sub.get('selections', [])[:4]:
                        print(f'    {mk} | {sel["outcome"]} @{sel["price"]} | {sel.get("marketUrl","")}')
    elif len(sys.argv) > 1 and sys.argv[1] == 'template':
        p = create_config_template()
        print(f'Config template created: {p}')
    else:
        print('Usage: python oraculo_pinnacle.py [discover|test-odds|template]')
        print(RUNNER_PATCH_INSTRUCTIONS)
