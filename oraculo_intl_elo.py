"""
Oraculo International Elo — National team Elo + Poisson model.
Trained from 49K+ historical international results (1872-present).
"""

import os, csv, math, pickle, logging
from datetime import datetime, date
from collections import defaultdict

log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(SCRIPT_DIR, 'models', 'intl_elo_state.pkl')
DATA_PATH  = os.path.join(SCRIPT_DIR, 'models', 'intl_results.csv')

TOURNAMENT_K = {
    'FIFA World Cup': 60, 'UEFA Euro': 55, 'Copa America': 55,
    'Africa Cup of Nations': 50, 'Asian Cup': 50, 'CONCACAF Gold Cup': 50,
    'UEFA Nations League': 45, 'Confederations Cup': 45,
    'WC Qualification': 40, 'Qualification': 35, 'Friendly': 20,
}
DEFAULT_K = 30
INITIAL_ELO = 1500.0
MIN_MATCHES = 5
RECENT_YEARS = 8

ALIASES = {
    'Bosnia and Herzegovina': 'Bosnia & Herzegovina',
    'Bosnia & Hercegovina': 'Bosnia & Herzegovina',
    'Bosnia-Herzegovina': 'Bosnia & Herzegovina',
    'Republic of Ireland': 'Ireland',
    'IR Iran': 'Iran',
    'Korea Republic': 'South Korea',
    'Korea DPR': 'North Korea',
    'China PR': 'China',
    'Turkiye': 'Turkey',
    'Turk\u00fcye': 'Turkey',
    'Czech Republic': 'Czech Republic',
    'Czechia': 'Czech Republic',
    'USA': 'United States',
    'Cape Verde Islands': 'Cape Verde',
}

def normalize(name):
    return ALIASES.get(name, name)

def _goal_weight(gd):
    if gd == 1: return 1.0
    if gd == 2: return 1.5
    return 1.75 + (gd - 3) * 0.1 if gd >= 3 else 1.0

def _k_factor(tournament):
    t = str(tournament)
    for key, k in TOURNAMENT_K.items():
        if key.lower() in t.lower():
            return k
    return DEFAULT_K

def _expected(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


class IntlElo:
    def __init__(self):
        self.ratings = {}
        self.matches = {}
        self.attack  = {}
        self.defense = {}
        self._scored   = defaultdict(list)
        self._conceded = defaultdict(list)
        self.trained_until = None

    def train(self, csv_path=DATA_PATH, min_year=None):
        if min_year is None:
            min_year = date.today().year - RECENT_YEARS
        n = 0
        with open(csv_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                try:
                    if datetime.strptime(row['date'], '%Y-%m-%d').year < min_year:
                        continue
                    home = normalize(row['home_team'])
                    away = normalize(row['away_team'])
                    hs, as_ = int(row['home_score']), int(row['away_score'])
                    tourn   = row.get('tournament', 'Friendly')
                    neutral = row.get('neutral', 'FALSE').upper() == 'TRUE'
                except (KeyError, ValueError):
                    continue
                for t in (home, away):
                    if t not in self.ratings: self.ratings[t] = INITIAL_ELO
                    if t not in self.matches: self.matches[t] = 0
                adv = 0 if neutral else 100
                e_h = _expected(self.ratings[home] + adv, self.ratings[away])
                if hs > as_:   ah, aa = 1.0, 0.0
                elif hs < as_: ah, aa = 0.0, 1.0
                else:          ah, aa = 0.5, 0.5
                gw = _goal_weight(abs(hs - as_))
                k  = _k_factor(tourn)
                self.ratings[home] += k * gw * (ah - e_h)
                self.ratings[away] += k * gw * (aa - (1 - e_h))
                self.matches[home] += 1
                self.matches[away] += 1
                self._scored[home].append(hs);   self._conceded[home].append(as_)
                self._scored[away].append(as_);  self._conceded[away].append(hs)
                n += 1
        avg = 1.35
        for t in self.ratings:
            sc = self._scored.get(t, []);  co = self._conceded.get(t, [])
            self.attack[t]  = (sum(sc)/len(sc)/avg)  if len(sc) >= MIN_MATCHES else 1.0
            self.defense[t] = (sum(co)/len(co)/avg)  if len(co) >= MIN_MATCHES else 1.0
        self.trained_until = date.today()
        log.info('IntlElo: %d teams, %d matches', len(self.ratings), n)
        return self

    def save(self, path=MODEL_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            pickle.dump({'ratings': self.ratings, 'matches': self.matches,
                         'attack': self.attack, 'defense': self.defense,
                         'trained_until': self.trained_until}, f)

    def load(self, path=MODEL_PATH):
        with open(path, 'rb') as f:
            s = pickle.load(f)
        self.ratings = s['ratings']; self.matches = s.get('matches', {})
        self.attack  = s.get('attack', {}); self.defense = s.get('defense', {})
        self.trained_until = s.get('trained_until')
        return self

    def predict_match(self, home, away, neutral=False):
        home, away = normalize(home), normalize(away)
        rh = self.ratings.get(home, INITIAL_ELO)
        ra = self.ratings.get(away, INITIAL_ELO)
        adv = 0 if neutral else 100
        e_h = _expected(rh + adv, ra)
        diff = abs(rh + adv - ra)
        p_draw = max(0.10, 0.26 - diff / 3000.0)
        p_home = e_h * (1 - p_draw)
        p_away = (1 - e_h) * (1 - p_draw)
        total = p_home + p_draw + p_away
        return p_home/total, p_draw/total, p_away/total

    def expected_goals(self, home, away, neutral=False):
        home, away = normalize(home), normalize(away)
        att_h = self.attack.get(home, 1.0); def_h = self.defense.get(home, 1.0)
        att_a = self.attack.get(away, 1.0); def_a = self.defense.get(away, 1.0)
        ha = 1.15 if not neutral else 1.0
        return att_h * def_a * 1.35 * ha, att_a * def_h * 1.35

    def prob_over(self, home, away, line=2.5, neutral=False):
        xh, xa = self.expected_goals(home, away, neutral)
        p_under = sum(
            math.exp(-xh) * xh**i / math.factorial(i) *
            math.exp(-xa) * xa**j / math.factorial(j)
            for i in range(int(line)+1) for j in range(int(line)+1-i)
            if i+j <= line
        )
        return max(0.0, min(1.0, 1.0 - p_under))

    def top_teams(self, n=20):
        q = {t: r for t, r in self.ratings.items() if self.matches.get(t,0) >= MIN_MATCHES}
        return sorted(q.items(), key=lambda x: x[1], reverse=True)[:n]
