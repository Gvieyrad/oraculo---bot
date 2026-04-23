"""
Cloudbet API Connector para Oráculo
====================================

Conector para Cloudbet Exchange (crypto betting)
- API REST completa con JWT auth
- Soporta 30+ cryptos (BTC, USDT, ETH)
- Peru: PERMITIDO (no bloqueado)
- API gratuita después de depositar 10 EUR

Autor: Oráculo ML System
Fecha: 2026-03-20
"""

import os
import requests
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class CloudbetConnector:
    """
    Conector para Cloudbet Exchange API

    API Documentation: https://cloudbet.github.io/wiki/en/docs/sports/api/
    """

    def __init__(self, api_key: str):
        """
        Inicializar conector Cloudbet

        Args:
            api_key: API key JWT de Cloudbet (obtener después de depositar 10 EUR)
        """
        self.api_key = api_key
        self.base_url = "https://sports-api.cloudbet.com"
        self.session = requests.Session()

        # Headers con autenticación JWT Bearer
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })

        logger.info("CloudbetConnector inicializado")

    def get_account_balance(self) -> Dict:
        """
        Obtener balance de cuenta

        Returns:
            Dict con currencies y balances
        """
        try:
            response = self.session.get(f"{self.base_url}/pub/v2/account/balances")
            response.raise_for_status()
            data = response.json()

            logger.info(f"Balance obtenido: {data}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error obteniendo balance: {e}")
            return None

    def get_soccer_competitions(self) -> List[Dict]:
        """
        Obtener todas las competiciones de fútbol disponibles

        Returns:
            Lista de competiciones
        """
        try:
            response = self.session.get(
                f"{self.base_url}/pub/v2/odds/sports/soccer"
            )
            response.raise_for_status()
            data = response.json()

            competitions = data.get('competitions', [])
            logger.info(f"Competiciones encontradas: {len(competitions)}")

            return competitions

        except requests.exceptions.RequestException as e:
            logger.error(f"Error obteniendo competiciones: {e}")
            return []

    def get_upcoming_matches(self, hours_ahead: int = 24) -> List[Dict]:
        """
        Obtener próximos partidos de fútbol

        Args:
            hours_ahead: Horas hacia adelante para buscar

        Returns:
            Lista de partidos
        """
        try:
            # Obtener todos los eventos de soccer
            response = self.session.get(
                f"{self.base_url}/pub/v2/odds/sports/soccer"
            )
            response.raise_for_status()
            data = response.json()

            all_matches = []
            now = datetime.utcnow()
            cutoff = now + timedelta(hours=hours_ahead)

            # Extraer eventos de todas las competiciones
            for competition in data.get('competitions', []):
                for event in competition.get('events', []):
                    # Parsear tiempo del evento
                    event_time_str = event.get('cutoffTime')
                    if event_time_str:
                        event_time = datetime.fromisoformat(event_time_str.replace('Z', '+00:00'))

                        # Filtrar por tiempo
                        if now <= event_time <= cutoff:
                            match_info = {
                                'event_id': event.get('id'),
                                'competition': competition.get('name'),
                                'competition_key': competition.get('key'),
                                'home': event.get('home', {}).get('name'),
                                'away': event.get('away', {}).get('name'),
                                'datetime': event_time.isoformat(),
                                'markets': event.get('markets', [])
                            }
                            all_matches.append(match_info)

            logger.info(f"Partidos próximos encontrados: {len(all_matches)}")
            return all_matches

        except requests.exceptions.RequestException as e:
            logger.error(f"Error obteniendo partidos: {e}")
            return []

    def get_event_odds(self, event_id: str) -> Optional[Dict]:
        """
        Obtener cuotas de un evento específico

        Args:
            event_id: ID del evento

        Returns:
            Dict con cuotas del evento
        """
        try:
            response = self.session.get(
                f"{self.base_url}/pub/v2/odds/events/{event_id}"
            )
            response.raise_for_status()
            data = response.json()

            logger.info(f"Cuotas obtenidas para evento {event_id}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error obteniendo cuotas del evento {event_id}: {e}")
            return None

    def find_match_odds(self, home_team: str, away_team: str) -> Optional[Dict]:
        """
        Encontrar cuotas de un partido específico

        Args:
            home_team: Equipo local
            away_team: Equipo visitante

        Returns:
            Dict con cuotas del partido o None
        """
        matches = self.get_upcoming_matches(hours_ahead=48)

        # Buscar partido
        for match in matches:
            if (self._normalize_team_name(match['home']) == self._normalize_team_name(home_team) and
                self._normalize_team_name(match['away']) == self._normalize_team_name(away_team)):

                # Obtener cuotas completas del evento
                event_odds = self.get_event_odds(match['event_id'])

                if event_odds:
                    # Extraer market "Match Winner" (1X2)
                    odds_result = self._extract_match_winner_odds(event_odds)
                    if odds_result:
                        return {
                            'event_id': match['event_id'],
                            'home': match['home'],
                            'away': match['away'],
                            'competition': match['competition'],
                            'datetime': match['datetime'],
                            'odds': odds_result
                        }

        logger.warning(f"No se encontró partido: {home_team} vs {away_team}")
        return None

    def _extract_match_winner_odds(self, event_data: Dict) -> Optional[Dict]:
        """
        Extraer cuotas del market "Match Winner" (1X2)

        Args:
            event_data: Datos del evento con markets

        Returns:
            Dict con cuotas {home, draw, away}
        """
        markets = event_data.get('markets', [])

        for market in markets:
            # Buscar market "Match Winner" o "Full Time Result"
            market_name = market.get('name', '').lower()
            market_url = market.get('submarketKey', '').lower()

            if 'match' in market_name or 'full' in market_name or 'winner' in market_url:
                selections = market.get('selections', [])

                odds_dict = {
                    'market_url': market.get('url'),
                    'home': None,
                    'draw': None,
                    'away': None
                }

                for selection in selections:
                    outcome = selection.get('params', '')
                    price = selection.get('price')

                    if 'home' in outcome.lower() or outcome == '1':
                        odds_dict['home'] = float(price) if price else None
                    elif 'draw' in outcome.lower() or outcome == 'X':
                        odds_dict['draw'] = float(price) if price else None
                    elif 'away' in outcome.lower() or outcome == '2':
                        odds_dict['away'] = float(price) if price else None

                if odds_dict['home'] and odds_dict['away']:
                    return odds_dict

        return None

    def place_bet(
        self,
        event_id: str,
        market_url: str,
        selection: str,
        stake: float,
        price: float,
        currency: str = 'BTC'
    ) -> Optional[Dict]:
        """
        Colocar apuesta

        Args:
            event_id: ID del evento
            market_url: URL del market (ej: /soccer/england/premier-league/match-123/markets/winner)
            selection: Selección ('home', 'draw', 'away')
            stake: Cantidad a apostar
            price: Cuota/odds aceptada
            currency: Moneda (BTC, USDT, ETH, etc.)

        Returns:
            Dict con resultado de la apuesta
        """
        try:
            bet_data = {
                'eventId': event_id,
                'marketUrl': market_url,
                'selection': selection,
                'stake': stake,
                'price': price,
                'currency': currency,
                'acceptPriceChange': 'BETTER'  # Aceptar solo mejores cuotas
            }

            logger.info(f"Colocando apuesta: {bet_data}")

            response = self.session.post(
                f"{self.base_url}/pub/v3/bets/place",
                json=bet_data
            )
            response.raise_for_status()
            result = response.json()

            logger.info(f"Apuesta colocada exitosamente: {result}")
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Error colocando apuesta: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Respuesta: {e.response.text}")
            return None

    def get_bet_history(self, limit: int = 50) -> List[Dict]:
        """
        Obtener historial de apuestas

        Args:
            limit: Número máximo de apuestas a retornar

        Returns:
            Lista de apuestas
        """
        try:
            response = self.session.get(
                f"{self.base_url}/pub/v2/bets",
                params={'limit': limit}
            )
            response.raise_for_status()
            data = response.json()

            bets = data.get('bets', [])
            logger.info(f"Historial obtenido: {len(bets)} apuestas")

            return bets

        except requests.exceptions.RequestException as e:
            logger.error(f"Error obteniendo historial: {e}")
            return []

    @staticmethod
    def _normalize_team_name(name: str) -> str:
        """Normalizar nombre de equipo para comparación"""
        return name.lower().strip().replace('-', ' ')


def load_config(config_file: str = 'cloudbet_config.json') -> Dict:
    """
    Cargar configuración desde archivo JSON.
    Supports env var CLOUDBET_API_KEY as override.

    Args:
        config_file: Ruta al archivo de configuración

    Returns:
        Dict con configuración
    """
    config = {}
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.warning(f"Config no encontrado: {config_file}, usando defaults")
    except json.JSONDecodeError:
        logger.error(f"Error parseando JSON: {config_file}")

    # Env var override for API key
    env_key = os.environ.get('CLOUDBET_API_KEY', '')
    if env_key:
        config['api_key'] = env_key

    # Validate no placeholder values
    api_key = config.get('api_key', '')
    if api_key.startswith('TU_') or not api_key:
        logger.warning('CLOUDBET_API_KEY not configured. Set env var or update %s', config_file)

    return config


# ==============================================================================
# EJEMPLO DE USO
# ==============================================================================

if __name__ == "__main__":
    print("="*80)
    print("CLOUDBET API CONNECTOR - ORÁCULO")
    print("="*80)
    print()

    # Cargar config
    config = load_config()

    if not config.get('api_key'):
        print("⚠️  CONFIGURACIÓN REQUERIDA")
        print()
        print("1. Crear cuenta en Cloudbet.com")
        print("2. Depositar 10 EUR equivalente en crypto (BTC, USDT, ETH)")
        print("3. Ir a Account -> API Keys")
        print("4. Generar API key")
        print("5. Copiar en cloudbet_config.json")
        print()
        print("Ejemplo cloudbet_config.json:")
        print(json.dumps({
            "api_key": "tu_api_key_jwt_aqui",
            "currency": "USDT",
            "max_stake": 0.05
        }, indent=2))
        exit(1)

    # Inicializar conector
    cloudbet = CloudbetConnector(api_key=config['api_key'])

    # Test 1: Balance
    print("1. VERIFICANDO BALANCE...")
    balance = cloudbet.get_account_balance()
    if balance:
        print(f"   ✓ Cuenta conectada")
        for currency_data in balance.get('currencies', []):
            curr = currency_data.get('name')
            available = currency_data.get('available', 0)
            print(f"   {curr}: {available}")
    else:
        print("   ✗ Error obteniendo balance")

    print()

    # Test 2: Competiciones
    print("2. OBTENIENDO COMPETICIONES...")
    competitions = cloudbet.get_soccer_competitions()
    print(f"   Total: {len(competitions)} competiciones")
    for comp in competitions[:5]:
        print(f"   - {comp.get('name')} ({comp.get('key')})")

    print()

    # Test 3: Próximos partidos
    print("3. PRÓXIMOS PARTIDOS (24h)...")
    matches = cloudbet.get_upcoming_matches(hours_ahead=24)
    print(f"   Total: {len(matches)} partidos")
    for match in matches[:5]:
        print(f"   - {match['home']} vs {match['away']}")
        print(f"     {match['competition']} | {match['datetime'][:16]}")

    print()
    print("="*80)
    print("CONECTOR CLOUDBET FUNCIONAL")
    print("="*80)
