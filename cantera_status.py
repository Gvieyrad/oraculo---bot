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
        'name': 'NFL',
        'query': "market_type='nfl_ml'",
        'threshold': 20,
        'note': '2026-08-04: Fase 1 expansion deportes. ELO con home advantage, fuente nflverse (ESPN bloqueado a nivel WAF/Akamai para este endpoint). Temporada sept-feb, sin muestra hasta entonces.',
        'days': 180,
    },
    {
        'name': 'EuroLeague (basketball)',
        'query': "market_type='euroleague_ml'",
        'threshold': 20,
        'note': '2026-08-04: Fase 3 expansion deportes. ELO con home advantage + margin-of-victory, fuente API oficial euroleague.net (20 equipos). Temporada oct-mayo, en pausa (solo outrights en Cloudbet ahora).',
        'days': 180,
    },
    {
        'name': 'Rugby Premiership (Inglaterra)',
        'query': "market_type='rugby_premiership_ml'",
        'threshold': 20,
        'note': '2026-08-04: Fase 2 expansion deportes. ELO via transientlunatic/Rugby-Data (octonion/rugby parado desde 2020-21 para esta liga, descartado). 10 equipos, 50 partidos historicos.',
        'days': 180,
    },
    {
        'name': 'Rugby URC (Celtic/Pro14)',
        'query': "market_type='rugby_urc_ml'",
        'threshold': 20,
        'note': '2026-08-04: Fase 2 expansion deportes. Misma fuente que Premiership -- ya incluye equipos sudafricanos (DHL Stormers, Vodacom Bulls) a diferencia de octonion/rugby. 16 equipos, 84 partidos.',
        'days': 180,
    },
    {
        'name': 'Rugby NPC/Mitre 10 Cup (Nueva Zelanda)',
        'query': "market_type='rugby_npc_ml'",
        'threshold': 20,
        'note': '2026-08-04: Fase 2 expansion deportes. 14 equipos, 70 partidos historicos.',
        'days': 180,
    },
    {
        'name': 'Cricket ODI internacionales',
        'query': "market_type='cricket_odi'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4a expansion deportes. ELO binario, home_adv=30, sin factor de toss (medido: 51.6%% WR con toss ganado, practicamente coinflip). Fuente cricsheet.org, 27 equipos/~2436 partidos.',
        'days': 180,
    },
    {
        'name': 'Cricket Test internacionales',
        'query': "market_type='cricket_test'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4a expansion deportes. Modelo de 3 resultados (home/away/draw, P_DRAW_TEST=0.195 fijo). Fix 2026-08-04: market key corregido a cricket.test_1x2 (cricket.winner no existe para Test, daba 0 picks siempre). 12 naciones/911 partidos.',
        'days': 180,
    },
    {
        'name': 'Cricket T20 IPL',
        'query': "market_type='cricket_t20_ipl'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4b expansion deportes. Reusa Elo binario de ODI, home_adv=0 (cancha neutral/rotativa). 10 franquicias.',
        'days': 180,
    },
    {
        'name': 'Cricket T20 Lanka Premier League',
        'query': "market_type='cricket_t20_lpl'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4b expansion deportes. Mapeo Colombo CC/Galle CC inferido por eliminacion (no confirmado 1:1 con partido real de esos equipos especificos) -- revisar cuando haya mas muestra.',
        'days': 180,
    },
    {
        'name': 'Cricket T20 Caribbean Premier League',
        'query': "market_type='cricket_t20_cpl'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4b expansion deportes. Mapeo best-effort (franquicias con historial de rebrand). 7 equipos.',
        'days': 180,
    },
    {
        'name': 'Cricket T20 Vitality Blast (Inglaterra)',
        'query': "market_type='cricket_t20_ntb'",
        'threshold': 20,
        'note': '2026-08-04: Fase 4b expansion deportes. 18 condados ingleses.',
        'days': 180,
    },
    {
        'name': 'Asian Handicap Liga MX (Poisson dedicado)',
        'query': "market_type='asian_handicap' AND league='LMX'",
        'threshold': 30,
        'note': '2026-08-03: Poisson dedicado (football-data.co.uk MEX.csv, 2022-2027). Backtest walk-forward salio mal (33.3%% hit-rate vs 52.4%% breakeven) pero el test cayo justo en las primeras 4 jornadas del Apertura nuevo (ratings heredados de temporada pasada, roster changes de mercado de pases) -- sesgo conocido, no descalifica el modelo. Shadow-only, revalidar con partidos de mitad de temporada.',
        'days': 60,
    },
    {
        'name': 'Asian Handicap ARG/BRA (Poisson dedicado)',
        'query': "market_type='asian_handicap' AND league IN ('ARG','BRA')",
        'threshold': 30,
        'note': '2026-08-03: Poisson separado entrenado con football-data.co.uk (2022-2026), no mezclado con modelo global 5 grandes ligas. Backtest walk-forward: ARG accuracy 41.9% vs 41.1% baseline home-win (senal debil), BRA 51.3% vs 49.6% (senal moderada). Shadow-only hasta validar con datos reales.',
        'days': 60,
    },
    {
        'name': 'Goals 2H Under — ligas BAJO scoring (debe ganar)',
        'query': "market_type='soccer_goals' AND league IN ('soccer-france-ligue-2','soccer-england-championship','soccer-italy-serie-a','soccer-portugal-primeira-liga','soccer-belgium-first-division-a','soccer-spain-laliga')",
        'threshold': 30,
        'note': 'League-aware: 2H Under en ligas bajo-scoring (2H-U1.5 56-59%, football-data). Hipotesis: gana mas que en ligas altas. Valida en agosto.',
        'days': 120,
    },
    {
        'name': 'Goals 2H Under — ligas ALTO scoring (control, deberia perder)',
        'query': "market_type='soccer_goals' AND league IN ('soccer-germany-bundesliga','soccer-netherlands-eredivisie','soccer-germany-2-bundesliga','soccer-england-premier-league','soccer-turkey-super-lig')",
        'threshold': 30,
        'note': 'Control: 2H Under en ligas alto-scoring (Bundesliga 3.19 gol/part). Si pierde vs bajo-scoring -> filtro league-aware validado.',
        'days': 120,
    },
    {
        'name': 'Under 2.5 intl/WC',
        'query': "market_type='under25_cantera'",
        'threshold': 30,
        'note': 'Backtest 5y intl OOS n=1106: raw p_u25>=0.60 WR 56.7pct, +EV solo odds>=1.80. CLV forward valida; calibrador propio con esta data.',
        'days': 60,
    },
    {
        'name': 'Rugby MLR (union US)',
        'query': "market_type='rugby_mlr_ml'",
        'threshold': 30,
        'note': 'ELO MLR (backtest 66.0%% acc OOS > NRL; liga blanda US). Shadow hasta CLV+. FUERA DE TEMPORADA (confirmado 2026-08-02: 0 eventos en Cloudbet) -- vuelve ~marzo 2027.',
        'days': 90,
    },
    {
        'name': 'Rugby NRL',
        'query': "market_type='rugby_ml'",
        'threshold': 30,
        'live': True,  # 2026-08-01: promovida a vivo por decision explicita del usuario
        'note': 'ELO NRL (backtest 63.8%% acc out-of-sample, calibrado). LIVE desde 2026-08-01, stake fijo $1 (bajo el umbral formal de 30, arrancado a pedido con 26/30 WR=58%% shadow).',
        'days': 90,
    },
    {
        'name': 'WNBA',
        'query': "sport='basketball' AND (league LIKE '%wnba%' OR side LIKE '%Lynx%' OR side LIKE '%Liberty%' OR side LIKE '%Aces%' OR side LIKE '%Mercury%' OR side LIKE '%Sky%')",
        'threshold': 20,
        'live': True,  # 2026-07-06: promovida a vivo (shadow=False en scan_wnba)
        'note': 'ELO model -- LIVE desde 2026-07-06, stake $1, odds<=1.90',
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
        'live': True,  # 2026-07-05: promovida a vivo (Challenger sigue bloqueado, ver filtro en oraculo_runner_auto.py)
        'note': 'Solo grass/hard (CLAY = desastre confirmado 0/10; Challenger tambien bloqueado). LIVE desde 2026-07-05.',
        'days': 90,
    },
    {
        'name': 'sets_under Challenger',
        'query': "market_type='sets_under' AND league LIKE '%challenger%'",
        'threshold': 20,
        'note': '2026-07-31: TennisExplorer fallback destrabo la resolucion (105->6 sin resolver). Bloqueado en vivo hasta juntar 20+ picks limpios.',
        'days': 90,
    },
    {
        'name': 'MMA (UFC)',
        'query': "market_type='mma_winner'",
        'threshold': 20,
        'note': 'ELO model (ESPN results, 901 fighters). Activar live cuando WR>=55%% en 20+ picks, odds<=3.50.',
        'days': 90,
    },
    {
        'name': 'Tennis Total Games',
        'query': "market_type='tennis_total_games'",
        'threshold': 40,
        'killed': True,
        'note': 'DESCARTADO 2026-06-19 backtest: no predictivo (corr=0.04, dir 48.7%%, bias +1.7). No perseguir.',
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
    {
        'name': 'MLB F5 Total (Under 4.5)',
        'query': "market_type='mlb_f5_total'",
        'threshold': 20,
        'live': True,  # 2026-07-31: desbloqueado (bug de strip incondicional que anulaba el filtro)
        'note': 'F5 Total Under 4.5 (WR=67%% n=246 Sibila, validado 2026-06-03). Bloqueado desde entonces por un bug de codigo (strip incondicional de under); arreglado 2026-07-31. LIVE, stake $8 cap.',
        'days': 90,
    },
    {
        'name': 'Soccer ML (5 grandes ligas: over15/over25/under35)',
        'query': "market_type IN ('over15','over25','under35') AND league IN ('PL','PD','SA','BL1','FL1')",
        'threshold': 30,
        'note': 'Modelo ML (MarketPredictor.predict_all + build_feature_vector) estaba roto para TODAS las ligas (mp.predict_match no existia) -- arreglado 2026-08-01. Shadow hasta acumular muestra; sin partidos hasta que arranquen las 5 grandes ligas (~10-ago).',
        'days': 30,
    },
    {
        'name': 'Soccer Goles Sudamerica (ARG/BRA over15/under35)',
        'query': "market_type IN ('over15','under35') AND league IN ('ARG','BRA')",
        'threshold': 20,
        'note': 'Fallback de tasa historica de goles (sin corners/tarjetas, football-data.co.uk). Backtest walk-forward: over1.5/over3.5 calibran mejor que coinflip; over2.5 NO se usa (sin señal real). Sin cuotas de mercado en la fuente -- ROI real solo validable en shadow contra Cloudbet.',
        'days': 30,
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
    elif s.get('live'):
        status = 'LIVE'
        progress = '(%d picks WR=%.0f%%)' % (resolved, wr)
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
