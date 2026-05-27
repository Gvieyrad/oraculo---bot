"""NHL ELO model + scanner for Oraculo. Shadow mode until 40+ picks validated.

Data sources:
- ESPN unofficial API for results (no auth)
- Cloudbet: hockey-usa-nhl, hockey.1x2 market

Activates automatically in October when NHL regular season starts.
Shadow mode until 40+ picks accumulated at WR > 53% (breakeven ~1.88).
"""
import os, json, time, logging, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
NHL_ELO_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nhl_elo.json')
NHL_RES_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nhl_results.json')

# Cloudbet display name → ESPN full name
CB_TO_NHL = {
    'Anaheim Ducks':        'Anaheim Ducks',
    'Arizona Coyotes':      'Utah Hockey Club',   # relocated 2024
    'Utah HC':              'Utah Hockey Club',
    'Boston Bruins':        'Boston Bruins',
    'Buffalo Sabres':       'Buffalo Sabres',
    'Calgary Flames':       'Calgary Flames',
    'Carolina Hurricanes':  'Carolina Hurricanes',
    'Chicago Blackhawks':   'Chicago Blackhawks',
    'Colorado Avalanche':   'Colorado Avalanche',
    'Columbus Blue Jackets':'Columbus Blue Jackets',
    'Dallas Stars':         'Dallas Stars',
    'Detroit Red Wings':    'Detroit Red Wings',
    'Edmonton Oilers':      'Edmonton Oilers',
    'Florida Panthers':     'Florida Panthers',
    'Los Angeles Kings':    'Los Angeles Kings',
    'Minnesota Wild':       'Minnesota Wild',
    'Montreal Canadiens':   'Montreal Canadiens',
    'Nashville Predators':  'Nashville Predators',
    'New Jersey Devils':    'New Jersey Devils',
    'New York Islanders':   'New York Islanders',
    'New York Rangers':     'New York Rangers',
    'Ottawa Senators':      'Ottawa Senators',
    'Philadelphia Flyers':  'Philadelphia Flyers',
    'Pittsburgh Penguins':  'Pittsburgh Penguins',
    'San Jose Sharks':      'San Jose Sharks',
    'Seattle Kraken':       'Seattle Kraken',
    'St. Louis Blues':      'St. Louis Blues',
    'Tampa Bay Lightning':  'Tampa Bay Lightning',
    'Toronto Maple Leafs':  'Toronto Maple Leafs',
    'Vancouver Canucks':    'Vancouver Canucks',
    'Vegas Golden Knights': 'Vegas Golden Knights',
    'Washington Capitals':  'Washington Capitals',
    'Winnipeg Jets':        'Winnipeg Jets',
}

NHL_FULL_TO_CB = {v: k for k, v in CB_TO_NHL.items()}


def _resolve_name(cb_name: str) -> str:
    if cb_name in CB_TO_NHL:
        return CB_TO_NHL[cb_name]
    # Fuzzy: last word (team nickname)
    last = cb_name.split()[-1].lower()
    for full in CB_TO_NHL.values():
        if full.split()[-1].lower() == last:
            return full
    return cb_name


def _sim(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    wa, wb = set(a.split()), set(b.split())
    if wa & wb:
        return 0.6
    return 0.0


class NHLElo:
    """Elo rating system for NHL. K=20, MoV multiplier, home advantage."""

    def __init__(self, k=20, initial=1500, home_adv=50):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)
        self._form        = defaultdict(list)

    def process_match(self, winner: str, loser: str, winner_home: bool, gd: int = 0):
        # NHL goal differential multiplier (softer than basketball — most games are 1-2 goal diff)
        mov = min(1.4, max(1.0, 1.0 + max(0, gd - 1) * 0.08))
        r_w, r_l = self.ratings[winner], self.ratings[loser]
        r_w_adj  = r_w + (self.home_adv if winner_home else 0)
        r_l_adj  = r_l + (0 if winner_home else self.home_adv)
        exp_w    = 1.0 / (1.0 + 10 ** ((r_l_adj - r_w_adj) / 400.0))
        change   = self.k * mov * (1 - exp_w)
        self.ratings[winner] += change
        self.ratings[loser]  -= change
        for team, result in ((winner, 1), (loser, 0)):
            self._match_count[team] += 1
            self._form[team] = (self._form[team] + [result])[-10:]

    def predict(self, home: str, away: str) -> float:
        r_h = self.ratings[home] + self.home_adv
        r_a = self.ratings[away]
        return 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))

    def form(self, team: str, n: int = 10):
        recent = self._form.get(team, [])
        return sum(recent[-n:]) / len(recent[-n:]) if len(recent) >= 3 else None

    def save(self):
        os.makedirs(os.path.dirname(NHL_ELO_CACHE), exist_ok=True)
        with open(NHL_ELO_CACHE, 'w') as f:
            json.dump({
                'ratings':     dict(self.ratings),
                'match_count': dict(self._match_count),
                'form':        dict(self._form),
            }, f)

    def load(self) -> bool:
        if not os.path.exists(NHL_ELO_CACHE):
            return False
        try:
            data = json.load(open(NHL_ELO_CACHE))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in data.get('form', {}).items():
                self._form[k] = v
            return True
        except Exception:
            return False


def fetch_nhl_results(force: bool = False) -> list:
    """Fetch NHL results from ESPN (free). Incremental from 2023-10-01."""
    os.makedirs(os.path.dirname(NHL_RES_CACHE), exist_ok=True)
    existing = []
    if os.path.exists(NHL_RES_CACHE):
        try:
            existing = json.load(open(NHL_RES_CACHE))
        except Exception:
            pass
        if not force:
            age = time.time() - os.path.getmtime(NHL_RES_CACHE)
            if age < 21600 and existing:
                return existing

    # Pull from 2023 season start for 2+ seasons of Elo history
    last = max((g['date'] for g in existing), default='2023-10-01')
    d    = datetime.strptime(last, '%Y-%m-%d')
    end  = datetime.utcnow()
    new  = 0
    while d <= end:
        ds  = d.strftime('%Y%m%d')
        url = f'https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={ds}'
        try:
            req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            for ev in data.get('events', []):
                comp  = ev.get('competitions', [{}])[0]
                status = comp.get('status', {}).get('type', {}).get('name', '')
                if status not in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
                    continue
                teams = comp.get('competitors', [])
                if len(teams) < 2:
                    continue
                ho = next((t for t in teams if t.get('homeAway') == 'home'), teams[0])
                aw = next((t for t in teams if t.get('homeAway') == 'away'), teams[1])
                hp = int(ho.get('score', 0) or 0)
                ap = int(aw.get('score', 0) or 0)
                if hp == 0 and ap == 0:
                    continue
                hn = ho.get('team', {}).get('displayName', '')
                an = aw.get('team', {}).get('displayName', '')
                game = {
                    'date':     d.strftime('%Y-%m-%d'),
                    'home':     hn, 'away': an,
                    'home_pts': hp, 'away_pts': ap,
                    'winner':   hn if hp > ap else an,
                    'gd':       abs(hp - ap),
                }
                if not any(g['date'] == game['date'] and g['home'] == game['home'] for g in existing):
                    existing.append(game)
                    new += 1
        except Exception:
            pass
        d += timedelta(days=1)

    existing.sort(key=lambda g: g['date'])
    if new:
        log.info('NHL: %d new games fetched (%d total)', new, len(existing))
    with open(NHL_RES_CACHE, 'w') as f:
        json.dump(existing, f)
    return existing


def train_nhl_elo(force: bool = False) -> NHLElo:
    """Train NHL Elo from ESPN results. Uses cache unless forced."""
    elo = NHLElo()
    if not force and elo.load() and len(elo.ratings) >= 28:
        log.info('NHL Elo loaded from cache (%d teams)', len(elo.ratings))
        return elo

    games = fetch_nhl_results(force=force)
    if not games:
        log.warning('NHL: no results to train from')
        return elo

    for g in sorted(games, key=lambda x: x['date']):
        winner = g['winner']
        loser  = g['home'] if g['away'] == winner else g['away']
        elo.process_match(winner, loser, winner == g['home'], g.get('gd', 0))

    elo.save()
    log.info('NHL Elo trained: %d teams, %d games', len(elo.ratings), len(games))
    return elo


def scan_nhl(api, state, elo: NHLElo = None, dry_run: bool = False, shadow: bool = True) -> list:
    """Scan NHL markets for value bets via hockey.1x2.

    shadow=True: logs to Sibila only, no real stake.
    Switch to False after 40+ picks with WR > 53% (breakeven at avg ~1.88).
    """
    if elo is None:
        elo = train_nhl_elo()

    if len(elo.ratings) < 28:
        log.warning('NHL Elo not ready (%d teams) — need at least one season', len(elo.ratings))
        return []

    events = api.get_odds('hockey-usa-nhl')
    if not events:
        return []

    now        = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=48)).isoformat() + 'Z'
    picks      = []

    for ev in events:
        if not ev or ev.get('type') == 'EVENT_TYPE_OUTRIGHT':
            continue
        ct = ev.get('cutoffTime', '')
        if not ct or ct < now.isoformat() + 'Z' or ct > cutoff_max:
            continue

        home_cb = (ev.get('home') or {}).get('name', '')
        away_cb = (ev.get('away') or {}).get('name', '')
        eid     = str(ev.get('id', ''))
        if not home_cb or not away_cb or not eid:
            continue

        mk = ev.get('markets', {}).get('hockey.1x2', {})
        if not mk:
            # Some books use hockey.moneyline
            mk = ev.get('markets', {}).get('hockey.moneyline', {})
        if not mk:
            continue

        # Full-time submarket
        ft_sub = None
        for sk, sv in mk.get('submarkets', {}).items():
            if 'ft' in sk.lower() or sk in ('period=ft', 'main'):
                ft_sub = sv
                break
        if ft_sub is None:
            for sv in mk.get('submarkets', {}).values():
                ft_sub = sv
                break
        if not ft_sub:
            continue

        home = _resolve_name(home_cb)
        away = _resolve_name(away_cb)

        if elo._match_count.get(home, 0) < 10 or elo._match_count.get(away, 0) < 10:
            log.debug('NHL [skip-low-data]: %s (%d) vs %s (%d)',
                      home, elo._match_count.get(home, 0),
                      away, elo._match_count.get(away, 0))
            continue

        prob_home = elo.predict(home, away)
        prob_away = 1.0 - prob_home

        for sel in ft_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price   = float(sel.get('price', 0) or 0)
            murl    = sel.get('marketUrl', '')

            if price < 1.10 or not murl or outcome == 'draw':
                continue
            if price > 2.60:
                continue

            prob = prob_home if outcome == 'home' else prob_away
            team = home_cb if outcome == 'home' else away_cb

            # Form adjustment ±2%
            f    = elo.form(home if outcome == 'home' else away)
            prob = min(0.92, max(0.08, prob + (0.02 if f and f > 0.70 else -0.02 if f and f < 0.30 else 0.0)))

            edge = round(prob * price - 1.0, 4)
            if edge < 0.06 or prob < 0.55:
                continue

            picks.append({
                'match':                f'{home_cb} vs {away_cb}',
                'league':               'hockey-usa-nhl',
                'sport':                'hockey',
                'event_id':             eid,
                'market':               'hockey.1x2',
                'market_url':           murl,
                'price':                price,
                'odds':                 price,
                'label':                f'NHL: {team}',
                'side':                 f'NHL: {team}',
                'model_prob':           round(prob, 4),
                'raw_model_prob_uncal': round(prob, 4),
                'confidence':           round(prob, 4),
                'edge':                 edge,
                'shadow':               shadow,
                '_max_stake':           1.00,
            })

    log.info('NHL: %d value picks (%s)', len(picks), 'SHADOW' if shadow else 'LIVE')
    return picks
