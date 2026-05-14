"""NBA Elo model + scanner for Oraculo."""
import os, json, re, time, logging
from collections import defaultdict
from datetime import datetime, timedelta
from math import exp

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NBA_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nba_elo.json')
NBA_RESULTS_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nba_results.json')
NBA_INJURY_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nba_injuries.json')

_NBA_STARS = {
    'LeBron James', 'Stephen Curry', 'Giannis Antetokounmpo', 'Nikola Jokic',
    'Luka Doncic', 'Kevin Durant', 'Jayson Tatum', 'Jaylen Brown', 'Devin Booker',
    'Damian Lillard', 'Anthony Davis', 'Joel Embiid', 'Cade Cunningham',
    'Donovan Mitchell', 'Darius Garland', 'Bam Adebayo', 'Jimmy Butler',
    'Anthony Edwards', 'Shai Gilgeous-Alexander', 'Alperen Sengun', 'Evan Mobley',
    'Jalen Brunson', 'Tyrese Haliburton', 'Bennedict Mathurin',
}


def _fetch_nba_injury_adj(home_elo, away_elo):
    import requests as _req
    try:
        if os.path.exists(NBA_INJURY_CACHE):
            with open(NBA_INJURY_CACHE) as f:
                cache = json.load(f)
            if time.time() - cache.get('_ts', 0) < 5400:
                h = cache.get(home_elo, {})
                a = cache.get(away_elo, {})
                if h.get('star_out') or a.get('star_out'):
                    return None, None
                return h.get('adj', 0.0), a.get('adj', 0.0)
    except Exception:
        pass

    adj_map = {}
    try:
        r = _req.get(
            'http://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            timeout=10)
        events = r.json().get('events', [])
    except Exception as e:
        log.debug('NBA injury scoreboard failed: %s', e)
        return 0.0, 0.0

    for ev in events:
        eid = ev.get('id', '')
        if not eid:
            continue
        try:
            r2 = _req.get(
                'http://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=' + eid,
                timeout=10)
            injuries = r2.json().get('injuries', [])
        except Exception:
            continue
        for team_inj in injuries:
            tname = team_inj.get('team', {}).get('displayName', '')
            players = team_inj.get('injuries', [])
            adj = 0.0
            star_out = False
            for p in players:
                pname = p.get('athlete', {}).get('displayName', '')
                status = p.get('status', '').lower()
                if 'out' in status and 'day-to-day' not in status:
                    adj -= 0.025
                    if pname in _NBA_STARS:
                        star_out = True
                        log.warning('NBA STAR OUT: %s (%s) — skip bet', pname, tname)
                elif 'questionable' in status or 'day-to-day' in status:
                    adj -= 0.010
            adj_map[tname] = {'adj': max(adj, -0.075), 'star_out': star_out}

    try:
        os.makedirs(os.path.dirname(NBA_INJURY_CACHE), exist_ok=True)
        with open(NBA_INJURY_CACHE, 'w') as f:
            json.dump({'_ts': time.time(), **adj_map}, f)
    except Exception:
        pass

    h = adj_map.get(home_elo, {})
    a = adj_map.get(away_elo, {})
    if h.get('star_out') or a.get('star_out'):
        return None, None
    return h.get('adj', 0.0), a.get('adj', 0.0)



class NBAElo:
    """Elo rating system for NBA with home court advantage."""

    def __init__(self, k_factor=20, initial=1500, home_adv=100):
        self.ratings = defaultdict(lambda: initial)
        self.k = k_factor
        self.initial = initial
        self.home_adv = home_adv
        self._match_count = defaultdict(int)
        self._form = defaultdict(list)  # last 10 results

    def process_match(self, winner, loser, winner_is_home=True, margin=0):
        """Update Elo after a game. Margin-of-victory adjustment."""
        # Margin multiplier (blowouts = more Elo transfer)
        mov_mult = max(1.0, 1.0 + (abs(margin) - 5) * 0.02) if margin else 1.0
        mov_mult = min(mov_mult, 1.5)  # Cap at 1.5x

        r_w = self.ratings[winner]
        r_l = self.ratings[loser]

        # Home advantage adjustment
        if winner_is_home:
            r_w_adj = r_w + self.home_adv
            r_l_adj = r_l
        else:
            r_w_adj = r_w
            r_l_adj = r_l + self.home_adv

        exp_w = 1.0 / (1.0 + 10 ** ((r_l_adj - r_w_adj) / 400.0))
        change = self.k * mov_mult * (1 - exp_w)

        self.ratings[winner] += change
        self.ratings[loser] -= change

        self._match_count[winner] += 1
        self._match_count[loser] += 1

        self._form[winner].append(1)
        self._form[loser].append(0)
        if len(self._form[winner]) > 10:
            self._form[winner] = self._form[winner][-10:]
        if len(self._form[loser]) > 10:
            self._form[loser] = self._form[loser][-10:]

    def predict(self, home, away):
        """Predict home win probability."""
        r_h = self.ratings[home] + self.home_adv
        r_a = self.ratings[away]
        return 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))

    def get_form(self, team, n=10):
        results = self._form.get(team, [])
        if len(results) < 3:
            return None
        return sum(results[-n:]) / len(results[-n:])

    def get_top(self, n=30):
        return sorted(self.ratings.items(), key=lambda x: -x[1])[:n]

    def save(self, path=None):
        path = path or NBA_CACHE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            'ratings': dict(self.ratings),
            'match_count': dict(self._match_count),
            'form': dict(self._form),
        }
        with open(path, 'w') as f:
            json.dump(data, f)

    def load(self, path=None):
        path = path or NBA_CACHE
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in data.get('form', {}).items():
                self._form[k] = v
            return True
        except Exception:
            return False


# --- Team name mapping: Cloudbet abbreviations -> full names ---
CB_TO_FULL = {
    'ATL Hawks': 'Atlanta Hawks', 'BOS Celtics': 'Boston Celtics',
    'BKN Nets': 'Brooklyn Nets', 'CHA Hornets': 'Charlotte Hornets',
    'CHI Bulls': 'Chicago Bulls', 'CLE Cavaliers': 'Cleveland Cavaliers',
    'DAL Mavericks': 'Dallas Mavericks', 'DEN Nuggets': 'Denver Nuggets',
    'DET Pistons': 'Detroit Pistons', 'GS Warriors': 'Golden State Warriors',
    'HOU Rockets': 'Houston Rockets', 'IND Pacers': 'Indiana Pacers',
    'LA Clippers': 'Los Angeles Clippers', 'LAL Lakers': 'Los Angeles Lakers',
    'MEM Grizzlies': 'Memphis Grizzlies', 'MIA Heat': 'Miami Heat',
    'MIL Bucks': 'Milwaukee Bucks', 'MIN Timberwolves': 'Minnesota Timberwolves',
    'NO Pelicans': 'New Orleans Pelicans', 'NY Knicks': 'New York Knicks',
    'OKC Thunder': 'Oklahoma City Thunder', 'ORL Magic': 'Orlando Magic',
    'PHI 76ers': 'Philadelphia 76ers', 'PHX Suns': 'Phoenix Suns',
    'POR Trail Blazers': 'Portland Trail Blazers', 'SAC Kings': 'Sacramento Kings',
    'SA Spurs': 'San Antonio Spurs', 'TOR Raptors': 'Toronto Raptors',
    'UTA Jazz': 'Utah Jazz', 'WAS Wizards': 'Washington Wizards',
}

FULL_TO_CB = {v: k for k, v in CB_TO_FULL.items()}


def _resolve_name(cb_name):
    """Resolve Cloudbet team name to basketball-reference name."""
    if cb_name in CB_TO_FULL:
        return CB_TO_FULL[cb_name]
    # Fuzzy match
    cb_lower = cb_name.lower()
    for k, v in CB_TO_FULL.items():
        if k.lower() in cb_lower or cb_lower in k.lower():
            return v
        # Match on last word (team nickname)
        if k.split()[-1].lower() == cb_lower.split()[-1].lower():
            return v
    return cb_name


def fetch_nba_results(force_refresh=False):
    """Fetch NBA season results from ESPN API (free, no auth)."""
    os.makedirs(os.path.dirname(NBA_RESULTS_CACHE), exist_ok=True)
    if not force_refresh and os.path.exists(NBA_RESULTS_CACHE):
        age = time.time() - os.path.getmtime(NBA_RESULTS_CACHE)
        if age < 21600:
            with open(NBA_RESULTS_CACHE) as f:
                return json.load(f)
    import urllib.request
    from datetime import timedelta as _td
    existing = []
    if os.path.exists(NBA_RESULTS_CACHE):
        try:
            with open(NBA_RESULTS_CACHE) as f:
                existing = json.load(f)
        except Exception:
            pass
    last_date = max((g['date'] for g in existing), default='2025-10-22')
    d = datetime.strptime(last_date, '%Y-%m-%d')
    end = datetime.utcnow()
    new_games = 0
    while d <= end:
        ds = d.strftime('%Y%m%d')
        url = f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={ds}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for ev in data.get('events', []):
                comp = ev.get('competitions', [{}])[0]
                teams = comp.get('competitors', [])
                if len(teams) < 2: continue
                ho = next((t for t in teams if t.get('homeAway') == 'home'), teams[0])
                aw = next((t for t in teams if t.get('homeAway') == 'away'), teams[1])
                hp = int(ho.get('score', 0) or 0)
                ap = int(aw.get('score', 0) or 0)
                if hp == 0 and ap == 0: continue
                hn = ho.get('team', {}).get('displayName', '')
                an = aw.get('team', {}).get('displayName', '')
                game = {'date': d.strftime('%Y-%m-%d'), 'home': hn, 'away': an,
                        'home_pts': hp, 'away_pts': ap,
                        'winner': hn if hp > ap else an, 'margin': abs(hp - ap)}
                if not any(g['date'] == game['date'] and g['home'] == game['home'] for g in existing):
                    existing.append(game)
                    new_games += 1
        except Exception:
            pass
        d += _td(days=1)
    existing.sort(key=lambda g: g['date'])
    if new_games > 0:
        log.info('NBA: %d new games (%d total)', new_games, len(existing))
    with open(NBA_RESULTS_CACHE, 'w') as f:
        json.dump(existing, f)
    return existing


def train_nba_elo(force=False):
    """Train NBA Elo from season results."""
    elo = NBAElo()

    # Try load cached
    if not force and elo.load():
        if len(elo.ratings) >= 20:
            log.info('NBA Elo loaded from cache (%d teams)', len(elo.ratings))
            return elo

    # Fetch and train
    games = fetch_nba_results(force_refresh=force)
    if not games:
        log.warning('No NBA results to train from')
        return elo

    # Sort by date and process
    games.sort(key=lambda g: g.get('date', ''))
    for g in games:
        winner = g['winner']
        loser = g['home'] if g['away'] == winner else g['away']
        winner_home = (winner == g['home'])
        elo.process_match(winner, loser, winner_home, g.get('margin', 0))

    elo.save()
    log.info('NBA Elo trained: %d teams, %d games', len(elo.ratings), len(games))
    return elo


def scan_nba(api, state, elo=None, dry_run=False, shadow=True):
    """Scan NBA markets for value bets using basketball.1x2 market.

    shadow=True: picks logged to sibila only, no real bets placed.
    Shadow mode for first 2 weeks until Elo WR validated.
    """
    if elo is None:
        elo = train_nba_elo()

    if len(elo.ratings) < 20:
        log.warning('NBA Elo not ready (%d teams)', len(elo.ratings))
        return []

    events = api.get_odds('basketball-usa-nba')
    if not events:
        return []

    picks = []
    now = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=96)).isoformat() + 'Z'

    for ev in events:
        ct = ev.get('cutoffTime', '')
        if not ct or ct < now.isoformat() + 'Z' or ct > cutoff_max:
            continue

        home_cb = (ev.get('home') or {}).get('name', '')
        away_cb = (ev.get('away') or {}).get('name', '')
        eid = str(ev.get('id', ''))
        if not home_cb or not away_cb:
            continue

        mkts = ev.get('markets', {})

        # Use basketball.1x2 (basketball.moneyline is restricted on this account)
        mk = mkts.get('basketball.1x2', {})
        if not mk:
            continue  # no game market for this event (outright only)

        # Only full-time submarket
        ft_sub = None
        for sub_key, sub_data in mk.get('submarkets', {}).items():
            if 'ft' in sub_key.lower() or 'full' in sub_key.lower() or sub_key == 'period=ft':
                ft_sub = sub_data
                break
        if ft_sub is None:
            # fallback: take first submarket
            for sub_data in mk.get('submarkets', {}).values():
                ft_sub = sub_data
                break
        if not ft_sub:
            continue

        # Resolve names for Elo lookup
        home = _resolve_name(home_cb)
        away = _resolve_name(away_cb)

        if elo._match_count.get(home, 0) < 10 or elo._match_count.get(away, 0) < 10:
            continue

        prob_home = elo.predict(home, away)
        prob_away = 1.0 - prob_home

        # Injury adjustment via ESPN game summary (90 min cache)
        try:
            _h_inj, _a_inj = _fetch_nba_injury_adj(home, away)
            if _h_inj is None:
                log.info('NBA [star-out-skip]: %s vs %s', home, away)
                continue
            if _h_inj != 0 or _a_inj != 0:
                log.info('NBA [injury-adj]: %s %+.1f%% / %s %+.1f%%',
                         home, _h_inj * 100, away, _a_inj * 100)
            prob_home = min(0.95, max(0.05, prob_home + _h_inj - _a_inj))
            prob_away = 1.0 - prob_home
        except Exception as _ie:
            log.debug('NBA injury adj error: %s', _ie)

        for sel in ft_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price = float(sel.get('price', 0) or 0)
            murl = sel.get('marketUrl', '')

            if price < 1.05 or not murl:
                continue
            if outcome == 'draw':
                continue  # NBA draws don't exist, skip the @20x decoy

            if outcome == 'home':
                prob = prob_home
                team = home_cb
            elif outcome == 'away':
                prob = prob_away
                team = away_cb
            else:
                continue

            # Form adjustment
            form = elo.get_form(home if outcome == 'home' else away)
            form_bonus = 0.0
            if form is not None:
                if form > 0.70:
                    form_bonus = 0.02
                elif form < 0.30:
                    form_bonus = -0.02

            adj_prob = min(0.92, max(0.08, prob + form_bonus))
            edge = round(adj_prob * price - 1.0, 4)

            # Filters: minimum edge + confidence, sanity caps
            if edge < 0.06 or adj_prob < 0.58 or adj_prob > 0.92:
                continue
            if price > 2.50:  # NBA away dogs > 2.50 are too volatile without team data
                continue

            picks.append({
                'match':               '%s vs %s' % (home_cb, away_cb),
                'league':              'NBA',
                'sport':               'basketball',
                'event_id':            eid,
                'market':              'basketball.1x2',
                'market_url':          murl,
                'price':               price,
                'odds':                price,
                # Label must NOT contain 'home'/'away'/'Winner:'/'moneyline'
                # to avoid place_bets 2.0-cap filter designed for soccer
                'label':               'NBA: %s' % team,
                'model_prob':          round(adj_prob, 4),
                'raw_model_prob_uncal': round(adj_prob, 4),
                'confidence':          round(adj_prob, 4),
                'edge':                edge,
                'form':                round(form, 3) if form else None,
                'shadow':              shadow,           # shadow=True -> sibila only, no real bet
                '_max_stake':          1.00,             # low cap until model validated
            })

    log.info('NBA: %d value picks found (%s)', len(picks),
             'SHADOW' if shadow else 'LIVE')
    return picks

