"""Cricket Elo models (ODI + Test) + scanner for Oraculo. Shadow mode until
20+ picks validated per formato.

2026-08-04: Fase 4a del plan de expansion de deportes. Fuente: cricsheet.org
(JSON por torneo, dataset abierto de la comunidad de cricket analytics,
actualizado -- confirmado sin bloqueo, a diferencia de ESPN/NFL). Mercado
Cloudbet: cricket.winner (moneyline simple).

Hallazgos de modelado (medidos con datos reales de cricsheet antes de
escribir este modulo, no asumidos):
- Toss NO es un factor fuerte (51.6% WR con toss ganado en 1,218 partidos
  IPL medidos) -- se ignora, no se modela como feature.
- ODI: ~5% de partidos sin ganador claro (empate/no-result/lluvia) -- se
  descartan del entrenamiento, resto es binario normal (igual que NFL/NBA).
- Test: 19-20% de partidos terminan en empate, PERO la tasa de empate es
  practicamente CONSTANTE independiente de la diferencia de Elo entre
  equipos (medido: ~18-23% en cada bucket de diff de Elo hasta 200 puntos,
  n>=57 cada uno) -- un partido entre equipos parejos no empata mas seguido
  que uno entre equipos dispares en Test. Por eso el modelo de Test usa una
  probabilidad de empate CONSTANTE (P_DRAW_TEST) en vez de una funcion de la
  diferencia de rating -- mas simple y ajustado a lo que muestran los datos,
  no una complicacion injustificada.
- Solo partidos gender='male' -- cricket femenino es un pool de equipos
  completamente distinto, mezclarlo corrompe el Elo.
"""
import os, json, time, logging, zipfile, io
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
ODI_ELO_CACHE     = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'cricket_odi_elo.json')
ODI_RES_CACHE     = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'cricket_odi_results.json')
TEST_ELO_CACHE    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'cricket_test_elo.json')
TEST_RES_CACHE    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'cricket_test_results.json')

CACHE_TTL = 43200  # 12h -- cricsheet se actualiza a lo sumo semanalmente, no hace falta refrescar seguido

P_DRAW_TEST = 0.195  # tasa base medida en datos reales (911 Test matches), constante (ver docstring)


class CricketEloODI:
    """Elo binario estandar para ODI. home_adv chico (30) -- en cricket
    internacional el pais anfitrion pesa menos que en deportes de liga
    domestica (las condiciones de cancha/clima importan mas que la
    localia en si), pero no es cero (ventaja real de conocer la cancha)."""

    def __init__(self, k=20, initial=1500, home_adv=30):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)

    def process_match(self, winner, loser, winner_home):
        r_w, r_l = self.ratings[winner], self.ratings[loser]
        r_w_adj  = r_w + (self.home_adv if winner_home else 0)
        r_l_adj  = r_l + (0 if winner_home else self.home_adv)
        exp_w    = 1.0 / (1.0 + 10 ** ((r_l_adj - r_w_adj) / 400.0))
        change   = self.k * (1 - exp_w)
        self.ratings[winner] += change
        self.ratings[loser]  -= change
        self._match_count[winner] += 1
        self._match_count[loser]  += 1

    def predict(self, home, away):
        r_h = self.ratings[home] + self.home_adv
        r_a = self.ratings[away]
        return 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'ratings': dict(self.ratings), 'match_count': dict(self._match_count)}, f)

    def load(self, path):
        if not os.path.exists(path):
            return False
        try:
            data = json.load(open(path))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            return True
        except Exception:
            return False


class CricketEloTest:
    """Elo de 3 resultados (home_win/away_win/draw) para Test. El motor de
    rating es igual al de ODI (empates no mueven el rating fuerte, solo un
    empuje chico hacia el 50/50), pero predict() devuelve 3 probabilidades
    en vez de 2 -- ver P_DRAW_TEST arriba para por que es una constante
    y no f(elo_diff)."""

    def __init__(self, k=16, initial=1500, home_adv=25):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)

    def process_match(self, home, away, winner, home_win):
        """winner: nombre del ganador, o None si empate."""
        r_h_adj = self.ratings[home] + self.home_adv
        r_a     = self.ratings[away]
        exp_h   = 1.0 / (1.0 + 10 ** ((r_a - r_h_adj) / 400.0))
        if winner is None:
            score_h = 0.5
        else:
            score_h = 1.0 if winner == home else 0.0
        change = self.k * (score_h - exp_h)
        self.ratings[home] += change
        self.ratings[away] -= change
        self._match_count[home] += 1
        self._match_count[away] += 1

    def predict(self, home, away):
        """Devuelve (p_home_win, p_away_win, p_draw)."""
        r_h = self.ratings[home] + self.home_adv
        r_a = self.ratings[away]
        p_h_raw = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
        p_home = p_h_raw * (1 - P_DRAW_TEST)
        p_away = (1 - p_h_raw) * (1 - P_DRAW_TEST)
        return p_home, p_away, P_DRAW_TEST

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'ratings': dict(self.ratings), 'match_count': dict(self._match_count)}, f)

    def load(self, path):
        if not os.path.exists(path):
            return False
        try:
            data = json.load(open(path))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            return True
        except Exception:
            return False


def _fetch_cricsheet(zip_url, cache_path, force=False):
    """Descarga y parsea un ZIP de cricsheet (JSON por partido). Cache TTL
    12h. Devuelve lista de dicts: date, home, away, winner (o None si
    empate/sin resultado -- el llamador decide si descartar)."""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    if not force and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < CACHE_TTL:
            try:
                cached = json.load(open(cache_path))
                if cached:
                    return cached
            except Exception:
                pass

    import urllib.request
    matches = []
    try:
        req = urllib.request.Request(zip_url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = urllib.request.urlopen(req, timeout=60).read()
        zf  = zipfile.ZipFile(io.BytesIO(raw))
        for name in zf.namelist():
            if not name.endswith('.json'):
                continue
            try:
                d = json.loads(zf.read(name))
            except Exception:
                continue
            info = d.get('info', {})
            if info.get('gender') != 'male':
                continue
            teams = info.get('teams', [])
            if len(teams) != 2:
                continue
            dates = info.get('dates', [])
            if not dates:
                continue
            oc = info.get('outcome', {})
            result = oc.get('result')
            winner = oc.get('winner')
            matches.append({
                'date':   dates[0],
                'home':   teams[0],
                'away':   teams[1],
                'winner': winner,
                'result': result,
            })
    except Exception as e:
        log.debug('Cricket fetch failed (%s): %s', zip_url, e)
        try:
            return json.load(open(cache_path)) if os.path.exists(cache_path) else []
        except Exception:
            return []

    matches.sort(key=lambda m: m['date'])
    log.info('Cricket: %d partidos fetcheados de %s', len(matches), zip_url.rsplit('/', 1)[-1])
    with open(cache_path, 'w') as f:
        json.dump(matches, f)
    return matches


def fetch_cricket_odi(force=False):
    return _fetch_cricsheet('https://cricsheet.org/downloads/odis_json.zip', ODI_RES_CACHE, force)


def fetch_cricket_test(force=False):
    return _fetch_cricsheet('https://cricsheet.org/downloads/tests_json.zip', TEST_RES_CACHE, force)


def train_cricket_odi_elo(force=False):
    elo = CricketEloODI()
    cache_age = (time.time() - os.path.getmtime(ODI_ELO_CACHE)) if os.path.exists(ODI_ELO_CACHE) else 1e18
    if not force and cache_age < CACHE_TTL and elo.load(ODI_ELO_CACHE) and len(elo.ratings) >= 10:
        log.info('Cricket ODI Elo loaded from cache (%d equipos)', len(elo.ratings))
        return elo

    matches = fetch_cricket_odi(force=force)
    if not matches:
        log.warning('Cricket ODI: no results to train from')
        return elo

    n_used = 0
    for m in matches:
        winner = m['winner']
        if not winner:
            continue
        loser = m['away'] if winner == m['home'] else m['home']
        elo.process_match(winner, loser, winner == m['home'])
        n_used += 1

    elo.save(ODI_ELO_CACHE)
    log.info('Cricket ODI Elo trained: %d equipos, %d partidos (de %d totales)',
              len(elo.ratings), n_used, len(matches))
    return elo


def train_cricket_test_elo(force=False):
    elo = CricketEloTest()
    cache_age = (time.time() - os.path.getmtime(TEST_ELO_CACHE)) if os.path.exists(TEST_ELO_CACHE) else 1e18
    if not force and cache_age < CACHE_TTL and elo.load(TEST_ELO_CACHE) and len(elo.ratings) >= 8:
        log.info('Cricket Test Elo loaded from cache (%d equipos)', len(elo.ratings))
        return elo

    matches = fetch_cricket_test(force=force)
    if not matches:
        log.warning('Cricket Test: no results to train from')
        return elo

    for m in matches:
        winner = m['winner']
        elo.process_match(m['home'], m['away'], winner, winner == m['home'] if winner else None)

    elo.save(TEST_ELO_CACHE)
    log.info('Cricket Test Elo trained: %d equipos, %d partidos', len(elo.ratings), len(matches))
    return elo


def _scan_market(api, comp_key, market_type, min_ratings, elo_lookup, predict_fn, dry_run=False):
    """Generico: escanea cricket.winner en una competicion, arma picks
    shadow-only. predict_fn(home, away) -> dict {'home': p, 'away': p}."""
    if len(elo_lookup) < min_ratings:
        log.warning('Cricket %s Elo not ready (%d equipos)', market_type, len(elo_lookup))
        return []

    events = api.get_odds(comp_key)
    if not events:
        return []

    now        = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=168)).isoformat() + 'Z'
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

        if home_cb not in elo_lookup or away_cb not in elo_lookup:
            continue

        mk = ev.get('markets', {}).get('cricket.winner', {})
        if not mk:
            continue
        default_sub = None
        for sv in mk.get('submarkets', {}).values():
            default_sub = sv
            break
        if not default_sub:
            continue

        probs = predict_fn(home_cb, away_cb)

        for sel in default_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price   = float(sel.get('price', 0) or 0)
            murl    = sel.get('marketUrl', '')
            if price < 1.05 or not murl or outcome not in ('home', 'away'):
                continue

            prob = probs.get(outcome)
            if prob is None:
                continue
            team = home_cb if outcome == 'home' else away_cb

            edge = round(prob * price - 1.0, 4)
            if edge < 0.06 or prob < 0.55:
                continue

            picks.append({
                'match':                f'{home_cb} vs {away_cb}',
                'league':               comp_key,
                'sport':                'cricket',
                'event_id':             eid,
                'market':               'cricket.winner',
                'market_url':           murl,
                'price':                price,
                'odds':                 price,
                'label':                f'Cricket: {team}',
                'side':                 team,
                'model_prob':           round(prob, 4),
                'raw_model_prob_uncal': round(prob, 4),
                'confidence':           round(prob, 4),
                'edge':                 edge,
                'shadow':               True,
                '_shadow_only':         True,
                'market_type':          market_type,
                '_max_stake':           1.00,
            })

    log.info('Cricket %s: %d picks (SHADOW)', market_type, len(picks))
    return picks


def scan_cricket_odi(api, state, elo=None, dry_run=False, shadow=True):
    """ODI internacionales. Shadow-only -- no colocar apuestas reales hasta
    validar WR en cantera (mismo checklist que NFL/EuroLeague/Rugby Union)."""
    if elo is None:
        elo = train_cricket_odi_elo()

    def predict_fn(home, away):
        p_h = elo.predict(home, away)
        return {'home': p_h, 'away': 1.0 - p_h}

    return _scan_market(api, 'cricket-international-one-day-internationals',
                         'cricket_odi', 10, elo.ratings, predict_fn, dry_run)


def scan_cricket_test(api, state, elo=None, dry_run=False, shadow=True):
    """Test internacionales. Shadow-only. Nota: el mercado cricket.winner de
    Cloudbet solo tiene 2 outcomes (home/away) -- el draw no es un outcome
    apostable directamente en este mercado, asi que el edge de home/away
    ya viene descontado por P_DRAW_TEST dentro de predict() (ver CricketEloTest),
    no hace falta manejar un tercer outcome aca."""
    if elo is None:
        elo = train_cricket_test_elo()

    def predict_fn(home, away):
        p_h, p_a, _p_draw = elo.predict(home, away)
        return {'home': p_h, 'away': p_a}

    return _scan_market(api, 'cricket-international-tc19e-international-test-match',
                         'cricket_test', 8, elo.ratings, predict_fn, dry_run)
