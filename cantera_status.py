#!/usr/bin/env python3
"""
Cantera -- sistemas en shadow validation antes de ir live en Oraculo.
Uso: python3 cantera_status.py
"""
import sqlite3
from datetime import datetime, timezone, timedelta

DB = '/home/noc/oraculo_v2/sibila.db'
NOW = datetime.now(timezone.utc)

CANTERA = [
    {
        'name': 'WNBA',
        'query': "sport='basketball' AND (league LIKE '%wnba%' OR side LIKE '%Lynx%' OR side LIKE '%Liberty%' OR side LIKE '%Aces%' OR side LIKE '%Mercury%' OR side LIKE '%Sky%')",
        'threshold': 20,
        'note': 'ELO model -- activar live cuando WR>=55%% en 20+ picks, odds<=1.90',
        'days': 90,
    },
    {
        'name': 'BTTS high-xG (WC)',
        'query': "market_type='btts_highxg'",
        'threshold': 40,
        'note': 'Backtest knockout BTTS=56%% (+EV si odds>=1.80). Dispara en octavos WC (~26-jun, ambos xG>=1.4).',
        'days': 90,
    },
    {
        'name': 'sets_under grass/hard',
        'query': "market_type='sets_under' AND COALESCE(surface,'') != 'clay'",
        'threshold': 20,
        'note': 'Solo grass/hard (CLAY = desastre confirmado 0/10). Activar live WR>=60%% en 20+.',
        'days': 90,
    },
    {
        'name': 'NBA',
        'query': "sport='basketball' AND league LIKE '%nba%' AND league NOT LIKE '%wnba%'",
        'threshold': 50,
        'note': 'ELO model -- fuera de temporada hasta Oct',
        'days': 90,
    },
    {
        'name': 'NHL',
        'query': "sport='hockey'",
        'threshold': 50,
        'note': 'ELO model -- fuera de temporada hasta Oct',
        'days': 90,
    },
    {
        'name': 'Soccer Corners',
        'query': "market_type='soccer_corners'",
        'threshold': None,
        'note': 'SHADOW PERMANENTE -- correlation=0.020, sin senal real',
        'days': 90,
    },
    {
        'name': 'RLM (Sharp Money)',
        'query': "market_type IN ('rlm_signal','steam_move')",
        'threshold': 30,
        'note': 'Steam moves + public-reverse + multi-book consensus',
        'days': 90,
    },
    {
        'name': 'Soccer Intl (MLS)',
        'query': "sport='soccer' AND placed=0 AND (league LIKE '%mls%' OR league LIKE '%copa-america%' OR league LIKE '%nations-league%' OR league LIKE '%conmebol%' OR league LIKE '%concacaf%')",
        'threshold': 30,
        'note': 'MLS + Copa + UEFA NL -- shadow hasta N>=30 WR>=60%%',
        'days': 180,
    },
    {
        'name': 'MLB F5 Shadow',
        'query': "market_type='mlb_f5_ml' AND placed=0",
        'threshold': 50,
        'killed': True,
        'note': 'MATADO 2026-06-18 por CLV -- shadow WR 56%% es ARTEFACTO; live real WR 24-33%%, CLV -0.325, -$87. NO REACTIVAR.',
        'days': 90,
    },
]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print('=' * 62)
print('  CANTERA -- Shadow Validation Dashboard')
print('  %s UTC' % NOW.strftime('%Y-%m-%d %H:%M'))
print('=' * 62)

for s in CANTERA:
    cutoff = (NOW - timedelta(days=s['days'])).strftime('%Y-%m-%d %H:%M:%S')
    q = ("SELECT result, COUNT(*) n FROM sibila_picks "
         "WHERE (%s) AND ts >= '%s' GROUP BY result" % (s['query'], cutoff))
    try:
        rows = {r['result']: r['n'] for r in conn.execute(q).fetchall()}
    except Exception as e:
        rows = {}

    total = sum(rows.values())
    wins = rows.get('WIN', 0)
    voids = rows.get('VOID', 0)
    resolved = total - rows.get(None, 0) - voids
    wr = wins / resolved * 100 if resolved > 0 else 0
    thr = s['threshold']

    if s.get('killed'):
        status = 'XX MATADO (CLV) -- NO REACTIVAR'
        progress = '(shadow %d WR=%.0f%% = ARTEFACTO)' % (resolved, wr) if resolved else ''
    elif thr is None:
        status = 'SHADOW PERMANENTE'
        progress = ''
    elif resolved == 0:
        status = 'SIN DATOS'
        progress = '(0/%d picks)' % thr
    elif resolved >= thr:
        badge = 'EVALUAR LIVE' if wr >= 55 else 'WR insuf (%.0f%%)' % wr
        status = badge
        progress = '(%d picks WR=%.0f%%)' % (resolved, wr)
    else:
        status = 'acumulando'
        progress = '(%d/%d  WR=%.0f%%)' % (resolved, thr, wr)

    print()
    print('  %-22s  %-30s %s' % (s['name'], status, progress))
    print('  %-22s  %s' % ('', s['note']))

print()
print('=' * 62)
conn.close()
