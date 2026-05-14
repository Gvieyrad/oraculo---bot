#!/usr/bin/env python3
"""
Semana 6-7: Reverse Line Movement integration
 - Copia oraculo_rlm.py al servidor
 - Parchea runner: import RLM + record_batch + tag_picks
 - Parchea oraculo_sibila.py: columna rlm_signal + stats separadas
"""

import os
import shutil
import textwrap

BASE   = '/home/noc/oraculo_v2'
RUNNER = f'{BASE}/oraculo_runner_auto.py'
SIBILA = f'{BASE}/oraculo_sibila.py'
RLM_SRC = os.path.join(os.path.dirname(__file__), 'oraculo_rlm.py')
RLM_DST = f'{BASE}/oraculo_rlm.py'

# ============================================================
# 1. Copiar oraculo_rlm.py al servidor
# ============================================================
shutil.copy(RLM_SRC, RLM_DST)
print(f'OK: oraculo_rlm.py copiado a {RLM_DST}')
try:
    with open(RLM_DST) as f:
        compile(f.read(), RLM_DST, 'exec')
    print('OK: oraculo_rlm.py SYNTAX OK')
except SyntaxError as e:
    print(f'ERROR oraculo_rlm.py: {e}')
    raise

# ============================================================
# 2. Parchear oraculo_runner_auto.py
# ============================================================
with open(RUNNER) as f:
    code = f.read()

changes = 0

# --- FIX R1: import RLM junto a fresh_line ---
old_import = ("try:\n"
              "    from oraculo_fresh_line import check_picks as _fresh_check, fresh_summary as _fresh_summary\n"
              "    _FRESH_ENABLED = True\n"
              "except ImportError:\n"
              "    _FRESH_ENABLED = False\n"
              "    def _fresh_check(p): return p\n"
              "    def _fresh_summary(p): return ''")
new_import = (old_import + "\n"
              "try:\n"
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
if old_import in code and '_RLM_ENABLED' not in code:
    code = code.replace(old_import, new_import, 1)
    changes += 1
    print('OK FixR1: import oraculo_rlm')
else:
    print('SKIP FixR1')

# --- FIX R2: record_batch football odds + tag_picks ---
# Despues de fresh_check en football, antes de sibila record
old_fb = ("    if _FRESH_ENABLED:\n"
          "        picks = _fresh_check(picks)\n"
          "        _fs = _fresh_summary(picks)\n"
          "        if _fs: log.info(_fs)\n"
          "    if _SIBILA_ENABLED:\n"
          "        for _sp in picks:\n"
          "            _sibila_record(_sp)\n"
          "    return picks")
new_fb = ("    if _FRESH_ENABLED:\n"
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
if old_fb in code:
    code = code.replace(old_fb, new_fb, 1)
    changes += 1
    print('OK FixR2: RLM en football picks')
else:
    print('SKIP FixR2: anchor football no encontrado')

# --- FIX R3: RLM en tennis picks ---
old_tn = ("    if _FRESH_ENABLED:\n"
          "        picks = _fresh_check(picks)\n"
          "        _fs = _fresh_summary(picks)\n"
          "        if _fs: log.info(_fs)\n"
          "    if _SIBILA_ENABLED:\n"
          "        for _sp in picks:\n"
          "            _sibila_record(_sp)")
new_tn = ("    if _FRESH_ENABLED:\n"
          "        picks = _fresh_check(picks)\n"
          "        _fs = _fresh_summary(picks)\n"
          "        if _fs: log.info(_fs)\n"
          "    if _RLM_ENABLED:\n"
          "        _RLM.record_batch(picks, book='cloudbet')\n"
          "        picks = _RLM.tag_picks(picks)\n"
          "    if _SIBILA_ENABLED:\n"
          "        for _sp in picks:\n"
          "            _sibila_record(_sp)")
if old_tn in code:
    code = code.replace(old_tn, new_tn, 1)
    changes += 1
    print('OK FixR3: RLM en tennis picks')
else:
    print('SKIP FixR3: anchor tennis no encontrado')

# --- FIX R4: RLM en MLB picks ---
old_mlb = ("            if _FRESH_ENABLED:\n"
           "                mlb_picks = _fresh_check(mlb_picks)\n"
           "                _fs = _fresh_summary(mlb_picks)\n"
           "                if _fs: log.info(_fs)\n"
           "            if _SIBILA_ENABLED:\n"
           "                for _sp in mlb_picks:\n"
           "                    _sibila_record(_sp)")
new_mlb = ("            if _FRESH_ENABLED:\n"
           "                mlb_picks = _fresh_check(mlb_picks)\n"
           "                _fs = _fresh_summary(mlb_picks)\n"
           "                if _fs: log.info(_fs)\n"
           "            if _RLM_ENABLED:\n"
           "                _RLM.record_batch(mlb_picks, book='cloudbet')\n"
           "                mlb_picks = _RLM.tag_picks(mlb_picks)\n"
           "            if _SIBILA_ENABLED:\n"
           "                for _sp in mlb_picks:\n"
           "                    _sibila_record(_sp)")
if old_mlb in code:
    code = code.replace(old_mlb, new_mlb, 1)
    changes += 1
    print('OK FixR4: RLM en MLB picks')
else:
    print('SKIP FixR4: anchor MLB no encontrado')

# --- FIX R5: boost de stake si RLM confirma pick ---
# Si rlm_signal=True y la senal esta alineada con nuestro pick → stake factor +20%
old_stake = ("            if _SIBILA_ENABLED:\n"
             "                _sibila_placed(p['match'], p['label'], bet_id, stake)")
new_stake = ("            if _SIBILA_ENABLED:\n"
             "                _sibila_placed(p['match'], p['label'], bet_id, stake)\n"
             "            # RLM boost: si sharp money confirma nuestro pick -> +20% stake\n"
             "            if _RLM_ENABLED and p.get('rlm_signal') and p.get('rlm_score', 0) >= 0.5:\n"
             "                _rlm_boost = min(stake * 1.20, stake + 20)  # max +20 unidades\n"
             "                log.info('  [RLM] Stake boost %.2f->%.2f (score=%.2f)', stake, _rlm_boost, p.get('rlm_score',0))\n"
             "                stake = round(_rlm_boost, 2)")
if old_stake in code:
    code = code.replace(old_stake, new_stake, 1)
    changes += 1
    print('OK FixR5: RLM stake boost al colocar')
else:
    print('SKIP FixR5: anchor placed no encontrado')

# --- FIX R6: purge en ciclo daily ---
old_purge_anchor = "log.info('=== Oraculo cycle start"
# Buscar primer occurrence de daily cleanup o simplemente agregar al final del loop
# Mas seguro: agregar purge en el bloque de inicio del ciclo
if '_RLM.purge_old' not in code:
    # Agregar purge semanal con un contador simple
    old_cycle_log = "log.info('=== Oraculo cycle start"
    if old_cycle_log in code:
        # Encontrar la linea y agregar despues
        idx = code.find(old_cycle_log)
        line_end = code.find('\n', idx)
        purge_snippet = ("\n    if _RLM_ENABLED:\n"
                         "        try:\n"
                         "            _RLM.purge_old(days=30)\n"
                         "        except Exception:\n"
                         "            pass")
        code = code[:line_end] + purge_snippet + code[line_end:]
        changes += 1
        print('OK FixR6: purge_old en ciclo')
    else:
        print('SKIP FixR6: anchor cycle log no encontrado')

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
# 3. Parchear oraculo_sibila.py: columna rlm_signal
# ============================================================
with open(SIBILA) as f:
    scode = f.read()

schanges = 0

# --- FIX S1: agregar columna rlm_signal al schema ---
old_schema_end = ("                fresh_line  INTEGER DEFAULT 0\n"
                  "            )")
new_schema_end = ("                fresh_line  INTEGER DEFAULT 0,\n"
                  "                rlm_signal  INTEGER DEFAULT 0,\n"
                  "                rlm_score   REAL DEFAULT 0.0\n"
                  "            )")
if old_schema_end in scode and 'rlm_signal' not in scode:
    scode = scode.replace(old_schema_end, new_schema_end, 1)
    schanges += 1
    print('OK FixS1: columnas rlm_signal/rlm_score en schema')
else:
    print('SKIP FixS1')

# --- FIX S2: ALTER TABLE para DBs existentes ---
old_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN fresh_line INTEGER DEFAULT 0')\n"
             "            except Exception:\n"
             "                pass  # ya existe")
new_alter = ("            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN fresh_line INTEGER DEFAULT 0')\n"
             "            except Exception:\n"
             "                pass  # ya existe\n"
             "            try:\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_signal INTEGER DEFAULT 0')\n"
             "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN rlm_score REAL DEFAULT 0.0')\n"
             "            except Exception:\n"
             "                pass  # ya existen")
if old_alter in scode and 'ALTER TABLE sibila_picks ADD COLUMN rlm_signal' not in scode:
    scode = scode.replace(old_alter, new_alter, 1)
    schanges += 1
    print('OK FixS2: ALTER TABLE rlm migracion')
else:
    print('SKIP FixS2')

# --- FIX S3: guardar rlm_signal en INSERT ---
old_insert = ("            INSERT INTO sibila_picks\n"
              "                (ts, sport, market, level, match, label, edge, model_prob,\n"
              "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line)\n"
              "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n"
              "            ''',\n"
              "            (int(time.time()), sport, market, level,\n"
              "             pick.get('match',''), pick.get('label',''),\n"
              "             pick.get('edge', 0), pick.get('model_prob', 0),\n"
              "             pick.get('price', 0), vstake,\n"
              "             1 if placed else 0, real_stake, bet_id,\n"
              "             1 if pick.get('fresh_line') else 0))")
new_insert = ("            INSERT INTO sibila_picks\n"
              "                (ts, sport, market, level, match, label, edge, model_prob,\n"
              "                 price, virtual_stake, placed, real_stake, bet_id, fresh_line,\n"
              "                 rlm_signal, rlm_score)\n"
              "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n"
              "            ''',\n"
              "            (int(time.time()), sport, market, level,\n"
              "             pick.get('match',''), pick.get('label',''),\n"
              "             pick.get('edge', 0), pick.get('model_prob', 0),\n"
              "             pick.get('price', 0), vstake,\n"
              "             1 if placed else 0, real_stake, bet_id,\n"
              "             1 if pick.get('fresh_line') else 0,\n"
              "             1 if pick.get('rlm_signal') else 0,\n"
              "             float(pick.get('rlm_score', 0) or 0)))")
if old_insert in scode:
    scode = scode.replace(old_insert, new_insert, 1)
    schanges += 1
    print('OK FixS3: guardar rlm_signal en INSERT')
else:
    print('SKIP FixS3: anchor INSERT no encontrado')

# --- FIX S4: stats de rlm_signal en get_stats() ---
old_grp_roi = ("    def _grp_roi(grp):\n"
               "        s = [r for r in grp if r['result'] in ('win','loss')]\n"
               "        if not s: return {'n': len(grp), 'roi': None, 'avg_clv': None}\n"
               "        p = sum(r['pnl'] or 0 for r in s)\n"
               "        st = sum(r['virtual_stake'] or 0 for r in s)\n"
               "        c = [r['clv'] for r in grp if r['clv'] is not None]\n"
               "        return {'n': len(grp), 'roi': p/st if st else None,\n"
               "                'avg_clv': sum(c)/len(c) if c else None}\n"
               "    return {")
new_grp_roi = ("    def _grp_roi(grp):\n"
               "        s = [r for r in grp if r['result'] in ('win','loss')]\n"
               "        if not s: return {'n': len(grp), 'roi': None, 'avg_clv': None}\n"
               "        p = sum(r['pnl'] or 0 for r in s)\n"
               "        st = sum(r['virtual_stake'] or 0 for r in s)\n"
               "        c = [r['clv'] for r in grp if r['clv'] is not None]\n"
               "        return {'n': len(grp), 'roi': p/st if st else None,\n"
               "                'avg_clv': sum(c)/len(c) if c else None}\n"
               "    rlm_on  = [r for r in rows if r.get('rlm_signal')]\n"
               "    rlm_off = [r for r in rows if not r.get('rlm_signal')]\n"
               "    return {")
if old_grp_roi in scode:
    scode = scode.replace(old_grp_roi, new_grp_roi, 1)
    schanges += 1
    print('OK FixS4: rlm groups en get_stats')
else:
    print('SKIP FixS4')

old_stats_return_end = ("        'fresh_line': _grp_roi(fresh),\n"
                        "        'stale_line': _grp_roi(stale),\n"
                        "    }")
new_stats_return_end = ("        'fresh_line': _grp_roi(fresh),\n"
                        "        'stale_line': _grp_roi(stale),\n"
                        "        'rlm_on':  _grp_roi(rlm_on),\n"
                        "        'rlm_off': _grp_roi(rlm_off),\n"
                        "    }")
if old_stats_return_end in scode:
    scode = scode.replace(old_stats_return_end, new_stats_return_end, 1)
    schanges += 1
    print('OK FixS4b: rlm_on/off en return stats')
else:
    print('SKIP FixS4b')

# --- FIX S5: mostrar RLM en format_telegram ---
old_tg_fresh = ("    # Fresh line vs stale comparison\n"
                "    fl = s.get('fresh_line', {})\n"
                "    sl = s.get('stale_line', {})\n"
                "    if fl.get('n', 0) > 0 or sl.get('n', 0) > 0:\n"
                "        lines.append('')\n"
                "        lines.append('*Lineas Frescas (<30 min)*')\n"
                "        def _fmt_grp(g, tag):\n"
                "            if not g.get('n'): return ''\n"
                "            roi_s = f\"{g['roi']*100:+.1f}%\" if g.get('roi') is not None else 'n/a'\n"
                "            clv_s = f\"{g['avg_clv']*100:+.1f}%\" if g.get('avg_clv') is not None else 'n/a'\n"
                "            return f'{tag}: {g[\"n\"]} picks | ROI {roi_s} | CLV {clv_s}'\n"
                "        fl_line = _fmt_grp(fl, 'Fresh')\n"
                "        sl_line = _fmt_grp(sl, 'Stale')\n"
                "        if fl_line: lines.append(fl_line)\n"
                "        if sl_line: lines.append(sl_line)\n"
                "    lines.append('')\n"
                "    lines.append('_Sibila Shadow Book_')\n"
                "    return '\\n'.join(lines)")
new_tg_fresh = ("    # Fresh line vs stale comparison\n"
                "    fl = s.get('fresh_line', {})\n"
                "    sl = s.get('stale_line', {})\n"
                "    if fl.get('n', 0) > 0 or sl.get('n', 0) > 0:\n"
                "        lines.append('')\n"
                "        lines.append('*Lineas Frescas (<30 min)*')\n"
                "        def _fmt_grp(g, tag):\n"
                "            if not g.get('n'): return ''\n"
                "            roi_s = f\"{g['roi']*100:+.1f}%\" if g.get('roi') is not None else 'n/a'\n"
                "            clv_s = f\"{g['avg_clv']*100:+.1f}%\" if g.get('avg_clv') is not None else 'n/a'\n"
                "            return f'{tag}: {g[\"n\"]} picks | ROI {roi_s} | CLV {clv_s}'\n"
                "        fl_line = _fmt_grp(fl, 'Fresh')\n"
                "        sl_line = _fmt_grp(sl, 'Stale')\n"
                "        if fl_line: lines.append(fl_line)\n"
                "        if sl_line: lines.append(sl_line)\n"
                "    # RLM comparison\n"
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
if old_tg_fresh in scode:
    scode = scode.replace(old_tg_fresh, new_tg_fresh, 1)
    schanges += 1
    print('OK FixS5: RLM en format_telegram')
else:
    print('SKIP FixS5: anchor telegram no encontrado')

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
print('  [RLM] Senal detectada: ... (aparece cuando steam/reverse detectado)')
print('  El primer scan solo registra odds (sin historial -> sin señal)')
print('  Señales aparecen desde el 2do scan en adelante')
