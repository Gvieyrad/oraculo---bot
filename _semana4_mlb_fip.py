#!/usr/bin/env python3
"""
Semana 4: MLB model upgrade con FIP (Fielding Independent Pitching).

FIP = (13*HR + 3*(BB+HBP) - 2*K) / IP + 3.10
- Elimina ruido de defensa que tiene ERA
- Mejor predictor de rendimiento futuro del lanzador
- Impacto esperado: +5-8% ROI en baseball

Cambios:
  1. Agregar _FIP_CONSTANT y _calc_fip() helper
  2. load_from_api() — fetch HR, BB, HBP, K para calcular FIP
  3. get_quality() — usar FIP en lugar de ERA (60% weight)
  4. scan_mlb() — usar FIP para pitcher_adj (4% por 1.0 FIP)
  5. Totals model — thresholds basados en FIP
  6. Log pitcher FIP en cada pick
"""

MLB = '/home/noc/oraculo_v2/oraculo_mlb.py'

with open(MLB) as f:
    code = f.read()

changes = 0

# ─────────────────────────────────────────────────────────────────────────
# FIX 1: FIP constant + helper despues de MLB_CACHE
# ─────────────────────────────────────────────────────────────────────────
old1 = 'MLB_CACHE = os.path.join(SCRIPT_DIR, \'.oraculo_cache\', \'mlb_teams.json\')\n\nMLB_API'
new1 = ('MLB_CACHE = os.path.join(SCRIPT_DIR, \'.oraculo_cache\', \'mlb_teams.json\')\n\n'
        '# FIP constant 2024-25 (scales FIP to ERA scale for comparison)\n'
        '_FIP_CONSTANT = 3.10\n'
        '_FIP_SCALE_LO = 2.0   # elite pitcher FIP\n'
        '_FIP_SCALE_HI = 5.5   # bad pitcher FIP\n'
        '\n'
        'def _calc_fip(hr, bb, hbp, k, ip):\n'
        '    """Fielding Independent Pitching -- removes defense/luck.\n'
        '    Better predictor of future performance than ERA.\n'
        '    Scale: <3.0 elite  3.0-3.5 great  3.5-4.2 avg  >5.0 bad\n'
        '    """\n'
        '    if not ip or ip < 1:\n'
        '        return 4.50  # league average default\n'
        '    return round((13 * hr + 3 * (bb + hbp) - 2 * k) / ip + _FIP_CONSTANT, 3)\n'
        '\n'
        'MLB_API')
if old1 in code:
    code = code.replace(old1, new1, 1)
    changes += 1
    print('OK Fix1: _calc_fip() added')
else:
    print('SKIP Fix1: anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 2: load_from_api() — fetch HR, BB, HBP, K + compute FIP
# ─────────────────────────────────────────────────────────────────────────
old2 = ("                            self.pitchers[pid].update({\n"
        "                                'era': float(s.get('era', 99)),\n"
        "                                'whip': float(s.get('whip', 9)),\n"
        "                                'k9': float(s.get('strikeoutsPer9Inn', 0)),\n"
        "                                'innings': float(s.get('inningsPitched', 0)),\n"
        "                                'games': int(s.get('gamesPlayed', 0)),\n"
        "                                'wins': int(s.get('wins', 0)),\n"
        "                                'losses': int(s.get('losses', 0)),\n"
        "                            })")
new2 = ("                            _ip  = float(s.get('inningsPitched', 0) or 0)\n"
        "                            _hr  = int(s.get('homeRuns', 0) or 0)\n"
        "                            _bb  = int(s.get('baseOnBalls', 0) or 0)\n"
        "                            _hbp = int(s.get('hitBatsmen', 0) or 0)\n"
        "                            _k   = int(s.get('strikeOuts', 0) or 0)\n"
        "                            _fip = _calc_fip(_hr, _bb, _hbp, _k, _ip)\n"
        "                            self.pitchers[pid].update({\n"
        "                                'era':    float(s.get('era', 99) or 99),\n"
        "                                'whip':   float(s.get('whip', 9) or 9),\n"
        "                                'k9':     float(s.get('strikeoutsPer9Inn', 0) or 0),\n"
        "                                'innings': _ip,\n"
        "                                'games':  int(s.get('gamesPlayed', 0) or 0),\n"
        "                                'wins':   int(s.get('wins', 0) or 0),\n"
        "                                'losses': int(s.get('losses', 0) or 0),\n"
        "                                'hr': _hr, 'bb': _bb, 'hbp': _hbp, 'k': _k,\n"
        "                                'fip': _fip,\n"
        "                            })")
if old2 in code:
    code = code.replace(old2, new2, 1)
    changes += 1
    print('OK Fix2: FIP calculated in load_from_api()')
else:
    print('SKIP Fix2: stats update anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 3: get_quality() — usar FIP en lugar de ERA
# ─────────────────────────────────────────────────────────────────────────
old3 = ("        if innings < 5:\n"
        "            return 0.5  # Unknown pitcher = average\n"
        "\n"
        "        # Normalize: ERA 2.0=excellent, 5.0=bad\n"
        "        era_score = max(0, min(1, (5.0 - era) / 3.0))\n"
        "        # WHIP: 0.9=excellent, 1.5=bad\n"
        "        whip_score = max(0, min(1, (1.5 - whip) / 0.6))\n"
        "        # K/9: 12=excellent, 5=bad\n"
        "        k9_score = max(0, min(1, (k9 - 5.0) / 7.0))\n"
        "\n"
        "        return era_score * 0.45 + whip_score * 0.35 + k9_score * 0.20")
new3 = ("        if innings < 5:\n"
        "            return 0.5  # Unknown pitcher = average\n"
        "\n"
        "        # Use FIP when available (better than ERA — removes defense noise)\n"
        "        fip = p.get('fip', era)  # fallback to ERA if FIP not computed\n"
        "\n"
        "        # FIP: 2.0=elite, 5.5=bad  (60% weight — primary metric)\n"
        "        fip_score  = max(0, min(1, (_FIP_SCALE_HI - fip) / (_FIP_SCALE_HI - _FIP_SCALE_LO)))\n"
        "        # WHIP: 0.9=excellent, 1.5=bad  (20% weight)\n"
        "        whip_score = max(0, min(1, (1.5 - whip) / 0.6))\n"
        "        # K/9: 12=excellent, 5=bad  (20% weight)\n"
        "        k9_score   = max(0, min(1, (k9 - 5.0) / 7.0))\n"
        "\n"
        "        return fip_score * 0.60 + whip_score * 0.20 + k9_score * 0.20")
if old3 in code:
    code = code.replace(old3, new3, 1)
    changes += 1
    print('OK Fix3: get_quality() uses FIP (60% weight)')
else:
    print('SKIP Fix3: get_quality anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 4: scan_mlb() — FIP-based pitcher_adj en lugar de ERA-based
# ─────────────────────────────────────────────────────────────────────────
old4 = ("                hp = mlb_game.get('teams', {}).get('home', {}).get('probablePitcher', {})\n"
        "                ap = mlb_game.get('teams', {}).get('away', {}).get('probablePitcher', {})\n"
        "                hp_era = _safe_era(hp.get('era'))\n"
        "                ap_era = _safe_era(ap.get('era'))\n"
        "                # Better home pitcher = boost home prob, better away pitcher = boost away\n"
        "                era_diff = (ap_era - hp_era) / 10.0  # Normalize\n"
        "                pitcher_adj = era_diff * 0.08  # Max ~3% adjustment\n"
        "                break")
new4 = ("                hp = mlb_game.get('teams', {}).get('home', {}).get('probablePitcher', {})\n"
        "                ap = mlb_game.get('teams', {}).get('away', {}).get('probablePitcher', {})\n"
        "                hp_era = _safe_era(hp.get('era'))\n"
        "                ap_era = _safe_era(ap.get('era'))\n"
        "                # FIP lookup from PitcherRating (more accurate than ERA from schedule)\n"
        "                hp_pid = hp.get('id')\n"
        "                ap_pid = ap.get('id')\n"
        "                if hp_pid and ap_pid and hasattr(scan_mlb, '_pitcher_rating'):\n"
        "                    _pr = scan_mlb._pitcher_rating\n"
        "                    hp_fip = _pr.pitchers.get(hp_pid, {}).get('fip', hp_era)\n"
        "                    ap_fip = _pr.pitchers.get(ap_pid, {}).get('fip', ap_era)\n"
        "                    hp_name = _pr.pitchers.get(hp_pid, {}).get('name', hp.get('fullName', '?'))\n"
        "                    ap_name = _pr.pitchers.get(ap_pid, {}).get('name', ap.get('fullName', '?'))\n"
        "                else:\n"
        "                    hp_fip, ap_fip = hp_era, ap_era\n"
        "                    hp_name = hp.get('fullName', '?')\n"
        "                    ap_name = ap.get('fullName', '?')\n"
        "                # FIP diff: research shows ~4% win prob per 1.0 FIP unit\n"
        "                fip_diff = ap_fip - hp_fip  # positive = away pitcher worse\n"
        "                pitcher_adj = fip_diff * 0.04\n"
        "                pitcher_adj = max(-0.08, min(0.08, pitcher_adj))  # cap at +/-8%\n"
        "                log.debug('  MLB pitchers: %s FIP=%.2f vs %s FIP=%.2f adj=%+.1f%%',\n"
        "                          hp_name, hp_fip, ap_name, ap_fip, pitcher_adj * 100)\n"
        "                break")
if old4 in code:
    code = code.replace(old4, new4, 1)
    changes += 1
    print('OK Fix4: FIP-based pitcher_adj in scan_mlb()')
else:
    print('SKIP Fix4: pitcher_adj anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 5: Totals model — usar FIP en lugar de ERA
# ─────────────────────────────────────────────────────────────────────────
old5 = ("                # Totals model: pitchers + park factor\n"
        "                avg_era = (_safe_era(hp.get('era')) + _safe_era(ap.get('era'))) / 2\n"
        "                # Park-adjusted ERA threshold: hitter parks lower the bar for \"over\"\n"
        "                over_threshold = 4.8 / park_factor\n"
        "                under_threshold = 3.8 / park_factor\n"
        "\n"
        "                if outcome == 'over' and avg_era > over_threshold:\n"
        "                    prob = 0.55 + (avg_era - over_threshold) * 0.05\n"
        "                elif outcome == 'under' and avg_era < under_threshold:\n"
        "                    prob = 0.55 + (under_threshold - avg_era) * 0.05\n"
        "                else:\n"
        "                    continue")
new5 = ("                # Totals model: FIP-based (better than ERA for run prediction)\n"
        "                avg_fip = (hp_fip + ap_fip) / 2\n"
        "                # Park-adjusted FIP thresholds\n"
        "                over_threshold  = 4.6 / park_factor  # bad pitchers -> more runs -> over\n"
        "                under_threshold = 3.6 / park_factor  # elite pitchers -> fewer runs -> under\n"
        "\n"
        "                if outcome == 'over' and avg_fip > over_threshold:\n"
        "                    prob = 0.55 + (avg_fip - over_threshold) * 0.06\n"
        "                elif outcome == 'under' and avg_fip < under_threshold:\n"
        "                    prob = 0.55 + (under_threshold - avg_fip) * 0.06\n"
        "                else:\n"
        "                    continue")
if old5 in code:
    code = code.replace(old5, new5, 1)
    changes += 1
    print('OK Fix5: Totals model uses FIP thresholds')
else:
    print('SKIP Fix5: totals anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 6: Cargar PitcherRating en train_mlb_elo() y guardarlo en scan_mlb
# ─────────────────────────────────────────────────────────────────────────
old6 = ("    log.info('MLB Elo trained: %d teams from %d games', len(elo.ratings), total_games)\n"
        "\n"
        "        # Cache\n"
        "        os.makedirs(os.path.dirname(MLB_CACHE), exist_ok=True)")
new6 = ("    log.info('MLB Elo trained: %d teams from %d games', len(elo.ratings), total_games)\n"
        "\n"
        "        # Load pitcher FIP stats and attach to scan_mlb\n"
        "        try:\n"
        "            _pr = PitcherRating()\n"
        "            _pr.load_from_api()\n"
        "            scan_mlb._pitcher_rating = _pr\n"
        "            fip_count = sum(1 for p in _pr.pitchers.values() if p.get('fip'))\n"
        "            log.info('MLB: %d pitchers loaded (%d with FIP)', len(_pr.pitchers), fip_count)\n"
        "        except Exception as _ep:\n"
        "            log.debug('MLB pitcher load skipped: %s', _ep)\n"
        "\n"
        "        # Cache\n"
        "        os.makedirs(os.path.dirname(MLB_CACHE), exist_ok=True)")
if old6 in code:
    code = code.replace(old6, new6, 1)
    changes += 1
    print('OK Fix6: PitcherRating loaded with FIP in train_mlb_elo()')
else:
    print('SKIP Fix6: train anchor not found')

# ─────────────────────────────────────────────────────────────────────────
# FIX 7: Log FIP en picks de MLB
# ─────────────────────────────────────────────────────────────────────────
old7 = ("                if edge > min_edge and prob > min_conf and edge < 0.40:\n"
        "                    picks.append({\n"
        "                        'match': f'{cb_home} vs {cb_away}',\n"
        "                        'league': 'MLB',\n"
        "                        'event_id': eid,\n"
        "                        'market_url': murl,\n"
        "                        'price': price,\n"
        "                        'label': f'F5 ML: {team}',")
new7 = ("                if edge > min_edge and prob > min_conf and edge < 0.40:\n"
        "                    picks.append({\n"
        "                        'match': f'{cb_home} vs {cb_away}',\n"
        "                        'league': 'MLB',\n"
        "                        'event_id': eid,\n"
        "                        'market_url': murl,\n"
        "                        'price': price,\n"
        "                        'label': f'F5 ML: {team} (FIP {hp_fip:.2f}/{ap_fip:.2f})',")
if old7 in code:
    code = code.replace(old7, new7, 1)
    changes += 1
    print('OK Fix7: FIP shown in pick labels')
else:
    print('SKIP Fix7')

# ─────────────────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────────────────
with open(MLB, 'w') as f:
    f.write(code)

if changes > 0:
    try:
        compile(code, MLB, 'exec')
        print('\nTotal changes: %d | SYNTAX OK' % changes)
    except SyntaxError as e:
        print('SYNTAX ERROR:', e)
else:
    print('No changes made')
