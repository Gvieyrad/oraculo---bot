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
