#!/usr/bin/env python3
"""
Cantera -- sistemas en shadow validation antes de ir live en Oraculo.
Uso: python3 cantera_status.py
"""
import sqlite3
from datetime import datetime, timedelta

DB = '/home/noc/oraculo_v2/sibila.db'
NOW = datetime.utcnow()

CANTERA = [
    {
        'name': 'WNBA',
        'query': "sport='basketball' AND (league LIKE '%wnba%' OR side LIKE '%Lynx%' OR side LIKE '%Liberty%' OR side LIKE '%Aces%' OR side LIKE '%Mercury%' OR side LIKE '%Sky%')",
        'threshold': 20,
        'note': 'ELO model -- activar live cuando WR>=55%% en 20+ picks, odds<=1.90',
    },
    {
        'name': 'NBA',
        'query': "sport='basketball' AND league LIKE '%nba%'",
        'threshold': 50,
        'note': 'ELO model -- fuera de temporada (Oct-Jun)',
    },
    {
        'name': 'NHL',
        'query': "sport='hockey'",
        'threshold': 50,
        'note': 'ELO model -- fuera de temporada (Oct-Jun)',
    },
    {
        'name': 'Soccer Corners',
        'query': "market_type='soccer_corners'",
        'threshold': None,
        'note': 'SHADOW PERMANENTE -- correlation=0.020, sin senal real',
    },
    {
        'name': 'RLM (Sharp Money)',
        'query': "market_type='rlm_signal' OR market_type='steam_move'",
        'threshold': 30,
        'note': 'Steam moves + public-reverse + multi-book consensus',
    },
    {
        'name': 'MLB Fade',
        'query': "market_type='mlb_f5_ml_fade'",
        'threshold': 20,
        'note': 'CHW/MIA/CHI/TEX/DET -- activar cuando WR>=60%% en 20+ picks',
    },
    {
        'name': 'Soccer Intl',
        'query': "sport='soccer' AND (league LIKE '%copa-america%' OR league LIKE '%nations-league%')",
        'threshold': 30,
        'note': 'Copa America + UEFA NL -- shadow hasta validar post-WC',
    },
]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print('=' * 62)
print('  CANTERA -- Shadow Validation Dashboard')
print('  %s UTC' % NOW.strftime('%Y-%m-%d %H:%M'))
print('=' * 62)

cutoff_90 = (NOW - timedelta(days=90)).isoformat()

for s in CANTERA:
    q = ("SELECT result, COUNT(*) n FROM sibila_picks "
         "WHERE (%s) AND ts >= '%s' GROUP BY result" % (s['query'], cutoff_90))
    try:
        rows = {r['result']: r['n'] for r in conn.execute(q).fetchall()}
    except Exception as e:
        rows = {}

    total = sum(rows.values())
    wins = rows.get('WIN', 0)
    pending = rows.get(None, 0) + rows.get('PENDING', 0) + rows.get('VOID', 0)
    resolved = total - pending
    wr = wins / resolved * 100 if resolved > 0 else 0
    thr = s['threshold']

    if thr is None:
        status = 'SHADOW PERMANENTE'
        progress = ''
    elif resolved == 0:
        status = 'SIN DATOS'
        progress = '(0/%d picks)' % thr
    elif resolved >= thr:
        badge = 'EVALUAR LIVE' if wr >= 55 else 'n OK / WR bajo'
        status = badge
        progress = '(%d picks WR=%.0f%%)' % (resolved, wr)
    else:
        status = 'acumulando'
        progress = '(%d/%d  WR=%.0f%%)' % (resolved, thr, wr)

    print()
    print('  %-20s  %-22s %s' % (s['name'], status, progress))
    print('  %-20s  %s' % ('', s['note']))

print()
print('=' * 62)
conn.close()
