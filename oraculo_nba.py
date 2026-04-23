"""NBA Elo model + scanner for Oraculo."""
import os, json, re, time, logging
from collections import defaultdict
from datetime import datetime, timedelta
from math import exp

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NBA_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nba_elo.json')
NBA_RESULTS_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nba_results.json')


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


def scan_nba(api, state, elo=None, dry_run=False):
    """Scan NBA markets for value bets."""
    if elo is None:
        elo = train_nba_elo()

    if len(elo.ratings) < 20:
        return []

    events = api.get_odds('basketball-usa-nba')
    if not events:
        return []

    picks = []
    now = datetime.utcnow()
    cutoff = (now + timedelta(hours=96)).isoformat() + 'Z'

    for ev in events:
        ct = ev.get('cutoffTime', '')
        if ct > cutoff or ct < now.isoformat():
            continue

        home_cb = (ev.get('home') or {}).get('name', '')
        away_cb = (ev.get('away') or {}).get('name', '')
        eid = str(ev.get('id', ''))
        if not home_cb or not away_cb:
            continue

        # Resolve names
        home = _resolve_name(home_cb)
        away = _resolve_name(away_cb)

        # Predict
        prob_home = elo.predict(home, away)
        prob_away = 1 - prob_home

        # Check match counts
        if elo._match_count.get(home, 0) < 10 or elo._match_count.get(away, 0) < 10:
            continue

        # Get odds
        markets = ev.get('markets', {})
        for mk_name in ['basketball.winner', 'basketball.moneyline', 'match_odds']:
            mk = markets.get(mk_name, {})
            if mk:
                break
        if not mk:
            for k, v in markets.items():
                if 'winner' in k.lower() or 'moneyline' in k.lower():
                    mk = v
                    break

        for sv in mk.get('submarkets', {}).values():
            for sel in sv.get('selections', []):
                outcome = sel.get('outcome', '')
                price = float(sel.get('price', 0) or 0)
                murl = sel.get('marketUrl', '')
                if price < 1.1 or not murl:
                    continue

                prob = prob_home if outcome == 'home' else prob_away
                player = home_cb if outcome == 'home' else away_cb
                edge = prob * price - 1

                # Form bonus
                form = elo.get_form(home if outcome == 'home' else away)
                form_bonus = 0
                if form is not None:
                    if form > 0.7:
                        form_bonus = 0.02
                    elif form < 0.3:
                        form_bonus = -0.02

                adj_prob = min(0.95, max(0.05, prob + form_bonus))
                adj_edge = adj_prob * price - 1

                if adj_edge > 0.05 and adj_prob > 0.55 and adj_edge < 0.45 and adj_prob < 0.92:  # sanity cap
                    picks.append({
                        'match': f'{home_cb} vs {away_cb}',
                        'league': 'NBA',
                        'event_id': eid,
                        'market_url': murl,
                        'price': price,
                        'label': f'Winner: {player}',
                        'model_prob': adj_prob,
                        'edge': adj_edge,
                        'sport': 'basketball',
                        'form': form,
                    })

    log.info('NBA: %d value picks found', len(picks))
    return picks
