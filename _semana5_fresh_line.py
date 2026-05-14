#!/usr/bin/env python3
"""
Semana 5: Line Opening Timing
 - Crea oraculo_fresh_line.py: detecta eventos nuevos en Cloudbet
 - Parchea runner: llama _fresh_check() despues de cada scan de deporte
 - Parchea oraculo_sibila.py: columna fresh_line + stats separadas
"""

import os
import textwrap

BASE   = '/home/noc/oraculo_v2'
RUNNER = f'{BASE}/oraculo_runner_auto.py'
SIBILA = f'{BASE}/oraculo_sibila.py'
FRESH  = f'{BASE}/oraculo_fresh_line.py'

# ============================================================
# 1. Crear oraculo_fresh_line.py
# ============================================================
FRESH_CODE = textwrap.dedent('''\
    #!/usr/bin/env python3
    """
    FreshLine — detecta lineas recien abiertas en Cloudbet.
    Primeros 30 min -> menor eficiencia del mercado -> mayor edge potencial.
    Uso:
        from oraculo_fresh_line import check_picks
        picks = check_picks(picks)   # agrega fresh_line=True/False a cada pick
    """

    import os
    import time
    import pickle
    import logging

    log = logging.getLogger("oraculo")

    _CACHE_PATH   = os.path.join(os.path.dirname(__file__), ".oraculo_cache", "known_event_ids.pkl")
    _FRESH_WINDOW = 1800  # segundos = 30 min

    # event_id -> primer timestamp visto
    _event_ts: dict = {}
    _loaded = False


    def _load():
        global _event_ts, _loaded
        try:
            if os.path.exists(_CACHE_PATH):
                with open(_CACHE_PATH, "rb") as f:
                    _event_ts = pickle.load(f)
        except Exception:
            _event_ts = {}
        _loaded = True


    def _save():
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, "wb") as f:
                pickle.dump(_event_ts, f)
        except Exception as exc:
            log.warning("[FreshLine] save failed: %s", exc)


    def check_picks(picks: list) -> list:
        """
        Agrega campo fresh_line=True a picks cuyo event_id fue
        visto por primera vez en los ultimos 30 minutos.
        Registra nuevos event_ids con timestamp actual.
        Devuelve la misma lista (modificada in-place).
        """
        global _event_ts, _loaded
        if not _loaded:
            _load()

        now       = time.time()
        new_count = 0

        for p in picks:
            eid = p.get("event_id") or p.get("match", "")
            if not eid:
                p.setdefault("fresh_line", False)
                continue

            if eid not in _event_ts:
                _event_ts[eid] = now
                new_count += 1
                p["fresh_line"] = True
            else:
                age             = now - _event_ts[eid]
                p["fresh_line"] = age < _FRESH_WINDOW

        # Limpiar eventos con mas de 7 dias
        cutoff   = now - 7 * 86400
        _event_ts = {k: v for k, v in _event_ts.items() if v > cutoff}

        fresh_count = sum(1 for p in picks if p.get("fresh_line"))
        if new_count:
            log.info("[FreshLine] %d nuevos eventos detectados (%d picks fresh)", new_count, fresh_count)
            _save()

        return picks


    def fresh_summary(picks: list) -> str:
        """Resumen de picks fresh vs stale para logging."""
        f = [p for p in picks if p.get("fresh_line")]
        s = [p for p in picks if not p.get("fresh_line")]
        if not f:
            return ""
        avg_edge_f = sum(p.get("edge", 0) for p in f) / len(f) * 100 if f else 0
        avg_edge_s = sum(p.get("edge", 0) for p in s) / len(s) * 100 if s else 0
        return (f"[FreshLine] fresh={len(f)} edge={avg_edge_f:.1f}% | "
                f"stale={len(s)} edge={avg_edge_s:.1f}%")
''')

with open(FRESH, 'w') as f:
    f.write(FRESH_CODE)
print('OK: oraculo_fresh_line.py creado')

# ============================================================
# 2. Parchear oraculo_runner_auto.py
# ============================================================
with open(RUNNER) as f:
    code = f.read()

changes = 0

# --- FIX R1: import fresh_line junto al import de sibila ---
old_import = ("try:\n"
              "    from oraculo_sibila import record_pick as _sibila_record, mark_placed as _sibila_placed, resolve_pick as _sibila_resolve, format_telegram as _sibila_fmt\n"
              "    _SIBILA_ENABLED = True\n"
              "except ImportError:\n"
              "    _SIBILA_ENABLED = False\n"
              "    def _sibila_record(*a, **kw): pass\n"
              "    def _sibila_placed(*a, **kw): pass\n"
              "    def _sibila_resolve(*a, **kw): pass\n"
              "    def _sibila_fmt(**kw): return 'Sibila no disponible'")
new_import = (old_import + "\n"
              "try:\n"
              "    from oraculo_fresh_line import check_picks as _fresh_check, fresh_summary as _fresh_summary\n"
              "    _FRESH_ENABLED = True\n"
              "except ImportError:\n"
              "    _FRESH_ENABLED = False\n"
              "    def _fresh_check(p): return p\n"
              "    def _fresh_summary(p): return ''")
if old_import in code and '_FRESH_ENABLED' not in code:
    code = code.replace(old_import, new_import, 1)
    changes += 1
    print('OK FixR1: import oraculo_fresh_line')
else:
    print('SKIP FixR1')

# --- FIX R2: fresh check para football picks ---
old_fb_sibila = ("    if _SIBILA_ENABLED:\n"
                 "        for _sp in picks:\n"
                 "            _sibila_record(_sp)\n"
                 "    return picks")
new_fb_sibila = ("    if _FRESH_ENABLED:\n"
                 "        picks = _fresh_check(picks)\n"
                 "        _fs = _fresh_summary(picks)\n"
                 "        if _fs: log.info(_fs)\n"
                 "    if _SIBILA_ENABLED:\n"
                 "        for _sp in picks:\n"
                 "            _sibila_record(_sp)\n"
                 "    return picks")
# Este anchor esta al final del bloque football (return picks)
# Solo el primero (football) tiene el return picks pattern
if old_fb_sibila in code:
    code = code.replace(old_fb_sibila, new_fb_sibila, 1)
    changes += 1
    print('OK FixR2: fresh check football picks')
else:
    print('SKIP FixR2: anchor football+sibila no encontrado')

# --- FIX R3: fresh check para tennis picks ---
# El bloque tennis NO tiene return picks, solo registra sibila
old_tn_sibila = ("    if _SIBILA_ENABLED:\n"
                 "        for _sp in picks:\n"
                 "            _sibila_record(_sp)")
new_tn_sibila = ("    if _FRESH_ENABLED:\n"
                 "        picks = _fresh_check(picks)\n"
                 "        _fs = _fresh_summary(picks)\n"
                 "        if _fs: log.info(_fs)\n"
                 "    if _SIBILA_ENABLED:\n"
                 "        for _sp in picks:\n"
                 "            _sibila_record(_sp)")
if old_tn_sibila in code:
    code = code.replace(old_tn_sibila, new_tn_sibila, 1)
    changes += 1
    print('OK FixR3: fresh check tennis picks')
else:
    print('SKIP FixR3: anchor tennis+sibila no encontrado')

# --- FIX R4: fresh check para MLB picks ---
old_mlb_sibila = ("            if _SIBILA_ENABLED:\n"
                  "                for _sp in mlb_picks:\n"
                  "                    _sibila_record(_sp)")
new_mlb_sibila = ("            if _FRESH_ENABLED:\n"
                  "                mlb_picks = _fresh_check(mlb_picks)\n"
                  "                _fs = _fresh_summary(mlb_picks)\n"
                  "                if _fs: log.info(_fs)\n"
                  "            if _SIBILA_ENABLED:\n"
                  "                for _sp in mlb_picks:\n"
                  "                    _sibila_record(_sp)")
if old_mlb_sibila in code:
    code = code.replace(old_mlb_sibila, new_mlb_sibila, 1)
    changes += 1
    print('OK FixR4: fresh check MLB picks')
else:
    print('SKIP FixR4: anchor MLB+sibila no encontrado')

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
# 3. Parchear oraculo_sibila.py: columna fresh_line + stats
# ============================================================
with open(SIBILA) as f:
    scode = f.read()

schanges = 0

# --- FIX S1: agregar columna fresh_line al schema ---
old_schema = ("            CREATE TABLE IF NOT EXISTS sibila_picks (\n"
              "                id          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
              "                ts          INTEGER NOT NULL,\n"
              "                sport       TEXT,\n"
              "                market      TEXT,\n"
              "                level       TEXT,\n"
              "                match       TEXT,\n"
              "                label       TEXT,\n"
              "                edge        REAL,\n"
              "                model_prob  REAL,\n"
              "                price       REAL,\n"
              "                virtual_stake REAL,\n"
              "                placed      INTEGER DEFAULT 0,\n"
              "                real_stake  REAL,\n"
              "                bet_id      TEXT,\n"
              "                result      TEXT,\n"
              "                pnl         REAL,\n"
              "                closing_odds REAL,\n"
              "                clv         REAL\n"
              "            )")
new_schema = ("            CREATE TABLE IF NOT EXISTS sibila_picks (\n"
              "                id          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
              "                ts          INTEGER NOT NULL,\n"
              "                sport       TEXT,\n"
              "                market      TEXT,\n"
              "                level       TEXT,\n"
              "                match       TEXT,\n"
              "                label       TEXT,\n"
              "                edge        REAL,\n"
              "                model_prob  REAL,\n"
              "                price       REAL,\n"
              "                virtual_stake REAL,\n"
              "                placed      INTEGER DEFAULT 0,\n"
              "                real_stake  REAL,\n"
              "                bet_id      TEXT,\n"
              "                result      TEXT,\n"
              "                pnl         REAL,\n"
              "                closing_odds REAL,\n"
              "                clv         REAL,\n"
              "                fresh_line  INTEGER DEFAULT 0\n"
              "            )\n"
              "            "  # keep trailing spaces consistent
              )
# Also add ALTER TABLE for existing DBs
old_schema_close = ("            CREATE TABLE IF NOT EXISTS sibila_picks (\n"
                    "                id          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                    "                ts          INTEGER NOT NULL,\n"
                    "                sport       TEXT,\n"
                    "                market      TEXT,\n"
                    "                level       TEXT,\n"
                    "                match       TEXT,\n"
                    "                label       TEXT,\n"
                    "                edge        REAL,\n"
                    "                model_prob  REAL,\n"
                    "                price       REAL,\n"
                    "                virtual_stake REAL,\n"
                    "                placed      INTEGER DEFAULT 0,\n"
                    "                real_stake  REAL,\n"
                    "                bet_id      TEXT,\n"
                    "                result      TEXT,\n"
                    "                pnl         REAL,\n"
                    "                closing_odds REAL,\n"
                    "                clv         REAL\n"
                    "            )")

# Replace schema
if old_schema_close in scode and 'fresh_line' not in scode:
    scode = scode.replace(old_schema_close,
                          old_schema_close.replace(
                              "                clv         REAL\n"
                              "            )",
                              "                clv         REAL,\n"
                              "                fresh_line  INTEGER DEFAULT 0\n"
                              "            )"), 1)
    schanges += 1
    print('OK FixS1: columna fresh_line en schema')
else:
    print('SKIP FixS1')

# --- FIX S2: ALTER TABLE para DBs existentes (en _init_db) ---
old_meta_create = ("            CREATE TABLE IF NOT EXISTS sibila_meta (\n"
                   "                key   TEXT PRIMARY KEY,\n"
                   "                value TEXT\n"
                   "            )")
new_meta_create = ("            CREATE TABLE IF NOT EXISTS sibila_meta (\n"
                   "                key   TEXT PRIMARY KEY,\n"
                   "                value TEXT\n"
                   "            )\n"
                   "            '''\n"
                   "            )\n"
                   "            # Migracion: agregar fresh_line si no existe\n"
                   "            try:\n"
                   "                cur.execute('ALTER TABLE sibila_picks ADD COLUMN fresh_line INTEGER DEFAULT 0')\n"
                   "            except Exception:\n"
                   "                pass  # ya existe")
if old_meta_create in scode and 'ALTER TABLE sibila_picks ADD COLUMN fresh_line' not in scode:
    scode = scode.replace(old_meta_create, new_meta_create, 1)
    schanges += 1
    print('OK FixS2: ALTER TABLE fresh_line migracion')
else:
    print('SKIP FixS2')

# --- FIX S3: guardar fresh_line en record_pick ---
old_insert = ("            INSERT INTO sibila_picks\n"
              "                (ts, sport, market, level, match, label, edge, model_prob,\n"
              "                 price, virtual_stake, placed, real_stake, bet_id)\n"
              "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)\n"
              "            ''',\n"
              "            (int(time.time()), sport, market, level,\n"
              "             pick.get('match',''), pick.get('label',''),\n"
              "             pick.get('edge', 0), pick.get('model_prob', 0),\n"
              "             pick.get('price', 0), vstake,\n"
              "             1 if placed else 0, real_stake, bet_id))")
new_insert = ("            INSERT INTO sibila_picks\n"
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
if old_insert in scode:
    scode = scode.replace(old_insert, new_insert, 1)
    schanges += 1
    print('OK FixS3: guardar fresh_line en INSERT')
else:
    print('SKIP FixS3: anchor INSERT no encontrado')

# --- FIX S4: stats de fresh_line en get_stats() ---
# Buscar donde se calculan stats por market para agregar fresh_line breakdown
old_stats_return = ("    return {\n"
                    "        'n': n,\n"
                    "        'n_settled': n_settled,\n"
                    "        'win_rate': win_rate,\n"
                    "        'roi': roi,\n"
                    "        'virtual_pnl': virtual_pnl,\n"
                    "        'virtual_bankroll': meta.get('bankroll', 1000.0),\n"
                    "        'avg_edge': avg_edge,\n"
                    "        'avg_clv': avg_clv,\n"
                    "        'by_sport': by_sport,\n"
                    "        'by_market': by_market,\n"
                    "        'by_level': by_level,\n"
                    "        'calibration': cal,\n"
                    "    }")
new_stats_return = ("    # Fresh line vs stale breakdown\n"
                    "    fresh = [r for r in rows if r.get('fresh_line')]\n"
                    "    stale = [r for r in rows if not r.get('fresh_line')]\n"
                    "    def _grp_roi(grp):\n"
                    "        s = [r for r in grp if r['result'] in ('win','loss')]\n"
                    "        if not s: return {'n': len(grp), 'roi': None, 'avg_clv': None}\n"
                    "        p = sum(r['pnl'] or 0 for r in s)\n"
                    "        st = sum(r['virtual_stake'] or 0 for r in s)\n"
                    "        c = [r['clv'] for r in grp if r['clv'] is not None]\n"
                    "        return {'n': len(grp), 'roi': p/st if st else None,\n"
                    "                'avg_clv': sum(c)/len(c) if c else None}\n"
                    "    return {\n"
                    "        'n': n,\n"
                    "        'n_settled': n_settled,\n"
                    "        'win_rate': win_rate,\n"
                    "        'roi': roi,\n"
                    "        'virtual_pnl': virtual_pnl,\n"
                    "        'virtual_bankroll': meta.get('bankroll', 1000.0),\n"
                    "        'avg_edge': avg_edge,\n"
                    "        'avg_clv': avg_clv,\n"
                    "        'by_sport': by_sport,\n"
                    "        'by_market': by_market,\n"
                    "        'by_level': by_level,\n"
                    "        'calibration': cal,\n"
                    "        'fresh_line': _grp_roi(fresh),\n"
                    "        'stale_line': _grp_roi(stale),\n"
                    "    }")
if old_stats_return in scode:
    scode = scode.replace(old_stats_return, new_stats_return, 1)
    schanges += 1
    print('OK FixS4: fresh_line stats en get_stats()')
else:
    print('SKIP FixS4: anchor return stats no encontrado')

# --- FIX S5: mostrar fresh_line en format_telegram ---
old_tg_end = ("    lines.append('')\n"
              "    lines.append('_Sibila Shadow Book_')\n"
              "    return '\\n'.join(lines)")
new_tg_end = ("    # Fresh line vs stale comparison\n"
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
if old_tg_end in scode:
    scode = scode.replace(old_tg_end, new_tg_end, 1)
    schanges += 1
    print('OK FixS5: fresh_line en format_telegram')
else:
    print('SKIP FixS5: anchor telegram end no encontrado')

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
print('Reinicia el runner: sudo systemctl restart oraculo')
