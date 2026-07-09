"""Passive observer for oraculo_lag_hunter's Fase 1 (see memory oraculo_lag_hunter_activation_plan).

Read-only: does NOT touch lag_hunter.py, does NOT place bets, does NOT modify
any existing file or DB. Logs whether soccer.match_odds is SELECTION_ENABLED
for each event in the same 9 leagues lag_hunter monitors, alongside hours-to-
kickoff, into its own new table (cb_market_timing.db). Goal: find the REAL
window in which Cloudbet enables this market for these specific leagues,
since lag_hunter's WINDOW_H=48h assumption ("CB abre R16 markets 2-3 dias
antes") does not match what was observed on 2026-07-06 (near-kickoff events
1-26h out were SELECTION_DISABLED; a 865h-out Copa Libertadores event was
enabled).
"""
import os, sqlite3, logging
from datetime import datetime

import oraculo_lag_finder as LF
import oraculo_lag_hunter as H

log = logging.getLogger('cb_market_timing')
DB_PATH = os.path.join(os.path.dirname(__file__), 'cb_market_timing.db')


def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at    TEXT NOT NULL,
            league_slug   TEXT,
            event_id      TEXT,
            home          TEXT,
            away          TEXT,
            kickoff_time  TEXT,
            hours_to_ko   REAL,
            enabled       INTEGER
        )
    """)
    conn.commit()
    return conn


def observe():
    conn = ensure_db()
    now = datetime.utcnow()
    n_checked = 0
    for _sport, slug, _lab, _tier in LF.LEAGUES:
        try:
            evs = LF.fetch_cb_events(slug)
        except Exception as e:
            log.warning('fetch_cb_events failed for %s: %s', slug, e)
            continue
        for e in evs:
            hrs = (e['time'] - now).total_seconds() / 3600
            try:
                cpr = H.cb_odds_urls(e['id'])
            except Exception as ex:
                log.debug('cb_odds_urls failed for %s: %s', e['id'], ex)
                cpr = None
            conn.execute(
                "INSERT INTO observations (checked_at, league_slug, event_id, home, away, "
                "kickoff_time, hours_to_ko, enabled) VALUES (?,?,?,?,?,?,?,?)",
                (now.isoformat(), slug, e['id'], e['home'], e['away'],
                 e['time'].isoformat(), round(hrs, 2), 1 if cpr else 0)
            )
            n_checked += 1
    conn.commit()
    conn.close()
    log.info('cb_market_timing observer: %d events checked', n_checked)
    print('cb_market_timing observer: %d events checked' % n_checked)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [cb_market_timing] %(message)s')
    observe()
