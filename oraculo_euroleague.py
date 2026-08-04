"""EuroLeague (basketball europeo de clubes) Elo model + scanner para Oraculo.
Shadow mode hasta validar WR.

2026-08-04: Fase 3 del plan de expansion de deportes. Cloudbet
(basketball-international-euroleague) solo tiene mercado de outrights ahora
mismo (temporada oct-mayo, agosto = pausa) -- mismo criterio que NFL/Rugby
Union: se construye en shadow-only, listo para cuando arranque la
temporada real y aparezcan mercados de partido (moneyline/1x2).

Fuente de datos: API oficial de EuroLeague (api-live.euroleague.net),
no ESPN -- distinto de NFL/NBA/WNBA/MMA. home_adv=65 (magnitud similar a
NBA=75/WNBA, algo mas bajo porque el basquet FIBA/europeo tiene menos
partidos por temporada que NBA, ventaja de local historicamente algo
menor en puntos-Elo equivalentes). Mismo patron de archivo que
oraculo_nfl.py (Elo class, fetch/cache/train/scan, mapeo explicito de
nombres, sin fuzzy matching -- evita corromper el Elo con nombres
ambiguos como "Maccabi" que puede referirse a mas de un club historico).
"""
import os, json, time, logging
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
EL_ELO_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'euroleague_elo.json')
EL_RES_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'euroleague_results.json')

# Codigo de club (API oficial EuroLeague) -> nombre completo.
EL_CODE_TO_FULL = {
    'ASV': 'LDLC ASVEL Villeurbanne',
    'BAR': 'FC Barcelona',
    'BAS': 'Kosner Baskonia Vitoria-Gasteiz',
    'DUB': 'Dubai Basketball',
    'HTA': 'Hapoel IBI Tel Aviv',
    'IST': 'Anadolu Efes Istanbul',
    'MAD': 'Real Madrid',
    'MCO': 'AS Monaco',
    'MIL': 'EA7 Emporio Armani Milan',
    'MUN': 'FC Bayern Munich',
    'OLY': 'Olympiacos Piraeus',
    'PAM': 'Valencia Basket',
    'PAN': 'Panathinaikos AKTOR Athens',
    'PAR': 'Partizan Mozzart Bet Belgrade',
    'PRS': 'Paris Basketball',
    'RED': 'Crvena Zvezda Meridianbet Belgrade',
    'TEL': 'Maccabi Rapyd Tel Aviv',
    'ULK': 'Fenerbahce Beko Istanbul',
    'VIR': 'Virtus Bologna',
    'ZAL': 'Zalgiris Kaunas',
}

# Nombre completo (API EuroLeague) -> outcome slug de Cloudbet. Confirmado
# 2026-08-04 contra el mercado de outrights (basketball-international-euroleague,
# 20 selecciones). MCO (AS Monaco) NO aparece en el mercado de Cloudbet --
# se deja fuera del mapa a proposito (mejor perder un pick que adivinar).
EL_FULL_TO_CB_SLUG = {
    'LDLC ASVEL Villeurbanne':               's-asvel-lyon',
    'FC Barcelona':                          's-fc-barcelona',
    'Kosner Baskonia Vitoria-Gasteiz':       's-baskonia',
    'Dubai Basketball':                      's-dubai-basketball',
    'Hapoel IBI Tel Aviv':                   's-hap-dot-tel-aviv',
    'Anadolu Efes Istanbul':                 's-anadolu-efes',
    'Real Madrid':                           's-real-madrid',
    'EA7 Emporio Armani Milan':              's-olimpia-milano',
    'FC Bayern Munich':                      's-fc-bayern',
    'Olympiacos Piraeus':                    's-olympiakos',
    'Valencia Basket':                       's-valencia-bc',
    'Panathinaikos AKTOR Athens':            's-panathinaikos',
    'Partizan Mozzart Bet Belgrade':         's-partizan-belgrade',
    'Paris Basketball':                      's-paris-basketball',
    'Crvena Zvezda Meridianbet Belgrade':    's-crvena-zvezda',
    'Maccabi Rapyd Tel Aviv':                's-maccabi-tel-aviv',
    'Fenerbahce Beko Istanbul':              's-fenerbahce',
    'Virtus Bologna':                        's-virtus-bologna',
    'Zalgiris Kaunas':                       's-zalgiris-kaunas',
}
CB_SLUG_TO_FULL = {v: k for k, v in EL_FULL_TO_CB_SLUG.items()}


class EuroleagueElo:
    """Elo con ventaja de local. K=20, margin-of-victory scaling (deporte de
    puntaje alto, igual criterio que NBA/NFL)."""

    def __init__(self, k=20, initial=1500, home_adv=65):
        self.ratings      = defaultdict(lambda: initial)
        self.k            = k
        self.home_adv     = home_adv
        self._match_count = defaultdict(int)
        self._form        = defaultdict(list)

    def process_match(self, winner, loser, winner_home, margin=0):
        mov = min(1.6, max(1.0, 1.0 + (abs(margin) - 5) * 0.02))
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

    def predict(self, home, away):
        r_h = self.ratings[home] + self.home_adv
        r_a = self.ratings[away]
        return 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))

    def form(self, team, n=10):
        recent = self._form.get(team, [])
        return sum(recent[-n:]) / len(recent[-n:]) if len(recent) >= 3 else None

    def save(self):
        os.makedirs(os.path.dirname(EL_ELO_CACHE), exist_ok=True)
        with open(EL_ELO_CACHE, 'w') as f:
            json.dump({
                'ratings':     dict(self.ratings),
                'match_count': dict(self._match_count),
                'form':        dict(self._form),
            }, f)

    def load(self):
        if not os.path.exists(EL_ELO_CACHE):
            return False
        try:
            data = json.load(open(EL_ELO_CACHE))
            for k, v in data.get('ratings', {}).items():
                self.ratings[k] = v
            for k, v in data.get('match_count', {}).items():
                self._match_count[k] = v
            for k, v in data.get('form', {}).items():
                self._form[k] = v
            return True
        except Exception:
            return False


def fetch_euroleague_results(force=False):
    """Fetch resultados de EuroLeague via API oficial (api-live.euroleague.net).
    Temporada actual + 1 anterior para mas profundidad de Elo. TTL 6h."""
    os.makedirs(os.path.dirname(EL_RES_CACHE), exist_ok=True)
    if not force and os.path.exists(EL_RES_CACHE):
        age = time.time() - os.path.getmtime(EL_RES_CACHE)
        if age < 21600:
            try:
                cached = json.load(open(EL_RES_CACHE))
                if cached:
                    return cached
            except Exception:
                pass

    import urllib.request
    games = []
    for season_code in ('E2025', 'E2024'):
        url = 'https://api-live.euroleague.net/v2/competitions/E/seasons/%s/games' % season_code
        try:
            req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            for g in data.get('data', []):
                if not g.get('played'):
                    continue
                local = g.get('local') or {}
                road  = g.get('road') or {}
                hn = EL_CODE_TO_FULL.get((local.get('club') or {}).get('code', ''))
                an = EL_CODE_TO_FULL.get((road.get('club') or {}).get('code', ''))
                hs, aws = local.get('score'), road.get('score')
                if not hn or not an or not hs or not aws:
                    continue
                hp, ap = int(hs), int(aws)
                games.append({
                    'date': (g.get('date') or '')[:10],
                    'home': hn, 'away': an,
                    'home_pts': hp, 'away_pts': ap,
                    'winner': hn if hp > ap else an,
                    'margin': abs(hp - ap),
                })
        except Exception as e:
            log.debug('Euroleague results fetch failed for %s: %s', season_code, e)

    if not games:
        try:
            return json.load(open(EL_RES_CACHE)) if os.path.exists(EL_RES_CACHE) else []
        except Exception:
            return []

    games.sort(key=lambda g: g['date'])
    log.info('Euroleague: %d games fetched', len(games))
    with open(EL_RES_CACHE, 'w') as f:
        json.dump(games, f)
    return games


def train_euroleague_elo(force=False):
    """Train Euroleague Elo. Cache TTL 6h (mismo patron NFL/WNBA/MMA)."""
    elo = EuroleagueElo()
    cache_age = (time.time() - os.path.getmtime(EL_ELO_CACHE)) if os.path.exists(EL_ELO_CACHE) else 1e18
    if not force and cache_age < 21600 and elo.load() and len(elo.ratings) >= 15:
        log.info('Euroleague Elo loaded from cache (%d equipos)', len(elo.ratings))
        return elo

    games = fetch_euroleague_results(force=force)
    if not games:
        log.warning('Euroleague: no results to train from')
        return elo

    for g in sorted(games, key=lambda x: x['date']):
        winner = g['winner']
        loser  = g['home'] if g['away'] == winner else g['away']
        elo.process_match(winner, loser, winner == g['home'], g.get('margin', 0))

    elo.save()
    log.info('Euroleague Elo trained: %d equipos, %d partidos', len(elo.ratings), len(games))
    return elo


def scan_euroleague(api, state, elo=None, dry_run=False, shadow=True):
    """Scan Cloudbet Euroleague (basketball-international-euroleague).
    shadow=True: logs a Sibila solamente, nunca apuesta plata real. No
    pasar a vivo hasta 20+ picks con WR validado -- mismo checklist que
    MMA/NFL/WNBA/sets_under.

    2026-08-04: mercado de partido (moneyline) todavia no visible en
    Cloudbet -- temporada en pausa, solo hay mercado de outrights. Se
    prueban 2 keys posibles (basketball.moneyline / basketball.1x2, mismo
    patron de migracion ya visto en oraculo_wnba.py) -- si ninguna aparece
    todavia, el scan simplemente no encuentra nada, no rompe nada."""
    if elo is None:
        elo = train_euroleague_elo()

    if len(elo.ratings) < 15:
        log.warning('Euroleague Elo not ready (%d equipos) -- need more history', len(elo.ratings))
        return []

    events = api.get_odds('basketball-international-euroleague')
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

        markets = ev.get('markets', {})
        mk = markets.get('basketball.moneyline') or markets.get('basketball.1x2')
        if not mk:
            continue

        default_sub = None
        for sv in mk.get('submarkets', {}).values():
            default_sub = sv
            break
        if not default_sub:
            continue

        home = CB_SLUG_TO_FULL.get('s-' + home_cb.lower().replace(' ', '-'))
        away = CB_SLUG_TO_FULL.get('s-' + away_cb.lower().replace(' ', '-'))
        if not home or not away:
            log.debug('Euroleague [skip-unmapped]: %s vs %s', home_cb, away_cb)
            continue

        if elo._match_count.get(home, 0) < 4 or elo._match_count.get(away, 0) < 4:
            log.debug('Euroleague [skip-low-data]: %s (%d) vs %s (%d)',
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
            if price > 3.50:
                continue

            prob = prob_home if outcome == 'home' else prob_away
            team = home_cb if outcome == 'home' else away_cb

            f    = elo.form(home if outcome == 'home' else away)
            prob = min(0.92, max(0.08, prob + (0.02 if f and f > 0.70 else -0.02 if f and f < 0.30 else 0.0)))

            edge = round(prob * price - 1.0, 4)
            if edge < 0.08 or prob < 0.55:
                continue

            picks.append({
                'match':                 '%s vs %s' % (home_cb, away_cb),
                'league':                'basketball-international-euroleague',
                'sport':                 'basketball',
                'event_id':              eid,
                'market':                'basketball.moneyline',
                'market_url':            murl,
                'price':                 price,
                'odds':                  price,
                'label':                 'Euroleague: %s' % team,
                'side':                  team,
                'model_prob':            round(prob, 4),
                'raw_model_prob_uncal':  round(prob, 4),
                'confidence':            round(prob, 4),
                'edge':                  edge,
                'shadow':                shadow,
                'market_type':           'euroleague_ml',
                '_max_stake':            1.00,
            })

    log.info('Euroleague: %d value picks (%s)', len(picks), 'SHADOW' if shadow else 'LIVE')
    return picks
