"""WNBA Elo model + scanner for Oraculo. Shadow mode until 20+ picks validated."""
import os, json, time, logging
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
WNBA_ELO_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'wnba_elo.json')
WNBA_RES_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'wnba_results.json')

# Cloudbet display name → ESPN full name
CB_TO_WNBA = {
    'NY Liberty':           'New York Liberty',
    'PHX Mercury':          'Phoenix Mercury',
    'PHX Mercury (w)':      'Phoenix Mercury',
    'LV Aces':              'Las Vegas Aces',
    'LV Aces (w)':          'Las Vegas Aces',
    'CHI Sky':              'Chicago Sky',
    'CHI Sky (w)':          'Chicago Sky',
    'SEA Storm':            'Seattle Storm',
    'SEA Storm (w)':        'Seattle Storm',
    'MIN Lynx':             'Minnesota Lynx',
    'MIN Lynx (w)':         'Minnesota Lynx',
    'DAL Wings':            'Dallas Wings',
    'DAL Wings (w)':        'Dallas Wings',
    'ATL Dream':            'Atlanta Dream',
    'ATL Dream (w)':        'Atlanta Dream',
    'IND Fever':            'Indiana Fever',
    'IND Fever (w)':        'Indiana Fever',
    'WAS Mystics':          'Washington Mystics',
    'WAS Mystics (w)':      'Washington Mystics',
    'LA Sparks':            'Los Angeles Sparks',
    'LA Sparks (w)':        'Los Angeles Sparks',
    'GS Valkyries':         'Golden State Valkyries',
    'GS Valkyries (w)':     'Golden State Valkyries',
    'Portland Fire':        'Portland Fire',
    'Portland Fire (w)':    'Portland Fire',
    'Toronto Tempo':        'Toronto Tempo',
    'Toronto Tempo (w)':    'Toronto Tempo',
}

WNBA_FULL_TO_CB = {v: k for k, v in CB_TO_WNBA.items() if '(w)' not in k}


def _resolve_name(cb_name: str) -> str:
    if cb_name in CB_TO_WNBA:
        return CB_TO_WNBA[cb_name]
    # Strip trailing " (w)"
    clean = cb_name.replace(' (w)', '').strip()
    if clean in CB_TO_WNBA:
        return CB_TO_WNBA[clean]
    # Fuzzy: match on last word (team nickname)
    cb_last = cb_name.split()[-1].lower().rstrip(')')
    for full in CB_TO_WNBA.values():
        if full.split()[-1].lower() == cb_last:
            return full
    return clean


class WNBAElo:
    """Elo rating system for WNBA. Identical mechanics to NBAElo."""

    def __init__(self, k=20, initial=1500, home_adv=75):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)
        self._form        = defaultdict(list)

    def process_match(self, winner: str, loser: str, winner_home: bool, margin: int = 0):
        mov = min(1.5, max(1.0, 1.0 + (abs(margin) - 5) * 0.02))
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
        os.makedirs(os.path.dirname(WNBA_ELO_CACHE), exist_ok=True)
        with open(WNBA_ELO_CACHE, 'w') as f:
            json.dump({
                'ratings':     dict(self.ratings),
                'match_count': dict(self._match_count),
                'form':        dict(self._form),
            }, f)

    def load(self) -> bool:
        if not os.path.exists(WNBA_ELO_CACHE):
            return False
        try:
            data = json.load(open(WNBA_ELO_CACHE))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in data.get('form', {}).items():
                self._form[k] = v
            return True
        except Exception:
            return False


def fetch_wnba_results(force: bool = False) -> list:
    """Fetch WNBA season results from ESPN (free, no auth). Incremental."""
    os.makedirs(os.path.dirname(WNBA_RES_CACHE), exist_ok=True)
    existing = []
    if os.path.exists(WNBA_RES_CACHE):
        try:
            existing = json.load(open(WNBA_RES_CACHE))
        except Exception:
            pass
        if not force:
            age = time.time() - os.path.getmtime(WNBA_RES_CACHE)
            if age < 21600 and existing:
                return existing

    import urllib.request
    # WNBA season typically starts mid-May. Pull from 2024 for Elo history.
    last = max((g['date'] for g in existing), default='2024-05-14')
    d    = datetime.strptime(last, '%Y-%m-%d')
    end  = datetime.utcnow()
    new  = 0
    while d <= end:
        ds  = d.strftime('%Y%m%d')
        url = f'https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={ds}'
        try:
            req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            for ev in data.get('events', []):
                comp  = ev.get('competitions', [{}])[0]
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
                    'date':   d.strftime('%Y-%m-%d'),
                    'home':   hn, 'away': an,
                    'home_pts': hp, 'away_pts': ap,
                    'winner': hn if hp > ap else an,
                    'margin': abs(hp - ap),
                }
                key = (game['date'], game['home'])
                if not any(g['date'] == game['date'] and g['home'] == game['home'] for g in existing):
                    existing.append(game)
                    new += 1
        except Exception:
            pass
        d += timedelta(days=1)

    existing.sort(key=lambda g: g['date'])
    if new:
        log.info('WNBA: %d new games fetched (%d total)', new, len(existing))
    with open(WNBA_RES_CACHE, 'w') as f:
        json.dump(existing, f)
    return existing


def train_wnba_elo(force: bool = False) -> WNBAElo:
    """Train WNBA Elo from ESPN results. Uses cache unless forced or stale."""
    elo = WNBAElo()
    # 2026-07-13: ported fix from oraculo_v2 -- cache had no TTL, froze ratings
    # indefinitely after first save. Match the 6h TTL fetch_wnba_results() uses.
    cache_age = (time.time() - os.path.getmtime(WNBA_ELO_CACHE)) if os.path.exists(WNBA_ELO_CACHE) else 1e18
    if not force and cache_age < 21600 and elo.load() and len(elo.ratings) >= 8:
        log.info('WNBA Elo loaded from cache (%d teams)', len(elo.ratings))
        return elo

    games = fetch_wnba_results(force=force)
    if not games:
        log.warning('WNBA: no results to train from')
        return elo

    for g in sorted(games, key=lambda x: x['date']):
        winner = g['winner']
        loser  = g['home'] if g['away'] == winner else g['away']
        elo.process_match(winner, loser, winner == g['home'], g.get('margin', 0))

    elo.save()
    log.info('WNBA Elo trained: %d teams, %d games', len(elo.ratings), len(games))
    return elo


def scan_wnba(api, state, elo: WNBAElo = None, dry_run: bool = False, shadow: bool = True) -> list:
    """Scan WNBA markets for value bets via basketball.1x2.

    shadow=True: logs to Sibila only, no real stake placed.
    Switch to False after 20+ picks with WR > 55% at odds <= 1.90 (Jun-04 pattern).
    """
    if elo is None:
        elo = train_wnba_elo()

    if len(elo.ratings) < 8:
        log.warning('WNBA Elo not ready (%d teams) — need at least one season', len(elo.ratings))
        return []

    events = api.get_odds('basketball-usa-wnba')
    if not events:
        return []

    now       = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=48)).isoformat() + 'Z'
    picks     = []

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

        mk = ev.get('markets', {}).get('basketball.1x2', {})
        if not mk:
            continue

        # Full-time submarket only
        ft_sub = None
        for sk, sv in mk.get('submarkets', {}).items():
            if 'ft' in sk.lower() or sk == 'period=ft':
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

        # Require at least 5 games (early season has few)
        if elo._match_count.get(home, 0) < 5 or elo._match_count.get(away, 0) < 5:
            log.debug('WNBA [skip-low-data]: %s (%d) vs %s (%d)',
                      home, elo._match_count.get(home, 0),
                      away, elo._match_count.get(away, 0))
            continue

        prob_home = elo.predict(home, away)
        prob_away = 1.0 - prob_home

        for sel in ft_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price   = float(sel.get('price', 0) or 0)
            murl    = sel.get('marketUrl', '')

            if price < 1.05 or not murl or outcome == 'draw':
                continue
            if price > 1.90:  # 2026-06-04: cap 1.90 (Sibila odds 1.6-1.9 WR=75%, 1.9-2.5 WR=50%)
                continue

            prob = prob_home if outcome == 'home' else prob_away
            team = home_cb if outcome == 'home' else away_cb

            # Form adjustment ±2%
            f    = elo.form(home if outcome == 'home' else away)
            prob = min(0.92, max(0.08, prob + (0.02 if f and f > 0.70 else -0.02 if f and f < 0.30 else 0.0)))

            edge = round(prob * price - 1.0, 4)
            if edge < 0.06 or prob < 0.58:
                continue

            picks.append({
                'match':                 f'{home_cb} vs {away_cb}',
                'league':                'basketball-usa-wnba',
                'sport':                 'basketball',
                'event_id':              eid,
                'market':                'basketball.1x2',
                'market_url':            murl,
                'price':                 price,
                'odds':                  price,
                'label':                 f'WNBA: {team}',
                'side':                  team,
                'model_prob':            round(prob, 4),
                'raw_model_prob_uncal':  round(prob, 4),
                'confidence':            round(prob, 4),
                'edge':                  edge,
                'shadow':                shadow,
                '_max_stake':            1.00,
            })

    log.info('WNBA: %d value picks (%s)', len(picks), 'SHADOW' if shadow else 'LIVE')
    return picks
