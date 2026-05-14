#!/usr/bin/env python3
"""
Semana 8-9: Portfolio Kelly integration
 - Copia oraculo_portfolio.py al servidor
 - Parchea runner:
     * Import PortfolioManager
     * Reemplaza stake calculation con portfolio-adjusted stake
     * Agrega /portfolio comando en Telegram
     * Agrega /portfolio en /help
 - Parchea oraculo_sibila.py: columna portfolio_adj + stats
"""

import os
import shutil

BASE   = '/home/noc/oraculo_v2'
RUNNER = f'{BASE}/oraculo_runner_auto.py'
SIBILA = f'{BASE}/oraculo_sibila.py'
PORT_SRC = os.path.join(os.path.dirname(__file__), 'oraculo_portfolio.py')
PORT_DST = f'{BASE}/oraculo_portfolio.py'

# ============================================================
# 1. Copiar oraculo_portfolio.py
# ============================================================
shutil.copy(PORT_SRC, PORT_DST)
print(f'OK: oraculo_portfolio.py copiado a {PORT_DST}')
try:
    with open(PORT_DST) as f:
        compile(f.read(), PORT_DST, 'exec')
    print('OK: oraculo_portfolio.py SYNTAX OK')
except SyntaxError as e:
    print(f'ERROR oraculo_portfolio.py: {e}')
    raise

# ============================================================
# 2. Parchear oraculo_runner_auto.py
# ============================================================
with open(RUNNER) as f:
    code = f.read()

changes = 0

# --- FIX R1: import PortfolioManager junto a RLM ---
old_import = ("try:\n"
              "    from oraculo_rlm import get_tracker as _rlm_tracker\n"
              "    _RLM = _rlm_tracker()\n"
              "    _RLM_ENABLED = True\n"
              "except Exception:\n"
              "    _RLM_ENABLED = False\n"
              "    class _FakeRLM:\n"
              "        def record_batch(self, *a, **kw): pass\n"
              "        def tag_picks(self, p): return p\n"
              "        def purge_old(self, **kw): pass\n"
              "    _RLM = _FakeRLM()")
new_import = (old_import + "\n"
              "try:\n"
              "    from oraculo_portfolio import PortfolioManager as _PortfolioManager\n"
              "    _PORTFOLIO = _PortfolioManager(\n"
              "        predictions_file=PREDICTIONS_FILE,\n"
              "        bankroll=1000.0,\n"
              "    )\n"
              "    _PORTFOLIO_ENABLED = True\n"
              "except Exception as _pe:\n"
              "    _PORTFOLIO_ENABLED = False\n"
              "    log.warning('Portfolio Kelly no disponible: %s', _pe)\n"
              "    class _FakePort:\n"
              "        def get_adjusted_stake(self, p, base_stake=None):\n"
              "            return {'stake_adj': base_stake or 0, 'skip': False, 'capped': False, 'reason': ''}\n"
              "        def invalidate_cache(self): pass\n"
              "        def format_status(self): return 'Portfolio no disponible'\n"
              "    _PORTFOLIO = _FakePort()")
if old_import in code and '_PORTFOLIO_ENABLED' not in code:
    code = code.replace(old_import, new_import, 1)
    changes += 1
    print('OK FixR1: import PortfolioManager')
else:
    print('SKIP FixR1')

# --- FIX R2: aplicar portfolio Kelly en el loop de colocacion ---
# Despues del Match Winner filter y stake_factor, antes de _place_bet
# Buscamos el bloque donde se calcula stake y se llama _place_bet
old_place_block = ("            if _SIBILA_ENABLED:\n"
                   "                _sibila_placed(p['match'], p['label'], bet_id, stake)\n"
                   "            # RLM boost: si sharp money confirma nuestro pick -> +20% stake\n"
                   "            if _RLM_ENABLED and p.get('rlm_signal') and p.get('rlm_score', 0) >= 0.5:\n"
                   "                _rlm_boost = min(stake * 1.20, stake + 20)  # max +20 unidades\n"
                   "                log.info('  [RLM] Stake boost %.2f->%.2f (score=%.2f)', stake, _rlm_boost, p.get('rlm_score',0))\n"
                   "                stake = round(_rlm_boost, 2)")
new_place_block = ("            # Portfolio Kelly: ajustar stake por correlacion con bets abiertas\n"
                   "            if _PORTFOLIO_ENABLED:\n"
                   "                _port_result = _PORTFOLIO.get_adjusted_stake(p, base_stake=stake)\n"
                   "                if _port_result.get('skip'):\n"
                   "                    log.info('  [Portfolio] SKIP: %s', _port_result.get('reason',''))\n"
                   "                    continue\n"
                   "                stake = _port_result['stake_adj']\n"
                   "                p['portfolio_adj']  = _port_result['stake_adj']\n"
                   "                p['portfolio_corr'] = _port_result.get('corr_penalty', 0)\n"
                   "                p['portfolio_capped'] = _port_result.get('capped', False)\n"
                   "            if _SIBILA_ENABLED:\n"
                   "                _sibila_placed(p['match'], p['label'], bet_id, stake)\n"
                   "            # RLM boost: si sharp money confirma nuestro pick -> +20% stake\n"
                   "            if _RLM_ENABLED and p.get('rlm_signal') and p.get('rlm_score', 0) >= 0.5:\n"
                   "                _rlm_boost = min(stake * 1.20, stake + 20)  # max +20 unidades\n"
                   "                log.info('  [RLM] Stake boost %.2f->%.2f (score=%.2f)', stake, _rlm_boost, p.get('rlm_score',0))\n"
                   "                stake = round(_rlm_boost, 2)")
if old_place_block in code:
    code = code.replace(old_place_block, new_place_block, 1)
    changes += 1
    print('OK FixR2: Portfolio Kelly en loop de colocacion')
else:
    print('SKIP FixR2: anchor placement no encontrado')

# --- FIX R3: invalidar cache de portfolio despues de colocar bet ---
old_settle = ("        _log_settlement(bet_id, result, wl)\n"
              "        if _SIBILA_ENABLED:\n"
              "            _sibila_resolve(bet_id=bet_id, result=result)")
new_settle = ("        _log_settlement(bet_id, result, wl)\n"
              "        if _SIBILA_ENABLED:\n"
              "            _sibila_resolve(bet_id=bet_id, result=result)\n"
              "        if _PORTFOLIO_ENABLED:\n"
              "            _PORTFOLIO.invalidate_cache()  # bet cerrada -> recalcular portafolio")
if old_settle in code:
    code = code.replace(old_settle, new_settle, 1)
    changes += 1
    print('OK FixR3: invalidar cache de portfolio al liquidar')
else:
    print('SKIP FixR3: anchor settlement no encontrado')

# --- FIX R4: comando /portfolio en Telegram ---
old_sibila_cmd = ("    elif cmd[0] == '/sibila':\n"
                  "        _days = int(cmd[1]) if len(cmd) > 1 and cmd[1].isdigit() else 30\n"
                  "        return _sibila_fmt(days=_days)")
new_sibila_cmd = ("    elif cmd[0] == '/sibila':\n"
                  "        _days = int(cmd[1]) if len(cmd) > 1 and cmd[1].isdigit() else 30\n"
                  "        return _sibila_fmt(days=_days)\n"
                  "    elif cmd[0] == '/portfolio':\n"
                  "        return _PORTFOLIO.format_status() if _PORTFOLIO_ENABLED else 'Portfolio no disponible'")
if old_sibila_cmd in code and "'/portfolio'" not in code:
    code = code.replace(old_sibila_cmd, new_sibila_cmd, 1)
    changes += 1
    print('OK FixR4: comando /portfolio en Telegram')
else:
    print('SKIP FixR4')

# --- FIX R5: /portfolio en /help ---
old_help = ("'/sibila - Libro sombra (ROI sin limites)\\n'\n"
            "                '/help - This message')")
new_help = ("'/sibila - Libro sombra (ROI sin limites)\\n'\n"
            "                '/portfolio - Estado del portafolio Kelly\\n'\n"
            "                '/help - This message')")
if old_help in code:
    code = code.replace(old_help, new_help, 1)
    changes += 1
    print('OK FixR5: /portfolio en /help')
else:
    print('SKIP FixR5: anchor help no encontrado')

if changes > 0:
    with open(RUNNER, 'w') as f:
        f.write(code)
    try:
        compile(code, RUNNER, 'exec')
        print(f'Runner SYNTAX OK ({changes} cambios)')
    except SyntaxError as e:
        print(f'Runner SYNTAX ERROR: {e}')
else:
    print('Runner: sin cambios')

# ============================================================
# 3. Parchear oraculo_sibila.py: columna portfolio_adj
# ============================================================
with open(SIBILA) as f:
    scode = f.read()

schanges = 0

# --- FIX S1: columna portfolio_adj en schema ---
old_schema_end = ("                rlm_signal  INTEGER DEFAULT 0,\n"
                  "                rlm_score   REAL DEFAULT 0.0\n"
                  "            )")
new_schema_end = ("                rlm_signal     INTEGER DEFAULT 0,\n"
                  "                rlm_score      REAL DEFAULT 0.0,\n"
                  "                portfolio_corr REAL DEFAULT 0.0,\n"
                  "                portfolio_cap  INTEGER DEFAULT 0\n"
                  "            )")
if old_schema_end in scode and 'portfolio_corr' not in scode:
    scode = scode.replace(old_schema_end, new_schema_end, 1)
    schanges += 1
    print('OK FixS1: columnas portfolio en schema')
else:
    print('SKIP FixS1')

# --- FIX S2: ALTER TABLE migracion ---
old_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_signal INTEGER DEFAULT 0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_score REAL DEFAULT 0.0')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
new_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_signal INTEGER DEFAULT 0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_score REAL DEFAULT 0.0')\n"
             "            except Exception:\n"
             "                pass  # ya existen\n"
             "            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_corr REAL DEFAULT 0.0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_cap INTEGER DEFAULT 0')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
if old_alter in scode and 'portfolio_corr' not in scode:
    scode = scode.replace(old_alter, new_alter, 1)
    schanges += 1
    print('OK FixS2: ALTER TABLE portfolio migracion')
else:
    print('SKIP FixS2')

# --- FIX S3: guardar portfolio_corr en INSERT ---
old_insert = ("             1 if pick.get('rlm_signal') else 0,\n"
              "             float(pick.get('rlm_score', 0) or 0)))")
new_insert = ("             1 if pick.get('rlm_signal') else 0,\n"
              "             float(pick.get('rlm_score', 0) or 0),\n"
              "             float(pick.get('portfolio_corr', 0) or 0),\n"
              "             1 if pick.get('portfolio_capped') else 0))")
if old_insert in scode and 'portfolio_corr' not in scode:
    # Tambien hay que agregar las columnas al INSERT columns list
    old_cols = ("                (ts, sport, market, level, match, label, edge, model_prob,\n"
                "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line,\n"
                "                 rlm_signal, rlm_score)\n"
                "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    new_cols  = ("                (ts, sport, market, level, match, label, edge, model_prob,\n"
                 "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line,\n"
                 "                 rlm_signal, rlm_score, portfolio_corr, portfolio_cap)\n"
                 "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    if old_cols in scode:
        scode = scode.replace(old_cols, new_cols, 1)
        scode = scode.replace(old_insert, new_insert, 1)
        schanges += 1
        print('OK FixS3: portfolio_corr en INSERT')
    else:
        print('SKIP FixS3: anchor INSERT columns no encontrado')
else:
    print('SKIP FixS3')

# --- FIX S4: stats portfolio_capped en get_stats ---
old_rlm_groups = ("    rlm_on  = [r for r in rows if r.get('rlm_signal')]\n"
                  "    rlm_off = [r for r in rows if not r.get('rlm_signal')]\n"
                  "    return {")
new_rlm_groups = ("    rlm_on    = [r for r in rows if r.get('rlm_signal')]\n"
                  "    rlm_off   = [r for r in rows if not r.get('rlm_signal')]\n"
                  "    port_cap  = [r for r in rows if r.get('portfolio_cap')]\n"
                  "    port_free = [r for r in rows if not r.get('portfolio_cap')]\n"
                  "    avg_corr  = (sum(float(r.get('portfolio_corr',0) or 0) for r in rows)\n"
                  "                 / len(rows) if rows else 0)\n"
                  "    return {")
if old_rlm_groups in scode:
    scode = scode.replace(old_rlm_groups, new_rlm_groups, 1)
    schanges += 1
    print('OK FixS4: portfolio groups en get_stats')
else:
    print('SKIP FixS4')

old_stats_end = ("        'rlm_on':  _grp_roi(rlm_on),\n"
                 "        'rlm_off': _grp_roi(rlm_off),\n"
                 "    }")
new_stats_end = ("        'rlm_on':     _grp_roi(rlm_on),\n"
                 "        'rlm_off':    _grp_roi(rlm_off),\n"
                 "        'port_cap':   _grp_roi(port_cap),\n"
                 "        'port_free':  _grp_roi(port_free),\n"
                 "        'avg_corr':   round(avg_corr, 3),\n"
                 "    }")
if old_stats_end in scode:
    scode = scode.replace(old_stats_end, new_stats_end, 1)
    schanges += 1
    print('OK FixS4b: portfolio stats en return')
else:
    print('SKIP FixS4b')

# --- FIX S5: mostrar portfolio en /sibila ---
old_tg_rlm = ("    # RLM comparison\n"
              "    ro = s.get('rlm_on', {})\n"
              "    roff = s.get('rlm_off', {})\n"
              "    if ro.get('n', 0) > 0:\n"
              "        lines.append('')\n"
              "        lines.append('*Sharp Money (RLM)*')\n"
              "        def _fmt_grp(g, tag):\n"
              "            if not g.get('n'): return ''\n"
              "            roi_s = f\"{g['roi']*100:+.1f}%\" if g.get('roi') is not None else 'n/a'\n"
              "            clv_s = f\"{g['avg_clv']*100:+.1f}%\" if g.get('avg_clv') is not None else 'n/a'\n"
              "            return f'{tag}: {g[\"n\"]} picks | ROI {roi_s} | CLV {clv_s}'\n"
              "        if ro.get('n'): lines.append(_fmt_grp(ro, 'Con RLM'))\n"
              "        if roff.get('n'): lines.append(_fmt_grp(roff, 'Sin RLM'))\n"
              "    lines.append('')\n"
              "    lines.append('_Sibila Shadow Book_')\n"
              "    return '\\n'.join(lines)")
new_tg_rlm  = ("    # RLM comparison\n"
               "    ro = s.get('rlm_on', {})\n"
               "    roff = s.get('rlm_off', {})\n"
               "    if ro.get('n', 0) > 0:\n"
               "        lines.append('')\n"
               "        lines.append('*Sharp Money (RLM)*')\n"
               "        def _fmt_grp(g, tag):\n"
               "            if not g.get('n'): return ''\n"
               "            roi_s = f\"{g['roi']*100:+.1f}%\" if g.get('roi') is not None else 'n/a'\n"
               "            clv_s = f\"{g['avg_clv']*100:+.1f}%\" if g.get('avg_clv') is not None else 'n/a'\n"
               "            return f'{tag}: {g[\"n\"]} picks | ROI {roi_s} | CLV {clv_s}'\n"
               "        if ro.get('n'): lines.append(_fmt_grp(ro, 'Con RLM'))\n"
               "        if roff.get('n'): lines.append(_fmt_grp(roff, 'Sin RLM'))\n"
               "    # Portfolio stats\n"
               "    pc = s.get('port_cap', {})\n"
               "    pf = s.get('port_free', {})\n"
               "    avg_c = s.get('avg_corr', 0)\n"
               "    if pc.get('n', 0) > 0 or pf.get('n', 0) > 0:\n"
               "        lines.append('')\n"
               "        lines.append('*Portfolio Kelly*')\n"
               "        lines.append(f'Correlacion media portafolio: {avg_c*100:.1f}%')\n"
               "        if pf.get('n'): lines.append(_fmt_grp(pf, 'Sin cap'))\n"
               "        if pc.get('n'): lines.append(_fmt_grp(pc, 'Capados'))\n"
               "    lines.append('')\n"
               "    lines.append('_Sibila Shadow Book_')\n"
               "    return '\\n'.join(lines)")
if old_tg_rlm in scode:
    scode = scode.replace(old_tg_rlm, new_tg_rlm, 1)
    schanges += 1
    print('OK FixS5: portfolio en format_telegram')
else:
    print('SKIP FixS5: anchor telegram RLM no encontrado')

if schanges > 0:
    with open(SIBILA, 'w') as f:
        f.write(scode)
    try:
        compile(scode, SIBILA, 'exec')
        print(f'Sibila SYNTAX OK ({schanges} cambios)')
    except SyntaxError as e:
        print(f'Sibila SYNTAX ERROR: {e}')
else:
    print('Sibila: sin cambios')

print(f'\nTotal runner={changes} sibila={schanges}')
print('Reinicia: sudo systemctl restart oraculo')
print()
print('Verificar en logs:')
print('  [Portfolio] OK Sinner vs Alcaraz | stake=45.20 (corr=0.050 expo=3.2%)')
print('  [Portfolio] CAP Liverpool vs City | 80.00->25.60 (corr=0.850) mismo evento')
print('  [Portfolio] SKIP Chelsea vs Arsenal — portafolio al limite (15.0% >= 15%)')
print()
print('Nuevo comando Telegram: /portfolio')
print('  Bets abiertos: 3')
print('  Exposicion: 8.4% de bankroll')
print('  Espacio disponible: 6.6%')
