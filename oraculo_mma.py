"""MMA (UFC) Elo model + scanner for Oraculo. Shadow mode until 20+ picks validated.

2026-07-27: nuevo mercado, agregado tras research de fuentes (Cloudbet no expone
resultados historicos via su API de odds -- solo mercados activos/liquidados sin
rastro). ESPN si tiene un scoreboard JSON limpio para MMA/UFC con winner:true/false
por pelea, igual patron que WNBA. Empieza solo por UFC (competicion mma-international-ufc
en Cloudbet) -- PFL/Oktagon quedan afuera por ahora, volumen menor y sin fuente
de resultados verificada todavia.
"""
import os, json, time, logging, unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
MMA_ELO_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'mma_elo.json')
MMA_RES_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'mma_results.json')


def _norm_name(name: str) -> str:
    """Normalize fighter name for matching across Cloudbet/ESPN (strip accents/case)."""
    if not name:
        return ''
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_name.lower().strip()


class MMAElo:
    """Elo rating system for MMA fighters. No home advantage (neutral cage),
    no margin-of-victory scaling -- MMA outcomes are binary win/loss/draw,
    unlike point-margin sports. K=32 (individual combat sport, higher variance
    per bout than team sports -- start wide, narrow later if data supports it)."""

    def __init__(self, k=32, initial=1500):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self._match_count = defaultdict(int)
        self._display     = {}  # norm_name -> last-seen display name

    def process_match(self, winner: str, loser: str, winner_disp: str = '', loser_disp: str = ''):
        r_w, r_l = self.ratings[winner], self.ratings[loser]
        exp_w    = 1.0 / (1.0 + 10 ** ((r_l - r_w) / 400.0))
        change   = self.k * (1 - exp_w)
        self.ratings[winner] += change
        self.ratings[loser]  -= change
        self._match_count[winner] += 1
        self._match_count[loser]  += 1
        if winner_disp:
            self._display[winner] = winner_disp
        if loser_disp:
            self._display[loser] = loser_disp

    def predict(self, a: str, b: str) -> float:
        """P(a beats b), neutral (no home/away in MMA)."""
        r_a, r_b = self.ratings[a], self.ratings[b]
        return 1.0 / (1.0 + 10 ** ((r_b - r_a) / 400.0))

    def save(self):
        os.makedirs(os.path.dirname(MMA_ELO_CACHE), exist_ok=True)
        with open(MMA_ELO_CACHE, 'w') as f:
            json.dump({
                'ratings':     dict(self.ratings),
                'match_count': dict(self._match_count),
                'display':     self._display,
            }, f)

    def load(self) -> bool:
        if not os.path.exists(MMA_ELO_CACHE):
            return False
        try:
            data = json.load(open(MMA_ELO_CACHE))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            self._display = data.get('display', {})
            return True
        except Exception:
            return False


def fetch_mma_results(force: bool = False) -> list:
    """Fetch UFC fight results from ESPN (free, no auth). Incremental, chunked
    in 365-day windows (API rejects wider ranges with HTTP 400 -- confirmed
    2026-07-27). Pulls last ~2 years on cold start."""
    os.makedirs(os.path.dirname(MMA_RES_CACHE), exist_ok=True)
    existing = []
    if os.path.exists(MMA_RES_CACHE):
        try:
            existing = json.load(open(MMA_RES_CACHE))
        except Exception:
            pass
        if not force:
            age = time.time() - os.path.getmtime(MMA_RES_CACHE)
            if age < 21600 and existing:
                return existing

    import urllib.request
    seen_keys = {(g['date'], g['fighter_a_norm'], g['fighter_b_norm']) for g in existing}
    end = datetime.utcnow()
    start = end - timedelta(days=730) if not existing else end - timedelta(days=380)
    new = 0

    window_end = end
    while window_end > start:
        window_start = max(start, window_end - timedelta(days=365))
        ds = window_start.strftime('%Y%m%d')
        de = window_end.strftime('%Y%m%d')
        url = (f'https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard'
               f'?dates={ds}-{de}&limit=1000')
        try:
            req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            for ev in data.get('events', []):
                ev_date = (ev.get('date') or '')[:10]
                for comp in ev.get('competitions', []):
                    status = comp.get('status', {}).get('type', {})
                    if not status.get('completed'):
                        continue
                    fighters = comp.get('competitors', [])
                    if len(fighters) != 2:
                        continue
                    a, b = fighters[0], fighters[1]
                    a_name = a.get('athlete', {}).get('displayName', '')
                    b_name = b.get('athlete', {}).get('displayName', '')
                    a_win, b_win = a.get('winner'), b.get('winner')
                    if not a_name or not b_name:
                        continue
                    if a_win is True and b_win is False:
                        winner, loser = a_name, b_name
                    elif b_win is True and a_win is False:
                        winner, loser = b_name, a_name
                    else:
                        continue  # draw / no-contest / missing data -- skip, don't feed Elo
                    a_norm, b_norm = _norm_name(a_name), _norm_name(b_name)
                    key = (ev_date, a_norm, b_norm)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    existing.append({
                        'date': ev_date,
                        'event': ev.get('name', ''),
                        'fighter_a': a_name, 'fighter_a_norm': a_norm,
                        'fighter_b': b_name, 'fighter_b_norm': b_norm,
                        'winner': winner, 'winner_norm': _norm_name(winner),
                        'loser': loser, 'loser_norm': _norm_name(loser),
                    })
                    new += 1
        except Exception as e:
            log.debug('MMA results fetch failed for %s-%s: %s', ds, de, e)
        window_end = window_start

    existing.sort(key=lambda g: g['date'])
    if new:
        log.info('MMA: %d new fights fetched (%d total)', new, len(existing))
    with open(MMA_RES_CACHE, 'w') as f:
        json.dump(existing, f)
    return existing


def train_mma_elo(force: bool = False) -> MMAElo:
    """Train MMA Elo from ESPN results. Cache TTL 6h (same pattern as WNBA fix
    2026-07-13 -- never cache indefinitely, ratings must stay current)."""
    elo = MMAElo()
    cache_age = (time.time() - os.path.getmtime(MMA_ELO_CACHE)) if os.path.exists(MMA_ELO_CACHE) else 1e18
    if not force and cache_age < 21600 and elo.load() and len(elo.ratings) >= 50:
        log.info('MMA Elo loaded from cache (%d fighters)', len(elo.ratings))
        return elo

    fights = fetch_mma_results(force=force)
    if not fights:
        log.warning('MMA: no results to train from')
        return elo

    for g in sorted(fights, key=lambda x: x['date']):
        elo.process_match(g['winner_norm'], g['loser_norm'], g['winner'], g['loser'])

    elo.save()
    log.info('MMA Elo trained: %d fighters, %d fights', len(elo.ratings), len(fights))
    return elo


def scan_mma(api, state, elo: MMAElo = None, dry_run: bool = False, shadow: bool = True) -> list:
    """Scan Cloudbet UFC markets (mma-international-ufc) for value bets via
    mma.winner. shadow=True: logs to Sibila only, no real stake placed.
    Do NOT flip to live until 20+ picks with WR clearing breakeven at the
    odds range seen -- same promotion checklist as WNBA/sets_under."""
    if elo is None:
        elo = train_mma_elo()

    if len(elo.ratings) < 50:
        log.warning('MMA Elo not ready (%d fighters) -- need more history', len(elo.ratings))
        return []

    events = api.get_odds('mma-international-ufc')
    if not events:
        return []

    now        = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=168)).isoformat() + 'Z'  # fight week windows are wider than weekly team sports
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

        mk = ev.get('markets', {}).get('mma.winner', {})
        if not mk:
            continue

        default_sub = None
        for sv in mk.get('submarkets', {}).values():
            default_sub = sv
            break
        if not default_sub:
            continue

        home = _norm_name(home_cb)
        away = _norm_name(away_cb)

        if elo._match_count.get(home, 0) < 3 or elo._match_count.get(away, 0) < 3:
            log.debug('MMA [skip-low-data]: %s (%d) vs %s (%d)',
                      home_cb, elo._match_count.get(home, 0),
                      away_cb, elo._match_count.get(away, 0))
            continue

        prob_home = elo.predict(home, away)
        prob_away = 1.0 - prob_home

        for sel in default_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price   = float(sel.get('price', 0) or 0)
            murl    = sel.get('marketUrl', '')

            if price < 1.05 or not murl or outcome not in ('home', 'away'):
                continue
            if price > 3.50:  # 2026-07-27: cap inicial conservador, sin data propia todavia -- ajustar con cantera
                continue

            prob = prob_home if outcome == 'home' else prob_away
            fighter = home_cb if outcome == 'home' else away_cb

            edge = round(prob * price - 1.0, 4)
            if edge < 0.08 or prob < 0.55:  # umbral inicial mas estricto que WNBA (0.06/0.58) -- deporte individual, mas varianza por bout
                continue

            picks.append({
                'match':                 f'{home_cb} vs {away_cb}',
                'league':                'mma-international-ufc',
                'sport':                 'mma',
                'event_id':              eid,
                'market':                'mma.winner',
                'market_url':            murl,
                'price':                 price,
                'odds':                  price,
                'label':                 f'MMA: {fighter}',
                'side':                  fighter,
                'model_prob':            round(prob, 4),
                'raw_model_prob_uncal':  round(prob, 4),
                'confidence':            round(prob, 4),
                'edge':                  edge,
                'shadow':                shadow,
                '_max_stake':            1.00,
            })

    log.info('MMA: %d value picks (%s)', len(picks), 'SHADOW' if shadow else 'LIVE')
    return picks
