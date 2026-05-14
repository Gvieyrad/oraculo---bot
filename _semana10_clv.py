#!/usr/bin/env python3
"""
Semana 10-11: Betfair SP como CLV oracle
 - Copia oraculo_clv.py al servidor
 - Parchea runner:
     * Import CLVOracle
     * Cada ciclo: fetch_and_record() para football/tennis/MLB
     * Al liquidar: resolve_clv_for_pick() reemplaza CLV simple
     * /clv ahora muestra fuente Betfair vs Pinnacle
 - Parchea oraculo_sibila.py:
     * Columnas clv_betfair, clv_pinnacle, clv_source
     * Stats CLV por fuente en /sibila
"""

import os
import shutil

BASE    = '/home/noc/oraculo_v2'
RUNNER  = f'{BASE}/oraculo_runner_auto.py'
SIBILA  = f'{BASE}/oraculo_sibila.py'
CLV_SRC = os.path.join(os.path.dirname(__file__), 'oraculo_clv.py')
CLV_DST = f'{BASE}/oraculo_clv.py'

# ============================================================
# 1. Copiar oraculo_clv.py
# ============================================================
shutil.copy(CLV_SRC, CLV_DST)
print(f'OK: oraculo_clv.py copiado a {CLV_DST}')
try:
    with open(CLV_DST) as f:
        compile(f.read(), CLV_DST, 'exec')
    print('OK: oraculo_clv.py SYNTAX OK')
except SyntaxError as e:
    print(f'ERROR oraculo_clv.py: {e}')
    raise

# ============================================================
# 2. Parchear oraculo_runner_auto.py
# ============================================================
with open(RUNNER) as f:
    code = f.read()

changes = 0

# --- FIX R1: import CLVOracle junto a Portfolio ---
old_import = ("try:\n"
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
new_import = (old_import + "\n"
              "try:\n"
              "    from oraculo_clv import CLVOracle as _CLVOracle\n"
              "    _CLV_ORACLE = _CLVOracle(odds_api_key=ODDS_API_KEY, rlm_tracker=_RLM if _RLM_ENABLED else None)\n"
              "    _CLV_ORACLE_ENABLED = True\n"
              "except Exception as _ce:\n"
              "    _CLV_ORACLE_ENABLED = False\n"
              "    log.warning('CLV Oracle no disponible: %s', _ce)\n"
              "    class _FakeCLV:\n"
              "        def fetch_and_record(self, *a, **kw): return 0\n"
              "        def resolve_clv_for_pick(self, *a, **kw): return {}\n"
              "        def clv_quality_label(self, clv): return ''\n"
              "    _CLV_ORACLE = _FakeCLV()")
if old_import in code and '_CLV_ORACLE_ENABLED' not in code:
    code = code.replace(old_import, new_import, 1)
    changes += 1
    print('OK FixR1: import CLVOracle')
else:
    print('SKIP FixR1')

# --- FIX R2: fetch_and_record en cada ciclo (football + tennis + MLB) ---
# Buscar el log de inicio de ciclo donde ya pusimos el purge de RLM
old_cycle = ("    if _RLM_ENABLED:\n"
             "        try:\n"
             "            _RLM.purge_old(days=30)\n"
             "        except Exception:\n"
             "            pass")
new_cycle = ("    if _RLM_ENABLED:\n"
             "        try:\n"
             "            _RLM.purge_old(days=30)\n"
             "        except Exception:\n"
             "            pass\n"
             "    if _CLV_ORACLE_ENABLED:\n"
             "        try:\n"
             "            _n = _CLV_ORACLE.fetch_and_record('soccer_epl', markets='h2h,totals', min_interval=900)\n"
             "            if _n: log.debug('[CLV] football: %d outcomes Betfair/Pinnacle', _n)\n"
             "            _n = _CLV_ORACLE.fetch_and_record('tennis_atp_french_open', markets='h2h', min_interval=900)\n"
             "            if _n: log.debug('[CLV] tennis: %d outcomes Betfair/Pinnacle', _n)\n"
             "            _n = _CLV_ORACLE.fetch_and_record('baseball_mlb', markets='h2h,totals', min_interval=900)\n"
             "            if _n: log.debug('[CLV] mlb: %d outcomes Betfair/Pinnacle', _n)\n"
             "        except Exception as _clve:\n"
             "            log.warning('[CLV] fetch error: %s', _clve)")
if old_cycle in code and '_CLV_ORACLE.fetch_and_record' not in code:
    code = code.replace(old_cycle, new_cycle, 1)
    changes += 1
    print('OK FixR2: fetch_and_record en ciclo')
else:
    print('SKIP FixR2')

# --- FIX R3: CLV oracle al liquidar, reemplaza CLV simple ---
# Actualmente el runner calcula CLV como: clv = closing_odds / entry_odds - 1
# Buscamos el bloque de settlement donde se llama _log_settlement
old_settle = ("        _log_settlement(bet_id, result, wl)\n"
              "        if _SIBILA_ENABLED:\n"
              "            _sibila_resolve(bet_id=bet_id, result=result)\n"
              "        if _PORTFOLIO_ENABLED:\n"
              "            _PORTFOLIO.invalidate_cache()  # bet cerrada -> recalcular portafolio")
new_settle = ("        _log_settlement(bet_id, result, wl)\n"
              "        # CLV Oracle: calcular CLV con Betfair/Pinnacle como referencia\n"
              "        _clv_data = {}\n"
              "        if _CLV_ORACLE_ENABLED:\n"
              "            try:\n"
              "                _clv_data = _CLV_ORACLE.resolve_clv_for_pick(\n"
              "                    p, entry_odds=p.get('price'), settled_ts=int(time.time())\n"
              "                )\n"
              "            except Exception as _clve:\n"
              "                log.warning('[CLV] resolve error: %s', _clve)\n"
              "        if _SIBILA_ENABLED:\n"
              "            _sibila_resolve(\n"
              "                bet_id=bet_id, result=result,\n"
              "                closing_odds=_clv_data.get('closing_odds_betfair') or _clv_data.get('closing_odds_pinnacle'),\n"
              "                clv_betfair=_clv_data.get('clv_betfair'),\n"
              "                clv_pinnacle=_clv_data.get('clv_pinnacle'),\n"
              "                clv_source=_clv_data.get('clv_source'),\n"
              "            )\n"
              "        if _PORTFOLIO_ENABLED:\n"
              "            _PORTFOLIO.invalidate_cache()  # bet cerrada -> recalcular portafolio")
if old_settle in code:
    code = code.replace(old_settle, new_settle, 1)
    changes += 1
    print('OK FixR3: CLV oracle en settlement')
else:
    print('SKIP FixR3: anchor settlement no encontrado')

# --- FIX R4: /clv mejorado con fuente Betfair ---
# Extender el comando /clv existente para mostrar fuente
old_clv_cmd = ("    elif cmd[0] == '/clv':\n"
               "        return format_clv_telegram(PREDICTIONS_FILE)")
new_clv_cmd = ("    elif cmd[0] == '/clv':\n"
               "        _clv_base = format_clv_telegram(PREDICTIONS_FILE)\n"
               "        if _CLV_ORACLE_ENABLED:\n"
               "            _clv_base += '\\n\\n_Referencia: Betfair Exchange + Pinnacle_'\n"
               "        return _clv_base")
if old_clv_cmd in code:
    code = code.replace(old_clv_cmd, new_clv_cmd, 1)
    changes += 1
    print('OK FixR4: /clv con fuente Betfair')
else:
    print('SKIP FixR4')

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
# 3. Parchear oraculo_sibila.py: columnas CLV Betfair
# ============================================================
with open(SIBILA) as f:
    scode = f.read()

schanges = 0

# --- FIX S1: columnas clv_betfair, clv_pinnacle, clv_source en schema ---
old_schema = ("                clv         REAL,\n"
              "                fresh_line  INTEGER DEFAULT 0,\n"
              "                rlm_signal     INTEGER DEFAULT 0,\n"
              "                rlm_score      REAL DEFAULT 0.0,\n"
              "                portfolio_corr REAL DEFAULT 0.0,\n"
              "                portfolio_cap  INTEGER DEFAULT 0\n"
              "            )")
new_schema  = ("                clv            REAL,\n"
               "                clv_betfair    REAL,\n"
               "                clv_pinnacle   REAL,\n"
               "                clv_source     TEXT,\n"
               "                fresh_line     INTEGER DEFAULT 0,\n"
               "                rlm_signal     INTEGER DEFAULT 0,\n"
               "                rlm_score      REAL DEFAULT 0.0,\n"
               "                portfolio_corr REAL DEFAULT 0.0,\n"
               "                portfolio_cap  INTEGER DEFAULT 0\n"
               "            )")
if old_schema in scode and 'clv_betfair' not in scode:
    scode = scode.replace(old_schema, new_schema, 1)
    schanges += 1
    print('OK FixS1: columnas clv_betfair/pinnacle/source en schema')
else:
    print('SKIP FixS1')

# --- FIX S2: ALTER TABLE migracion ---
old_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_corr REAL DEFAULT 0.0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_cap INTEGER DEFAULT 0')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
new_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_corr REAL DEFAULT 0.0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN portfolio_cap INTEGER DEFAULT 0')\n"
             "            except Exception:\n"
             "                pass  # ya existen\n"
             "            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_betfair REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_pinnacle REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_source TEXT')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
if old_alter in scode and 'clv_betfair' not in scode:
    scode = scode.replace(old_alter, new_alter, 1)
    schanges += 1
    print('OK FixS2: ALTER TABLE clv_betfair migracion')
else:
    print('SKIP FixS2')

# --- FIX S3: resolve_pick() acepta clv_betfair/pinnacle/source ---
# Buscar la firma de resolve_pick
old_resolve_sig = ("def resolve_pick(bet_id: str = None, match: str = None, label: str = None,\n"
                   "                 result: str = None, closing_odds: float = None) -> bool:")
new_resolve_sig  = ("def resolve_pick(bet_id: str = None, match: str = None, label: str = None,\n"
                    "                 result: str = None, closing_odds: float = None,\n"
                    "                 clv_betfair: float = None, clv_pinnacle: float = None,\n"
                    "                 clv_source: str = None) -> bool:")
if old_resolve_sig in scode:
    scode = scode.replace(old_resolve_sig, new_resolve_sig, 1)
    schanges += 1
    print('OK FixS3: resolve_pick() acepta clv_betfair/pinnacle/source')
else:
    print('SKIP FixS3: firma resolve_pick no encontrada')

# --- FIX S4: UPDATE en resolve_pick guarda clv_betfair/pinnacle ---
old_resolve_update = ("            cur.execute(\n"
                      "                'UPDATE sibila_picks SET result=?, pnl=?, closing_odds=?, clv=? WHERE id=?',\n"
                      "                (result, pnl, closing_odds, clv, row['id'])\n"
                      "            )")
new_resolve_update  = ("            # Usar CLV de Betfair/Pinnacle si disponible, sino el calculado\n"
                       "            _best_clv = clv_betfair if clv_betfair is not None else (clv_pinnacle if clv_pinnacle is not None else clv)\n"
                       "            cur.execute(\n"
                       "                'UPDATE sibila_picks SET result=?, pnl=?, closing_odds=?, clv=?,'\n"
                       "                ' clv_betfair=?, clv_pinnacle=?, clv_source=? WHERE id=?',\n"
                       "                (result, pnl, closing_odds, _best_clv,\n"
                       "                 clv_betfair, clv_pinnacle, clv_source, row['id'])\n"
                       "            )")
if old_resolve_update in scode:
    scode = scode.replace(old_resolve_update, new_resolve_update, 1)
    schanges += 1
    print('OK FixS4: UPDATE guarda clv_betfair/pinnacle')
else:
    print('SKIP FixS4: anchor UPDATE resolve no encontrado')

# --- FIX S5: stats CLV por fuente en get_stats() ---
old_avg_clv = ("    avg_clv = (sum(r['clv'] for r in rows if r['clv'] is not None)\n"
               "               / max(1, sum(1 for r in rows if r['clv'] is not None)))")
new_avg_clv  = ("    avg_clv = (sum(r['clv'] for r in rows if r['clv'] is not None)\n"
                "               / max(1, sum(1 for r in rows if r['clv'] is not None)))\n"
                "    # CLV por fuente\n"
                "    _bf_rows   = [r for r in rows if r.get('clv_betfair') is not None]\n"
                "    _pinn_rows = [r for r in rows if r.get('clv_pinnacle') is not None and r.get('clv_betfair') is None]\n"
                "    avg_clv_betfair  = (sum(r['clv_betfair'] for r in _bf_rows) / len(_bf_rows)) if _bf_rows else None\n"
                "    avg_clv_pinnacle = (sum(r['clv_pinnacle'] for r in _pinn_rows) / len(_pinn_rows)) if _pinn_rows else None\n"
                "    n_clv_betfair    = len(_bf_rows)\n"
                "    n_clv_pinnacle   = len(_pinn_rows)")
if old_avg_clv in scode:
    scode = scode.replace(old_avg_clv, new_avg_clv, 1)
    schanges += 1
    print('OK FixS5: CLV stats por fuente')
else:
    print('SKIP FixS5: anchor avg_clv no encontrado')

# --- FIX S6: incluir CLV betfair en return de get_stats ---
old_return_clv = ("        'avg_clv': avg_clv,\n"
                  "        'by_sport': by_sport,")
new_return_clv  = ("        'avg_clv':          avg_clv,\n"
                   "        'avg_clv_betfair':  avg_clv_betfair,\n"
                   "        'avg_clv_pinnacle': avg_clv_pinnacle,\n"
                   "        'n_clv_betfair':    n_clv_betfair,\n"
                   "        'n_clv_pinnacle':   n_clv_pinnacle,\n"
                   "        'by_sport': by_sport,")
if old_return_clv in scode:
    scode = scode.replace(old_return_clv, new_return_clv, 1)
    schanges += 1
    print('OK FixS6: CLV betfair en return get_stats')
else:
    print('SKIP FixS6')

# --- FIX S7: mostrar CLV Betfair en format_telegram ---
# Reemplazar la linea de CLV actual con desglose por fuente
old_clv_tg = ("    avg_clv = s.get('avg_clv')\n"
              "    clv_str = f'{avg_clv*100:+.2f}%' if avg_clv is not None else 'sin datos'")
new_clv_tg  = ("    avg_clv = s.get('avg_clv')\n"
               "    clv_str = f'{avg_clv*100:+.2f}%' if avg_clv is not None else 'sin datos'\n"
               "    # CLV por fuente\n"
               "    _bf_clv   = s.get('avg_clv_betfair')\n"
               "    _pinn_clv = s.get('avg_clv_pinnacle')\n"
               "    _n_bf     = s.get('n_clv_betfair', 0)\n"
               "    _n_pinn   = s.get('n_clv_pinnacle', 0)\n"
               "    if _bf_clv is not None:\n"
               "        clv_str = (f'Betfair: {_bf_clv*100:+.2f}% ({_n_bf}p)'\n"
               "                   + (f' | Pinnacle: {_pinn_clv*100:+.2f}% ({_n_pinn}p)' if _pinn_clv is not None else ''))\n"
               "    elif _pinn_clv is not None:\n"
               "        clv_str = f'Pinnacle: {_pinn_clv*100:+.2f}% ({_n_pinn}p)'")
if old_clv_tg in scode:
    scode = scode.replace(old_clv_tg, new_clv_tg, 1)
    schanges += 1
    print('OK FixS7: CLV Betfair en format_telegram')
else:
    print('SKIP FixS7: anchor CLV telegram no encontrado')

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
print('  [CLV] fetch_and_record(soccer_epl): 120 outcomes Betfair/Pinnacle')
print('  [CLV] Djokovic vs Sinner | entry=1.85 betfair=1.72 pinn=1.74 → CLV betfair=+7.56%')
print()
print('NOTA: si ODDS_API_KEY no esta definida como constante, ajustar:')
print('  _CLV_ORACLE = _CLVOracle(odds_api_key=ODDS_API_KEY, ...)')
print('  → buscar la variable exacta en el runner y editar la linea de import')
