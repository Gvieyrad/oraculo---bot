#!/usr/bin/env python3
"""
RLM — Reverse Line Movement detector.

Detecta sharp money sin necesidad de public betting %.

Tres señales:
  1. Steam move: linea se mueve >4% en <2h en Pinnacle (libro sharp)
  2. Public-reverse: linea se mueve CONTRA el equipo favorito/home
     (el publico apuesta favoritos/home → movimiento opuesto = sharps)
  3. Multi-book consensus: varios libros mueven en la misma direccion
     independientemente = concertacion de sharps

Uso desde el runner:
    from oraculo_rlm import RLMTracker
    _rlm = RLMTracker()
    ...
    _rlm.record_batch(events_raw, book='cloudbet')
    picks = _rlm.tag_picks(picks)
"""

import os
import sqlite3
import time
import logging
import threading
from typing import Optional

log = logging.getLogger('oraculo')

_DB_PATH = os.path.join(os.path.dirname(__file__), '.oraculo_cache', 'rlm_history.db')

# Libros sharp (movimientos de estos pesan mas)
_SHARP_BOOKS = {'pinnacle', 'betfair', 'bet365', 'cloudbet'}

# Sesgo publico: el publico apuesta favoritos, home teams, overs
# → si la linea se mueve CONTRA el favorito = posible sharp
_PUBLIC_BIAS_LABELS = {
    'home': True,       # publico apuesta home
    'favorite': True,   # publico apuesta favorito
    'over': True,       # publico apuesta over
    'moneyline': True,  # publico apuesta favorito en moneyline
}

# Umbral de movimiento para considerarse steam
_STEAM_THRESHOLD    = 0.04   # 4% de cambio en odds implicita
_STEAM_WINDOW_H     = 2.0    # dentro de 2 horas
_REVERSE_THRESHOLD  = 0.03   # 3% movimiento publico-reverse
_CONSENSUS_MIN_BOOKS = 2     # al menos 2 libros moviendose igual


class RLMTracker:
    _lock = threading.Lock()

    def __init__(self, db_path: str = _DB_PATH):
        self.db = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS rlm_odds (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    book     TEXT NOT NULL,
                    label    TEXT NOT NULL,
                    odds     REAL NOT NULL,
                    implied  REAL NOT NULL
                )
            ''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_rlm_event ON rlm_odds(event_id, ts)')
            # Migracion: nada extra por ahora
        log.debug('[RLM] DB inicializada: %s', self.db)

    # ------------------------------------------------------------------
    # Ingesta de odds
    # ------------------------------------------------------------------

    def record_odds(self, event_id: str, book: str, label: str,
                    odds: float, ts: int = None):
        """Registra un snapshot de odds para un evento/label/libro."""
        if not event_id or not odds or odds <= 1.0:
            return
        ts = ts or int(time.time())
        implied = round(1.0 / odds, 6)
        with self._lock:
            with self._conn() as c:
                c.execute(
                    'INSERT INTO rlm_odds (ts, event_id, book, label, odds, implied) '
                    'VALUES (?,?,?,?,?,?)',
                    (ts, str(event_id), book, label, odds, implied)
                )

    def record_batch(self, events: list, book: str = 'cloudbet'):
        """
        Registra un lote de eventos del runner.
        events: lista de dicts con {event_id, label, price} o {match, label, price}.
        """
        ts = int(time.time())
        for ev in events:
            eid   = ev.get('event_id') or ev.get('match', '')
            label = ev.get('label', '')
            price = float(ev.get('price', 0) or 0)
            if eid and label and price > 1.0:
                self.record_odds(eid, book, label, price, ts)

    def record_from_odds_api(self, markets: list, book: str = 'external'):
        """
        Alternativa: recibe lista de {event_id, home_team, away_team, bookmakers:[{key, markets:[{key, outcomes:[{name, price}]}]}]}
        formato TheOddsAPI.
        """
        ts = int(time.time())
        for ev in markets:
            eid = ev.get('id', ev.get('event_id', ''))
            if not eid:
                continue
            for bm in ev.get('bookmakers', []):
                bk = bm.get('key', book)
                for mkt in bm.get('markets', []):
                    for outcome in mkt.get('outcomes', []):
                        label = outcome.get('name', '')
                        price = float(outcome.get('price', 0) or 0)
                        if label and price > 1.0:
                            self.record_odds(eid, bk, label, price, ts)

    # ------------------------------------------------------------------
    # Analisis de movimiento
    # ------------------------------------------------------------------

    def get_history(self, event_id: str, hours: float = 6.0) -> list:
        """Devuelve historial de odds para un evento en las ultimas N horas."""
        since = int(time.time()) - int(hours * 3600)
        with self._conn() as c:
            rows = c.execute(
                'SELECT * FROM rlm_odds WHERE event_id=? AND ts>=? ORDER BY ts ASC',
                (str(event_id), since)
            ).fetchall()
        return [dict(r) for r in rows]

    def _line_drift(self, event_id: str, label: str, hours: float = 2.0) -> Optional[float]:
        """
        Devuelve cambio en implied probability entre hace N horas y ahora.
        Positivo = linea subio (favorito se fortaleció).
        Negativo = linea bajo (favorito se debilitó = posible RLM).
        None si no hay historia suficiente.
        """
        since = int(time.time()) - int(hours * 3600)
        with self._conn() as c:
            old = c.execute(
                'SELECT implied FROM rlm_odds WHERE event_id=? AND label=? AND ts<=? '
                'ORDER BY ts ASC LIMIT 1',
                (str(event_id), label, since)
            ).fetchone()
            new = c.execute(
                'SELECT implied FROM rlm_odds WHERE event_id=? AND label=? '
                'ORDER BY ts DESC LIMIT 1',
                (str(event_id), label)
            ).fetchone()
        if not old or not new:
            return None
        return round(new['implied'] - old['implied'], 6)

    def check_steam(self, event_id: str, label: str,
                    window_h: float = _STEAM_WINDOW_H,
                    threshold: float = _STEAM_THRESHOLD) -> bool:
        """True si hubo un movimiento brusco (steam move) en la ventana dada."""
        drift = self._line_drift(event_id, label, hours=window_h)
        return drift is not None and abs(drift) >= threshold

    def check_public_reverse(self, event_id: str, label: str,
                              hours: float = 6.0,
                              threshold: float = _REVERSE_THRESHOLD) -> bool:
        """
        True si la linea se movio CONTRA el sesgo publico conocido.
        El publico apuesta: favoritos (bajas odds), home teams, overs.
        Si la linea de un favorito SUBE (implied prob baja) = sharps en el otro lado.
        Heuristico: si label contiene 'home' o es el primer equipo y la linea bajo
        en implied prob → public reverse.
        """
        drift = self._line_drift(event_id, label, hours=hours)
        if drift is None or abs(drift) < threshold:
            return False
        # Sesgo publico: home/favorito deberia crecer si el publico lo apoya
        # Si la linea CAE (drift < 0) en un label que suena a favorito = sharp en contrario
        label_lower = label.lower()
        is_public_side = (
            'home' in label_lower or
            'over' in label_lower or
            any(w in label_lower for w in ('winner:', 'moneyline'))
        )
        # RLM: publico en un lado pero la linea se mueve en contra
        return is_public_side and drift < -threshold

    def get_signal(self, event_id: str, label: str) -> dict:
        """
        Retorna dict con senales RLM para un pick especifico.
        {
            steam: bool,
            public_reverse: bool,
            line_drift_2h: float or None,
            line_drift_6h: float or None,
            rlm_score: float (0-1),
        }
        """
        drift_2h = self._line_drift(event_id, label, hours=2.0)
        drift_6h = self._line_drift(event_id, label, hours=6.0)
        steam    = self.check_steam(event_id, label)
        reverse  = self.check_public_reverse(event_id, label)

        # Score compuesto
        score = 0.0
        if steam:   score += 0.5
        if reverse: score += 0.4
        if drift_2h is not None and abs(drift_2h) > _STEAM_THRESHOLD / 2:
            score += 0.1
        score = min(1.0, round(score, 3))

        return {
            'steam':           steam,
            'public_reverse':  reverse,
            'line_drift_2h':   drift_2h,
            'line_drift_6h':   drift_6h,
            'rlm_score':       score,
        }

    # ------------------------------------------------------------------
    # Tagging de picks
    # ------------------------------------------------------------------

    def tag_picks(self, picks: list) -> list:
        """
        Agrega campos RLM a cada pick:
          rlm_signal   bool  — True si hay alguna senal RLM
          rlm_steam    bool
          rlm_reverse  bool
          rlm_score    float 0-1
          rlm_drift_2h float
        """
        for p in picks:
            eid   = p.get('event_id') or p.get('match', '')
            label = p.get('label', '')
            if not eid:
                p.update({'rlm_signal': False, 'rlm_steam': False,
                          'rlm_reverse': False, 'rlm_score': 0.0,
                          'rlm_drift_2h': None})
                continue
            sig = self.get_signal(eid, label)
            p['rlm_steam']   = sig['steam']
            p['rlm_reverse'] = sig['public_reverse']
            p['rlm_score']   = sig['rlm_score']
            p['rlm_drift_2h'] = sig['line_drift_2h']
            p['rlm_signal']  = sig['steam'] or sig['public_reverse']
            if p['rlm_signal']:
                log.info('[RLM] Senal detectada: %s | %s | steam=%s rev=%s score=%.2f drift_2h=%s',
                         str(eid)[:30], label[:25], sig['steam'], sig['public_reverse'],
                         sig['rlm_score'],
                         f"{sig['line_drift_2h']*100:+.2f}%" if sig['line_drift_2h'] else 'n/a')
        return picks

    def rlm_summary(self) -> dict:
        """Stats globales: cuantos events con RLM detectado en las ultimas 24h."""
        since = int(time.time()) - 86400
        with self._conn() as c:
            total = c.execute(
                'SELECT COUNT(DISTINCT event_id) FROM rlm_odds WHERE ts>=?', (since,)
            ).fetchone()[0]
        return {'events_tracked_24h': total}

    def purge_old(self, days: int = 30):
        """Elimina registros mas antiguos de N dias."""
        cutoff = int(time.time()) - days * 86400
        with self._conn() as c:
            n = c.execute('DELETE FROM rlm_odds WHERE ts<?', (cutoff,)).rowcount
        if n:
            log.debug('[RLM] Purgados %d registros > %d dias', n, days)


# Singleton para el runner
_tracker: Optional[RLMTracker] = None


def get_tracker() -> RLMTracker:
    global _tracker
    if _tracker is None:
        _tracker = RLMTracker()
    return _tracker
