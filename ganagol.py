#!/usr/bin/env python3
"""
ganagol.py — Genera predicciones 1X2 para Ganagol de Latinka.
Modelo: Dixon-Coles (WA) + GBC meta-learner blend (α=0.20).

Uso:
  python3 ganagol.py
  python3 ganagol.py "Liverpool" "Man City" "Barcelona" "Real Madrid" ...
  (pares de equipos: local visitante local visitante ...)

Modo interactivo acepta pick manual al final:
  Jugada 11: La Serena vs Limache V
  Jugada 12: CA Cerro vs Central Esp. X
"""
import sys, json, os, pickle
from difflib import get_close_matches, SequenceMatcher
try:
    from ganagol_standings import load_standings as _load_api_standings, lookup_team, format_tag as _stag
    _HAS_STANDINGS = True
except ImportError:
    _HAS_STANDINGS = False
    def _load_api_standings(): return {}
    def lookup_team(n, c): return None
    def _stag(i): return None

try:
    from ganagol_standings_csv import load_csv_standings as _load_csv_standings
    _HAS_CSV_STANDINGS = True
except ImportError:
    _HAS_CSV_STANDINGS = False
    def _load_csv_standings(): return {}

def load_standings():
    result = {}
    if _HAS_STANDINGS:
        try:
            result = _load_api_standings()
        except PermissionError:
            # standings_cache.json is root-owned; fall back to reading existing cache
            _cache = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'standings_cache.json')
            try:
                with open(_cache) as _f:
                    result = json.load(_f)
            except Exception:
                pass
        except Exception:
            pass
    if _HAS_CSV_STANDINGS:
        for k, v in _load_csv_standings().items():
            if k not in result:
                result[k] = v
    return result
from math import exp, factorial

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DC_CACHE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'dixon_coles.json')
GBC_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'gbc_ganagol.pkl')
META_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'meta_lr_ganagol.pkl')


# ── DC math ───────────────────────────────────────────────────────────────────

def _tau(x, y, lam, mu, rho):
    if x == 0 and y == 0: return 1 - lam * mu * rho
    elif x == 0 and y == 1: return 1 + lam * rho
    elif x == 1 and y == 0: return 1 + mu * rho
    elif x == 1 and y == 1: return 1 - rho
    return 1.0

def _pmf(k, lam):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    return exp(-lam) * (lam ** k) / factorial(k)


# ── Model loading ─────────────────────────────────────────────────────────────

def load_model():
    if not os.path.exists(DC_CACHE):
        sys.exit(f"Cache no encontrado: {DC_CACHE}")
    with open(DC_CACHE) as f:
        return json.load(f)


_gbc_bundle = None

def _load_gbc():
    global _gbc_bundle
    if _gbc_bundle is None:
        if os.path.exists(GBC_CACHE):
            try:
                with open(GBC_CACHE, 'rb') as f:
                    _gbc_bundle = pickle.load(f)
            except Exception:
                _gbc_bundle = {}
        else:
            _gbc_bundle = {}
    return _gbc_bundle


_meta_bundle = None

def _load_meta_lr():
    global _meta_bundle
    if _meta_bundle is None:
        if os.path.exists(META_CACHE):
            try:
                with open(META_CACHE, 'rb') as f:
                    _meta_bundle = pickle.load(f)
            except Exception:
                _meta_bundle = {}
        else:
            _meta_bundle = {}
    return _meta_bundle


# ── League home advantage ─────────────────────────────────────────────────────

_LEAGUE_HOME_ADV = {
    'PL': 1.18, 'E0': 1.18, 'E1': 1.18,
    'PD': 1.22, 'SP1': 1.22,
    'SA': 1.22, 'I1': 1.22,
    'BL1': 1.20, 'D1': 1.20,
    'FL1': 1.20, 'F1': 1.20,
    'DED': 1.18, 'N1': 1.18,
    'PPL': 1.22, 'P1': 1.22,
    'TUR': 1.25, 'T1': 1.25,
    'ARG': 1.35,
    'BRA': 1.25,
    'CHI': 1.30,
    'URU': 1.25,
    'RUS': 1.25,
    'JPN': 1.15,
    'NOR': 1.20,
    'IRL': 1.25,
    'PER': 1.30,
    'CYP': 1.22,
    'EGY': 1.28,
    'INTL': 1.05,
}


# ── Empirical league draw rates (calibrated on 23 885 matches) ────────────────

_LEAGUE_DRAW_RATE = {
    'ARG': 0.303, 'TUR': 0.291, 'URU': 0.282, 'PL': 0.273,
    'PPL': 0.271, 'BRA': 0.269, 'DED': 0.261, 'SA':  0.259,
    'RUS': 0.258, 'JPN': 0.249, 'CHI': 0.249, 'PD':  0.246,
    'BL1': 0.245, 'FL1': 0.243,
    'NOR': 0.250, 'IRL': 0.270, 'PER': 0.265,
    'CYP': 0.245, 'EGY': 0.255, 'INTL': 0.265,
}
_GLOBAL_DRAW_RATE = 0.270


def _draw_risk(lam, mu, league='', pd=0.0):
    """Return draw risk level: 'HIGH' | 'MED' | 'LOW'.

    Data shows: xG (lam+mu) and strength imbalance are the main predictors.
    Recent form / morale have NO significant correlation (tested on 23 885 matches).

    HIGH: actual draw rate ~39 % (lam+mu < 2.0) or ~35 % (evenly matched + low xG)
    MED : actual draw rate ~30-33 %
    LOW : model has a clear favourite, fewer draws expected
    """
    xg  = lam + mu
    imb = abs(lam - mu) / (xg + 0.001)
    lg_dr = _LEAGUE_DRAW_RATE.get(league, _GLOBAL_DRAW_RATE)

    if xg < 2.0 or pd >= 0.35 or (xg < 2.3 and imb < 0.08 and lg_dr >= 0.27):
        return 'HIGH'
    if (xg < 2.5 and imb < 0.15) or pd >= 0.27:
        return 'MED'
    return 'LOW'

# ── Empirical league draw rates (calibrated on 23 885 matches) ────────────────

_LEAGUE_DRAW_RATE = {
    'ARG': 0.303, 'TUR': 0.291, 'URU': 0.282, 'PL': 0.273,
    'PPL': 0.271, 'BRA': 0.269, 'DED': 0.261, 'SA':  0.259,
    'RUS': 0.258, 'JPN': 0.249, 'CHI': 0.249, 'PD':  0.246,
    'BL1': 0.245, 'FL1': 0.243,
}
_GLOBAL_DRAW_RATE = 0.270


# ── Prediction ────────────────────────────────────────────────────────────────

def lambda_(model, team, opp, is_home):
    league = model.get('team_league', {}).get(team, '')
    avg    = model.get('league_avgs', {}).get(league, model['league_avg'])
    base   = avg / 2
    atk    = model['attack'].get(team, 1.0)
    def_   = model['defense'].get(opp, 1.0)
    adv    = _LEAGUE_HOME_ADV.get(league, model['home_adv']) if is_home else 1.0
    return base * atk * def_ * adv


def predict(model, home, away, max_goals=8, neutral=False):
    rho = model['rho'] if model['rho'] != 0.0 else -0.13
    lam = lambda_(model, home, away, not neutral)
    mu  = lambda_(model, away, home, False)
    ph = pd = pa = p_o25 = 0
    for i in range(max_goals):
        for j in range(max_goals):
            p = _pmf(i, lam) * _pmf(j, mu) * _tau(i, j, lam, mu, rho)
            if   i > j: ph += p
            elif i == j: pd += p
            else:        pa += p
            if i + j > 2: p_o25 += p
    total = ph + pd + pa
    if total > 0: ph, pd, pa = ph/total, pd/total, pa/total
    return ph, pd, pa, p_o25, lam, mu


def predict_blended(model, home, away, max_goals=8, neutral=False):
    """DC + GBC blend, optionally recalibrated by meta-LR stacking.
    Priority: meta-LR > alpha-blend > pure DC.
    """
    ph, pd, pa, p_o25, lam, mu = predict(model, home, away, max_goals, neutral=neutral)

    bundle = _load_gbc()
    clf = bundle.get('clf')
    league_ids = bundle.get('league_ids', {})
    alpha = bundle.get('alpha', 0.20)

    if clf is None:
        return ph, pd, pa, p_o25, lam, mu

    try:
        import numpy as np
        lg  = model.get('team_league', {}).get(home, '')
        lid = float(league_ids.get(lg, 0))
        feat = np.array([[lam, mu, ph, pd, pa, lam-mu, lam+mu, lid]],
                        dtype=np.float32)
        gp = clf.predict_proba(feat)[0]  # [P(home), P(draw), P(away)]

        meta_bundle = _load_meta_lr()
        meta_clf = meta_bundle.get('clf')
        if meta_clf is not None:
            feat_meta = np.array(
                [[ph, pd, pa, gp[0], gp[1], gp[2], lam, mu, lid]],
                dtype=np.float32)
            mp = meta_clf.predict_proba(feat_meta)[0]
            ph, pd, pa = float(mp[0]), float(mp[1]), float(mp[2])
        else:
            bh = (1 - alpha) * ph + alpha * gp[0]
            bd = (1 - alpha) * pd + alpha * gp[1]
            ba = (1 - alpha) * pa + alpha * gp[2]
            total = bh + bd + ba
            ph, pd, pa = bh/total, bd/total, ba/total
    except Exception:
        pass  # silently fall back to pure DC

    return ph, pd, pa, p_o25, lam, mu


# ── Aliases / newcomer profiles ───────────────────────────────────────────────

_ALIASES = {
    'wolverhampton': 'Wolves',
    'wolverhampton wanderers': 'Wolves',
    'atletico madrid': 'Ath Madrid',
    'universidad catolica': 'Universidad Católica',
    'u.católica': 'Universidad Católica',
    'u.catolicа': 'Universidad Católica',
    'atletico paranaense': 'Athletico-PR',
    'athletico paranaense': 'Athletico-PR',
    'at.paranaense': 'Athletico-PR',
    'at. paranaense': 'Athletico-PR',
    'monterrey wanderers': 'M. Wanderers',
    'deportivo maldonado': 'Dep. Maldonado',
    'limache': None,
    'central esp.': None,
    'central espanol': None,
    'central español': None,
    'flamengo': 'Flamengo RJ',
    'flamengo rj': 'Flamengo RJ',
    # NOR aliases
    'bodo glimt': 'Bodo/Glimt',
    'bodo/glimt': 'Bodo/Glimt',
    'bodo-glimt': 'Bodo/Glimt',
    'bodoe/glimt': 'Bodo/Glimt',
    'ham-kam': 'HamKam',
    'hamkam': 'HamKam',
    'ham kam': 'HamKam',
    'rosenburgo': 'Rosenborg',
    'viking fk': 'Viking',
    'molde fk': 'Molde',
    'brann': 'Brann',
    'fredrikstad fk': 'Fredrikstad',
    # IRL aliases
    'st patricks': 'St. Patricks',
    'st. patricks': 'St. Patricks',
    'saint patricks': 'St. Patricks',
    'pat s': 'St. Patricks',
    'pats': 'St. Patricks',
    # CYP aliases (names match api-football / DC model)
    'apollon': 'Apollon Limassol',
    'apollon limassol': 'Apollon Limassol',
    'apoel': 'Apoel Nicosia',
    'apoel nicosia': 'Apoel Nicosia',
    'aek larnaca': 'AEK Larnaca',
    'aek larnaka': 'AEK Larnaca',
    'pafos': 'Pafos',
    'pafos fc': 'Pafos',
    'omonia': 'Omonia Nicosia',
    'omonia nicosia': 'Omonia Nicosia',
    'anorthosis': 'Anorthosis',
    # EGY aliases (names match api-football / DC model)
    'el gaish': 'El Geish',
    'al gaish': 'El Geish',
    'tala el gaish': 'El Geish',
    'wadi degla': None,
    'pharco': 'Pharco',
    'pharco fc': 'Pharco',
    'ismaily': 'Ismaily SC',
    'ismaily sc': 'Ismaily SC',
    'al ahly': 'Al Ahly',
    'zamalek': 'Zamalek SC',
    # INTL aliases
    'egypt': 'Egypt',
    'egipto': 'Egypt',
    'russia': 'Russia',
    'rusia': 'Russia',
    'hungria': 'Hungary',
    'hungría': 'Hungary',
    'bielorrusia': 'Belarus',
    'bosnia y herzegovina': 'Bosnia & Herzegovina',
    'bosnia': 'Bosnia & Herzegovina',
    'north macedonia': 'FYR Macedonia',
    'macedonia del norte': 'FYR Macedonia',
    'north makedonia': 'FYR Macedonia',
    # PER aliases
    'atletico grau': 'Atletico Grau',
    'atlético grau': 'Atletico Grau',
    'universitario': 'Universitario',
    'alianza lima': 'Alianza Lima',
    'sporting cristal': 'Sporting Cristal',
    'moquegua': None,
    'moquegua fc': None,
    # selecciones internacionales no en DC model → usar ELO fallback
    'suecia': None,
    'grecia': None,
    'eslovaquia': None,
    'burkina faso': None,
    'irlanda del norte': None,
    'guinea': None,
    'azerbaiyan': None,
    'azerbaiyán': None,
    'malta': None,
    'uganda': None,
    # clubes africanos / USL no en DC model
    'agadir': None,
    'husa agadir': None,
    'hassania agadir': None,
    'fus rabat': None,
    'js kabylie': None,
    'belouizdad': None,
    'cr belouizdad': None,
    'mostaganem': None,
    'es mostaganem': None,
    'el bayadh': None,
    'mc el bayadh': None,
    'birmingham legion': None,
    'birmingham legion fc': None,
    'louisville': None,
    'louisville city': None,
    'louisville city fc': None,
    # selecciones femeninas no en DC model
    'espana f': None,
    'españa f': None,
    'inglaterra f': None,
    'dinamarca f': None,
    'suecia f': None,
}
_NO_DATA = {k for k, v in _ALIASES.items() if v is None}

_NEWCOMER_ATK = {
    'CHI': 0.65, 'URU': 0.70,
    'ARG': 0.72, 'BRA': 0.75, 'RUS': 0.78, 'JPN': 0.80,
    'PL': 0.82, 'E0': 0.82, 'E1': 0.82,
    'BL1': 0.82, 'FL1': 0.82, 'SA': 0.80,
    'PD': 0.80, 'DED': 0.82, 'PPL': 0.78, 'TUR': 0.78,
    'NOR': 0.82, 'IRL': 0.80, 'PER': 0.70,
}
_NEWCOMER_DEF = {
    'CHI': 1.55, 'URU': 1.40,
    'ARG': 1.35, 'BRA': 1.30, 'RUS': 1.28, 'JPN': 1.20,
    'PL': 1.25, 'E0': 1.25, 'E1': 1.25,
    'BL1': 1.25, 'FL1': 1.25, 'SA': 1.25,
    'PD': 1.25, 'DED': 1.25, 'PPL': 1.25, 'TUR': 1.25,
    'NOR': 1.25, 'IRL': 1.28, 'PER': 1.38,
}

_MANUAL_MAP = {'1': '1', 'l': '1', 'x': 'X', 'e': 'X', '2': '2', 'v': '2'}

_FUZZY_WARN = 0.72


def resolve(name, known_teams):
    if name in known_teams:
        return name, True, 1.0
    name_lower = name.lower()
    if name_lower in _NO_DATA:
        return None, False, 0.0
    alias = _ALIASES.get(name_lower)
    if alias and alias in known_teams:
        return alias, True, 1.0
    matches = get_close_matches(name, known_teams, n=3, cutoff=0.50)
    if not matches:
        return None, False, 0.0
    best = max(matches, key=lambda m: SequenceMatcher(None, name.lower(), m.lower()).ratio())
    score = SequenceMatcher(None, name.lower(), best.lower()).ratio()
    return best, False, score


def _parse_jugada(raw):
    parts = [p.strip() for p in raw.split('vs')]
    if len(parts) != 2:
        return None, None, None
    away_tokens = parts[1].rsplit(None, 1)
    if len(away_tokens) == 2 and away_tokens[1].lower() in _MANUAL_MAP:
        return parts[0], away_tokens[0].strip(), _MANUAL_MAP[away_tokens[1].lower()]
    return parts[0], parts[1], None


def stars(conf, cap=None):
    c = min(conf, cap) if cap is not None else conf
    n = min(5, 1 + int(c * 5))
    return '★' * n + '☆' * (5 - n)


# ── Box output ────────────────────────────────────────────────────────────────

_BW = 68
_IW = _BW - 2
_PW = 24

def _print_box(results, pairs, model):
    bundle = _load_gbc()
    meta_bundle = _load_meta_lr()
    has_gbc  = bundle.get('clf') is not None
    has_meta = meta_bundle.get('clf') is not None
    if has_meta:
        mode = 'DC+GBC+LR'
    elif has_gbc:
        mode = 'DC+GBC'
    else:
        mode = 'DC'
    n = len(model['attack'])
    SEP   = '╠' + '═' * _IW + '╣'
    title = 'GANAGOL — BOLETO FINAL   ({} {} equipos)'.format(mode, n)
    col_h = ('  ##  ' + '{:<{w}}'.format('Partido', w=_PW) +
             '{:>6}{:>6}{:>6}'.format('L%', 'E%', 'V%') +
             '{:>5}{:>7}'.format('Pk', 'Conf'))

    print('╔' + '═' * _IW + '╗')
    print('║' + '{:^{w}}'.format(title, w=_IW) + '║')
    print(SEP)
    print('║' + '{:<{w}}'.format(col_h, w=_IW) + '║')
    print(SEP)

    golazo        = []
    high_risk_ids = []
    med_risk_ids  = []
    nodata_ids    = []

    for idx, (r, (raw_h, raw_a)) in enumerate(zip(results, pairs), 1):
        partido = raw_h + ' vs ' + raw_a
        if len(partido) > _PW:
            partido = partido[:_PW - 1] + '…'
        ph, pd, pa = r['ph'], r['pd'], r['pa']
        eff_pick = r['manual_pick'] if r.get('manual_pick') else r['pick']
        pk = {'1': 'L', 'X': 'E', '2': 'V'}[eff_pick]
        dr = r.get('draw_risk', 'LOW')

        if r.get('manual_pick'):
            conf_str = 'MANUAL'
        elif r.get('is_full_est'):
            pk = '?'
            conf_str = 'SIN DATOS'
            nodata_ids.append('{:02d}'.format(idx))
        elif r.get('is_est'):
            conf_str = stars(r['conf'], cap=0.50) + '~'
        else:
            cap = 0.48 if dr == 'HIGH' else (0.55 if dr == 'MED' else None)
            conf_str = stars(r['conf'], cap=cap)

        if r.get('mkt_label'):
            conf_str = conf_str + '★'
        if r.get('draw_override'):
            risk_tag = ' ⧐E'
        elif dr == 'HIGH':
            risk_tag = ' ⚠E'
        elif dr == 'MED':
            risk_tag = ' ~E'
        else:
            risk_tag = '   '
        if r.get('is_friendly') and '♦' not in risk_tag:
            risk_tag = (risk_tag.rstrip() + '♦').ljust(4)
        line = ('  {:02d}  '.format(idx) + '{:<{w}}'.format(partido, w=_PW) +
                '  {:>4.0%}  {:>4.0%}  {:>4.0%}'.format(ph, pd, pa) +
                '  [{}]  {}{}'.format(pk, conf_str, risk_tag))
        print('║' + '{:<{w}}'.format(line, w=_IW) + '║')

        if r['po25'] >= 0.55:
            golazo.append('{:02d}'.format(idx))
        if dr == 'HIGH':
            high_risk_ids.append('{:02d}'.format(idx))
        elif dr == 'MED':
            med_risk_ids.append('{:02d}'.format(idx))

    print(SEP)
    if not golazo:
        g = '  GOLAZO 200: NO  (ningún partido con más de 2.5 goles probable)'
    else:
        rec = 'JUGARLO' if len(golazo) >= 3 else 'OPCIONAL'
        g = '  GOLAZO 200: {}  •  Marcá SI en: {}'.format(rec, ' '.join(golazo))
    print('║' + '{:<{w}}'.format(g, w=_IW) + '║')

    if high_risk_ids:
        rl = '  ⚠ Alto riesgo empate — considerá doblar: {}'.format(' '.join(high_risk_ids))
        print('║' + '{:<{w}}'.format(rl, w=_IW) + '║')
    if med_risk_ids:
        rl = '  ~ Riesgo medio de empate: {}'.format(' '.join(med_risk_ids))
        print('║' + '{:<{w}}'.format(rl, w=_IW) + '║')
    if nodata_ids:
        nd = '  ✗ SIN DATOS — pick [?] = revisar manualmente: {}'.format(' '.join(nodata_ids))
        print('║' + '{:<{w}}'.format(nd, w=_IW) + '║')

    mkt_ids = ['{:02d}'.format(i+1) for i,r in enumerate(results) if r.get('mkt_label')]
    if mkt_ids:
        ml = '  ★ Cuotas mercado aplicadas: {}'.format(' '.join(mkt_ids))
        print('║' + '{:<{w}}'.format(ml, w=_IW) + '║')
    draw_ovr_ids = ['{:02d}'.format(i+1) for i,r in enumerate(results) if r.get('draw_override')]
    friendly_ids = ['{:02d}'.format(i+1) for i,r in enumerate(results) if r.get('is_friendly')]
    if draw_ovr_ids:
        dl = '  ⧐ Pick→E (margen<12pp vs empate): {}'.format(' '.join(draw_ovr_ids))
        print('║' + '{:<{w}}'.format(dl, w=_IW) + '║')
    if friendly_ids:
        fl = '  ♦ Amistoso (+8pp empate): {}'.format(' '.join(friendly_ids))
        print('║' + '{:<{w}}'.format(fl, w=_IW) + '║')
    tabla_lines = []
    for tidx, r in enumerate(results, 1):
        th = _stag(r.get('st_h'))
        ta = _stag(r.get('st_a'))
        if th or ta:
            label_h = '{}: {}'.format(r['home'], th) if th else r['home']
            label_a = '{}: {}'.format(r['away'], ta) if ta else r['away']
            tabla_lines.append('  J{:02d}  {}  vs  {}'.format(tidx, label_h, label_a))
    if tabla_lines:
        print('║' + '{:<{w}}'.format('  POSICION EN TABLA:', w=_IW) + '║')
        for tl in tabla_lines:
            print('║' + '{:<{w}}'.format(tl, w=_IW) + '║')
    print('╚' + '═' * _IW + '╝')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    model = load_model()
    teams = list(model['attack'].keys())
    tl    = model.setdefault('team_league', {})

    neutral_set   = set()
    friendly_set  = set()
    elim_home_set = set()
    elim_away_set = set()
    if len(sys.argv) > 2:
        args = sys.argv[1:]
        new_args = []
        i = 0
        while i < len(args):
            if args[i] == '--neutral' and i + 1 < len(args):
                for n in args[i+1].split(','):
                    try: neutral_set.add(int(n.strip()))
                    except ValueError: pass
                i += 2
            elif args[i] == '--friendly' and i + 1 < len(args):
                for n in args[i+1].split(','):
                    try: friendly_set.add(int(n.strip()))
                    except ValueError: pass
                i += 2
            elif args[i] == '--elim-home' and i + 1 < len(args):
                for n in args[i+1].split(','):
                    try: elim_home_set.add(int(n.strip()))
                    except ValueError: pass
                i += 2
            elif args[i] == '--elim-away' and i + 1 < len(args):
                for n in args[i+1].split(','):
                    try: elim_away_set.add(int(n.strip()))
                    except ValueError: pass
                i += 2
            else:
                new_args.append(args[i])
                i += 1
        args = new_args
        if len(args) % 2 != 0:
            sys.exit("Error: pasa pares de equipos (local visitante ...)")
        pairs   = [(args[i], args[i+1]) for i in range(0, len(args), 2)]
        manuals = [None] * len(pairs)
    else:
        print("=== GANAGOL — Ingresa los partidos (hasta 14 jugadas) ===")
        print("Formato: Local vs Visitante  |  pick opcional: 1/X/2  o  L/E/V")
        print("Ejemplo: La Serena vs Limache V   <- fuerza pick Visita\n")
        pairs   = []
        manuals = []
        for i in range(1, 15):
            try:
                raw = input('Jugada {:02d}: '.format(i)).strip()
            except EOFError:
                break
            if not raw:
                break
            home, away, manual = _parse_jugada(raw)
            if home is None:
                print("  Formato: Local vs Visitante [1/X/2]")
                continue
            pairs.append((home, away))
            manuals.append(manual)

    if not pairs:
        sys.exit("Sin partidos ingresados.")

    standings = load_standings() if _HAS_STANDINGS else {}
    results  = []
    warnings = []

    for idx, ((raw_h, raw_a), manual) in enumerate(zip(pairs, manuals), 1):
        home, h_exact, h_score = resolve(raw_h, teams)
        away, a_exact, a_score = resolve(raw_a, teams)

        is_est      = False
        is_full_est = False
        if home is None: home, is_est = raw_h, True
        if away is None: away, is_est = raw_a, True

        if is_est:
            h_league = tl.get(home, '')
            a_league = tl.get(away, '')
            if not h_league and not a_league:
                is_full_est = True  # both teams unknown → garbage prediction
            elif not h_league and a_league:
                tl[home] = a_league
                model['attack'][home]  = _NEWCOMER_ATK.get(a_league, 0.80)
                model['defense'][home] = _NEWCOMER_DEF.get(a_league, 1.20)
            elif not a_league and h_league:
                tl[away] = h_league
                model['attack'][away]  = _NEWCOMER_ATK.get(h_league, 0.80)
                model['defense'][away] = _NEWCOMER_DEF.get(h_league, 1.20)

        if not h_exact and home != raw_h and h_score < _FUZZY_WARN:
            warnings.append('J{:02d} "{}" -> "{}" ({:.0%})'.format(idx, raw_h, home, h_score))
        if not a_exact and away != raw_a and a_score < _FUZZY_WARN:
            warnings.append('J{:02d} "{}" -> "{}" ({:.0%})'.format(idx, raw_a, away, a_score))

        ph, pd, pa, po25, lam, mu = predict_blended(model, home, away, neutral=(idx in neutral_set))
        # ── ELO fallback: equipos desconocidos o match fuzzy muy pobre ───────
        if is_full_est or h_score < 0.68 or a_score < 0.68:
            try:
                from ganagol_intl import predict_intl as _pi
                _r = _pi(raw_h, raw_a, neutral=(idx in neutral_set), friendly=(idx in friendly_set), home_elim=(idx in elim_home_set), away_elim=(idx in elim_away_set))
                if _r:
                    ph, pd, pa, lam, mu = _r
                    po25 = 0.0
                    is_full_est = False
                    is_est = True
            except Exception:
                pass
        # Market odds blend (60% mercado, 40% modelo) via SofaScore
        _mkt_label = None
        try:
            from ganagol_odds import blend as _odds_blend
            ph, pd, pa, _mkt_label = _odds_blend(ph, pd, pa, raw_h, raw_a)
        except Exception:
            pass

        # Friendly draw boost
        if idx in friendly_set:
            _fb = min(0.08, (ph + pa) * 0.10)
            _ph_c = _fb * ph / (ph + pa + 1e-9)
            _pa_c = _fb * pa / (ph + pa + 1e-9)
            ph = max(0.05, ph - _ph_c)
            pa = max(0.05, pa - _pa_c)
            pd = min(0.50, pd + _fb)
            _tt = ph + pd + pa; ph, pd, pa = ph/_tt, pd/_tt, pa/_tt

        probs = {'1': ph, 'X': pd, '2': pa}
        pick  = max(probs, key=probs.get)
        conf  = probs[pick]

        # Draw override: favorito gana <12pp sobre empate y empate>25%
        _draw_override = False
        if pick != 'X' and (probs[pick] - probs['X']) < 0.12 and probs['X'] > 0.25:
            pick = 'X'
            conf = probs['X']
            _draw_override = True

        league = model.get('team_league', {}).get(home, '')
        dr = _draw_risk(lam, mu, league, pd)
        results.append({'pick': pick, 'conf': conf, 'po25': po25,
                        'home': home, 'away': away, 'lam': lam, 'mu': mu,
                        'ph': ph, 'pd': pd, 'pa': pa,
                        'is_est': is_est, 'is_full_est': is_full_est,
                        'manual_pick': manual,
                        'draw_risk': dr,
                        'draw_override': _draw_override,
                        'is_friendly': (idx in friendly_set),
                        'mkt_label': _mkt_label,
                        'st_h': lookup_team(home, standings),
                        'st_a': lookup_team(away, standings)})

    if warnings:
        print('\n  Matches aproximados (verificar):')
        for w in warnings:
            print('    ' + w)

    print()
    _print_box(results, pairs, model)


if __name__ == '__main__':
    main()
