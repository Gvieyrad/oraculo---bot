#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ORÁCULO - BANKROLL MANAGER
Gestión automática de capital usando Kelly Criterion
"""

import json
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class BankrollManager:
    """
    Gestor de bankroll con Kelly Criterion

    Características:
    - Quarter Kelly (conservador)
    - Límite máximo 5% por apuesta
    - Tracking de P&L
    - Rebalanceo automático
    """

    def __init__(self, initial_bankroll: float,
                 kelly_fraction: float = 0.25,
                 max_stake_pct: float = 0.05,
                 min_stake: float = 2.0):
        """
        Inicializa el gestor de bankroll

        Args:
            initial_bankroll: Capital inicial
            kelly_fraction: Fracción de Kelly (0.25 = Quarter Kelly)
            max_stake_pct: Máximo % del bankroll por apuesta (0.05 = 5%)
            min_stake: Stake mínimo en $ (ej: 2.0)
        """
        self.initial = initial_bankroll
        self.current = initial_bankroll
        self.kelly_fraction = kelly_fraction
        self.max_stake_pct = max_stake_pct
        self.min_stake = min_stake

        # Tracking
        self.bets_history = []
        self.total_wagered = 0
        self.total_profit = 0
        self.total_bets = 0
        self.wins = 0
        self.losses = 0

    def kelly_stake(self, odds: float, win_probability: float) -> float:
        """
        Calcula stake óptimo usando Kelly Criterion

        Args:
            odds: Cuota decimal (ej: 1.80)
            win_probability: Probabilidad de ganar (0-1)

        Returns:
            Stake en $
        """
        # Validar inputs
        if odds <= 1.0 or win_probability <= 0 or win_probability >= 1:
            logger.warning(f"⚠️ Parámetros inválidos: odds={odds}, prob={win_probability}")
            return 0

        # Kelly completo
        b = odds - 1  # Ganancia neta por $ apostado
        p = win_probability
        q = 1 - p

        kelly_full = (b * p - q) / b

        # Si Kelly es negativo o cero, no hay value
        if kelly_full <= 0:
            logger.info(f"📊 No hay value bet: Kelly={kelly_full:.3f}")
            return 0

        # Aplicar fracción (conservador)
        kelly_fractional = kelly_full * self.kelly_fraction

        # Aplicar límite máximo
        kelly_limited = min(kelly_fractional, self.max_stake_pct)

        # Calcular stake
        stake = self.current * kelly_limited

        # Aplicar stake mínimo
        if stake < self.min_stake and kelly_limited > 0:
            logger.warning(f"⚠️ Stake calculado ${stake:.2f} < mínimo ${self.min_stake:.2f}")
            if self.current >= self.min_stake:
                stake = self.min_stake
            else:
                logger.error(f"❌ Bankroll insuficiente para stake mínimo")
                return 0

        # Verificar que no exceda bankroll disponible
        stake = min(stake, self.current * 0.95)  # Dejar al menos 5% de reserva

        return round(stake, 2)

    def calculate_value(self, odds: float, model_prob: float,
                       min_edge: float = 0.05) -> Dict:
        """
        Calcula si una apuesta tiene value

        Args:
            odds: Cuota del mercado
            model_prob: Probabilidad del modelo
            min_edge: Edge mínimo requerido (default 5%)

        Returns:
            Dict con análisis completo
        """
        # Probabilidad implícita del mercado
        market_prob = 1 / odds

        # Edge
        edge = model_prob - market_prob

        # Kelly stake
        stake = self.kelly_stake(odds, model_prob)

        # Valor esperado
        if stake > 0:
            expected_value = stake * (odds - 1) * model_prob - stake * (1 - model_prob)
            roi_expected = (expected_value / stake) * 100 if stake > 0 else 0
        else:
            expected_value = 0
            roi_expected = 0

        # Es value bet?
        is_value = edge >= min_edge and stake > 0

        return {
            'market_prob': market_prob,
            'model_prob': model_prob,
            'edge': edge,
            'edge_pct': edge * 100,
            'stake': stake,
            'stake_pct': (stake / self.current * 100) if self.current > 0 else 0,
            'expected_value': expected_value,
            'roi_expected': roi_expected,
            'is_value_bet': is_value,
            'recommendation': 'APOSTAR' if is_value else 'NO APOSTAR'
        }

    def record_bet(self, bet_info: Dict):
        """
        Registra una apuesta colocada

        Args:
            bet_info: Dict con info de la apuesta
                {
                    'match': str,
                    'selection': str,
                    'odds': float,
                    'stake': float,
                    'model_prob': float,
                    'bet_id': str (opcional),
                    'market_id': str (opcional)
                }
        """
        bet_record = {
            **bet_info,
            'timestamp': datetime.now().isoformat(),
            'bankroll_before': self.current,
            'status': 'PENDING'
        }

        self.bets_history.append(bet_record)
        self.total_wagered += bet_info['stake']
        self.total_bets += 1

        # Actualizar bankroll (reservar la cantidad apostada)
        self.current -= bet_info['stake']

        logger.info(f"📝 Apuesta registrada: {bet_info['match']} - ${bet_info['stake']:.2f}")
        logger.info(f"💰 Bankroll: ${self.current:.2f}")

    def settle_bet(self, bet_id: str, result: str, profit: float = None):
        """
        Liquida una apuesta

        Args:
            bet_id: ID de la apuesta (o índice en history)
            result: 'WON' o 'LOST'
            profit: Ganancia/pérdida (opcional, se calcula si no se provee)
        """
        # Buscar apuesta en history
        bet = None
        for b in self.bets_history:
            if b.get('bet_id') == bet_id or str(self.bets_history.index(b)) == str(bet_id):
                bet = b
                break

        if not bet:
            logger.error(f"❌ Apuesta no encontrada: {bet_id}")
            return

        # Calcular profit si no se provee
        if profit is None:
            if result == 'WON':
                profit = bet['stake'] * (bet['odds'] - 1)
            else:
                profit = -bet['stake']

        # Actualizar registro
        bet['status'] = result
        bet['profit'] = profit
        bet['settled_time'] = datetime.now().isoformat()
        bet['bankroll_after'] = self.current + bet['stake'] + profit

        # Actualizar bankroll (devolver stake + ganancia)
        self.current += bet['stake'] + profit
        self.total_profit += profit

        # Actualizar contadores
        if result == 'WON':
            self.wins += 1
            logger.info(f"✅ Apuesta GANADA: +${profit:.2f}")
        else:
            self.losses += 1
            logger.info(f"❌ Apuesta PERDIDA: ${profit:.2f}")

        logger.info(f"💰 Bankroll actualizado: ${self.current:.2f}")

    def get_stats(self) -> Dict:
        """Obtiene estadísticas del bankroll"""
        total_settled = self.wins + self.losses
        win_rate = (self.wins / total_settled * 100) if total_settled > 0 else 0

        roi = (self.total_profit / self.total_wagered * 100) if self.total_wagered > 0 else 0

        return {
            'initial_bankroll': self.initial,
            'current_bankroll': self.current,
            'total_profit': self.total_profit,
            'roi': roi,
            'roi_pct': (self.current / self.initial - 1) * 100,
            'total_bets': self.total_bets,
            'settled_bets': total_settled,
            'pending_bets': self.total_bets - total_settled,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': win_rate,
            'total_wagered': self.total_wagered,
            'avg_stake': self.total_wagered / self.total_bets if self.total_bets > 0 else 0
        }

    def save_state(self, filename: str = 'bankroll_state.json'):
        """Guarda estado del bankroll a archivo"""
        state = {
            'initial': self.initial,
            'current': self.current,
            'kelly_fraction': self.kelly_fraction,
            'max_stake_pct': self.max_stake_pct,
            'min_stake': self.min_stake,
            'stats': self.get_stats(),
            'history': self.bets_history,
            'updated': datetime.now().isoformat()
        }

        with open(filename, 'w') as f:
            json.dump(state, f, indent=2)

        logger.info(f"💾 Estado guardado: {filename}")

    @classmethod
    def load_state(cls, filename: str = 'bankroll_state.json') -> 'BankrollManager':
        """Carga estado desde archivo"""
        try:
            with open(filename, 'r') as f:
                state = json.load(f)

            manager = cls(
                initial_bankroll=state['initial'],
                kelly_fraction=state['kelly_fraction'],
                max_stake_pct=state['max_stake_pct'],
                min_stake=state['min_stake']
            )

            manager.current = state['current']
            manager.bets_history = state['history']

            # Recalcular stats desde history
            manager.total_bets = len(manager.bets_history)
            manager.wins = sum(1 for b in manager.bets_history if b.get('status') == 'WON')
            manager.losses = sum(1 for b in manager.bets_history if b.get('status') == 'LOST')
            manager.total_wagered = sum(b['stake'] for b in manager.bets_history)
            manager.total_profit = sum(b.get('profit', 0) for b in manager.bets_history if 'profit' in b)

            logger.info(f"📂 Estado cargado: {filename}")
            return manager

        except FileNotFoundError:
            logger.warning(f"⚠️ Archivo no encontrado: {filename}")
            return None


# EJEMPLO DE USO
if __name__ == '__main__':
    print("="*70)
    print("ORÁCULO - BANKROLL MANAGER")
    print("="*70)

    # Crear gestor con $1000
    bankroll = BankrollManager(initial_bankroll=1000.0)

    print(f"\n💰 Bankroll inicial: ${bankroll.current:.2f}")
    print(f"   Kelly fraction: {bankroll.kelly_fraction} (Quarter Kelly)")
    print(f"   Max stake: {bankroll.max_stake_pct*100}%")

    # Ejemplo 1: Genoa vs Udinese (predicción de hoy)
    print("\n" + "="*70)
    print("EJEMPLO 1: Genoa vs Udinese")
    print("="*70)

    odds = 1.80
    model_prob = 0.84

    analysis = bankroll.calculate_value(odds, model_prob)

    print(f"\nCuota mercado:       {odds}")
    print(f"Prob. mercado:       {analysis['market_prob']*100:.1f}%")
    print(f"Prob. modelo:        {analysis['model_prob']*100:.1f}%")
    print(f"EDGE:                {analysis['edge_pct']:.1f}%")
    print(f"\nStake recomendado:   ${analysis['stake']:.2f} ({analysis['stake_pct']:.1f}% bankroll)")
    print(f"Valor esperado:      ${analysis['expected_value']:.2f}")
    print(f"ROI esperado:        {analysis['roi_expected']:.1f}%")
    print(f"\n{'✅ ' + analysis['recommendation']}")

    # Simular colocación de apuesta
    if analysis['is_value_bet']:
        bankroll.record_bet({
            'match': 'Genoa vs Udinese',
            'selection': 'Genoa gana',
            'odds': odds,
            'stake': analysis['stake'],
            'model_prob': model_prob,
            'bet_id': 'BET001'
        })

    # Ejemplo 2: Leipzig vs Hoffenheim
    print("\n" + "="*70)
    print("EJEMPLO 2: Leipzig vs Hoffenheim")
    print("="*70)

    odds2 = 3.50
    model_prob2 = 0.71

    analysis2 = bankroll.calculate_value(odds2, model_prob2)

    print(f"\nCuota mercado:       {odds2}")
    print(f"Prob. mercado:       {analysis2['market_prob']*100:.1f}%")
    print(f"Prob. modelo:        {analysis2['model_prob']*100:.1f}%")
    print(f"EDGE:                {analysis2['edge_pct']:.1f}%")
    print(f"\nStake recomendado:   ${analysis2['stake']:.2f} ({analysis2['stake_pct']:.1f}% bankroll)")
    print(f"Valor esperado:      ${analysis2['expected_value']:.2f}")
    print(f"ROI esperado:        {analysis2['roi_expected']:.1f}%")
    print(f"\n{'✅ ' + analysis2['recommendation']}")

    if analysis2['is_value_bet']:
        bankroll.record_bet({
            'match': 'Leipzig vs Hoffenheim',
            'selection': 'Empate',
            'odds': odds2,
            'stake': analysis2['stake'],
            'model_prob': model_prob2,
            'bet_id': 'BET002'
        })

    # Ver estadísticas
    print("\n" + "="*70)
    print("ESTADÍSTICAS ACTUALES")
    print("="*70)

    stats = bankroll.get_stats()
    print(f"\nBankroll inicial:    ${stats['initial_bankroll']:.2f}")
    print(f"Bankroll actual:     ${stats['current_bankroll']:.2f}")
    print(f"Total apostado:      ${stats['total_wagered']:.2f}")
    print(f"Apuestas totales:    {stats['total_bets']}")
    print(f"Apuestas pendientes: {stats['pending_bets']}")

    # Simular liquidación de apuesta
    print("\n" + "="*70)
    print("SIMULACIÓN: Genoa gana (apuesta ganada)")
    print("="*70)

    bankroll.settle_bet('BET001', 'WON')

    stats = bankroll.get_stats()
    print(f"\nBankroll actualizado: ${stats['current_bankroll']:.2f}")
    print(f"Ganancia total:       ${stats['total_profit']:.2f}")
    print(f"ROI:                  {stats['roi']:.1f}%")
    print(f"Win rate:             {stats['win_rate']:.1f}%")

    # Guardar estado
    print("\n" + "="*70)
    bankroll.save_state()
    print("="*70)
