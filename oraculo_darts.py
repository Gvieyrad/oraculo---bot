"""
oraculo_darts.py — Darts module for Oráculo v2
Shadow mode: logs picks to Sibila, no real bets until 2w validation.
Markets: darts.winner (Premier League, Modus Live League)
"""

import json
import math
import os
import time
import logging
import requests

log = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(__file__), '.oraculo_cache', 'darts_elo.json')
_CACHE_TTL = 86400  # 24h

# ── Elo seeds from PDC Order of Merit (2025/2026) ──────────────────────────
PDC_OOM_SEED = {
    'Luke Littler':        1780,
    'Luke Humphries':      1720,
    'Michael van Gerwen':  1700,
    'Gerwyn Price':        1610,
    'Jonny Clayton':       1600,
    'Peter Wright':        1590,
    'Michael Smith':       1580,
    'Dimitri Van den Bergh': 1570,
    'Nathan Aspinall':     1560,
    'Rob Cross':           1550,
    'Jose de Sousa':       1540,
    'Danny Noppert':       1530,
    'Dave Chisnall':       1520,
    'Damon Heta':          1510,
    'Callan Rydz':         1500,
    'Ryan Searle':         1490,
    'Chris Dobey':         1485,
    'Martin Schindler':    1480,
    'Dirk van Duijvenbode': 1475,
    'Gian van Veen':       1470,
    'Stephen Bunting':     1465,
    'Andrew Gilding':      1460,
    'Ricardo Pietreczko':  1455,
    'Scott Williams':      1450,
    'Joe Cullen':          1445,
    'Mike De Decker':      1440,
    'Raymond van Barneveld': 1435,
    'Gary Anderson':       1430,
    'Mensur Suljovic':     1425,
    'Krzysztof Ratajski':  1420,
    'Florian Hempel':      1415,
    'Connor Scutt':        1410,
    'Cameron Menzies':     1405,
    'Ryan Joyce':          1400,
    'Daryl Gurney':        1395,
    'Ricky Evans':         1390,
    'Mickey Mansell':      1385,
    'Ian White':           1380,
    'Alan Soutar':         1375,
    'Brendan Dolan':       1370,
}

# Cloudbet name → PDC name normalization
CB_TO_PDC = {
    'M. van Gerwen':         'Michael van Gerwen',
    'MvG':                   'Michael van Gerwen',
    'G. Price':              'Gerwyn Price',
    'J. Clayton':            'Jonny Clayton',
    'P. Wright':             'Peter Wright',
    'M. Smith':              'Michael Smith',
    'D. Van den Bergh':      'Dimitri Van den Bergh',
    'N. Aspinall':           'Nathan Aspinall',
    'R. Cross':              'Rob Cross',
    'J. De Sousa':           'Jose de Sousa',
    'D. Noppert':            'Danny Noppert',
    'D. Chisnall':           'Dave Chisnall',
    'D. Heta':               'Damon Heta',
    'C. Rydz':               'Callan Rydz',
    'R. Searle':             'Ryan Searle',
    'C. Dobey':              'Chris Dobey',
    'M. Schindler':          'Martin Schindler',
    'D. Van Duijvenbode':    'Dirk van Duijvenbode',
    'G. Van Veen':           'Gian van Veen',
    'S. Bunting':            'Stephen Bunting',
    'A. Gilding':            'Andrew Gilding',
    'R. Pietreczko':         'Ricardo Pietreczko',
    'S. Williams':           'Scott Williams',
    'J. Cullen':             'Joe Cullen',
    'M. De Decker':          'Mike De Decker',
    'R. Van Barneveld':      'Raymond van Barneveld',
    'G. Anderson':           'Gary Anderson',
    'K. Ratajski':           'Krzysztof Ratajski',
    'F. Hempel':             'Florian Hempel',
    'C. Scutt':              'Connor Scutt',
    'C. Menzies':            'Cameron Menzies',
    'R. Joyce':              'Ryan Joyce',
    'D. Gurney':             'Daryl Gurney',
    'R. Evans':              'Ricky Evans',
    'M. Mansell':            'Mickey Mansell',
    'I. White':              'Ian White',
    'A. Soutar':             'Alan Soutar',
    'B. Dolan':              'Brendan Dolan',
    'L. Littler':            'Luke Littler',
    'L. Humphries':          'Luke Humphries',
}


class DartsElo:
    K = 24

    def __init__(self):
        self.ratings = {}
        self.history = {}  # player → list of (ts, opponent, result)

    def _resolve(self, name):
        if name in CB_TO_PDC:
            return CB_TO_PDC[name]
        # Partial last-name match
        lower = name.lower().strip()
        for cb, pdc in CB_TO_PDC.items():
            if lower in pdc.lower() or pdc.lower() in lower:
                return pdc
        return name

    def get(self, name):
        name = self._resolve(name)
        if name not in self.ratings:
            # Default: check seed dict (case-insensitive), else 1400
            for seed_name, seed_val in PDC_OOM_SEED.items():
                if seed_name.lower() == name.lower():
                    self.ratings[name] = seed_val
                    return seed_val
            self.ratings[name] = 1400
        return self.ratings[name]

    def expected(self, ra, rb):
        return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

    def process_match(self, winner, loser, legs_w=None, legs_l=None, ts=None):
        winner = self._resolve(winner)
        loser = self._resolve(loser)
        ra = self.get(winner)
        rb = self.get(loser)
        ea = self.expected(ra, rb)

        # Margin-of-victory multiplier (optional, only if leg scores provided)
        mov = 1.0
        if legs_w and legs_l:
            margin = legs_w - legs_l
            mov = math.log(abs(margin) + 1) / math.log(4)  # log scale, caps ~2x
            mov = max(0.5, min(2.0, mov))

        k = self.K * mov
        self.ratings[winner] = ra + k * (1 - ea)
        self.ratings[loser] = rb + k * (0 - (1 - ea))

        for name, result in [(winner, 'W'), (loser, 'L')]:
            if name not in self.history:
                self.history[name] = []
            self.history[name].append({'ts': ts or time.time(), 'result': result})

    def predict(self, home, away):
        ra = self.get(home)
        rb = self.get(away)
        p_home = self.expected(ra, rb)
        return p_home

    def get_form(self, name, n=10):
        name = self._resolve(name)
        hist = self.history.get(name, [])
        recent = sorted(hist, key=lambda x: x.get('ts', 0))[-n:]
        if not recent:
            return None
        wins = sum(1 for r in recent if r['result'] == 'W')
        return wins / len(recent)

    def save(self, path=_CACHE_FILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            'ts': time.time(),
            'ratings': self.ratings,
            'history': self.history,
        }
        with open(path, 'w') as f:
            json.dump(data, f)

    @classmethod
    def load(cls, path=_CACHE_FILE):
        elo = cls()
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            if time.time() - data.get('ts', 0) > _CACHE_TTL:
                return None
            elo.ratings = data.get('ratings', {})
            elo.history = data.get('history', {})
            return elo
        except Exception:
            return None


def fetch_darts_results(days_back=90):
    """Fetch recent PDC darts results from ESPN API."""
    results = []
    base = 'https://site.api.espn.com/apis/site/v2/sports/darts/pdc/scoreboard'
    try:
        resp = requests.get(base, params={'limit': 200}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        events = data.get('events', [])
        for ev in events:
            status = ev.get('status', {}).get('type', {}).get('completed', False)
            if not status:
                continue
            comps = ev.get('competitions', [{}])
            for comp in comps:
                competitors = comp.get('competitors', [])
                if len(competitors) != 2:
                    continue
                c0, c1 = competitors[0], competitors[1]
                if c0.get('winner'):
                    winner_name = c0.get('athlete', {}).get('displayName', '') or c0.get('team', {}).get('displayName', '')
                    loser_name = c1.get('athlete', {}).get('displayName', '') or c1.get('team', {}).get('displayName', '')
                    lw = int(c0.get('score', 0) or 0)
                    ll = int(c1.get('score', 0) or 0)
                elif c1.get('winner'):
                    winner_name = c1.get('athlete', {}).get('displayName', '') or c1.get('team', {}).get('displayName', '')
                    loser_name = c0.get('athlete', {}).get('displayName', '') or c0.get('team', {}).get('displayName', '')
                    lw = int(c1.get('score', 0) or 0)
                    ll = int(c0.get('score', 0) or 0)
                else:
                    continue
                if winner_name and loser_name:
                    results.append({
                        'winner': winner_name,
                        'loser': loser_name,
                        'legs_w': lw,
                        'legs_l': ll,
                        'ts': time.time(),
                    })
    except Exception as e:
        log.debug('fetch_darts_results ESPN error: %s', e)

    return results


def train_darts_elo():
    """Load from cache or seed fresh Elo from PDC rankings."""
    cached = DartsElo.load()
    if cached:
        log.info('Darts Elo loaded from cache (%d players)', len(cached.ratings))
        return cached

    elo = DartsElo()
    # Seed from PDC OOM
    for name, rating in PDC_OOM_SEED.items():
        elo.ratings[name] = rating

    # Try to enrich with recent results
    results = fetch_darts_results(days_back=90)
    log.info('Darts: training Elo with %d results', len(results))
    for r in sorted(results, key=lambda x: x.get('ts', 0)):
        try:
            elo.process_match(
                r['winner'], r['loser'],
                legs_w=r.get('legs_w'), legs_l=r.get('legs_l'),
                ts=r.get('ts'),
            )
        except Exception:
            pass

    elo.save()
    log.info('Darts Elo trained (%d players)', len(elo.ratings))
    return elo


def scan_darts(api, state, elo=None, dry_run=False, shadow=True):
    """
    Scan Cloudbet darts markets.
    Returns list of pick dicts (shadow=True → not placed, logged to Sibila).
    """
    if elo is None:
        elo = train_darts_elo()

    picks = []
    MIN_EDGE = 0.07    # 7% Kelly edge
    MIN_CONF = 0.58    # 58% model confidence
    MAX_ODDS = 2.20    # avoid longshots in darts
    MIN_ODDS = 1.30    # ignore heavy favourites
    MAX_STAKE = 1.00   # shadow cap

    SLUGS = [
        'darts-international-premier-league',
        'darts-international-t6b6c-modus-darts-online-live-league',
        'darts-world-championship',
        'darts-international-grand-prix',
    ]

    for slug in SLUGS:
        try:
            events = api.get_odds(slug) or []
        except Exception as e:
            log.debug('Darts scan_odds error [%s]: %s', slug, e)
            continue

        for ev in events:
            try:
                home = ev.get('home', {}).get('name', '')
                away = ev.get('away', {}).get('name', '')
                if not home or not away:
                    continue

                mkts = ev.get('markets', {})
                winner_data = mkts.get('darts.winner', {})
                submarkets = winner_data.get('submarkets', {})
                if not submarkets:
                    continue

                sm = list(submarkets.values())[0]
                selections = sm.get('selections', [])

                for sel in selections:
                    outcome = sel.get('outcome', '')
                    price = float(sel.get('price', 0) or 0)
                    if price < MIN_ODDS or price > MAX_ODDS:
                        continue

                    if outcome == 'home':
                        team = home
                        opponent = away
                    elif outcome == 'away':
                        team = away
                        opponent = home
                    else:
                        continue

                    prob = elo.predict(team, opponent) if outcome == 'home' else (1 - elo.predict(home, away))
                    if prob < MIN_CONF:
                        continue

                    edge = round(prob * price - 1.0, 4)
                    if edge < MIN_EDGE:
                        continue

                    form = elo.get_form(team, n=8)
                    label = 'Darts: %s' % team

                    pick = {
                        'match':               '%s vs %s' % (home, away),
                        'sport':               'darts',
                        'market':              'darts.winner',
                        'outcome':             outcome,
                        'label':               label,
                        'odds':                price,
                        'price':               price,
                        'edge':                edge,
                        'confidence':          round(prob, 4),
                        'model_prob':          round(prob, 4),
                        'raw_model_prob_uncal': round(prob, 4),
                        'form':                round(form, 3) if form is not None else None,
                        '_max_stake':          MAX_STAKE,
                        'shadow':              shadow,
                        'event_id':            ev.get('id', ''),
                        'competition':         ev.get('competition', {}).get('name', slug),
                    }
                    picks.append(pick)
                    log.debug('Darts pick: %s | edge=%.1f%% conf=%.0f%% @%.3f',
                              label, edge * 100, prob * 100, price)

            except Exception as e:
                log.debug('Darts event error: %s', e)
                continue

    if picks:
        log.info('Darts: %d value picks found', len(picks))
    else:
        log.debug('Darts: 0 value picks')

    return picks
