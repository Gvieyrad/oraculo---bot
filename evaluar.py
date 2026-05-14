#!/usr/bin/env python3
"""
Evalua un pick antes de apostarlo.

Uso:
  python evaluar.py "Reds vs Rockies" "Over 6 F5" 2.50
  python evaluar.py "Yankees vs Rangers" "Yankees ML" 1.77 --sport mlb
  python evaluar.py "Fils vs Lehecka" "Lehecka" 2.40 --sport tennis
  python evaluar.py "Atletico vs Arsenal" "BTTS Yes" 1.91 --sport soccer

Opciones:
  --sport    tennis | soccer | mlb  (default: auto-detecta)
  --bm       odds del libro sharp (Pinnacle) para el mismo outcome
             si no se provee, usa 1/price como aproximacion
  --bankroll tu bankroll actual en USDT (default: lee open_bets.json)
"""

import sys
import os
import json
import argparse
import sqlite3
from collections import defaultdict

# Windows cp1252 safe output
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE           = os.path.dirname(os.path.abspath(__file__))
SIBILA_DB      = os.path.join(BASE, 'sibila.db')
SIBILA_BACKFILL= os.path.join(BASE, 'sibila_backfill.db')   # DB historico
LEARNED_RULES  = os.path.join(BASE, 'learned_rules.json')   # reglas pre-computadas
CALIB_DATA     = os.path.join(BASE, 'calibration_data.json')# calibracion pre-computada
OPEN_BETS      = os.path.join(BASE, '.oraculo_cache', 'open_bets.json')
BANKROLL       = 50.0

LINE = '-' * 56


def load_open_bets() -> list:
    """Carga bets abiertas desde open_bets.json."""
    try:
        with open(OPEN_BETS) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_open_bet(match, label, sport, stake, odds):
    """Agrega un bet al archivo de bets abiertas."""
    bets = load_open_bets()
    bets.append({
        'match': match, 'label': label, 'sport': sport,
        'stake': stake, 'odds': odds,
        'status': 'open'
    })
    os.makedirs(os.path.dirname(OPEN_BETS), exist_ok=True)
    with open(OPEN_BETS, 'w') as f:
        json.dump(bets, f, indent=2)


def detect_sport(match: str, label: str) -> str:
    label_l = label.lower()
    match_l = match.lower()
    teams_mlb = ['yankees','rangers','reds','rockies','padres','cubs','phillies',
                 'giants','mets','nationals','dodgers','astros','braves','marlins',
                 'pirates','cardinals']
    if any(t in match_l for t in teams_mlb):
        return 'mlb'
    if any(w in label_l for w in ['btts','over','under','1x2','home','away']):
        if any(w in match_l for w in ['atletico','arsenal','liverpool','madrid',
                                       'chelsea','city','united','barca','real']):
            return 'soccer'
    return 'tennis'


def devig2(o1: float, o2: float) -> float:
    if o1 <= 1 or o2 <= 1:
        return 1.0 / o1 if o1 > 1 else 0.5
    r1, r2 = 1/o1, 1/o2
    return round(r1 / (r1 + r2), 4)


def load_rules_from_json(path: str) -> dict:
    """Carga learned_rules.json (generado por _learn_from_losses.py) al formato interno."""
    try:
        with open(path) as f:
            data = json.load(f)
        rules_list = data.get('rules', data) if isinstance(data, dict) else data
        rules = {}
        for r in rules_list:
            dim = r.get('dimension', '')
            val = r.get('value', '')
            roi = r.get('roi_observed', 0)
            n   = r.get('n', 0)
            act = r.get('action', 'skip')
            # Mapear action al formato interno
            if act == 'reduce_stake_50pct':
                act_internal = 'stake_x0.5'
            elif act == 'reduce_model_prob':
                continue   # calibracion se maneja aparte
            else:
                act_internal = 'skip'
            # Crear key al formato price:X / edge:X
            if dim == 'price':
                key = f'price:{val}'
            elif dim == 'edge':
                key = f'edge:{val}'
            elif dim == 'market':
                key = f'market:{val}'
            elif dim == 'level':
                key = f'level:{val}'
            else:
                continue
            rules[key] = {'roi': roi, 'n': n, 'action': act_internal}
        return rules
    except Exception:
        return {}


def load_calibration_from_json(path: str) -> dict:
    """Carga calibration_data.json al formato interno."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and 'calibration' not in data:
            # Formato directo bucket -> {wr, n, error_pp}
            return {float(k): v for k, v in data.items() if k not in ('generated', 'rules')}
        return {}
    except Exception:
        return {}


def load_calibration(db_path: str) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT raw_model_prob, result FROM sibila_picks WHERE result IN ('win','loss') AND raw_model_prob > 0"
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    buckets = defaultdict(list)
    for mp, result in rows:
        b = round(round(float(mp) * 10) / 10, 1)
        buckets[b].append(1 if result == 'win' else 0)

    cal = {}
    for b, outcomes in buckets.items():
        if len(outcomes) < 8:
            continue
        wr = sum(outcomes) / len(outcomes)
        cal[b] = {'wr': wr, 'n': len(outcomes), 'error_pp': (wr - b) * 100}
    return cal


def load_rules(db_path: str) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT edge, price, result, virtual_stake, pnl, level, market
            FROM sibila_picks WHERE result IN ('win','loss')
        ''').fetchall()
        conn.close()
    except Exception:
        return {}

    def bucket_price(p):
        if p < 1.5: return '1.20-1.50'
        elif p < 2.0: return '1.50-2.00'
        elif p < 2.5: return '2.00-2.50'
        elif p < 3.0: return '2.50-3.00'
        else: return '3.00+'

    def bucket_edge(e):
        ep = e * 100
        if ep < 2: return '1-2%'
        elif ep < 4: return '2-4%'
        elif ep < 7: return '4-7%'
        elif ep < 12: return '7-12%'
        else: return '12%+'

    groups = defaultdict(lambda: {'pnl': 0, 'stake': 0, 'n': 0})
    for edge, price, result, vstake, pnl, level, market in rows:
        edge  = float(edge or 0)
        price = float(price or 0)
        vstake = float(vstake or 0)
        pnl    = float(pnl or 0)
        if price > 1:
            groups[f'price:{bucket_price(price)}']['pnl']   += pnl
            groups[f'price:{bucket_price(price)}']['stake'] += vstake
            groups[f'price:{bucket_price(price)}']['n']     += 1
        if edge > 0:
            groups[f'edge:{bucket_edge(edge)}']['pnl']   += pnl
            groups[f'edge:{bucket_edge(edge)}']['stake'] += vstake
            groups[f'edge:{bucket_edge(edge)}']['n']     += 1

    rules = {}
    for k, d in groups.items():
        if d['n'] < 15 or d['stake'] == 0:
            continue
        r = d['pnl'] / d['stake'] * 100
        if r < -3:
            rules[k] = {
                'roi': r, 'n': d['n'],
                'action': 'skip' if r < -8 else 'stake_x0.5'
            }
    return rules


def portfolio_analysis(match: str, sport: str, open_bets: list, bankroll: float) -> dict:
    total_stake  = sum(float(b.get('stake', 0)) for b in open_bets)
    same_match   = [b for b in open_bets if b.get('match', '').lower() == match.lower()]
    same_sport   = [b for b in open_bets if b.get('sport', '') == sport and b.get('match', '').lower() != match.lower()]
    same_stake   = sum(float(b.get('stake', 0)) for b in same_match)
    exposure     = total_stake / bankroll if bankroll else 0
    same_exp     = same_stake / bankroll if bankroll else 0

    if same_exp >= 0.08:
        corr, corr_label = 0.85, 'mismo partido (limite alcanzado)'
    elif len(same_match) > 0:
        corr, corr_label = 0.85, f'mismo partido ({len(same_match)} bet(s) abiertos)'
    elif len(same_sport) > 0:
        corr, corr_label = 0.15, f'mismo deporte ({len(same_sport)} bet(s) abiertos)'
    else:
        corr, corr_label = 0.0, 'sin correlacion'

    return {
        'exposure':    round(exposure, 4),
        'same_exp':    round(same_exp, 4),
        'total_stake': round(total_stake, 2),
        'same_stake':  round(same_stake, 2),
        'n_same':      len(same_match),
        'corr':        corr,
        'corr_label':  corr_label,
        'at_limit':    exposure >= 0.15 or same_exp >= 0.08,
    }


def evaluate(match: str, label: str, price: float,
             sport: str, bm_odds: float = None,
             bankroll: float = BANKROLL) -> dict:

    open_bets = load_open_bets()
    # Reglas: JSON pre-computado primero, luego DB live
    rules = load_rules_from_json(LEARNED_RULES) or load_rules(SIBILA_BACKFILL) or load_rules(SIBILA_DB)
    # Calibracion: JSON primero, luego DB
    cal   = load_calibration_from_json(CALIB_DATA) or load_calibration(SIBILA_BACKFILL) or load_calibration(SIBILA_DB)

    # --- Bm implied ---
    bm = devig2(price, bm_odds) if bm_odds else round(1.0 / price, 4)
    bm_source = 'Pinnacle devigged' if bm_odds else 'estimado (1/price)'

    # --- Model prob (heuristico sin modelo completo) ---
    # Sin modelo Elo completo local, usamos bm como base + ajuste por contexto
    # En servidor real: llamar a predict() de ServeReturnElo / FootballElo / MlbElo
    mp = round(bm * 1.05, 4)   # asumimos leve ventaja vs mercado como prior
    mp = min(0.92, max(0.08, mp))

    alpha = {'tennis': 0.65, 'soccer': 0.60, 'mlb': 0.55}.get(sport, 0.60)

    # --- Calibracion ---
    # Solo aplicar calibracion cuando el mercado coincide con lo que fue entrenado
    # Backfill actual: solo soccer home_win -> calibracion aplica a home_win/1x2
    _label_l = label.lower()
    _is_home_win = any(w in _label_l for w in ['home', '1x2', 'ml', 'money line', 'winner'])
    _apply_cal   = _is_home_win or sport in ('tennis', 'mlb')  # tennis/mlb: 1 ganador = siempre aplica
    raw_b  = round(round(mp * 10) / 10, 1)
    cal_c  = cal.get(raw_b, {}) if _apply_cal else {}
    cal_factor = 1.0
    cal_note   = ''
    if cal_c and cal_c.get('error_pp', 0) < -6:
        cal_factor = cal_c['wr'] / raw_b if raw_b > 0 else 1.0
        mp_cal     = round(mp * cal_factor, 4)
        cal_note   = f"corregido {mp*100:.1f}%→{mp_cal*100:.1f}% (WR real {cal_c['wr']*100:.1f}%)"
        mp = mp_cal
    else:
        err = cal_c.get('error_pp', 0)
        if not _apply_cal:
            cal_note = f"N/A (calibracion es solo para home_win/ML — {label})"
        else:
            cal_note = f"OK (error historico {err:+.1f}pp)" if cal_c else "sin datos historicos aun"

    # --- Benter blend ---
    blend      = round(alpha * mp + (1 - alpha) * bm, 4)
    edge_raw   = round(mp * price - 1, 4)
    edge       = round(blend * price - 1, 4)

    # --- Learned rules ---
    stake_factor = 1.0
    skip_rule    = None
    rule_notes   = []

    # Odds bucket
    if price < 1.5:    ob = '1.20-1.50'
    elif price < 2.0:  ob = '1.50-2.00'
    elif price < 2.5:  ob = '2.00-2.50'
    elif price < 3.0:  ob = '2.50-3.00'
    else:              ob = '3.00+'

    # Market check
    mkt_key = f'market:{label.lower().replace(" ", "_")}'
    if mkt_key in rules:
        r = rules[mkt_key]
        if r['action'] == 'skip':
            skip_rule = f"mercado {label} -> ROI {r['roi']:+.1f}% ({r['n']}p)"
        else:
            stake_factor *= 0.5
            rule_notes.append(f"mercado {label} -> ROI {r['roi']:+.1f}% => stake x0.5")

    if f'price:{ob}' in rules:
        r = rules[f'price:{ob}']
        if r['action'] == 'skip':
            skip_rule = f"odds {ob} -> ROI historico {r['roi']:+.1f}% ({r['n']}p)"
        else:
            stake_factor *= 0.5
            rule_notes.append(f"odds {ob} -> ROI {r['roi']:+.1f}% => stake x0.5")

    # Edge bucket
    ep = edge * 100
    if ep < 2:    eb = '1-2%'
    elif ep < 4:  eb = '2-4%'
    elif ep < 7:  eb = '4-7%'
    elif ep < 12: eb = '7-12%'
    else:         eb = '12%+'

    if f'edge:{eb}' in rules:
        r = rules[f'edge:{eb}']
        if r['action'] == 'skip':
            skip_rule = skip_rule or f"edge {eb} -> ROI {r['roi']:+.1f}% ({r['n']}p)"
        else:
            stake_factor *= 0.5
            rule_notes.append(f"edge {eb} -> ROI {r['roi']:+.1f}% => stake x0.5")

    # --- Portfolio ---
    port = portfolio_analysis(match, sport, open_bets, bankroll)

    # --- Kelly stake ---
    if edge > 0 and not skip_rule and not port['at_limit']:
        b      = price - 1
        kf     = max(0.0, edge / b) * 0.25
        stake  = round(min(bankroll * 0.05, bankroll * kf) * stake_factor
                       * (1 - port['corr'] * 0.8), 2)
    else:
        stake = 0.0

    # --- Decision ---
    if edge <= 0:
        decision = 'SKIP'
        reason   = 'edge negativo post-Benter'
    elif skip_rule:
        decision = 'SKIP'
        reason   = f'Regla aprendida: {skip_rule}'
    elif port['at_limit']:
        decision = 'SKIP'
        if port['same_exp'] >= 0.08:
            reason = f'mismo partido: ya tienes ${port["same_stake"]:.2f} apostado (limite 8%)'
        else:
            reason = f'portafolio lleno: {port["exposure"]*100:.1f}% expuesto (limite 15%)'
    elif stake < max(0.25, bankroll * 0.004):
        decision = 'SKIP'
        reason   = f'stake calculado ${stake:.2f} < minimo ${max(0.25, bankroll*0.004):.2f} (0.4% bankroll)'
    else:
        decision = 'BET'
        reason   = ''

    return {
        'match': match, 'label': label, 'price': price,
        'sport': sport, 'bm': bm, 'bm_source': bm_source,
        'mp': mp, 'alpha': alpha, 'blend': blend,
        'edge_raw': edge_raw, 'edge': edge,
        'cal_note': cal_note, 'rule_notes': rule_notes,
        'stake_factor': stake_factor,
        'portfolio': port, 'stake': stake,
        'decision': decision, 'reason': reason,
        'ev': round(stake * edge, 3),
    }


def print_result(r: dict, bankroll: float):
    print()
    print(LINE)
    dec_label = f'  {"BET" if r["decision"]=="BET" else "SKIP"}  |  {r["match"]} - {r["label"]}'
    print(dec_label)
    print(LINE)
    print(f'  Odds:       {r["price"]}')
    print(f'  bm implied: {r["bm"]*100:.1f}%  ({r["bm_source"]})')
    print()
    print(f'  Calibracion:  {r["cal_note"]}')
    print(f'  Benter (a={r["alpha"]}): {r["mp"]*100:.1f}% + {r["bm"]*100:.1f}% => {r["blend"]*100:.1f}%')
    print(f'  Edge:  raw {r["edge_raw"]*100:+.1f}%  =>  post-Benter {r["edge"]*100:+.1f}%')
    print()

    port = r['portfolio']
    print(f'  Portfolio:')
    print(f'    Exposicion actual: ${port["total_stake"]:.2f} ({port["exposure"]*100:.1f}% de ${bankroll:.0f})')
    if port['n_same'] > 0:
        print(f'    Mismo partido:    ${port["same_stake"]:.2f} ({port["n_same"]} bet(s)) <- ALERTA')
    print(f'    Correlacion:      {port["corr_label"]}')

    if r['rule_notes']:
        print()
        print('  Reglas aprendidas:')
        for note in r['rule_notes']:
            print(f'    -> {note}')

    print()
    if r['decision'] == 'BET':
        ev = r['ev']
        print(f'  DECISION: BET ${r["stake"]:.2f} @ {r["price"]}')
        print(f'            EV esperado: +${ev:.2f}')
        print(f'            ({r["stake"]/bankroll*100:.1f}% de tu bankroll)')
    else:
        print(f'  DECISION: SKIP')
        print(f'            Razon: {r["reason"]}')
    print(LINE)


def main():
    parser = argparse.ArgumentParser(
        description='Evalua un pick antes de apostarlo',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Ejemplos:
  python evaluar.py "Reds vs Rockies" "Over 6 F5" 2.50
  python evaluar.py "Atletico vs Arsenal" "BTTS Yes" 1.91 --sport soccer
  python evaluar.py "Yankees vs Rangers" "Yankees ML" 1.77 --bm 1.74
  python evaluar.py "Fils vs Lehecka" "Lehecka" 2.40 --sport tennis
  python evaluar.py --liquidar "Atletico vs Arsenal" "BTTS Yes" win
        '''
    )
    parser.add_argument('match',    help='Nombre del partido')
    parser.add_argument('label',    help='Mercado/seleccion (ej: "Over 2.5", "Yankees ML")')
    parser.add_argument('price',    help='Odds decimales en Cloudbet (o win|loss con --liquidar)')
    parser.add_argument('--sport',  default=None, help='tennis|soccer|mlb')
    parser.add_argument('--bm',     type=float, default=None,
                        help='Odds de Pinnacle para el mismo outcome (para CLV real)')
    parser.add_argument('--bankroll', type=float, default=BANKROLL,
                        help=f'Tu bankroll en USDT (default: {BANKROLL})')
    parser.add_argument('--liquidar', action='store_true',
                        help='Marcar bet como resuelto: --liquidar "match" "label" win|loss')
    args = parser.parse_args()

    if args.liquidar:
        # python evaluar.py --liquidar "match" "label" win|loss
        result = args.price  # positional "price" slot holds win|loss here
        if result not in ('win', 'loss'):
            print(f'Uso: python evaluar.py --liquidar "match" "label" win|loss')
            print(f'     Recibido: "{result}"')
            return
        bets = load_open_bets()
        updated = False
        for b in bets:
            if (b.get('match','').lower() == args.match.lower() and
                    b.get('label','').lower() == args.label.lower()):
                b['status'] = str(result)
                updated = True
        if updated:
            with open(OPEN_BETS, 'w') as f:
                json.dump(bets, f, indent=2)
            print(f'OK: {args.match} — {args.label} marcado como {result}')
        else:
            print(f'No encontrado: {args.match} — {args.label}')
        return

    try:
        price_f = float(args.price)
    except ValueError:
        print(f'Error: price debe ser un numero decimal (ej: 1.91). Para liquidar usa --liquidar.')
        sys.exit(1)

    sport = args.sport or detect_sport(args.match, args.label)

    print(f'\n  Evaluando: {args.match} | {args.label} @ {price_f}')
    print(f'  Deporte: {sport} | Bankroll: ${args.bankroll:.2f}')

    r = evaluate(args.match, args.label, price_f,
                 sport=sport, bm_odds=args.bm, bankroll=args.bankroll)

    print_result(r, args.bankroll)

    if r['decision'] == 'BET':
        resp = input(f'\n  Confirmas la apuesta de ${r["stake"]:.2f}? (s/n): ').strip().lower()
        if resp == 's':
            save_open_bet(args.match, args.label, sport, r['stake'], price_f)
            print(f'  Registrado en open_bets.json')
            print(f'  Al terminar el partido: python evaluar.py --liquidar "{args.match}" "{args.label}" win|loss')
        else:
            print('  Cancelado.')


if __name__ == '__main__':
    main()
