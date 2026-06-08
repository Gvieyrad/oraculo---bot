#!/usr/bin/env python3
"""
ganagol_odds.py — Auto-fetch 1X2 market odds.

FUENTE PRINCIPAL: The-Odds-API (free tier, 500 requests gratuitos)
  Setup (una sola vez):
    1. Registrarse en https://the-odds-api.com (solo email, sin tarjeta)
    2. Copiar la API key
    3. Exportar: export ODDS_API_KEY="tu_key"
  Consmo: ~1 request por sesion de Ganagol (bien dentro del free tier)

FALLBACK: modelo ELO/DC sin cuotas de mercado (funciona sin key).

Uso standalone:
  python3 ganagol_odds.py "Sweden" "Greece"
  python3 ganagol_odds.py --clear-cache
"""
import os, sys, json, time, unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher

try:
    import requests as _req
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'odds_events.json')
CACHE_TTL   = 7200   # 2 horas

ODDS_API_KEY = os.environ.get('ODDS_API_KEY', '')
ODDS_API_URL = 'https://api.the-odds-api.com/v4'
BLEND_ALPHA  = 0.60  # 60% mercado, 40% modelo
FUZZ_MIN     = 0.58  # similitud minima de nombre

# Sports disponibles en el free tier de The-Odds-API (junio 2026)
# Cobertura: Copa Lib/Sud, WC2026, Noruega, Suecia, Brasil-B, Chile, Japon, China, Spain2
_SOCCER_SPORTS = [
    'soccer_conmebol_copa_libertadores',   # Copa Libertadores
    'soccer_conmebol_copa_sudamericana',   # Copa Sudamericana
    'soccer_fifa_world_cup',               # WC 2026 (grupo + playoffs, desde Jun 11)
    'soccer_norway_eliteserien',           # Eliteserien Noruega
    'soccer_sweden_allsvenskan',           # Allsvenskan Suecia
    'soccer_sweden_superettan',            # Superettan Suecia
    'soccer_brazil_serie_b',              # Brasil Serie B
    'soccer_chile_campeonato',             # Primera Division Chile
    'soccer_japan_j_league',              # J-League Japon
    'soccer_china_superleague',           # Super League China
    'soccer_spain_segunda_division',      # La Liga 2
]

# ── Normalizacion ─────────────────────────────────────────────────────────────

def _norm(s):
    nfkd = unicodedata.normalize('NFKD', s.lower().strip())
    return ''.join(c for c in nfkd if not unicodedata.combining(c))

def _sim(a, b):
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
        if d.get('ts', 0) + CACHE_TTL > time.time():
            return d.get('events', [])
    except Exception:
        pass
    return None

def _save_cache(events):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'events': events}, f)
    except Exception:
        pass

# ── The-Odds-API ──────────────────────────────────────────────────────────────

def _api_get(path, params=None, timeout=10):
    if not _HAS_REQUESTS or not ODDS_API_KEY:
        return None
    url = f'{ODDS_API_URL}{path}'
    p = dict(apiKey=ODDS_API_KEY)
    if params:
        p.update(params)
    try:
        r = _req.get(url, params=p, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            print('  [odds] API key invalida. Revisa ODDS_API_KEY.')
        elif r.status_code == 422:
            pass  # sport no disponible en free tier
    except Exception:
        pass
    return None

def _fetch_odds_events(verbose=True):
    """Descarga todos los eventos con odds 1X2 de los sports relevantes."""
    if not ODDS_API_KEY:
        return []

    if verbose:
        print('  [odds] Consultando The-Odds-API...', end=' ', flush=True)

    all_events = []
    for sport in _SOCCER_SPORTS:
        data = _api_get(f'/sports/{sport}/odds/', {
            'regions': 'eu',
            'markets': 'h2h',
            'oddsFormat': 'decimal',
        })
        if not data:
            continue
        for ev in data:
            try:
                home = ev['home_team']
                away = ev['away_team']
                bookmakers = ev.get('bookmakers', [])
                if not bookmakers:
                    continue
                # tomar el primer bookmaker con h2h market
                for bm in bookmakers:
                    for mkt in bm.get('markets', []):
                        if mkt['key'] != 'h2h':
                            continue
                        outcomes = {o['name']: o['price'] for o in mkt['outcomes']}
                        od1 = outcomes.get(home)
                        od2 = outcomes.get(away)
                        odx = None
                        for k in outcomes:
                            if k not in (home, away):
                                odx = outcomes[k]
                                break
                        if od1 and od2 and odx:
                            p1, px, p2 = 1/od1, 1/odx, 1/od2
                            t = p1 + px + p2
                            all_events.append({
                                'home': home, 'away': away,
                                'ph': p1/t, 'pd': px/t, 'pa': p2/t,
                                'sport': sport,
                            })
                        break
                    break
            except Exception:
                pass
        time.sleep(0.2)

    if verbose:
        print(f'{len(all_events)} partidos con odds', flush=True)
    return all_events

# ── In-memory event cache ─────────────────────────────────────────────────────

_EVENTS = None
_NO_KEY_WARNED = False

def _ensure_events(verbose=True):
    global _EVENTS, _NO_KEY_WARNED
    if _EVENTS is not None:
        return _EVENTS

    if not ODDS_API_KEY:
        if verbose and not _NO_KEY_WARNED:
            _NO_KEY_WARNED = True
            print('  [odds] Sin API key — modo modelo puro.')
            print('  [odds] Para activar odds: export ODDS_API_KEY="tu_key"')
            print('  [odds] Key gratis en: https://the-odds-api.com')
        _EVENTS = []
        return _EVENTS

    # Intenta cache local
    cached = _load_cache()
    if cached is not None:
        _EVENTS = cached
        if verbose:
            print(f'  [odds] Cache local ({len(_EVENTS)} partidos)', flush=True)
        return _EVENTS

    # Fetch fresco
    _EVENTS = _fetch_odds_events(verbose=verbose)
    if _EVENTS:
        _save_cache(_EVENTS)
    return _EVENTS

# ── API publica ───────────────────────────────────────────────────────────────

def find_event(home_raw, away_raw):
    """
    Busca el evento que mejor coincida.
    Returns (event_dict, score) o (None, 0).
    """
    events = _ensure_events()
    if not events:
        return None, 0.0

    best, best_score = None, 0.0
    for ev in events:
        h_s = _sim(home_raw, ev['home'])
        a_s = _sim(away_raw, ev['away'])
        if h_s < FUZZ_MIN or a_s < FUZZ_MIN:
            continue
        score = (h_s * 2 + a_s) / 3
        if score > best_score:
            best_score = score
            best = ev

    return best, best_score

def fetch_probs(home_raw, away_raw):
    """
    Devuelve (ph, pd, pa, label) o (None, None, None, None).
    """
    ev, score = find_event(home_raw, away_raw)
    if ev is None:
        return None, None, None, None

    label = f"{ev['home']} vs {ev['away']} ({score:.0%})"
    return ev['ph'], ev['pd'], ev['pa'], label

def blend(model_ph, model_pd, model_pa, home_raw, away_raw, alpha=BLEND_ALPHA):
    """
    Mezcla probabilidades del modelo con cuotas de mercado.
    alpha = peso del mercado (default 0.60).
    Returns (ph, pd, pa, label) donde label=None si no hay cuotas.
    """
    mkt_ph, mkt_pd, mkt_pa, label = fetch_probs(home_raw, away_raw)
    if mkt_ph is None:
        return model_ph, model_pd, model_pa, None

    ph = alpha * mkt_ph + (1 - alpha) * model_ph
    pd = alpha * mkt_pd + (1 - alpha) * model_pd
    pa = alpha * mkt_pa + (1 - alpha) * model_pa
    t  = ph + pd + pa
    return ph/t, pd/t, pa/t, label

def clear_cache():
    global _EVENTS
    _EVENTS = None
    try:
        os.remove(CACHE_FILE)
        print('  [odds] Cache borrado.')
    except Exception:
        pass

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not _HAS_REQUESTS:
        sys.exit('pip install requests')

    if '--clear-cache' in sys.argv:
        clear_cache()
        sys.exit(0)

    if len(sys.argv) >= 3:
        h, a = sys.argv[1], sys.argv[2]
        ph, pd, pa, lbl = fetch_probs(h, a)
        if ph:
            pk = 'L' if ph == max(ph,pd,pa) else ('E' if pd == max(ph,pd,pa) else 'V')
            print(f'\n  Match: {lbl}')
            print(f'  L={ph:.1%}  E={pd:.1%}  V={pa:.1%}  [{pk}]')
        else:
            print(f'  Sin odds para: {h} vs {a}')
    else:
        tests = [
            ('Suecia','Grecia'), ('Hungría','Finlandia'),
            ('España F','Inglaterra F'), ('Dinamarca F','Suecia F'),
            ('Birmingham Legion','Louisville'), ('Agadir','FUS Rabat'),
        ]
        print(f"\n{'Partido':<38} {'L%':>6} {'E%':>6} {'V%':>6}  Pk")
        print('-' * 65)
        for h, a in tests:
            ph, pd, pa, lbl = fetch_probs(h, a)
            if ph:
                pk = 'L' if ph == max(ph,pd,pa) else ('E' if pd == max(ph,pd,pa) else 'V')
                print(f"{h+' vs '+a:<38} {ph:>6.1%} {pd:>6.1%} {pa:>6.1%}  [{pk}]  {lbl}")
            else:
                print(f"{h+' vs '+a:<38}  sin odds (sin key o partido no encontrado)")
