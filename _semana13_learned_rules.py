#!/usr/bin/env python3
"""
Semana 13: Learned Rules Integration
Carga learned_rules.json en el runner y filtra picks antes de place_bets.
Reglas aplicadas: skip odds 2.50+, skip edge 12%+, reduce stake market/level.
"""
import os, re

BASE   = '/home/noc/oraculo_v2'
RUNNER = f'{BASE}/oraculo_runner_auto.py'

with open(RUNNER) as f:
    code = f.read()

changes = 0

# ── FIX 1: Import + loader de learned_rules al inicio del módulo ──────────
old_import_anchor = "try:\n    from oraculo_fresh_line import"
new_rules_block = '''\
# ── Learned Rules (generado por _learn_from_losses.py) ──────────────────
import json as _json
_LEARNED_RULES       = []
_LEARNED_RULES_MTIME = 0.0
_LEARNED_RULES_PATH  = os.path.join(os.path.dirname(__file__), 'learned_rules.json')

def _load_learned_rules():
    global _LEARNED_RULES, _LEARNED_RULES_MTIME
    try:
        mtime = os.path.getmtime(_LEARNED_RULES_PATH)
        if mtime == _LEARNED_RULES_MTIME:
            return
        data  = _json.load(open(_LEARNED_RULES_PATH))
        rules = data.get('rules', data) if isinstance(data, dict) else data
        _LEARNED_RULES = rules if isinstance(rules, list) else []
        _LEARNED_RULES_MTIME = mtime
        log.info('[Rules] %d reglas aprendidas cargadas desde learned_rules.json', len(_LEARNED_RULES))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning('[Rules] Error cargando learned_rules.json: %s', exc)

def _apply_learned_rules(picks: list) -> list:
    """Filtra/reduce picks segun reglas aprendidas de historial."""
    _load_learned_rules()
    if not _LEARNED_RULES:
        return picks
    out = []
    for p in picks:
        price  = float(p.get('odds', p.get('price', 0)) or 0)
        edge   = float(p.get('edge', 0) or 0)
        market = str(p.get('market_type', p.get('market', '')) or '').lower()
        level  = str(p.get('level', '') or '').lower()
        skip   = False
        factor = 1.0
        for r in _LEARNED_RULES:
            dim = r.get('dimension', '')
            val = r.get('value', '')
            act = r.get('action', 'skip')
            match = False
            if dim == 'price':
                if val == '1.20-1.50' and 1.20 <= price < 1.50: match = True
                elif val == '1.50-2.00' and 1.50 <= price < 2.00: match = True
                elif val == '2.00-2.50' and 2.00 <= price < 2.50: match = True
                elif val == '2.50-3.00' and 2.50 <= price < 3.00: match = True
                elif val == '3.00+' and price >= 3.00: match = True
            elif dim == 'edge':
                ep = edge * 100
                if val == '1-2%' and 1 <= ep < 2: match = True
                elif val == '2-4%' and 2 <= ep < 4: match = True
                elif val == '4-7%' and 4 <= ep < 7: match = True
                elif val == '7-12%' and 7 <= ep < 12: match = True
                elif val == '12%+' and ep >= 12: match = True
            elif dim == 'market' and val.lower() in market: match = True
            elif dim == 'level'  and val.lower() in level:  match = True
            if match:
                if act == 'skip':
                    skip = True
                    log.info('[Rules] SKIP %s | %s @ %.2f — regla: %s %s ROI %.1f%%',
                             p.get('match','?'), p.get('label','?'), price, dim, val,
                             r.get('roi_observed', 0))
                    break
                elif act in ('reduce_stake_50pct', 'stake_x0.5'):
                    factor = min(factor, 0.5)
        if not skip:
            if factor < 1.0:
                p['_rules_stake_factor'] = factor
            out.append(p)
    removed = len(picks) - len(out)
    if removed:
        log.info('[Rules] %d picks filtrados por learned_rules', removed)
    return out

'''

if '_LEARNED_RULES' not in code:
    code = code.replace(old_import_anchor,
                        new_rules_block + old_import_anchor, 1)
    changes += 1
    print('OK Fix1: loader learned_rules insertado')
else:
    print('SKIP Fix1: ya existe')

# ── FIX 2: Aplicar reglas en place_bets antes de iterar picks ────────────
old_place = '''\
    bankroll = state['bankroll']
    if bankroll < CIRCUIT_BREAKER:'''

new_place = '''\
    # Filtrar picks con learned_rules.json
    picks = _apply_learned_rules(list(picks))

    bankroll = state['bankroll']
    if bankroll < CIRCUIT_BREAKER:'''

if old_place in code and '_apply_learned_rules' not in code[code.find('def place_bets'):code.find('def place_bets')+200]:
    code = code.replace(old_place, new_place, 1)
    changes += 1
    print('OK Fix2: _apply_learned_rules en place_bets')
else:
    print('SKIP Fix2: ya existe o anchor no encontrado')

# ── FIX 3: Aplicar stake factor si rule redujo stake ─────────────────────
# Buscar donde se calcula el stake en place_bets (kelly sizing)
old_kelly = "            kelly_f = max(0, (edge * (b + 1) - 1) / b)"
new_kelly  = ("            kelly_f = max(0, (edge * (b + 1) - 1) / b)\n"
              "            kelly_f *= p.get('_rules_stake_factor', 1.0)  # rules reduction")
if old_kelly in code and '_rules_stake_factor' not in code:
    code = code.replace(old_kelly, new_kelly, 1)
    changes += 1
    print('OK Fix3: stake factor aplicado en Kelly')
else:
    print('SKIP Fix3')

# ── Syntax check ──────────────────────────────────────────────────────────
with open(RUNNER, 'w') as f:
    f.write(code)

import subprocess
rc = subprocess.run(['python3', '-m', 'py_compile', RUNNER])
if rc.returncode == 0:
    print(f'Runner SYNTAX OK ({changes} cambios)')
else:
    print('SYNTAX ERROR — revisa el runner')

print('\nReinicia: sudo systemctl restart oraculo-v2')
