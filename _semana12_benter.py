#!/usr/bin/env python3
"""
Semana 12: Benter Trick integration
 - Copia oraculo_benter.py al servidor
 - Parchea runner:
     * Import BenterCalibrator
     * Aplica Benter a football picks antes de retornar
     * Aplica Benter a tennis picks
     * Aplica Benter a MLB picks
     * /benter en Telegram: estado de alphas aprendidos
 - Parchea oraculo_sibila.py:
     * Columnas benter_alpha, bm_implied, raw_model_prob, edge_delta
     * Stats: picks Benter-filtrados vs pasados
"""

import os
import shutil

BASE      = '/home/noc/oraculo_v2'
RUNNER    = f'{BASE}/oraculo_runner_auto.py'
SIBILA    = f'{BASE}/oraculo_sibila.py'
BENT_SRC  = os.path.join(os.path.dirname(__file__), 'oraculo_benter.py')
BENT_DST  = f'{BASE}/oraculo_benter.py'

# ============================================================
# 1. Copiar oraculo_benter.py
# ============================================================
shutil.copy(BENT_SRC, BENT_DST)
print(f'OK: oraculo_benter.py copiado a {BENT_DST}')
try:
    with open(BENT_DST) as f:
        compile(f.read(), BENT_DST, 'exec')
    print('OK: oraculo_benter.py SYNTAX OK')
except SyntaxError as e:
    print(f'ERROR oraculo_benter.py: {e}')
    raise

# ============================================================
# 2. Parchear oraculo_runner_auto.py
# ============================================================
with open(RUNNER) as f:
    code = f.read()

changes = 0

# --- FIX R1: import BenterCalibrator junto a CLV Oracle ---
old_import = ("try:\n"
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
new_import = (old_import + "\n"
              "try:\n"
              "    from oraculo_benter import get_calibrator as _benter_cal\n"
              "    _BENTER = _benter_cal(\n"
              "        sibila_db=os.path.join(os.path.dirname(__file__), '.oraculo_cache', 'sibila.db')\n"
              "    )\n"
              "    _BENTER_ENABLED = True\n"
              "    log.info('Benter Calibrator cargado (alpha football=%.2f tennis=%.2f mlb=%.2f)',\n"
              "             _BENTER._alphas.get('soccer',0.60),\n"
              "             _BENTER._alphas.get('tennis',0.65),\n"
              "             _BENTER._alphas.get('mlb',0.55))\n"
              "except Exception as _be:\n"
              "    _BENTER_ENABLED = False\n"
              "    log.warning('Benter no disponible: %s', _be)\n"
              "    class _FakeBenter:\n"
              "        _alphas = {}\n"
              "        def apply_batch(self, picks, **kw): return picks\n"
              "        def recalibrate(self, **kw): return False\n"
              "    _BENTER = _FakeBenter()")
if old_import in code and '_BENTER_ENABLED' not in code:
    code = code.replace(old_import, new_import, 1)
    changes += 1
    print('OK FixR1: import BenterCalibrator')
else:
    print('SKIP FixR1')

# --- FIX R2: Benter en football picks (antes de fresh/RLM/sibila) ---
old_fb_fresh = ("    if _FRESH_ENABLED:\n"
                "        picks = _fresh_check(picks)\n"
                "        _fs = _fresh_summary(picks)\n"
                "        if _fs: log.info(_fs)\n"
                "    if _RLM_ENABLED:\n"
                "        _RLM.record_batch(picks, book='cloudbet')\n"
                "        picks = _RLM.tag_picks(picks)\n"
                "    if _SIBILA_ENABLED:\n"
                "        for _sp in picks:\n"
                "            _sibila_record(_sp)\n"
                "    return picks")
new_fb_fresh = ("    # Benter trick: recalibrar model_prob con implied del bookmaker\n"
                "    if _BENTER_ENABLED:\n"
                "        picks = _BENTER.apply_batch(picks, book='cloudbet')\n"
                "    if _FRESH_ENABLED:\n"
                "        picks = _fresh_check(picks)\n"
                "        _fs = _fresh_summary(picks)\n"
                "        if _fs: log.info(_fs)\n"
                "    if _RLM_ENABLED:\n"
                "        _RLM.record_batch(picks, book='cloudbet')\n"
                "        picks = _RLM.tag_picks(picks)\n"
                "    if _SIBILA_ENABLED:\n"
                "        for _sp in picks:\n"
                "            _sibila_record(_sp)\n"
                "    return picks")
if old_fb_fresh in code:
    code = code.replace(old_fb_fresh, new_fb_fresh, 1)
    changes += 1
    print('OK FixR2: Benter en football picks')
else:
    print('SKIP FixR2: anchor football+fresh no encontrado')

# --- FIX R3: Benter en tennis picks ---
old_tn_fresh = ("    if _FRESH_ENABLED:\n"
                "        picks = _fresh_check(picks)\n"
                "        _fs = _fresh_summary(picks)\n"
                "        if _fs: log.info(_fs)\n"
                "    if _RLM_ENABLED:\n"
                "        _RLM.record_batch(picks, book='cloudbet')\n"
                "        picks = _RLM.tag_picks(picks)\n"
                "    if _SIBILA_ENABLED:\n"
                "        for _sp in picks:\n"
                "            _sibila_record(_sp)")
new_tn_fresh = ("    # Benter trick\n"
                "    if _BENTER_ENABLED:\n"
                "        picks = _BENTER.apply_batch(picks, book='cloudbet')\n"
                "    if _FRESH_ENABLED:\n"
                "        picks = _fresh_check(picks)\n"
                "        _fs = _fresh_summary(picks)\n"
                "        if _fs: log.info(_fs)\n"
                "    if _RLM_ENABLED:\n"
                "        _RLM.record_batch(picks, book='cloudbet')\n"
                "        picks = _RLM.tag_picks(picks)\n"
                "    if _SIBILA_ENABLED:\n"
                "        for _sp in picks:\n"
                "            _sibila_record(_sp)")
if old_tn_fresh in code:
    code = code.replace(old_tn_fresh, new_tn_fresh, 1)
    changes += 1
    print('OK FixR3: Benter en tennis picks')
else:
    print('SKIP FixR3: anchor tennis+fresh no encontrado')

# --- FIX R4: Benter en MLB picks ---
old_mlb_fresh = ("            if _FRESH_ENABLED:\n"
                 "                mlb_picks = _fresh_check(mlb_picks)\n"
                 "                _fs = _fresh_summary(mlb_picks)\n"
                 "                if _fs: log.info(_fs)\n"
                 "            if _RLM_ENABLED:\n"
                 "                _RLM.record_batch(mlb_picks, book='cloudbet')\n"
                 "                mlb_picks = _RLM.tag_picks(mlb_picks)\n"
                 "            if _SIBILA_ENABLED:\n"
                 "                for _sp in mlb_picks:\n"
                 "                    _sibila_record(_sp)")
new_mlb_fresh = ("            if _BENTER_ENABLED:\n"
                 "                mlb_picks = _BENTER.apply_batch(mlb_picks, book='cloudbet')\n"
                 "            if _FRESH_ENABLED:\n"
                 "                mlb_picks = _fresh_check(mlb_picks)\n"
                 "                _fs = _fresh_summary(mlb_picks)\n"
                 "                if _fs: log.info(_fs)\n"
                 "            if _RLM_ENABLED:\n"
                 "                _RLM.record_batch(mlb_picks, book='cloudbet')\n"
                 "                mlb_picks = _RLM.tag_picks(mlb_picks)\n"
                 "            if _SIBILA_ENABLED:\n"
                 "                for _sp in mlb_picks:\n"
                 "                    _sibila_record(_sp)")
if old_mlb_fresh in code:
    code = code.replace(old_mlb_fresh, new_mlb_fresh, 1)
    changes += 1
    print('OK FixR4: Benter en MLB picks')
else:
    print('SKIP FixR4: anchor MLB+fresh no encontrado')

# --- FIX R5: recalibrar Benter en ciclo diario ---
old_clv_cycle = ("    if _CLV_ORACLE_ENABLED:\n"
                 "        try:\n"
                 "            _n = _CLV_ORACLE.fetch_and_record('soccer_epl', markets='h2h,totals', min_interval=900)")
new_clv_cycle = ("    if _BENTER_ENABLED:\n"
                 "        try:\n"
                 "            _BENTER.recalibrate()  # aprende alphas de Sibila si hay datos\n"
                 "        except Exception as _be2:\n"
                 "            log.debug('[Benter] recalibrate error: %s', _be2)\n"
                 "    if _CLV_ORACLE_ENABLED:\n"
                 "        try:\n"
                 "            _n = _CLV_ORACLE.fetch_and_record('soccer_epl', markets='h2h,totals', min_interval=900)")
if old_clv_cycle in code:
    code = code.replace(old_clv_cycle, new_clv_cycle, 1)
    changes += 1
    print('OK FixR5: recalibrar Benter en ciclo')
else:
    print('SKIP FixR5')

# --- FIX R6: comando /benter en Telegram ---
old_portfolio_cmd = ("    elif cmd[0] == '/portfolio':\n"
                     "        return _PORTFOLIO.format_status() if _PORTFOLIO_ENABLED else 'Portfolio no disponible'")
new_portfolio_cmd = ("    elif cmd[0] == '/portfolio':\n"
                     "        return _PORTFOLIO.format_status() if _PORTFOLIO_ENABLED else 'Portfolio no disponible'\n"
                     "    elif cmd[0] == '/benter':\n"
                     "        if not _BENTER_ENABLED:\n"
                     "            return 'Benter no disponible'\n"
                     "        _ba = _BENTER._alphas\n"
                     "        lines = ['*Benter Calibrator*', '']\n"
                     "        lines.append('Alpha por deporte (modelo vs mercado):')\n"
                     "        for _sp, _al in sorted(_ba.items()):\n"
                     "            if _sp == 'default': continue\n"
                     "            lines.append(f'  {_sp}: {_al:.2f} ({_al*100:.0f}% modelo / {(1-_al)*100:.0f}% mercado)')\n"
                     "        lines.append('')\n"
                     "        lines.append('_Alpha 1.0 = solo modelo | 0.0 = solo mercado_')\n"
                     "        return '\\n'.join(lines)")
if old_portfolio_cmd in code and "'/benter'" not in code:
    code = code.replace(old_portfolio_cmd, new_portfolio_cmd, 1)
    changes += 1
    print('OK FixR6: comando /benter en Telegram')
else:
    print('SKIP FixR6')

# --- FIX R7: /benter en /help ---
old_help = ("'/portfolio - Estado del portafolio Kelly\\n'\n"
            "                '/help - This message')")
new_help  = ("'/portfolio - Estado del portafolio Kelly\\n'\n"
             "                '/benter - Alphas modelo vs mercado\\n'\n"
             "                '/help - This message')")
if old_help in code:
    code = code.replace(old_help, new_help, 1)
    changes += 1
    print('OK FixR7: /benter en /help')
else:
    print('SKIP FixR7')

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
# 3. Parchear oraculo_sibila.py
# ============================================================
with open(SIBILA) as f:
    scode = f.read()

schanges = 0

# --- FIX S1: columnas Benter en schema ---
old_schema = ("                portfolio_corr REAL DEFAULT 0.0,\n"
              "                portfolio_cap  INTEGER DEFAULT 0\n"
              "            )")
new_schema  = ("                portfolio_corr  REAL DEFAULT 0.0,\n"
               "                portfolio_cap   INTEGER DEFAULT 0,\n"
               "                benter_alpha    REAL,\n"
               "                bm_implied      REAL,\n"
               "                raw_model_prob  REAL,\n"
               "                edge_delta      REAL\n"
               "            )")
if old_schema in scode and 'benter_alpha' not in scode:
    scode = scode.replace(old_schema, new_schema, 1)
    schanges += 1
    print('OK FixS1: columnas Benter en schema')
else:
    print('SKIP FixS1')

# --- FIX S2: ALTER TABLE migracion ---
old_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_betfair REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_pinnacle REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_source TEXT')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
new_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_betfair REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_pinnacle REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN clv_source TEXT')\n"
             "            except Exception:\n"
             "                pass  # ya existen\n"
             "            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN benter_alpha REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN bm_implied REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN raw_model_prob REAL')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN edge_delta REAL')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
if old_alter in scode and 'benter_alpha' not in scode:
    scode = scode.replace(old_alter, new_alter, 1)
    schanges += 1
    print('OK FixS2: ALTER TABLE Benter migracion')
else:
    print('SKIP FixS2')

# --- FIX S3: guardar campos Benter en INSERT ---
old_cols = ("                (ts, sport, market, level, match, label, edge, model_prob,\n"
            "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line,\n"
            "                 rlm_signal, rlm_score, portfolio_corr, portfolio_cap)\n"
            "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
new_cols  = ("                (ts, sport, market, level, match, label, edge, model_prob,\n"
             "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line,\n"
             "                 rlm_signal, rlm_score, portfolio_corr, portfolio_cap,\n"
             "                 benter_alpha, bm_implied, raw_model_prob, edge_delta)\n"
             "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
if old_cols in scode:
    scode = scode.replace(old_cols, new_cols, 1)
    schanges += 1
    print('OK FixS3: INSERT columns Benter')
else:
    print('SKIP FixS3')

old_values = ("             1 if pick.get('rlm_signal') else 0,\n"
              "             float(pick.get('rlm_score', 0) or 0),\n"
              "             float(pick.get('portfolio_corr', 0) or 0),\n"
              "             1 if pick.get('portfolio_capped') else 0))")
new_values  = ("             1 if pick.get('rlm_signal') else 0,\n"
               "             float(pick.get('rlm_score', 0) or 0),\n"
               "             float(pick.get('portfolio_corr', 0) or 0),\n"
               "             1 if pick.get('portfolio_capped') else 0,\n"
               "             pick.get('benter_alpha'),\n"
               "             pick.get('bm_implied'),\n"
               "             pick.get('raw_model_prob'),\n"
               "             pick.get('edge_delta')))")
if old_values in scode:
    scode = scode.replace(old_values, new_values, 1)
    schanges += 1
    print('OK FixS3b: INSERT values Benter')
else:
    print('SKIP FixS3b')

# --- FIX S4: stats Benter en get_stats() ---
old_rlm_groups = ("    rlm_on    = [r for r in rows if r.get('rlm_signal')]\n"
                  "    rlm_off   = [r for r in rows if not r.get('rlm_signal')]\n"
                  "    port_cap  = [r for r in rows if r.get('portfolio_cap')]\n"
                  "    port_free = [r for r in rows if not r.get('portfolio_cap')]\n"
                  "    avg_corr  = (sum(float(r.get('portfolio_corr',0) or 0) for r in rows)\n"
                  "                 / len(rows) if rows else 0)\n"
                  "    return {")
new_rlm_groups = ("    rlm_on    = [r for r in rows if r.get('rlm_signal')]\n"
                  "    rlm_off   = [r for r in rows if not r.get('rlm_signal')]\n"
                  "    port_cap  = [r for r in rows if r.get('portfolio_cap')]\n"
                  "    port_free = [r for r in rows if not r.get('portfolio_cap')]\n"
                  "    avg_corr  = (sum(float(r.get('portfolio_corr',0) or 0) for r in rows)\n"
                  "                 / len(rows) if rows else 0)\n"
                  "    # Benter stats\n"
                  "    benter_rows    = [r for r in rows if r.get('benter_alpha') is not None]\n"
                  "    avg_alpha      = (sum(r['benter_alpha'] for r in benter_rows)\n"
                  "                     / len(benter_rows)) if benter_rows else None\n"
                  "    avg_edge_delta = (sum(float(r.get('edge_delta',0) or 0) for r in benter_rows)\n"
                  "                     / len(benter_rows)) if benter_rows else None\n"
                  "    return {")
if old_rlm_groups in scode:
    scode = scode.replace(old_rlm_groups, new_rlm_groups, 1)
    schanges += 1
    print('OK FixS4: Benter stats en get_stats()')
else:
    print('SKIP FixS4')

old_stats_end = ("        'port_cap':   _grp_roi(port_cap),\n"
                 "        'port_free':  _grp_roi(port_free),\n"
                 "        'avg_corr':   round(avg_corr, 3),\n"
                 "    }")
new_stats_end  = ("        'port_cap':     _grp_roi(port_cap),\n"
                  "        'port_free':    _grp_roi(port_free),\n"
                  "        'avg_corr':     round(avg_corr, 3),\n"
                  "        'benter_alpha': avg_alpha,\n"
                  "        'edge_delta':   avg_edge_delta,\n"
                  "        'n_benter':     len(benter_rows),\n"
                  "    }")
if old_stats_end in scode:
    scode = scode.replace(old_stats_end, new_stats_end, 1)
    schanges += 1
    print('OK FixS4b: Benter en return stats')
else:
    print('SKIP FixS4b')

# --- FIX S5: mostrar Benter en /sibila ---
old_tg_port = ("    lines.append('')\n"
               "    lines.append('_Sibila Shadow Book_')\n"
               "    return '\\n'.join(lines)")
new_tg_port  = ("    # Benter stats\n"
                "    _ba   = s.get('benter_alpha')\n"
                "    _bed  = s.get('edge_delta')\n"
                "    _nben = s.get('n_benter', 0)\n"
                "    if _ba is not None and _nben > 0:\n"
                "        lines.append('')\n"
                "        lines.append('*Benter Calibrator*')\n"
                "        lines.append(f'Alpha medio: {_ba:.2f} ({_nben} picks)')\n"
                "        if _bed is not None:\n"
                "            lines.append(f'Edge ajuste medio: {_bed*100:+.2f}% (negativo=conservador)')\n"
                "    lines.append('')\n"
                "    lines.append('_Sibila Shadow Book_')\n"
                "    return '\\n'.join(lines)")
if old_tg_port in scode:
    scode = scode.replace(old_tg_port, new_tg_port, 1)
    schanges += 1
    print('OK FixS5: Benter en format_telegram')
else:
    print('SKIP FixS5')

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
print('Verificar en logs al iniciar:')
print('  Benter Calibrator cargado (alpha football=0.60 tennis=0.65 mlb=0.55)')
print()
print('Verificar en cada scan:')
print('  [Benter] Sinner vs Alcaraz | Sets < 2.5 | model=61.2% bm=54.8% → blend=58.8%')
print('           edge +16.2%→+13.1% (alpha=0.65)')
print('  [Benter] 1 picks filtrados por Benter (edge reducido < 0)')
print()
print('Nuevo comando: /benter')
print('  Alpha por deporte:')
print('    football: 0.60  (60% modelo / 40% mercado)')
print('    tennis:   0.65  (65% modelo / 35% mercado)')
print('    mlb:      0.55  (55% modelo / 45% mercado)')
