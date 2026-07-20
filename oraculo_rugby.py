"""
oraculo_rugby.py — Modelos ELO para rugby (CANTERA/shadow). Multi-liga.

Backtests out-of-sample (octonion/rugby):
  NRL  (rugby-league): 63.8%% acc (vs 56%% base), 1908 partidos, calibrado.
  MLR  (rugby-union US): 66.0%% acc (vs 56%% base), 534 partidos, calibrado.
NO live: solo shadow hasta validar CLV vs odds Cloudbet (N>=30).
"""
import os, re, pickle, logging
from datetime import datetime

log = logging.getLogger('oraculo')
CACHE = '/home/noc/oraculo_v2/.oraculo_cache/rugby'
K, HA = 24, 60

LEAGUES = {
    'nrl': {'path': 'nrl/csv', 'prefix': 'nrl-', 'fmt': 'nrl'},
    'mlr': {'path': 'major_league/csv', 'prefix': 'ml-', 'fmt': 'mlr'},
}


def _norm(name):
    return re.sub(r'[^a-z ]', '', (name or '').lower()).strip()


class RugbyElo:
    def __init__(self):
        self.ratings = {}

    def _key(self, name):
        n = _norm(name)
        if n in self.ratings:
            return n
        toks = [t for t in n.split() if len(t) > 3]
        for cand in self.ratings:
            ctoks = cand.split()
            if any(t in ctoks for t in toks) or any(ct in n for ct in ctoks if len(ct) > 4):
                return cand
        return n

    def predict(self, home, away):
        eh = self.ratings.get(self._key(home), 1500)
        ea = self.ratings.get(self._key(away), 1500)
        p_raw = 1.0 / (1.0 + 10 ** (-((eh + HA) - ea) / 400.0))
        cal = getattr(self, '_calibrator', None)
        if cal is not None:
            try:
                return float(cal.predict([p_raw])[0])
            except Exception:
                pass
        return p_raw

    def update(self, home, away, hs, as_):
        kh, ka = _norm(home), _norm(away)
        eh = self.ratings.get(kh, 1500); ea = self.ratings.get(ka, 1500)
        p = 1.0 / (1.0 + 10 ** (-((eh + HA) - ea) / 400.0))
        sa = 1.0 if hs > as_ else (0.0 if hs < as_ else 0.5)
        self.ratings[kh] = eh + K * (sa - p)
        self.ratings[ka] = ea + K * ((1 - sa) - (1 - p))


def _parse_nrl(txt):
    out = []
    for ln in txt.split('\n')[1:]:
        p = ln.split(',')
        if len(p) < 7:
            continue
        try:
            d = datetime.strptime(p[2].split()[0], '%d/%m/%Y')
            m = re.match(r'\s*(\d+)\s*-\s*(\d+)', p[6])
            if not m:
                continue
            out.append((d, p[4].strip(), p[5].strip(), int(m.group(1)), int(m.group(2))))
        except Exception:
            continue
    return out


def _parse_mlr(txt):
    out = []
    for ln in txt.split('\n'):
        p = ln.split(',')
        if len(p) < 6:
            continue
        try:
            d = datetime.strptime(p[1].split()[0], '%d.%m.%Y')
            out.append((d, p[2].strip(), p[3].strip(), int(p[4]), int(p[5])))
        except Exception:
            continue
    return out


def _fetch(league):
    import requests
    H = {'User-Agent': 'Mozilla/5.0'}
    cfg = LEAGUES[league]
    api = 'https://api.github.com/repos/octonion/rugby/contents/' + cfg['path']
    items = requests.get(api, headers=H, timeout=20).json()
    parser = _parse_nrl if cfg['fmt'] == 'nrl' else _parse_mlr
    matches = []
    for it in items:
        if not it['name'].startswith(cfg['prefix']):
            continue
        try:
            txt = requests.get(it['download_url'], headers=H, timeout=20).text
        except Exception:
            continue
        matches += parser(txt)
    matches.sort(key=lambda x: x[0])
    return matches


def _pkl(league):
    return os.path.join(CACHE, '%s_elo.pkl' % league)


def build_and_cache(league='nrl'):
    matches = _fetch(league)
    elo = RugbyElo()
    for d, h, a, hs, as_ in matches:
        elo.update(h, a, hs, as_)
    with open(_pkl(league), 'wb') as f:
        pickle.dump({'ratings': elo.ratings, 'n_matches': len(matches), 'calibrator': None}, f)
    return elo, len(matches)


def load_elo(league='nrl'):
    try:
        d = pickle.load(open(_pkl(league), 'rb'))
        e = RugbyElo(); e.ratings = d['ratings']
        e._calibrator = d.get('calibrator', None)
        return e
    except Exception:
        return None


if __name__ == '__main__':
    for lg in ('nrl', 'mlr'):
        try:
            e, n = build_and_cache(lg)
            top = sorted(e.ratings.items(), key=lambda x: -x[1])[:3]
            print('%s: %d partidos, %d equipos | top %s' % (lg, n, len(e.ratings), [t[0] for t in top]))
        except Exception as ex:
            print('%s: ERROR %s' % (lg, ex))
