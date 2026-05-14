#!/usr/bin/env python3
"""
Benter Trick — usa la probabilidad implicita del bookmaker como feature del modelo.

Bill Benter (Hong Kong, ~$1B apostando a carreras):
  Descubrio que el precio del bookmaker contiene informacion que su modelo no tiene:
  lesiones last-minute, flujo de dinero sharp, condiciones del dia, etc.
  Incorporar esa informacion como INPUT aumenta precision del modelo.

Formula central:
  final_prob = alpha * model_prob + (1 - alpha) * bm_implied_devigged

  alpha = cuanto confiamos en nuestro modelo vs el mercado
  - alpha alto (0.8) = confiamos mas en nuestro modelo (leagues poco liquidas)
  - alpha bajo (0.4) = confiamos mas en el mercado (Betfair/Pinnacle muy eficientes)

Por que funciona:
  Si el modelo dice 65% y Pinnacle dice 52%:
  - Sin Benter: edge = 65% * 2.10 - 1 = +36.5%  ← probablemente sobreestimado
  - Con Benter (alpha=0.6): final = 0.6*0.65 + 0.4*0.52 = 59.8%
                             edge = 59.8% * 2.10 - 1 = +25.6%  ← mas realista
  Resultado: menos apuestas, pero las que pasan el filtro son mas fiables.

Devig (remover margen del bookmaker):
  Bookmaker: Home 1.80, Away 2.10 → raw implied = 55.6% + 47.6% = 103.2%
  Devigged:  Home = 55.6/103.2 = 53.9%, Away = 46.1%
"""

import math
import logging
from typing import Dict, Optional, Tuple

log = logging.getLogger('oraculo')

# Alpha por defecto: fraccion que confiamos en nuestro modelo
# Aprende de Sibila con learn_alpha(), estos son los priors
_DEFAULT_ALPHA = {
    'soccer':   0.60,   # football: mercado muy eficiente, mas peso al bm
    'football': 0.60,
    'tennis':   0.65,   # tennis: menos liquidez, mas peso al modelo
    'mlb':      0.55,   # mlb: reducido 0.85→0.55 (modelo descalibrado, dar mas peso al mercado)
    'baseball': 0.55,   # alias — reducido junto con mlb
    'default':  0.60,
}

# Ajuste de alpha segun calidad del mercado (libro de referencia)
_BOOK_QUALITY_FACTOR = {
    'betfair_ex_eu': -0.10,  # mercado muy eficiente → menos alpha (mas peso bm)
    'betfair':       -0.10,
    'pinnacle':      -0.08,
    'bet365':        -0.04,
    'cloudbet':       0.00,
    'soft':          +0.10,  # mercado blando → mas peso al modelo
}

# Si nuestro edge es muy grande, probablemente hay error → reducir alpha
# Si edge < 5%: alpha normal
# Si edge 5-15%: alpha -= 0.05
# Si edge > 15%: alpha -= 0.12 (sospechoso, confiar mas en el mercado)
_EDGE_ALPHA_ADJ = [
    (0.05,  0.00),
    (0.15, -0.05),
    (1.00, -0.12),
]


def devig(odds_dict: Dict[str, float]) -> Dict[str, float]:
    """
    Remueve el margen del bookmaker usando normalizacion proporcional.
    odds_dict: {label: decimal_odds}
    Retorna: {label: fair_probability}
    """
    if not odds_dict:
        return {}
    raw = {k: 1.0 / v for k, v in odds_dict.items() if v > 1.0}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: round(v / total, 6) for k, v in raw.items()}


def devig_single(our_odds: float, counterpart_odds: float) -> float:
    """
    Devig para mercado de 2 opciones (lo mas comun: home/away, over/under).
    Retorna la fair probability de our_odds.
    """
    if our_odds <= 1.0 or counterpart_odds <= 1.0:
        return None
    raw_ours   = 1.0 / our_odds
    raw_other  = 1.0 / counterpart_odds
    total      = raw_ours + raw_other
    return round(raw_ours / total, 6)


def get_alpha(sport: str, book: str = 'default', edge: float = 0.0) -> float:
    """
    Alpha ajustado por deporte, calidad del libro y magnitud del edge.
    """
    base  = _DEFAULT_ALPHA.get(sport.lower(), _DEFAULT_ALPHA['default'])
    bq    = _BOOK_QUALITY_FACTOR.get(book.lower(), 0.0)

    # Ajuste por edge
    edge_adj = 0.0
    for threshold, adj in _EDGE_ALPHA_ADJ:
        if abs(edge) <= threshold:
            edge_adj = adj
            break

    alpha = base + bq + edge_adj
    return round(min(0.95, max(0.30, alpha)), 3)


def benter_blend(model_prob: float, bm_implied: float,
                 sport: str = 'soccer', book: str = 'default',
                 raw_edge: float = 0.0) -> Tuple[float, float]:
    """
    Mezcla model_prob con bm_implied usando el Benter trick.

    Args:
        model_prob:  probabilidad de nuestro modelo (0-1)
        bm_implied:  probabilidad implicita del bookmaker, DEVIGGED (0-1)
        sport:       'soccer' / 'tennis' / 'mlb'
        book:        libro de referencia para calidad del mercado
        raw_edge:    edge sin ajuste (para calibrar alpha)

    Returns:
        (final_prob, alpha_used)
    """
    if not (0 < model_prob < 1) or not (0 < bm_implied < 1):
        return model_prob, 1.0

    alpha       = get_alpha(sport, book, raw_edge)
    final_prob  = alpha * model_prob + (1.0 - alpha) * bm_implied
    final_prob  = round(min(0.99, max(0.01, final_prob)), 6)

    return final_prob, alpha


def adjusted_edge(model_prob: float, bm_implied: float,
                  price: float, sport: str = 'soccer',
                  book: str = 'default') -> dict:
    """
    Calcula edge ajustado por Benter.

    Args:
        model_prob:  probabilidad cruda del modelo
        bm_implied:  probabilidad del bookmaker (devigged)
        price:       odds que nos ofrecen (Cloudbet)
        sport, book: para calibrar alpha

    Returns dict con:
        raw_edge:      edge original del modelo
        raw_model_prob: model_prob original
        final_prob:    probabilidad Benter-blended
        final_edge:    edge recalculado con final_prob
        alpha:         alpha usado
        bm_implied:    bm_implied usado
        edge_delta:    cuanto cambio el edge (negativo = Benter fue conservador)
        passes_filter: True si final_edge > 0
    """
    if price <= 1.0:
        return {'raw_edge': 0, 'final_edge': 0, 'passes_filter': False}

    raw_edge    = round(model_prob * price - 1.0, 6)
    final_prob, alpha = benter_blend(model_prob, bm_implied, sport, book, raw_edge)
    final_edge  = round(final_prob * price - 1.0, 6)
    edge_delta  = round(final_edge - raw_edge, 6)

    return {
        'raw_edge':       raw_edge,
        'raw_model_prob': model_prob,
        'final_prob':     final_prob,
        'final_edge':     final_edge,
        'alpha':          alpha,
        'bm_implied':     bm_implied,
        'edge_delta':     edge_delta,
        'passes_filter':  final_edge > 0,
    }


# ------------------------------------------------------------------
# Aprendizaje de alpha desde Sibila
# ------------------------------------------------------------------

def learn_alpha_from_sibila(sibila_db_path: str,
                             sport: str = None,
                             min_picks: int = 50) -> Dict[str, float]:
    """
    Aprende el alpha optimo por deporte usando picks resueltos en Sibila.
    Minimiza Brier Score entre final_prob y resultado real.

    Retorna {sport: optimal_alpha} o {} si no hay suficientes datos.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(sibila_db_path)
        conn.row_factory = sqlite3.Row
        q = '''
            SELECT sport, prob_model AS model_prob, odds AS price, result, clv
            FROM sibila_picks
            WHERE result IN ('WIN','LOSS')
              AND prob_model > 0
              AND odds > 1.0
        '''
        if sport:
            q += f" AND sport='{sport}'"
        rows = conn.execute(q).fetchall()
        conn.close()
    except Exception as e:
        log.warning('[Benter] learn_alpha error: %s', e)
        return {}

    # Agrupar por deporte
    by_sport: Dict[str, list] = {}
    for r in rows:
        sp = r['sport'] or 'unknown'
        by_sport.setdefault(sp, []).append(dict(r))

    result = {}
    for sp, picks in by_sport.items():
        if len(picks) < min_picks:
            log.debug('[Benter] %s: solo %d picks, necesita %d', sp, len(picks), min_picks)
            continue

        # Grid search sobre alpha [0.3 ... 0.95]
        best_alpha = _DEFAULT_ALPHA.get(sp, 0.60)
        best_brier = float('inf')

        for a in [i / 20 for i in range(6, 20)]:  # 0.30, 0.35, ... 0.95
            brier = 0.0
            for p in picks:
                mp      = float(p['model_prob'])
                price   = float(p['price'])
                outcome = 1.0 if p['result'] == 'WIN' else 0.0

                # bm_implied: estimar de CLV y precio si no tenemos el dato directo
                clv = p.get('clv')
                if clv is not None:
                    closing = price / (1.0 + clv)
                    bm_imp  = min(0.98, max(0.02, 1.0 / closing))
                else:
                    bm_imp = min(0.98, max(0.02, 1.0 / price))

                blended = a * mp + (1.0 - a) * bm_imp
                brier  += (blended - outcome) ** 2

            brier /= len(picks)
            if brier < best_brier:
                best_brier = brier
                best_alpha = a

        # Floor: MLB model has zone-specific edge, don't let bad zones drag alpha below 0.80
        _ALPHA_FLOOR = {'mlb': 0.50, 'baseball': 0.50}
        if best_alpha < _ALPHA_FLOOR.get(sp, 0.0):
            best_alpha = _ALPHA_FLOOR[sp]
            log.info('[Benter] %s alpha floor aplicado: %.2f (raw=%.2f)', sp, best_alpha, best_alpha)
        log.info('[Benter] %s optimal alpha=%.2f (Brier=%.4f, n=%d)',
                 sp, best_alpha, best_brier, len(picks))
        result[sp] = best_alpha

    return result


class BenterCalibrator:
    """
    Mantiene alphas aprendidos por deporte y aplica el Benter trick a picks.
    Puede recalibrar automaticamente cuando hay suficientes picks resueltos.
    """

    def __init__(self, sibila_db: str = None):
        import time as _t
        self.sibila_db   = sibila_db
        self._alphas     = dict(_DEFAULT_ALPHA)  # copia mutable
        self._last_learn = _t.time()  # no recalibrar inmediatamente al arrancar
        self._learn_ttl  = 24 * 3600  # recalibrar cada 24h

    def recalibrate(self, force: bool = False) -> bool:
        """Aprende alphas optimos de Sibila si hay datos suficientes."""
        import time
        now = time.time()
        if not force and (now - self._last_learn) < self._learn_ttl:
            return False
        if not self.sibila_db:
            return False

        learned = learn_alpha_from_sibila(self.sibila_db)
        if learned:
            for sp, alpha in learned.items():
                self._alphas[sp] = alpha
                log.info('[Benter] alpha actualizado: %s → %.2f', sp, alpha)
            self._last_learn = now
            return True
        return False

    def apply(self, pick: dict, bm_implied: float = None,
              book: str = 'default') -> dict:
        """
        Aplica Benter trick a un pick.
        Modifica pick in-place y retorna el dict de ajuste.

        Si bm_implied=None, se calcula a partir del precio del pick
        (el bookmaker ofrece X → su fair prob es 1/X devigged, pero
        sin el counterpart no podemos deviggar → usamos raw 1/price como aproximacion).
        """
        mp    = float(pick.get('model_prob', 0) or 0)
        price = float(pick.get('price', 0) or 0)
        sport = pick.get('sport', 'soccer')

        if not mp or not price:
            return {}

        # Si no tenemos bm_implied, usar 1/price como aproximacion del mercado
        # (overround incluido, conservador → trusts bm menos)
        if bm_implied is None:
            bm_implied = round(1.0 / price, 6) if price > 1 else mp

        adj = adjusted_edge(
            model_prob=mp,
            bm_implied=bm_implied,
            price=price,
            sport=sport,
            book=book,
        )

        # Override con alpha aprendido (self._alphas tiene prioridad sobre _DEFAULT_ALPHA)
        _learned = self._alphas.get(sport, self._alphas.get('default', None))
        if _learned is not None and abs(_learned - adj['alpha']) > 0.001:
            _fp = round(min(0.99, max(0.01, _learned * mp + (1.0 - _learned) * bm_implied)), 6)
            _fe = round(_fp * price - 1.0, 6)
            adj['final_prob']  = _fp
            adj['final_edge']  = _fe
            adj['alpha']       = _learned
            adj['edge_delta']  = round(_fe - adj['raw_edge'], 6)

        # Actualizar pick con valores Benter
        pick['raw_model_prob'] = adj['raw_model_prob']
        pick['model_prob']     = adj['final_prob']      # reemplazar para calculos downstream
        pick['edge']           = adj['final_edge']
        pick['benter_alpha']   = adj['alpha']
        pick['bm_implied']     = adj['bm_implied']
        pick['edge_delta']     = adj['edge_delta']

        if abs(adj['edge_delta']) > 0.005:
            log.info('[Benter] %s | %s | model=%.1f%% bm=%.1f%% → blend=%.1f%% '
                     'edge %+.1f%%→%+.1f%% (alpha=%.2f)',
                     pick.get('match', '')[:25], pick.get('label', '')[:20],
                     adj['raw_model_prob']*100, adj['bm_implied']*100,
                     adj['final_prob']*100,
                     adj['raw_edge']*100, adj['final_edge']*100,
                     adj['alpha'])

        return adj

    def apply_batch(self, picks: list, book: str = 'default') -> list:
        """Aplica Benter a una lista de picks. Modifica in-place."""
        import time
        self.recalibrate()
        for p in picks:
            self.apply(p, book=book)
        # Filtrar picks que ya no superan el edge minimo
        before = len(picks)
        picks  = [p for p in picks if p.get('edge', 0) > 0]
        dropped = before - len(picks)
        if dropped:
            log.info('[Benter] %d picks filtrados por Benter (edge reducido < 0)', dropped)
        return picks


# Singleton para el runner
_calibrator: BenterCalibrator = None


def get_calibrator(sibila_db: str = None) -> BenterCalibrator:
    global _calibrator
    if _calibrator is None:
        _calibrator = BenterCalibrator(sibila_db=sibila_db)
    return _calibrator
