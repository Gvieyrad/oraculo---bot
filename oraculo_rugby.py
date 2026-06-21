"""
oraculo_rugby.py — Modelo ELO para rugby-league (NRL). CANTERA/shadow.

Backtest (2026-06-21): ELO out-of-sample sobre 1908 partidos NRL = 63.8% accuracy
(vs 56% baseline), bien calibrado. PASO el backtest -> a cantera shadow.
Data: github.com/octonion/rugby (nrl/csv/nrl-YYYY-*.csv).

NO live: solo shadow hasta validar CLV vs odds Cloudbet (N>=30, WR/edge+).
"""
import os, re, json, pickle, logging
from datetime import datetime

log = logging.getLogger('oraculo')
CACHE = '/home/noc/oraculo_v2/.oraculo_cache/rugby'
ELO_PKL = os.path.join(CACHE, 'nrl_elo.pkl')
K, HA = 24, 60  # K-factor y home advantage (del backtest)


def _norm(name):
    return re.sub(r'[^a-z ]', '', (name or '').lower()).strip()


class RugbyElo:
    def __init__(self):
        self.ratings = {}   # norm_name -> elo
        self.aliases = {}   # palabra clave -> norm_name canonico

    def _key(self, name):
        n = _norm(name)
        if n in self.ratings:
            return n
        # match por substring (Cloudbet 'Manly Sea Eagles' vs octonion 'Sea Eagles')
        toks = [t for t in n.split() if len(t) > 3]
        for cand in self.ratings:
            ctoks = cand.split()
            if any(t in ctoks for t in toks) or any(ct in n for ct in ctoks if len(ct) > 4):
                return cand
        return n

    def predict(self, home, away):
        eh = self.ratings.get(self._key(home), 1500)
        ea = self.ratings.get(self._key(away), 1500)
        return 1.0 / (1.0 + 10 ** (-((eh + HA) - ea) / 400.0))

    def update(self, home, away, hs, as_):
        kh, ka = _norm(home), _norm(away)
        eh = self.ratings.get(kh, 1500); ea = self.ratings.get(ka, 1500)
        p = 1.0 / (1.0 + 10 ** (-((eh + HA) - ea) / 400.0))
        sa = 1.0 if hs > as_ else (0.0 if hs < as_ else 0.5)
        self.ratings[kh] = eh + K * (sa - p)
        self.ratings[ka] = ea + K * ((1 - sa) - (1 - p))


def _fetch_nrl():
    import requests
    H = {'User-Agent': 'Mozilla/5.0'}
    api = 'https://api.github.com/repos/octonion/rugby/contents/nrl/csv'
    items = requests.get(api, headers=H, timeout=20).json()
    matches = []
    for it in items:
        if not it['name'].startswith('nrl-'):
            continue
        try:
            txt = requests.get(it['download_url'], headers=H, timeout=20).text
        except Exception:
            continue
        for ln in txt.split('\n')[1:]:
            p = ln.split(',')
            if len(p) < 7:
                continue
            try:
                d = datetime.strptime(p[2].split()[0], '%d/%m/%Y')
                m = re.match(r'\s*(\d+)\s*-\s*(\d+)', p[6])
                if not m:
                    continue
                matches.append((d, p[4].strip(), p[5].strip(), int(m.group(1)), int(m.group(2))))
            except Exception:
                continue
    matches.sort(key=lambda x: x[0])
    return matches


def build_and_cache():
    matches = _fetch_nrl()
    elo = RugbyElo()
    for d, h, a, hs, as_ in matches:
        elo.update(h, a, hs, as_)
    with open(ELO_PKL, 'wb') as f:
        pickle.dump({'ratings': elo.ratings, 'n_matches': len(matches),
                     'built': datetime.utcnow().isoformat()}, f)
    return elo, len(matches)


def load_elo():
    """Carga el ELO cacheado. NO-OP si no existe."""
    try:
        d = pickle.load(open(ELO_PKL, 'rb'))
        e = RugbyElo(); e.ratings = d['ratings']
        return e
    except Exception:
        return None


if __name__ == '__main__':
    e, n = build_and_cache()
    print('ELO NRL construido: %d partidos, %d equipos' % (n, len(e.ratings)))
    print('top ratings:')
    for name, r in sorted(e.ratings.items(), key=lambda x: -x[1])[:8]:
        print('  %-22s %.0f' % (name, r))
