"""NFL Elo model + scanner for Oraculo. Shadow mode until 20+ picks validated.

2026-08-04: nuevo mercado, Fase 1 del plan de expansion de deportes. A
diferencia de MMA (jaula neutral) y CS de tenis, NFL SI tiene ventaja de
local real y consistente -- home_adv=57 (calibrado en linea con el estandar
de la industria de ~2.0-2.5 puntos de spread equivalentes a 400*log10(...),
mismo orden de magnitud que NBA/WNBA home_adv=75 mas conservador, dado que
NFL juega solo 1x/semana y el home advantage medido historicamente es menor
en puntos-Elo que en deportes de mas partidos). Sigue el mismo patron de
archivo que oraculo_mma.py (Elo class, fetch/cache/train) y oraculo_wnba.py
(mapeo explicito de nombres Cloudbet->ESPN, evita matching difuso que
corrompe el Elo en un deporte con nombres ambiguos: NY Giants vs NY Jets,
LA Rams vs LA Chargers).
"""
import os, json, time, logging
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
NFL_ELO_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nfl_elo.json')
NFL_RES_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'nfl_results.json')

# Cloudbet display name -> ESPN full name. Confirmado 2026-08-04 contra las
# 32 franquicias reales devueltas por /odds/competitions/american-football-usa-nfl.
CB_TO_NFL = {
    'ARZ Cardinals':   'Arizona Cardinals',
    'ATL Falcons':     'Atlanta Falcons',
    'BAL Ravens':      'Baltimore Ravens',
    'BUF Bills':       'Buffalo Bills',
    'CAR Panthers':    'Carolina Panthers',
    'CHI Bears':       'Chicago Bears',
    'CIN Bengals':     'Cincinnati Bengals',
    'CLE Browns':      'Cleveland Browns',
    'DAL Cowboys':     'Dallas Cowboys',
    'DEN Broncos':     'Denver Broncos',
    'DET Lions':       'Detroit Lions',
    'GB Packers':      'Green Bay Packers',
    'HOU Texans':      'Houston Texans',
    'IND Colts':       'Indianapolis Colts',
    'JAX Jaguars':     'Jacksonville Jaguars',
    'KC Chiefs':       'Kansas City Chiefs',
    'LA Chargers':     'Los Angeles Chargers',
    'LA Rams':         'Los Angeles Rams',
    'LV Raiders':      'Las Vegas Raiders',
    'MIA Dolphins':    'Miami Dolphins',
    'MIN Vikings':     'Minnesota Vikings',
    'NE Patriots':     'New England Patriots',
    'NO Saints':       'New Orleans Saints',
    'NY Giants':       'New York Giants',
    'NY Jets':         'New York Jets',
    'PHI Eagles':      'Philadelphia Eagles',
    'PIT Steelers':    'Pittsburgh Steelers',
    'SEA Seahawks':    'Seattle Seahawks',
    'SF 49ers':        'San Francisco 49ers',
    'TB Buccaneers':   'Tampa Bay Buccaneers',
    'TEN Titans':      'Tennessee Titans',
    'WAS Commanders':  'Washington Commanders',
}
NFL_FULL_TO_CB = {v: k for k, v in CB_TO_NFL.items()}
REAL_NFL_TEAMS = set(CB_TO_NFL.values())

# nflverse team codes (games.csv) -> nombre completo (mismo set que CB_TO_NFL.values()).
# Codigos historicos de franquicias reubicadas (OAK, SD, STL, etc) quedan
# fuera a proposito -- se descartan en el fetch, no corrompen el Elo actual.
NFLVERSE_TO_FULL = {
    'ARI': 'Arizona Cardinals',   'ATL': 'Atlanta Falcons',
    'BAL': 'Baltimore Ravens',    'BUF': 'Buffalo Bills',
    'CAR': 'Carolina Panthers',   'CHI': 'Chicago Bears',
    'CIN': 'Cincinnati Bengals',  'CLE': 'Cleveland Browns',
    'DAL': 'Dallas Cowboys',      'DEN': 'Denver Broncos',
    'DET': 'Detroit Lions',       'GB':  'Green Bay Packers',
    'HOU': 'Houston Texans',      'IND': 'Indianapolis Colts',
    'JAX': 'Jacksonville Jaguars','KC':  'Kansas City Chiefs',
    'LAC': 'Los Angeles Chargers','LAR': 'Los Angeles Rams','LA': 'Los Angeles Rams',  # 2026-08-04: nflverse usa 'LA' para Rams, no 'LAR'
    'LV':  'Las Vegas Raiders',   'MIA': 'Miami Dolphins',
    'MIN': 'Minnesota Vikings',   'NE':  'New England Patriots',
    'NO':  'New Orleans Saints',  'NYG': 'New York Giants',
    'NYJ': 'New York Jets',       'PHI': 'Philadelphia Eagles',
    'PIT': 'Pittsburgh Steelers', 'SEA': 'Seattle Seahawks',
    'SF':  'San Francisco 49ers', 'TB':  'Tampa Bay Buccaneers',
    'TEN': 'Tennessee Titans',    'WAS': 'Washington Commanders',
}


def _resolve_name(cb_name: str) -> str:
    if cb_name in CB_TO_NFL:
        return CB_TO_NFL[cb_name]
    # No fuzzy fallback a proposito -- a diferencia de WNBA/MMA, los nombres
    # NFL son ambiguos por ciudad compartida (NY/LA) y un match flojo por
    # ultima palabra puede confundir franquicias reales. Si no esta en el
    # mapa exacto, se descarta (mejor perder un pick que corromper el Elo).
    return None


class NFLElo:
    """Elo rating system for NFL. home_adv=57 (ventaja de local real, a
    diferencia de MMA que es jaula neutral). K=20, margin-of-victory scaling
    como NBA/WNBA (deporte de puntaje alto, el margen es señal real)."""

    def __init__(self, k=20, initial=1500, home_adv=57):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)
        self._form        = defaultdict(list)

    def process_match(self, winner: str, loser: str, winner_home: bool, margin: int = 0):
        mov = min(1.6, max(1.0, 1.0 + (abs(margin) - 3) * 0.03))
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
        os.makedirs(os.path.dirname(NFL_ELO_CACHE), exist_ok=True)
        with open(NFL_ELO_CACHE, 'w') as f:
            json.dump({
                'ratings':     dict(self.ratings),
                'match_count': dict(self._match_count),
                'form':        dict(self._form),
            }, f)

    def load(self) -> bool:
        if not os.path.exists(NFL_ELO_CACHE):
            return False
        try:
            data = json.load(open(NFL_ELO_CACHE))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in data.get('form', {}).items():
                self._form[k] = v
            return True
        except Exception:
            return False


def fetch_nfl_results(force: bool = False) -> list:
    """Fetch NFL season results desde nflverse (GitHub Releases, dataset
    abierto de la comunidad de analitica NFL). 2026-08-04: ESPN
    site.api.espn.com esta bloqueado a nivel WAF (Akamai) especificamente
    para el endpoint de NFL -- confirmado con 403 Access Denied incluso
    sin parametros de fecha, mientras WNBA/MMA/NBA siguen funcionando bien
    con la misma infraestructura. nflverse es un snapshot completo (no
    necesita ventanas de fecha como ESPN), TTL 6h alcanza de sobra ya que
    se actualiza como mucho 1x/semana en temporada."""
    os.makedirs(os.path.dirname(NFL_RES_CACHE), exist_ok=True)
    if not force and os.path.exists(NFL_RES_CACHE):
        age = time.time() - os.path.getmtime(NFL_RES_CACHE)
        if age < 21600:
            try:
                cached = json.load(open(NFL_RES_CACHE))
                if cached:
                    return cached
            except Exception:
                pass

    import urllib.request, csv, io
    url = 'https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv'
    games = []
    try:
        req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urllib.request.urlopen(req, timeout=30).read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(data))
        for row in reader:
            # game_type: REG=temporada regular, POST=playoffs -- excluye PRE
            # (semanas de exhibicion con suplentes distorsionan el Elo real).
            if row.get('game_type') not in ('REG', 'POST'):
                continue
            hs, aws = row.get('home_score'), row.get('away_score')
            if not hs or not aws:
                continue  # partido futuro/programado, sin resultado todavia
            try:
                hp, ap = int(float(hs)), int(float(aws))
            except (ValueError, TypeError):
                continue
            hn = NFLVERSE_TO_FULL.get(row.get('home_team', ''))
            an = NFLVERSE_TO_FULL.get(row.get('away_team', ''))
            if not hn or not an:
                continue  # franquicia historica reubicada (OAK/SD/STL) -- fuera del mapa a proposito
            if hp == ap:
                continue  # 2026-08-04 fix: empates reales de NFL (raros pero existen, ej.
                # Browns-Steelers 21-21 2018) caian al else de 'winner' y se contaban
                # como victoria del visitante -- corrompia el Elo en silencio
            games.append({
                'date': row.get('gameday', ''),
                'home': hn, 'away': an,
                'home_pts': hp, 'away_pts': ap,
                'winner': hn if hp > ap else an,
                'margin': abs(hp - ap),
            })
    except Exception as e:
        log.debug('NFL results fetch failed (nflverse): %s', e)
        try:
            return json.load(open(NFL_RES_CACHE)) if os.path.exists(NFL_RES_CACHE) else []
        except Exception:
            return []

    games.sort(key=lambda g: g['date'])
    log.info('NFL: %d games fetched from nflverse', len(games))
    with open(NFL_RES_CACHE, 'w') as f:
        json.dump(games, f)
    return games


def train_nfl_elo(force: bool = False) -> NFLElo:
    """Train NFL Elo from ESPN results. Cache TTL 6h (mismo patron WNBA/MMA)."""
    elo = NFLElo()
    cache_age = (time.time() - os.path.getmtime(NFL_ELO_CACHE)) if os.path.exists(NFL_ELO_CACHE) else 1e18
    if not force and cache_age < 21600 and elo.load() and len(elo.ratings) >= 28:
        log.info('NFL Elo loaded from cache (%d teams)', len(elo.ratings))
        return elo

    # 2026-08-04 fix: elo.load() (llamado arriba dentro del and) puebla self.ratings
    # con efecto de lado aunque el cache este parcial/corrupto (<28 equipos) -- sin
    # esta instancia nueva, el reentrenamiento de abajo se aplicaba ENCIMA de los
    # ratings ya cargados en vez de arrancar de 1500 neutral, acumulando swings de
    # forma no idempotente cada vez que este camino se disparaba.
    elo = NFLElo()

    games = fetch_nfl_results(force=force)
    if not games:
        log.warning('NFL: no results to train from')
        return elo

    for g in sorted(games, key=lambda x: x['date']):
        winner = g['winner']
        loser  = g['home'] if g['away'] == winner else g['away']
        elo.process_match(winner, loser, winner == g['home'], g.get('margin', 0))

    elo.save()
    log.info('NFL Elo trained: %d teams, %d games', len(elo.ratings), len(games))
    return elo


def scan_nfl(api, state, elo: NFLElo = None, dry_run: bool = False, shadow: bool = True) -> list:
    """Scan Cloudbet NFL markets (american-football-usa-nfl) via
    american_football.moneyline. shadow=True: logs a Sibila solamente, nunca
    apuesta plata real. No pasar a vivo hasta 20+ picks con WR validado --
    mismo checklist de promocion que MMA/WNBA/sets_under."""
    if elo is None:
        elo = train_nfl_elo()

    if len(elo.ratings) < 28:
        log.warning('NFL Elo not ready (%d teams) -- need more history', len(elo.ratings))
        return []

    events = api.get_odds('american-football-usa-nfl')
    if not events:
        return []

    now        = datetime.utcnow()
    cutoff_max = (now + timedelta(hours=168)).isoformat() + 'Z'  # semana NFL completa
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

        mk = ev.get('markets', {}).get('american_football.moneyline', {})
        if not mk:
            continue

        default_sub = None
        for sv in mk.get('submarkets', {}).values():
            default_sub = sv
            break
        if not default_sub:
            continue

        home = _resolve_name(home_cb)
        away = _resolve_name(away_cb)
        if not home or not away:
            log.debug('NFL [skip-unmapped]: %s vs %s', home_cb, away_cb)
            continue

        if elo._match_count.get(home, 0) < 4 or elo._match_count.get(away, 0) < 4:
            log.debug('NFL [skip-low-data]: %s (%d) vs %s (%d)',
                      home_cb, elo._match_count.get(home, 0),
                      away_cb, elo._match_count.get(away, 0))
            continue

        prob_home = elo.predict(home, away)
        prob_away = 1.0 - prob_home

        for sel in default_sub.get('selections', []):
            outcome = sel.get('outcome', '')
            price   = float(sel.get('price', 0) or 0)
            murl    = sel.get('marketUrl', '')
            status  = sel.get('status', '')

            if status != 'SELECTION_ENABLED' or price < 1.05 or not murl or outcome not in ('home', 'away'):
                continue
            if price > 3.50:  # cap inicial conservador, sin cantera propia todavia
                continue

            prob = prob_home if outcome == 'home' else prob_away
            team = home_cb if outcome == 'home' else away_cb

            f    = elo.form(home if outcome == 'home' else away)
            prob = min(0.92, max(0.08, prob + (0.02 if f and f > 0.70 else -0.02 if f and f < 0.30 else 0.0)))

            edge = round(prob * price - 1.0, 4)
            if edge < 0.08 or prob < 0.55:
                continue

            picks.append({
                'match':                 f'{home_cb} vs {away_cb}',
                'league':                'american-football-usa-nfl',
                'sport':                 'american_football',
                'event_id':              eid,
                'market':                'american_football.moneyline',
                'market_url':            murl,
                'price':                 price,
                'odds':                  price,
                'label':                 f'NFL: {team}',
                'side':                  team,
                'model_prob':            round(prob, 4),
                'raw_model_prob_uncal':  round(prob, 4),
                'confidence':            round(prob, 4),
                'edge':                  edge,
                'shadow':                shadow,
                'market_type':           'nfl_ml',
                '_max_stake':            1.00,
            })

    log.info('NFL: %d value picks (%s)', len(picks), 'SHADOW' if shadow else 'LIVE')
    return picks
