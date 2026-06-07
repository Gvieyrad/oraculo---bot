"""
Oraculo Autonomous Runner
=========================
24/7 autonomous betting system for Cloudbet.
Scans football + tennis markets, places bets, settles results, syncs Obsidian.

Usage:
                            _max_stake: 2.00,
                            _features: {elo_diff:round(_eh-_ea,3),surf_elo_diff:round(_sh-_sa,3),rank_diff:round(_rdiff4,1),form_diff:round(_form_ph-_form_pa,3),form_a:round(_form_ph,3),form_b:round(_form_pa,3),surf_form_a:round(_sf_ph,3),surf_form_b:round(_sf_pa,3),h2h_rate:round(_h2h_rate4,3)},
    python oraculo_runner_auto.py --status     # Show state
    python oraculo_runner_auto.py --results    # Check/settle bets
    python oraculo_runner_auto.py --dry-run    # Scan without placing

Author: Oraculo ML System
Date: 2026-03-22
"""

import os, sys, json, time, logging, argparse, uuid, re, gc
from datetime import datetime, timedelta, timezone
from itertools import combinations

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, 'oraculo_auto_state.json')
OBSIDIAN_DIR = os.path.join(SCRIPT_DIR, 'Samael')
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger('oraculo_auto')
log.setLevel(logging.INFO)
log.propagate = False  # evita mensajes duplicados via root logger
_fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
log.addHandler(_ch)
try:
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(
        os.path.join(LOG_DIR, 'oraculo_auto.log'),
        maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCAN_INTERVAL = 3600        # 1 hour between market scans
RESULT_INTERVAL = 1800      # 30 min between result checks
MIN_EDGE = 0.08             # 8% minimum edge — subido de 5%: modelo sobre-estima edge (13.6% declarado vs -7% ROI real)
MIN_CONF = 0.60             # 60% minimum confidence
TENNIS_MIN_CONF = 0.72       # 2026-05-26: all tennis segments negative below 72%; raised from 60%
TENNIS_MIN_EDGE = 0.10       # 2026-06-04: edge 5-9%% WR=57%% Sibila (n=37) -> raise to 0.10 (edge 10%%+ WR=67%%)
MAX_DAILY_PCT = 0.40        # 40% of bankroll per day (no usado, ver DAILY_BUDGET)
DAILY_BUDGET = 50.0         # 2026-06-04: presupuesto diario fijo $50
MAX_PER_BET = 0.08          # 8% per bet (~$16 a bankroll $203)
KELLY_FRAC = 0.25           # Quarter Kelly (global fallback)
SPORT_KELLY = {             # Per-sport Kelly fractions
    'tennis':     0.20,  # 2026-05-29: post-filter WR 70% (58 bets) raised from 0.15
    'baseball':     0.25,  # 2026-06-04: daily budget $50 -> Kelly 0.10->0.25 (~$5/bet)
    'basketball': 0.10,
    'darts':      0.10,
    'soccer':       0.20,
    'soccer_under': 0.25,  # 2026-05-22: U2.5 +25.5% ROI, U1.5 +47.3% -> boost Kelly
}
MIN_STAKE = 0.50            # Minimum bet $0.50
CIRCUIT_BREAKER = 10.0      # Stop if bankroll < $10
LOSS_STREAK_LIMIT = 5       # Reduce stake after 5 consecutive losses
LOSS_STREAK_FACTOR = 0.50   # Reduce to 50%
MAX_BETS_PER_SCAN = 8       # Max bets placed per scan cycle
MAX_PER_MATCH = 1           # Max 1 bet per match (avoid correlated exposure)
MAX_EXPOSURE_PER_MATCH = 0.10  # Max 10% of bankroll on a single match
MAX_EXPOSURE_PER_EVENT = 0.05  # Max 5% of bankroll per event_id (cross-cycle)
MAX_TOTAL_EXPOSURE = 0.30     # Max 30% of bankroll in pending bets — reduced from 60% (crash prevention)
TENNIS_BUDGET_RESERVE = 0.30  # Reserve 30% of daily budget for tennis
PARLAYS_ENABLED = False     # Disabled 2026-05-12: 1W/5L, ROI -61.8%, -$15.12 (stale 87.5% WR was sample bias)
PARLAY_MIN_LEGS = 2         # 2-leg parlays only (3+ leg hit rate too low)
PARLAY_MAX_LEGS = 2         # Hard cap at 2 legs
PARLAY_MIN_CONF = 0.65      # Lowered: 87.5% WR validates higher parlay volume
TENNIS_PARLAY_STAKE_PCT = 0.05  # 5% of bankroll for tennis parlay

STEAM_ENABLED = False          # Disable steam move betting (getting RESTRICTED by Cloudbet)
SOCCER_ENABLED = True          # Re-enabled: 86% WR on 14 real bets, +$185 PnL
SOCCER_GOALS_ENABLED = True    # Re-enabled: referee goal-rate multiplier + stricter thresholds (P3.1)
MLB_ENABLED = True             # 2026-06-01: re-enabled; 2026-06-02: f5_ml ACTIVADO LIVE (shadow WR=58.1% n=210 > umbral 54%/30)
MLB_PROB_CALIBRATION = 0.85      # Systematic overestimation correction: raw WR 36.3% vs implied 43.6%
MLB_MIN_EDGE = 0.15              # 2026-05-22: raised from 0.08 global — ROI -9.3% on 102 real picks
MLB_F5_ML_MIN_EDGE = 0.08        # 2026-06-02: f5_ml lower threshold — shadow WR=58.1% n=210 picks
# 2026-06-03: fade teams — modelo WR<30% con n>=7; registrar oponente en Sibila shadow
MLB_FADE_TEAMS = {
    'CHI Cubs', 'TEX Rangers', 'DET Tigers',    # 0%/25%/25% WR (n>=7, Jun-03)
    'MIA Marlins',                               # MIA 37% WR n=13; CHW removed 2026-06-05 WR=35% reversed
}
MLB_FADE_TEAMS_LOWER = {t.lower() for t in MLB_FADE_TEAMS}  # sync auto; usar este en filtros
# 2026-06-04: boost teams — modelo WR>=89% n>=14 (90d Sibila); cap $2.00 (doble del normal)
MLB_BOOST_TEAMS = {
    'MIL Brewers',    # WR=93% n=14 PnL=+$237
    'WAS Nationals',  # WR=89% n=18 PnL=+$333
}
# WC 2026 Fase C constants (2026-05-22)
WC_ENABLED = True
WC_MIN_EDGE = 0.10    # 10% — conservative; result_1x2 historical ROI -28.2%
WC_MIN_CONF = 0.55    # 55% — DC model 60.9% accuracy
WC_STAKE_PCT = 0.02   # fixed 2% of wc_reserve per bet
WC_START_DATE = '2026-06-12'
TENNIS_MAX_EDGE = 0.18         # Cap: 0.20+ bucket is -7.8% ROI — model overestimates extreme edges
TENNIS_PLATT_A = 1.044   # 2026-05-22: Platt calibration fitted on 124 Sibila picks (15% overestim corrected)
TENNIS_PLATT_B = -0.7552  # logit(p) scaled + shifted; reduces avg prob 68%->53% matching real WR
SHARP_REF_ENABLED = True       # Pre-placement check: skip if model differs from Pinnacle no-vig by >10%
CB_BASE = 'https://sports-api.cloudbet.com'
# Initial deposits (known constants for bankroll reconciliation)
INITIAL_DEPOSIT = 57.03        # Total initial deposit (USDC + USDT)
INITIAL_DEPOSIT_USDC = 34.56   # 57.03 x 39.98/65.98 -- proportional split
INITIAL_DEPOSIT_USDT = 22.47   # 57.03 x 26.00/65.98 -- proportional split

# Markets (V3: no BTTS)
LEAGUE_MARKETS = {
    'PL': ['over25', 'over15', 'under35', 'corners_o95'],
    'PD': ['over25', 'over15', 'under35', 'corners_o95'],
    'SA': ['over25', 'over15', 'under35', 'corners_o95'],
    'BL1': ['over25', 'over15', 'under35'],
    'FL1': ['over25', 'over15', 'under35'],
    'ELC': ['over25', 'over15'],
    'DED': ['over25', 'over15'],
    'PPL': ['over25', 'over15', 'under35'],  # 2026-05-13: U3.5=74.5% historical
    'MLS': ['over25', 'over15'],
    'LMX': ['over25', 'over15'],
    'ARG': ['over25', 'over15'],
    'BRA': ['over25', 'over15'],
    'BEL': ['over25'],
    'SWE': ['over25'],
    'NOR': ['over25'],
    'SWZ': ['over25'],
    'BL2': ['over25'],
    'SB':  ['over25'],
    'FL2': ['over25'],
    'PER': ['over25'],
    'ESP2': ['over25'],
    'BRA2': ['over25'],
    'COL':  ['over25'],
    'CHL':  ['over25'],
    'URU':  ['over25'],
    'TUR':  ['over25', 'over15'],  # 2026-05-12: O2.5=55.3% U3.5=65.5%
    'SCO':  ['over25', 'over15'],  # 2026-05-12: O2.5=60.5% Over-friendly
}

CB_COMPS = {
    'PL': 'soccer-england-premier-league',
    'PD': 'soccer-spain-laliga',
    'SA': 'soccer-italy-serie-a',
    'BL1': 'soccer-germany-bundesliga',
    'FL1': 'soccer-france-ligue-1',
    'DED': 'soccer-netherlands-eredivisie',
    'PPL': 'soccer-portugal-primeira-liga',
    'ELC': 'soccer-england-championship',
    'MLS': 'soccer-usa-mls',
    'LMX': 'soccer-mexico-t90c7-liga-mx-apertura',
    'ARG': 'soccer-argentina-superliga',
    'BRA': 'soccer-brazil-brasileiro-serie-a',
    'ESP2': 'soccer-spain-laliga-2',
    'BRA2': 'soccer-brazil-brasileiro-serie-b',
    'COL': 'soccer-colombia-primera-a-clausura',
    'CHL': 'soccer-chile-primera-division',
    'URU': 'soccer-uruguay-primera-division',
    'TUR': 'soccer-turkey-super-lig',
    'SCO': 'soccer-scotland-premiership',
    'PER': 'soccer-peru-primera-division',
    'BEL': 'soccer-belgium-first-division-a',
    'BL2': 'soccer-germany-2-bundesliga',
    'FL2': 'soccer-france-ligue-2',
    'NOR': 'soccer-norway-eliteserien',
    'SWE': 'soccer-sweden-allsvenskan',
    'SWZ': 'soccer-switzerland-super-league',
    'SB':  'soccer-italy-serie-b',
}

CB_TENNIS = [
    # ATP Slams + Masters (current Cloudbet keys)
    'tennis-atp-australian-open-men-singles-qual',
    'tennis-atp-french-open-men-singles',
    'tennis-atp-wimbledon-men-s-singles',
    'tennis-atp-us-open-men-singles',
    'tennis-atp-atp-miami-usa-men-singles',
    'tennis-atp-t8bd9-grand-slam',
    'tennis-atp-monte-carlo-men-singles',
    'tennis-atp-barcelona-spain-men-singles',
    'tennis-atp-madrid-spain-men-singles',
    'tennis-atp-rome-italy-men-singles',
    # WTA
    'tennis-wta-wta-miami-usa-women-singles',
    'tennis-wta-t8bda-grand-slam',
    'tennis-wta-madrid-spain-women-singles',
    # Challenger (high-quality data)
    'tennis-challenger-atp-challenger-sarasota-usa-men-singles',
    'tennis-challenger-t7e82-atp-challenger-madrid-spain-men-singles',
    'tennis-challenger-atp-challenger-campinas-brazil-men-singles',
]

CB_MARKETS_MAP = {
    'over25': ('soccer.total_goals', [('over', 'total=2.5'), ('under', 'total=2.5')]),
    'over15': ('soccer.total_goals', [('over', 'total=1.5')]),
    'under35': ('soccer.total_goals', [('under', 'total=3.5')]),
    'corners_o95': ('soccer.total_corners', [('over', 'total=9.5')]),
    'asian_handicap': ('soccer.asian_handicap', []),
    'btts_yes':  ('soccer.both_teams_to_score', [('yes', '')]),
    'btts_no':   ('soccer.both_teams_to_score', [('no', '')]),
    'ou15_1h':   ('soccer.total_goals_period_first_half', [('over', 'total=0.5'), ('under', 'total=0.5')]),
    'corners_h': ('soccer.corner_match_odds', [('home', ''), ('away', ''), ('draw', '')]),
}

# ---------------------------------------------------------------------------
# Cloudbet API (dual auth: v2=Bearer, v4=X-API-Key)
# ---------------------------------------------------------------------------
import requests

class CloudbetAPI:
    def __init__(self):
        cfg_path = os.path.join(SCRIPT_DIR, 'cloudbet_config.json')
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.api_key = cfg['api_key']
        self.currency = cfg.get('currency', 'USDC')
        # v2 session (Bearer token) for odds
        self.v2 = requests.Session()
        self.v2.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Accept': 'application/json',
        })
        # v4 session (X-API-Key) for bets
        self.v4 = requests.Session()
        self.v4.headers.update({
            'X-API-Key': self.api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })

    def get_odds(self, comp_key):
        """Fetch events+odds for a competition (v2 Bearer). Retries on 429/5xx."""
        for attempt in range(3):
            try:
                r = self.v2.get(f'{CB_BASE}/pub/v2/odds/competitions/{comp_key}', timeout=15)
                if r.status_code == 200:
                    return r.json().get('events', [])
                if r.status_code == 429:
                    wait = 2 ** (attempt + 2)  # 4s, 8s, 16s
                    log.warning('Odds 429 rate-limit %s — backoff %ds', comp_key, wait)
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    log.warning('Odds %d server error %s — backoff %ds',
                                r.status_code, comp_key, wait)
                    time.sleep(wait)
                    continue
                log.debug('Odds HTTP %d for %s', r.status_code, comp_key)
                return []
            except Exception as e:
                log.debug('Odds fetch failed %s: %s', comp_key, e)
                time.sleep(2 ** attempt)
        return []

    def place_straight(self, event_id, market_url, price, stake):
        """Place single bet (v4 X-API-Key). Returns response dict or None."""
        ref_id = str(uuid.uuid4())
        payload = {
            'referenceId': ref_id,
            'currency': self.currency,
            'stake': str(round(stake, 2)),
            'acceptPartialStake': True,
            'priceChange': {'value': 'ANY'},
            'selection': {
                'eventId': str(event_id),
                'marketUrl': market_url,
                'price': str(price),
            },
        }
        max_reoffers = 1
        reoffers = 0
        for attempt in range(3):
            backoff = 2 ** (attempt + 1)  # 2s, 4s, 8s
            try:
                r = self.v4.post(f'{CB_BASE}/pub/v4/bets/place/straight',
                                 json=payload, timeout=10)
                if not r.text or not r.text.strip():
                    log.warning('  Empty response (HTTP %d), attempt %d, backoff %ds',
                                r.status_code, attempt + 1, backoff)
                    time.sleep(backoff)
                    continue
                try:
                    resp = r.json()
                except ValueError:
                    log.warning('  Non-JSON response (HTTP %d), backoff %ds',
                                r.status_code, backoff)
                    time.sleep(backoff)
                    continue

                if resp.get('status') == 'INTERNAL_SERVER_ERROR' or resp.get('error'):
                    log.warning('  API error: %s, backoff %ds',
                                resp.get('error', '?'), backoff)
                    time.sleep(backoff)
                    continue

                st = resp.get('state', resp.get('status', ''))
                if st in ('ACCEPTED', 'PENDING_ACCEPTANCE'):
                    log.info('  Bet ACCEPTED: %s', resp.get('betId', '')[:12])
                    return resp
                reoffer = resp.get('selection', {}).get('reofferPrice')
                if reoffer and reoffers < max_reoffers:
                    reoffers += 1
                    log.info('  Price reoffer to %s, accepting...', reoffer)
                    payload['selection']['price'] = str(reoffer)
                    payload['referenceId'] = str(uuid.uuid4())
                    time.sleep(1.5)
                    continue
                err = resp.get('error', resp.get('message', st))
                log.warning('  Bet rejected: %s | full_resp: %s', err, str(resp)[:800])
                if resp.get('rejectionCode') == 'RESTRICTED' or resp.get('selection', {}).get('rejectionCode') == 'RESTRICTED':
                    return {'RESTRICTED': True, 'event_id': str(event_id), 'market_url': market_url}
                _rej_code = resp.get('rejectionCode') or resp.get('selection', {}).get('rejectionCode', '')
                if _rej_code == 'INSUFFICIENT_FUNDS':
                    return {'INSUFFICIENT_FUNDS': True}
                return None
            except Exception as e:
                log.error('  Bet placement error: %s', e)
                time.sleep(backoff)
        log.warning('  All 3 attempts exhausted for %s', market_url[:40])
        return None

    def place_parlay(self, selections, stake):
        """Place multi-leg parlay (v4). selections=[{eventId, marketUrl, price}]."""
        ref_id = str(uuid.uuid4())
        payload = {
            'referenceId': ref_id,
            'currency': self.currency,
            'stake': str(round(stake, 2)),
            'acceptPartialStake': True,
            'priceChange': {'value': 'ANY'},
            'selections': [
                {'eventId': str(s['eventId']), 'marketUrl': s['marketUrl'],
                 'price': str(s['price'])}
                for s in selections
            ],
        }
        try:
            r = self.v4.post(f'{CB_BASE}/pub/v4/bets/place/multiple',
                             json=payload, timeout=10)
            if not r.text or not r.text.strip():
                log.warning('Parlay empty response (HTTP %d)', r.status_code)
                return None
            try:
                resp = r.json()
            except ValueError:
                log.warning('Parlay non-JSON response: %s', r.text[:100])
                return None
            st = resp.get('state', resp.get('status', ''))
            if st in ('ACCEPTED', 'PENDING_ACCEPTANCE'):
                log.info('  Parlay ACCEPTED: %s', resp.get('betId', '')[:12])
                return resp
            # Handle reoffer
            reoffer_sels = resp.get('selections', [])
            has_reoffer = any(s.get('reofferPrice') for s in reoffer_sels)
            if has_reoffer:
                new_sels = []
                for orig, reoff in zip(payload['selections'], reoffer_sels):
                    rp = reoff.get('reofferPrice', orig['price'])
                    new_sels.append({'eventId': orig['eventId'],
                                     'marketUrl': orig['marketUrl'],
                                     'price': str(rp)})
                payload['selections'] = new_sels
                payload['referenceId'] = str(uuid.uuid4())
                log.info('  Parlay reoffer, retrying...')
                time.sleep(1.5)
                try:
                    r2 = self.v4.post(f'{CB_BASE}/pub/v4/bets/place/multiple',
                                      json=payload, timeout=10)
                    resp2 = r2.json()
                    st2 = resp2.get('state', resp2.get('status', ''))
                    if st2 in ('ACCEPTED', 'PENDING_ACCEPTANCE'):
                        log.info('  Parlay ACCEPTED on reoffer: %s', resp2.get('betId', '')[:12])
                        return resp2
                    log.warning('  Parlay rejected after reoffer: %s', resp2.get('error', st2))
                except Exception as e2:
                    log.error('  Parlay reoffer error: %s', e2)
                return None
            log.warning('Parlay rejected: %s', resp.get('error', st))
        except Exception as e:
            log.error('Parlay error: %s', e)
        return None

    def get_bets(self, settled_only=False, limit=50, days_back=30):
        """Get bet history (v4 X-API-Key). Looks back `days_back` days."""
        params = {'limit': limit}
        if settled_only:
            params['isSettled'] = 'true'
        from_dt = (datetime.now(tz=None) - timedelta(days=days_back)).strftime('%Y-%m-%dT00:00:00Z')
        params['from'] = from_dt
        try:
            r = self.v4.get(f'{CB_BASE}/pub/v4/bets', params=params, timeout=15)
            if r.status_code == 200:
                data = r.json() if r.text.strip() else {}
                return data.get('items', [])
            log.warning('Get bets HTTP %d: %s', r.status_code, r.text[:200] if r.text else '')
        except Exception as e:
            log.error('Get bets error: %s', e)
        return []

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def load_state():
    default = {
        'bankroll': 63.0, 'daily_staked': 0.0, 'daily_date': '',
        'last_scan': '', 'last_result_check': '',
        'active_bets': [], 'settled_today': [],
        'daily_pnl': 0.0, 'total_pnl': 42.83,
        'wins': 13, 'losses': 1, 'consecutive_losses': 0,
        'bets_placed_today': 0,
        'bankroll_by_currency': {'USDC': INITIAL_DEPOSIT_USDC, 'USDT': INITIAL_DEPOSIT_USDT},
    }
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        for k, v in default.items():
            state.setdefault(k, v)
        # Restore persisted strategy params
        global MAX_TOTAL_EXPOSURE, MIN_EDGE, MIN_CONF
        if 'persisted_max_exposure' in state:
            MAX_TOTAL_EXPOSURE = round(float(state['persisted_max_exposure']), 2)
        # Daily reset
        today = datetime.now().strftime('%Y-%m-%d')
        if state.get('daily_date') != today:
            state['daily_staked'] = 0.0
            state['daily_pnl'] = 0.0
            state['settled_today'] = []
            state['bets_placed_today'] = 0
            state['daily_date'] = today
            state['football_parlay_placed_today'] = False
            state['mixed_parlay_placed_today'] = False
            state['tennis_parlay_placed_today'] = False
            _rejected_keys.clear()
            state["restricted_event_ids"] = []  # Reset daily - stale events cause silent skips
        return state
    except Exception:
        # Try rolling auto-backup before falling back to stale hardcoded defaults
        _bak = STATE_FILE + '.bak_auto'
        if os.path.exists(_bak):
            try:
                with open(_bak) as _f:
                    state = json.load(_f)
                log.warning('STATE: primary corrupt/missing — loaded from .bak_auto')
                for k, v in default.items():
                    state.setdefault(k, v)
                return state
            except Exception:
                pass
        log.warning('STATE: all state sources failed — starting from hardcoded defaults')
        default['daily_date'] = datetime.now().strftime('%Y-%m-%d')
        return default

def save_state(state):
    state['updated'] = datetime.now().isoformat()
    _tmp = STATE_FILE + '.tmp'
    with open(_tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(_tmp, STATE_FILE)  # atomic on Linux — safe against mid-write corruption
    # Rolling auto-backup (previous cycle) — used as fallback in load_state
    try:
        import shutil as _sh
        _sh.copy2(STATE_FILE, STATE_FILE + '.bak_auto')
    except Exception:
        pass

def reconcile_engine_state():
    """Sync picks/engine_state.json pending bets into auto state.
    The old bet_engine had its own state file — import any pending bets once."""
    engine_path = os.path.join(SCRIPT_DIR, 'picks', 'engine_state.json')
    marker_path = os.path.join(SCRIPT_DIR, 'picks', '.reconciled')
    if os.path.exists(marker_path) or not os.path.exists(engine_path):
        return
    try:
        with open(engine_path) as f:
            eng = json.load(f)
        pending = [b for b in eng.get('active_bets', []) if b.get('status') == 'PENDING']
        if not pending:
            # Mark reconciled
            with open(marker_path, 'w') as f:
                f.write(datetime.now().isoformat())
            return
        state = load_state()
        existing_ids = {b.get('bet_id') for b in state.get('active_bets', [])}
        imported = 0
        for b in pending:
            bid = b.get('betId', b.get('bet_id', ''))
            if bid in existing_ids:
                continue
            if _save_bet(state, bid, b.get('match','?'), b.get('market',''), 'soccer',
                         b.get('odds',0), b.get('stake',0), source='engine_import'):
                imported += 1
        if imported:
            save_state(state)
            log.info('Reconciled %d pending bets from engine_state.json', imported)
        with open(marker_path, 'w') as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        log.warning('Reconcile failed: %s', e)

# ---------------------------------------------------------------------------
# Kelly stake calculator
# ---------------------------------------------------------------------------
def _get_dynamic_kelly_fraction(base_frac=KELLY_FRAC, window=20):
    """
    Adjust Kelly fraction based on recent performance.
    Uses last `window` settled bets from predictions_log.jsonl.

    Recent ROI > +15%  -> increase Kelly to 0.35 (betting well)
    Recent ROI  0-15%  -> keep base 0.25
    Recent ROI -10-0%  -> reduce to 0.20 (be cautious)
    Recent ROI < -10%  -> reduce to 0.15 (protect bankroll)
    < 5 settled bets   -> use base (not enough data)
    """
    try:
        if not os.path.exists(PREDICTIONS_FILE):
            return base_frac
        lines = open(PREDICTIONS_FILE).readlines()
        settled = []
        for ln in lines:
            try:
                e = json.loads(ln)
                if e.get('result') in ('WIN', 'LOSS'):
                    settled.append(e)
            except Exception:
                pass
        recent = settled[-window:]
        if len(recent) < 5:
            return base_frac
        total_staked = sum(float(b.get('stake', 0)) for b in recent)
        total_pnl = sum(float(b.get('win_loss', 0)) for b in recent)
        if total_staked <= 0:
            return base_frac
        recent_roi = total_pnl / total_staked
        if recent_roi > 0.15:
            frac = min(0.35, base_frac * 1.4)
            log.info('  [Kelly] Recent ROI +%.1f%% -> Kelly %.2f (UP)', recent_roi*100, frac)
        elif recent_roi > 0:
            frac = base_frac
        elif recent_roi > -0.10:
            frac = max(0.20, base_frac * 0.80)
            log.info('  [Kelly] Recent ROI %.1f%% -> Kelly %.2f (DOWN)', recent_roi*100, frac)
        else:
            frac = max(0.15, base_frac * 0.60)
            log.info('  [Kelly] Recent ROI %.1f%% -> Kelly %.2f (PROTECT)', recent_roi*100, frac)
        return round(frac, 3)
    except Exception:
        return base_frac



def _auto_tune_strategy(state):
    """
    Auto-adjust strategy parameters based on recent performance.
    Primary source: sibila_picks DB (placed=1, settled). Fallback: predictions_log.jsonl.
    Adjusts: MIN_EDGE, MIN_CONF, MAX_PER_BET, MAX_TOTAL_EXPOSURE, MAX_BETS_PER_SCAN, SPORT_KELLY.
    """
    global MIN_EDGE, MIN_CONF, MAX_PER_BET, MAX_TOTAL_EXPOSURE, MAX_BETS_PER_SCAN, SPORT_KELLY
    try:
        # ── Primary: read from Sibila DB ─────────────────────────────────────
        settled = []
        _sibila_db = os.path.join(SCRIPT_DIR, 'sibila.db')
        _use_sibila = False
        if os.path.exists(_sibila_db):
            try:
                import sqlite3 as _sq3
                _conn = _sq3.connect(_sibila_db)
                _rows = _conn.execute("""
                    SELECT sport, prob_model, edge, odds, result, pnl, real_stake, bet_id
                    FROM sibila_picks
                    WHERE placed=1 AND result IN ('WIN','LOSS')
                    ORDER BY ts DESC LIMIT 200
                """).fetchall()
                _conn.close()
                for r in _rows:
                    sp, pm, eg, odds, result, pnl, stake, bid = r
                    settled.append({
                        'sport': sp or 'unknown',
                        'prob_model': pm or 0,
                        'edge': eg or 0,
                        'odds': odds or 2.0,
                        'result': result,
                        'stake': stake or 0,
                        'win_loss': (float(stake)*(float(odds or 2.0)-1.0)) if result=='WIN' and stake else (-float(stake) if result=='LOSS' and stake else 0),
                        'bet_id': bid,
                    })
                _use_sibila = len(settled) >= 15
            except Exception as _e:
                log.debug('AutoTune Sibila read error: %s', _e)

        if not _use_sibila:
            # Fallback: predictions_log.jsonl
            if not os.path.exists(PREDICTIONS_FILE):
                return
            lines = open(PREDICTIONS_FILE).readlines()
            for ln in lines:
                try:
                    e = json.loads(ln)
                    if e.get('result') in ('WIN', 'LOSS'):
                        if e.get('stake', 0) > 0 and e.get('bet_id'):
                            settled.append(e)
                except Exception:
                    pass

        if len(settled) < 10:
            return  # Not enough data

        recent = settled[:100]  # Already ordered DESC, take 100 most recent
        wins = sum(1 for b in recent if b.get('result') == 'WIN')
        losses = len(recent) - wins
        win_rate = wins / len(recent)
        total_staked = sum(float(b.get('stake', 0)) for b in recent)
        total_pnl = sum(float(b.get('win_loss', 0)) for b in recent)
        roi = total_pnl / total_staked if total_staked > 0 else 0

        # Analyze edge accuracy: were high-edge bets actually winning more?
        high_edge = [b for b in recent if float(b.get('edge', 0)) > 0.10]
        low_edge = [b for b in recent if 0.05 <= float(b.get('edge', 0)) <= 0.10]
        high_wr = sum(1 for b in high_edge if b.get('result') == 'WIN') / max(len(high_edge), 1)
        low_wr = sum(1 for b in low_edge if b.get('result') == 'WIN') / max(len(low_edge), 1)

        # Per-sport analysis
        football = [b for b in recent if b.get('sport') == 'soccer']
        tennis = [b for b in recent if b.get('sport') == 'tennis']
        baseball = [b for b in recent if b.get('sport') == 'baseball']
        fb_wr = sum(1 for b in football if b.get('result') == 'WIN') / max(len(football), 1)
        tn_wr = sum(1 for b in tennis if b.get('result') == 'WIN') / max(len(tennis), 1)
        bb_wr = sum(1 for b in baseball if b.get('result') == 'WIN') / max(len(baseball), 1)

        old_edge = MIN_EDGE
        old_conf = MIN_CONF
        old_exp = MAX_TOTAL_EXPOSURE

        # --- AUTO-ADJUST RULES ---

        # 1. MIN_EDGE: if low-edge bets lose too much, raise threshold
        if low_wr < 0.40 and len(low_edge) >= 15:  # umbral mas bajo, muestra minima mayor
            MIN_EDGE = min(0.10, MIN_EDGE + 0.005)  # sube mas lento
        elif low_wr > 0.65 and len(low_edge) >= 15:
            MIN_EDGE = max(0.06, MIN_EDGE - 0.005)  # baja mas lento, piso mas alto
        # else: insufficient data -- leave MIN_EDGE unchanged

        # 2. MIN_CONF: adjust based on baseball-specific WR (tennis WR=73% must not poison baseball threshold)
        if len(baseball) >= 20:
            if bb_wr > 0.60:
                MIN_CONF = max(0.55, MIN_CONF - 0.01)  # piso 0.55: permite raw probs 55-60%
            elif bb_wr < 0.40:
                MIN_CONF = min(0.62, MIN_CONF + 0.01)  # cap 0.62: Platt cal comprime probs, mayor cap mata picks
        elif len(recent) >= 30 and len(baseball) < 20:
            # Fall back to overall WR only if insufficient baseball sample
            if win_rate > 0.75:
                MIN_CONF = max(0.55, MIN_CONF - 0.01)
            elif win_rate < 0.40:
                MIN_CONF = min(0.62, MIN_CONF + 0.01)
        # else: leave MIN_CONF unchanged

        # 3. MAX_TOTAL_EXPOSURE: expand if winning, contract if losing
        if len(recent) >= 10:
            if roi > 0.15 and win_rate > 0.65:
                MAX_TOTAL_EXPOSURE = min(0.60, MAX_TOTAL_EXPOSURE + 0.05)
            elif roi < -0.05:
                MAX_TOTAL_EXPOSURE = max(0.25, MAX_TOTAL_EXPOSURE - 0.10)
        # else: leave MAX_TOTAL_EXPOSURE unchanged

        # 4. MAX_PER_BET: scale with confidence
        if len(recent) >= 10:
            if roi > 0.10 and win_rate > 0.70:
                MAX_PER_BET = min(0.08, 0.05 * 1.3)
            elif roi < -0.10:
                MAX_PER_BET = max(0.03, 0.05 * 0.70)

        # 5. MAX_BETS_PER_SCAN: more volume if profitable, less if not
        if len(recent) >= 10:
            if roi > 0.10:
                MAX_BETS_PER_SCAN = 12
            elif roi < -0.05:
                MAX_BETS_PER_SCAN = 5

        # Per-sport Kelly fine-tuning from Sibila data
        for _sport, _min_wr, _max_wr, _step in [
            ('tennis',   0.72, 0.95, 0.02),
            ('baseball', 0.38, 0.55, 0.02),
            ('soccer',   0.50, 0.70, 0.02),
            ('basketball', 0.45, 0.65, 0.02),
        ]:
            _sp_bets = [b for b in recent if b.get('sport') == _sport]
            if len(_sp_bets) < 15:
                continue
            _sp_settled = [b for b in _sp_bets if b.get('result') in ('WIN','LOSS')]
            if not _sp_settled:
                continue
            _sp_wr = sum(1 for b in _sp_settled if b.get('result') == 'WIN') / len(_sp_settled)
            _cur_k = SPORT_KELLY.get(_sport, 0.25)
            if _sp_wr > _max_wr:
                SPORT_KELLY[_sport] = min(_cur_k + _step, 0.50)
            elif _sp_wr < _min_wr:
                SPORT_KELLY[_sport] = max(_cur_k - _step, 0.05)

        # Store tuning state for Obsidian/logging
        state['_autotune'] = {
            'win_rate': round(win_rate, 3),
            'roi': round(roi, 3),
            'high_edge_wr': round(high_wr, 3),
            'low_edge_wr': round(low_wr, 3),
            'fb_wr': round(fb_wr, 3),
            'tn_wr': round(tn_wr, 3),
            'bb_wr': round(bb_wr, 3),
            'sample_size': len(recent),
            'min_edge': MIN_EDGE,
            'min_conf': MIN_CONF,
            'max_exposure': MAX_TOTAL_EXPOSURE,
            'max_per_bet': MAX_PER_BET,
            'max_bets_scan': MAX_BETS_PER_SCAN,
        }

        state['persisted_max_exposure'] = round(MAX_TOTAL_EXPOSURE, 2)
        if MIN_EDGE != old_edge or MIN_CONF != old_conf or MAX_TOTAL_EXPOSURE != old_exp:
            log.info('[AutoTune] WR=%.0f%% ROI=%+.1f%% -> edge=%.0f%% conf=%.0f%% exposure=%.0f%%',
                     win_rate*100, roi*100, MIN_EDGE*100, MIN_CONF*100, MAX_TOTAL_EXPOSURE*100)

        # Weekly backtest: optimize params from historical data
        _bt_cache = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'backtest_results.json')
        _run_bt = False
        if os.path.exists(_bt_cache):
            import time as _t
            if _t.time() - os.path.getmtime(_bt_cache) > 604800:  # 7 days
                _run_bt = True
        else:
            _run_bt = len(settled) >= 30

        # Calibration report (also weekly)
        try:
            from oraculo_calibration import full_calibration_report
            _cal = full_calibration_report()
            if _cal:
                state['_calibration'] = {
                    'brier': _cal['overall']['brier_score'],
                    'ece': _cal['overall']['ece'],
                    'diagnosis': _cal.get('diagnosis', []),
                }
        except Exception:
            pass

        if _run_bt:
            try:
                from oraculo_backtest import Backtester
                _bt = Backtester(initial_bankroll=11.37)
                _bets = _bt.load_settled_bets()
                if len(_bets) >= 20:
                    _best = _bt.recommend_params(_bets)
                    if _best:
                        # Apply recommended params (blended 50% current + 50% backtest)
                        MIN_EDGE = round((MIN_EDGE + _best.get('min_edge', MIN_EDGE)) / 2, 3)
                        MIN_CONF = round((MIN_CONF + _best.get('min_conf', MIN_CONF)) / 2, 3)
                        log.info('[Backtest] Recommended: edge=%.0f%% conf=%.0f%% kelly=%.2f',
                                 _best.get('min_edge',0)*100, _best.get('min_conf',0)*100,
                                 _best.get('kelly_frac',0.25))
            except Exception as e:
                log.debug('Backtest error: %s', e)

    except Exception as e:
        log.debug('AutoTune error: %s', e)

def kelly_stake(bankroll, prob, odds, consecutive_losses=0, sport=None):
    """Dynamic Kelly with circuit breaker, loss streak, and recent ROI adaptation."""
    if bankroll < CIRCUIT_BREAKER:
        return 0
    b = odds - 1
    if b <= 0:
        return 0
    kelly = (b * prob - (1 - prob)) / b
    if sport and sport in SPORT_KELLY:
        dyn_frac = SPORT_KELLY[sport]
    else:
        dyn_frac = _get_dynamic_kelly_fraction()
    kelly = max(0, kelly * dyn_frac)
    kelly = min(kelly, MAX_PER_BET)
    stake = bankroll * kelly
    if consecutive_losses >= LOSS_STREAK_LIMIT:
        stake *= LOSS_STREAK_FACTOR
        log.info('  Loss streak %d: stake reduced to $%.2f', consecutive_losses, stake)
    return max(round(stake, 2), 0) if stake >= MIN_STAKE else 0

# ---------------------------------------------------------------------------
# Football market scanner
# ---------------------------------------------------------------------------
def scan_football(api, state, dry_run=False):
    _cal = _load_calibration()
    """Scan Cloudbet soccer markets, find value bets."""
    # Load enhanced data sources
    _xg_data = None
    try:
        from oraculo_xg import load_xg_data, get_team_xg, xg_adjusted_prob, fetch_match_results, load_xg_data_v2
        from oraculo_scrape_guard import guarded_fetch
        # Primary: Firecrawl + Understat (real xG, no IP ban)
        try:
            from oraculo_xg_firecrawl import load_xg_firecrawl
            _xg_data = load_xg_firecrawl()
        except Exception as _e_fc:
            log.warning('Firecrawl xG failed: %s', _e_fc)
            _xg_data = None
        else:
            if _xg_data:
                leagues = [k for k in _xg_data if not k.startswith('_')]
                log.info('Firecrawl xG loaded: %d leagues (%s)', len(leagues), ', '.join(leagues))
        if not _xg_data:
            # Fallback: proxy-based FBref
            try:
                from oraculo_xg import load_xg_via_proxy
                _xg_data = guarded_fetch('fbref_proxy', load_xg_via_proxy)
            except Exception:
                _xg_data = None
        if not _xg_data:
            _xg_data = guarded_fetch('xg_data', load_xg_data_v2)
        if not _xg_data:
            # Ultimate fallback: football-data.co.uk + goal-based pseudo-xG
            from oraculo_xg import fetch_footballdata_results, build_xg_from_results
            _fd_matches = guarded_fetch('footballdata', fetch_footballdata_results)
            if _fd_matches:
                _xg_data = build_xg_from_results(_fd_matches)
                log.info('Using football-data.co.uk pseudo-xG (%d leagues)', len(_xg_data))
        # Also fetch match results for Dixon-Coles training
        try:
            pass  # Results now fetched by load_xg_data_v2 via Understat
        except Exception:
            pass
    except Exception as e:
        log.debug('xG data unavailable: %s', e)

    _dc_model = None
    try:
        from oraculo_dixon_coles import DixonColesModel
        _dc_model = DixonColesModel()
        if not _dc_model.load():
            # Auto-train from xG cache (FBref match results)
            _dc_matches = []
            _xg_cache = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_matches.json')
            if os.path.exists(_xg_cache):
                with open(_xg_cache) as f:
                    _dc_matches = json.load(f)
            if len(_dc_matches) >= 50:
                _dc_model.train(_dc_matches)
                _dc_model.save()
                log.info('Dixon-Coles auto-trained from %d matches', len(_dc_matches))
            else:
                log.debug('Dixon-Coles: insufficient match data (%d < 50)', len(_dc_matches))
                _dc_model = None
    except Exception as e:
        log.debug('Dixon-Coles unavailable: %s', e)

    _injury_data = None
    try:
        from oraculo_injuries import load_injuries
        _injury_data = load_injuries()
    except Exception as e:
        log.debug('Injury data unavailable: %s', e)

    _gbm = None
    try:
        from oraculo_gbm import GBMEnsemble, build_features
        _gbm = GBMEnsemble()
        if not _gbm.load():
            _gbm = GBMEnsemble()  # Will use weighted blend fallback
    except Exception as e:
        log.debug('GBM unavailable: %s', e)

    # Load confirmed lineups from Sofascore
    _lineup_events = None
    try:
        from oraculo_lineups import load_lineups_for_today, get_confirmed_lineup, lineup_adjustment
        _lineup_events = load_lineups_for_today()
        if _lineup_events:
            log.info('Sofascore: %d events loaded for lineup matching', len(_lineup_events))
    except Exception as e:
        log.debug('Lineups unavailable: %s', e)

    log.info('=== SCANNING FOOTBALL MARKETS ===')
    picks = []

    # Try to load ML model — cached on function to avoid reloading 49MB pkl each cycle
    mp = None
    try:
        from oraculo_market_predictor import MarketPredictor
        if not hasattr(scan_football, '_mp_cache') or scan_football._mp_cache is None:
            _mp_new = MarketPredictor('picks_global')
            if _mp_new.load():
                scan_football._mp_cache = _mp_new
                gc.collect()
                log.info('MarketPredictor loaded and cached (picks_global)')
            else:
                scan_football._mp_cache = None
                log.warning('No trained model, using Poisson/Elo only')
        mp = scan_football._mp_cache
    except Exception as e:
        log.warning('MarketPredictor unavailable: %s', e)
        scan_football._mp_cache = None

    # Load math models
    poisson, elo = None, None
    try:
        from oraculo_models_advanced import PoissonGoalModel, EloRating
        import pickle
        models_dir = os.path.join(SCRIPT_DIR, 'models')
        poisson = PoissonGoalModel()
        elo = EloRating()
        pp = os.path.join(models_dir, 'poisson_state.pkl')
        ep = os.path.join(models_dir, 'elo_state.pkl')
        if os.path.exists(pp):
            with open(pp, 'rb') as f:
                poisson.__dict__.update(pickle.load(f))
        if os.path.exists(ep):
            with open(ep, 'rb') as f:
                elo.__dict__.update(pickle.load(f))
    except Exception as e:
        log.warning('Math models unavailable: %s', e)

    # Seed Poisson ratings from Firecrawl/Understat real xG
    if _xg_data and poisson and poisson._fitted:
        try:
            from oraculo_models_advanced import _normalize_team as _nt
            _all_xg = {}
            for _fkey in ["PL", "SA", "BL1", "FL1", "PD"]:
                _ld = _xg_data.get(_fkey, {})
                if not _ld: continue
                _xg_vals = [_t.get("xg", 0) / max(_t.get("mp", 1), 1) for _t in _ld.values()]
                _lavg = sum(_xg_vals) / len(_xg_vals) if _xg_vals else 1.3
                for _tn, _st in _ld.items():
                    _mp = max(_st.get("mp", 1), 1)
                    _all_xg[_tn] = (_st.get("xg", 0)/_mp, _st.get("xga", 0)/_mp, _lavg)
            _known = list(poisson.attack.keys())
            _upd = 0
            for _tn, (_xg_pg, _xga_pg, _lavg) in _all_xg.items():
                _nn = _nt(_tn, _known)
                poisson.attack[_nn] = _xg_pg / max(_lavg, 0.5)
                poisson.defense[_nn] = _xga_pg / max(_lavg, 0.5)
                _upd += 1
            log.info("Poisson seeded with real xG: %d teams", _upd)
        except Exception as _ex:
            log.warning("Poisson xG seeding failed: %s", _ex)

    for league, comp_key in CB_COMPS.items():
        events = api.get_odds(comp_key)
        if not events:
            continue
        league_mkts = LEAGUE_MARKETS.get(league, ['over25'])
        log.info('  %s: %d events, markets: %s', league, len(events), league_mkts)

        # Filter: only events within 24h
        cutoff_limit = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for ev in events:
            if not ev or not isinstance(ev, dict):
                continue
            # Skip events beyond 24h window
            ev_cutoff = ev.get('cutoffTime', '')
            if ev_cutoff and ev_cutoff > cutoff_limit:
                continue
            home = (ev.get('home') or {}).get('name', '')
            away = (ev.get('away') or {}).get('name', '')
            eid = str(ev.get('id', ''))
            markets = ev.get('markets', {})
            if not home or not away:
                continue

            for mkt_key in league_mkts:
                # --- ASIAN HANDICAP ---
                if mkt_key == 'asian_handicap' and poisson:
                    try:
                        lh, la = poisson.predict_lambda(home, away)
                        model_margin = lh - la
                        ah_data = markets.get('soccer.asian_handicap', {})
                        for sub in ah_data.get('submarkets', {}).values():
                            for sel in sub.get('selections', []):
                                if sel.get('status') != 'SELECTION_ENABLED':
                                    continue
                                price = sel.get('price', 0)
                                murl = sel.get('marketUrl', '')
                                if price < 1.3 or not murl:
                                    continue
                                # Parse handicap line from params
                                params = sel.get('params', '')
                                try:
                                    line = float(params.split('=')[-1]) if '=' in params else 0
                                except Exception:
                                    line = 0
                                implied = 1.0 / price
                                outcome = sel.get('outcome', '')
                                # Poisson scoreline matrix for true AH probability
                                try:
                                    model_prob = poisson.prob_ah(home, away, line, outcome)
                                except Exception:
                                    if outcome == 'home':
                                        model_prob = min(0.85, max(0.35, 0.5 + (model_margin + line) * 0.15))
                                    else:
                                        model_prob = min(0.85, max(0.35, 0.5 + (-model_margin - line) * 0.15))
                                edge = model_prob - implied
                                # GBM ensemble blend (if available)
                                if _gbm is not None:
                                    try:
                                        _feats = build_features(
                                            home, away, league,
                                            dc_model=_dc_model, xg_data=_xg_data,
                                            injury_data=_injury_data)
                                        _gbm_pred = _gbm.predict(_feats)
                                        _gbm_prob = _gbm_pred.get(side + '_win', model_prob) if side != 'draw' else _gbm_pred.get('draw', model_prob)
                                        # Blend: 50% GBM + 30% original + 20% DC
                                        model_prob = 0.5 * _gbm_prob + 0.3 * model_prob + 0.2 * (_dc_model.predict(home, away).get(side + '_win' if side != 'draw' else 'draw', model_prob) if _dc_model else model_prob)
                                        edge = model_prob * odds_val - 1
                                    except Exception:
                                        pass
                                if edge > MIN_EDGE and model_prob > MIN_CONF and edge < 0.45 and model_prob < 0.92:
                                    picks.append({
                                        'match': f'{home} vs {away}', 'league': league,
                                        'event_id': eid, 'market_url': murl,
                                        'price': price, 'label': f'AH {outcome} {line:+.1f}',
                                        'model_prob': model_prob, 'edge': edge,
                                        'sport': 'soccer',
                                    })
                    except Exception:
                        pass
                    continue

                # --- OVER/UNDER, CORNERS ---
                cb_info = CB_MARKETS_MAP.get(mkt_key)
                if not cb_info:
                    continue
                cb_mkt_key, outcomes = cb_info

                # Get model prediction
                model_prob_over = None
                _peru_ml_ok = False
                if mp:
                    try:
                        preds = mp.predict_match(home, away)
                        if preds and mkt_key in preds:
                            model_prob_over = preds[mkt_key].get('prob_yes')
                    except Exception:
                        pass
                # Peru Liga 1: dedicated ML model with altitude + form features
                if league == 'PER' and mkt_key in ('over25', 'btts_yes', 'btts_no'):
                    try:
                        from oraculo_peru import predict_peru, load_peru_matches, TEAM_CITY as _PC
                        _peru_ctx = load_peru_matches()
                        _venue = next(
                            (c for t, c in _PC.items()
                             if t.lower() in home.lower() or home.lower() in t.lower()), '')
                        _res = predict_peru(
                            {'home_team': home, 'away_team': away, 'venue_city': _venue},
                            _peru_ctx)
                        if _res:
                            if mkt_key == 'over25':
                                _p = _res.get('over25', {}).get('prob_yes')
                            elif mkt_key == 'btts_yes':
                                _p = _res.get('btts', {}).get('prob_yes')
                            else:
                                _p = _res.get('btts', {}).get('prob_no')
                            if _p is not None:
                                model_prob_over = round(float(_p), 4)
                                _peru_ml_ok = True
                                log.debug('PER ML %s %s vs %s: p=%.3f',
                                          mkt_key, home, away, model_prob_over)
                    except Exception as _e:
                        log.debug('PER ML failed: %s', _e)
                # Fallback to Poisson for over25
                if model_prob_over is None and mkt_key == 'over25' and poisson:
                    try:
                        p_mkts = poisson.predict_markets(home, away)
                        model_prob_over = p_mkts.get('over25', 0.5)
                    except Exception:
                        pass
                # Altitude multiplier: only when ML model unavailable (Poisson fallback path)
                if league == 'PER' and not _peru_ml_ok and model_prob_over is not None:
                    try:
                        from oraculo_peru import TEAM_CITY as _PC, ALTITUDE as _PA
                        _hcity = next(
                            (c for t, c in _PC.items()
                             if t.lower() in home.lower() or home.lower() in t.lower()), '')
                        _valt = _PA.get(_hcity, 0)
                        _mult = (1.12 if _valt > 3500 else
                                 1.08 if _valt > 2800 else
                                 1.04 if _valt > 2000 else 1.0)
                        if _mult > 1.0:
                            model_prob_over = round(min(model_prob_over * _mult, 0.88), 4)
                            log.debug('PER altitude fallback @%.0fm (%s): x%.2f -> p=%.3f',
                                      _valt, _hcity, _mult, model_prob_over)
                    except Exception:
                        pass
                # BTTS: P(home>=1)*P(away>=1) -- bookmakers less sharp here
                if model_prob_over is None and mkt_key in ('btts_yes', 'btts_no') and poisson:
                    try:
                        from math import exp
                        _lh, _la = poisson.predict_lambda(home, away)
                        _p_btts = (1 - exp(-_lh)) * (1 - exp(-_la))
                        model_prob_over = _p_btts if mkt_key == 'btts_yes' else (1 - _p_btts)
                    except Exception:
                        pass
                if model_prob_over is None:
                    continue

                # Weather adjustment (rain/wind suppress over/btts probability)
                if mkt_key in ('over25', 'over15', 'btts_yes', 'btts_no'):
                    try:
                        from oraculo_xg_weather import get_forecast_adjustment as _wfadj
                        _wadj = _wfadj(home, ev_cutoff)
                        if _wadj != 1.0:
                            if mkt_key in ('over25', 'over15', 'btts_yes'):
                                model_prob_over = round(model_prob_over * _wadj, 4)
                            else:  # under/btts_no: rain HELPS
                                model_prob_over = round(min(model_prob_over / _wadj, 0.95), 4)
                            log.debug('Weather adj %.3f for %s %s: p=%.3f',
                                      _wadj, home, mkt_key, model_prob_over)
                    except Exception:
                        pass

                mkt_data = markets.get(cb_mkt_key, {})
                for outcome, params_filter in outcomes:
                    prob = model_prob_over if outcome == 'over' else (1 - model_prob_over)
                    best_price, best_url = None, ''
                    for sub in mkt_data.get('submarkets', {}).values():
                        for sel in sub.get('selections', []):
                            if sel.get('status') != 'SELECTION_ENABLED':
                                continue
                            if sel.get('outcome') != outcome:
                                continue
                            if params_filter and params_filter not in sel.get('params', ''):
                                continue
                            p = sel.get('price', 0)
                            if p > 1.2 and (best_price is None or p > best_price):
                                best_price = p
                                best_url = sel.get('marketUrl', '')
                    if not best_price:
                        continue
                    implied = 1.0 / best_price
                    edge = prob - implied
                    if edge > MIN_EDGE and prob > MIN_CONF and edge < 0.45 and prob < 0.92:
                        picks.append({
                            'match': f'{home} vs {away}', 'league': league,
                            'event_id': eid, 'market_url': best_url,
                            'price': best_price, 'label': f'{mkt_key} {outcome}',
                            'model_prob': prob, 'edge': edge,
                            'sport': 'soccer',
                        })


    # --- INTERNATIONAL / FIFA WC QUALIFIERS ---
    intl_elo = None
    try:
        from oraculo_intl_elo import IntlElo
        intl_elo = IntlElo()
        intl_elo.load()
    except Exception as e:
        log.debug('IntlElo unavailable: %s', e)

    if intl_elo:
        INTL_COMPS = {
            'FIFA_WC': 'soccer-international-world-cup',
            'UEFA_NL': 'soccer-international-uefa-nations-league',
            'COPA_AM': 'soccer-south-america-copa-america',

            'CONMEBOL_WCQ': 'soccer-south-america-world-cup-qualification',
            'UEFA_EURO': 'soccer-international-european-championship',
            'CHAMPIONS': 'soccer-international-clubs-uefa-champions-league',
            'EUROPA_L': 'soccer-international-clubs-uefa-europa-league',
            'CONF_L': 'soccer-international-clubs-t6eeb-uefa-europa-conference-league',
            'LIBERTADORES': 'soccer-international-clubs-copa-libertadores',
            'SUDAMERICANA': 'soccer-international-clubs-copa-sudamericana',
        }
        INTL_MARKETS = ['ft_result', 'over25']
        BLACKLIST_TEAMS = ['Ukraine']  # Skip teams with war/displacement context
        cutoff_intl = (datetime.now(timezone.utc) + timedelta(hours=36)).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Pre-load WC round-3 collusion risk (loaded once, checked per match)
        _wc_collusion = {}
        try:
            import json as _wc_json
            _col_path = os.path.join(SCRIPT_DIR, 'wc2026/collusion_risk.json')
            _wc_collusion = _wc_json.load(open(_col_path))
        except Exception:
            pass

        for intl_league, comp_key in INTL_COMPS.items():
            try:
                events = api.get_odds(comp_key)
            except Exception:
                continue
            if not events:
                continue
            log.info('  %s (intl): %d events', intl_league, len(events))
            for ev in events:
                if not ev or not isinstance(ev, dict):
                    continue
                ev_cutoff = ev.get('cutoffTime', '')
                if ev_cutoff and ev_cutoff > cutoff_intl:
                    continue
                home = (ev.get('home') or {}).get('name', '')
                away = (ev.get('away') or {}).get('name', '')
                eid  = str(ev.get('id', ''))
                if not home or not away:
                    continue
                if home in BLACKLIST_TEAMS or away in BLACKLIST_TEAMS:
                    continue
                markets = ev.get('markets', {})
                neutral = ev.get('neutral', False)

                # Get 1X2 odds
                ft_mkt = markets.get('soccer.match_odds', {})
                odds_1x2 = {}
                for sub_val in ft_mkt.get('submarkets', {}).values():
                    for s in sub_val.get('selections', []):
                        out = s.get('outcome', '')
                        price = float(s.get('price', 0) or 0)
                        murl  = s.get('marketUrl', '')
                        if out in ('home', 'draw', 'away') and price > 1.1:
                            odds_1x2[out] = (price, murl)

                # WC knockout detection: if draw not offered (R16/QF/SF/F), re-normalise
                _wc_knockout = False
                if intl_league == 'FIFA_WC' and 'draw' not in odds_1x2:
                    _wc_knockout = True

                # Get Over/Under 2.5 odds
                ou_mkt = markets.get('soccer.total_goals', {})
                odds_ou = {}
                for sub_val in ou_mkt.get('submarkets', {}).values():
                    for s in sub_val.get('selections', []):
                        param = str(s.get('params', ''))
                        out   = s.get('outcome', '')
                        price = float(s.get('price', 0) or 0)
                        murl  = s.get('marketUrl', '')
                        if '2.5' in param and price > 1.1:
                            odds_ou[out] = (price, murl)

                # Get BTTS odds (WC: already live on Cloudbet since May 27)
                odds_btts = {}
                if intl_league == 'FIFA_WC':
                    _btts_mkt = markets.get('soccer.both_teams_to_score', {})
                    for _sv in _btts_mkt.get('submarkets', {}).values():
                        for _s in _sv.get('selections', []):
                            _out = _s.get('outcome', '')
                            _pr  = float(_s.get('price', 0) or 0)
                            _mu  = _s.get('marketUrl', '')
                            if _out in ('yes', 'no') and _pr > 1.1:
                                odds_btts[_out] = (_pr, _mu)

                # Get 2H goals O/U 1.5 odds (live since May 27)
                odds_2h = {}
                if intl_league == 'FIFA_WC':
                    _h2_mkt = markets.get('soccer.total_goals_period_second_half', {})
                    for _sv in _h2_mkt.get('submarkets', {}).values():
                        for _s in _sv.get('selections', []):
                            _param = str(_s.get('params', ''))
                            _out   = _s.get('outcome', '')
                            _pr    = float(_s.get('price', 0) or 0)
                            _mu    = _s.get('marketUrl', '')
                            if '1.5' in _param and _pr > 1.1:
                                odds_2h[_out] = (_pr, _mu)

                # Compute model probabilities
                import math as _m
                try:
                    _is_wc = intl_league == 'FIFA_WC'
                    if _is_wc and hasattr(intl_elo, 'predict_match_wc'):
                        # DC model (neutral fix + altitude + player factors)
                        ph, pd, pa, _xg_h, _xg_a = intl_elo.predict_match_wc(home, away, True)
                        # Knockout: no draw in 90min — collapse pd into ph/pa proportionally
                        if _wc_knockout and pd > 0:
                            _renorm = 1.0 / (ph + pa) if (ph + pa) > 0 else 1.0
                            ph, pa, pd = ph * _renorm, pa * _renorm, 0.0
                        _xg_h *= 1.043  # WC xG calib: DC underestimates by 4.3% vs WC 2022
                        _xg_a *= 1.043
                        # O/U 2.5 from DC xG Poisson (unified model — no ELO fallback)
                        _pu25 = sum(
                            _m.exp(-_xg_h) * _xg_h**_i / _m.factorial(_i) *
                            _m.exp(-_xg_a) * _xg_a**_j / _m.factorial(_j)
                            for _i in range(4) for _j in range(4) if _i + _j <= 2
                        )
                        p_o25 = max(0.0, min(1.0, 1.0 - _pu25))
                        # BTTS from player-adjusted DC xG (injury factors applied, 66.7% WR in club soccer)
                        try:
                            from oraculo_wc_model import get_player_adjusted_xg as _paxg
                            _padj = _paxg(home, away)
                            _xg_h_adj, _xg_a_adj = float(_padj[0]), float(_padj[1])
                        except Exception:
                            _xg_h_adj, _xg_a_adj = _xg_h, _xg_a
                        p_btts_yes = max(0.0, min(1.0, (1 - _m.exp(-_xg_h_adj)) * (1 - _m.exp(-_xg_a_adj))))
                        p_btts_no  = 1.0 - p_btts_yes
                        # 2H goals: ~45% of player-adjusted xG
                        _xh2, _xa2 = _xg_h_adj * 0.45, _xg_a_adj * 0.45
                        _pu15_2h = sum(
                            _m.exp(-_xh2) * _xh2**i / _m.factorial(i) *
                            _m.exp(-_xa2) * _xa2**j / _m.factorial(j)
                            for i in range(2) for j in range(2) if i + j <= 1
                        )
                        p_o15_2h = max(0.0, min(1.0, 1.0 - _pu15_2h))
                        p_u15_2h = 1.0 - p_o15_2h
                    else:
                        ph, pd, pa = intl_elo.predict_match(home, away, neutral)
                        _is_wc = intl_league == 'FIFA_WC'
                        if _is_wc and hasattr(intl_elo, 'prob_over_wc'):
                            p_o25 = intl_elo.prob_over_wc(home, away, 2.5, neutral)
                        else:
                            p_o25 = intl_elo.prob_over(home, away, 2.5, neutral)
                        p_btts_yes = p_btts_no = None
                        p_o15_2h = p_u15_2h = None
                        _xg_h = _xg_a = 0.0
                    p_u25 = 1 - p_o25
                except Exception:
                    continue

                match_label = f'{home} vs {away}'

                # WC round-3 collusion guard (disgrace risk >= 8% → skip 1X2 + BTTS-Yes)
                _skip_1x2 = False
                _skip_btts_yes = False
                if intl_league == 'FIFA_WC' and _wc_collusion:
                    _ev_teams = {home, away}
                    for _cgv in _wc_collusion.values():
                        if ((_ev_teams == set(_cgv.get('round3_game1', []))) or
                                (_ev_teams == set(_cgv.get('round3_game2', [])))):
                            _pdis = _cgv.get('p_disgrace_setup', 0)
                            if _pdis >= 0.08:
                                _skip_1x2 = True
                                _skip_btts_yes = True
                                log.info('WC collusion guard %s p_disgrace=%.3f — 1X2+BTTS-Yes skipped',
                                         match_label, _pdis)
                            break

                # 1X2 edges
                for out_key, prob in [('home', ph), ('draw', pd), ('away', pa)]:
                    if out_key in odds_1x2:
                        price, murl = odds_1x2[out_key]
                        edge = prob * price - 1
                        # WC Fase C: stricter thresholds for result_1x2 (historical -28.2% ROI on non-WC)
                        _is_wc = intl_league == 'FIFA_WC'
                        _e_min = WC_MIN_EDGE if _is_wc else MIN_EDGE
                        _c_min = WC_MIN_CONF if _is_wc else MIN_CONF
                        if not _skip_1x2 and edge >= _e_min and prob >= _c_min and edge < 0.45 and prob < 0.92:
                            _pick = {
                                'match': match_label, 'league': intl_league,
                                'event_id': eid, 'market_url': murl,
                                'price': price, 'label': out_key.title() + ' Win',
                                'model_prob': prob, 'edge': edge,
                                'sport': 'soccer', 'intl': True,
                            }
                            if _is_wc:
                                _pick['_wc_phase_c'] = True
                            picks.append(_pick)
                            # A/B log: model_A = current
                            try:
                                from oraculo_backtest import ABTester
                                ABTester().log_pick(picks[-1], 'model_A', True)
                            except Exception:
                                pass

                # Over/Under edges
                for ou_key, prob in [('over', p_o25), ('under', p_u25)]:
                    if ou_key in odds_ou:
                        price, murl = odds_ou[ou_key]
                        edge = prob * price - 1
                        if edge >= MIN_EDGE and prob >= MIN_CONF and edge < 0.45 and prob < 0.92:
                            picks.append({
                                'match': match_label, 'league': intl_league,
                                'event_id': eid, 'market_url': murl,
                                'price': price, 'label': f'{ou_key.title()} 2.5',
                                'model_prob': prob, 'edge': edge,
                                'sport': 'soccer', 'intl': True,
                            })

                # WC BTTS edges (DC xG model — 66.7% WR in club soccer)
                if intl_league == 'FIFA_WC' and p_btts_yes is not None:
                    for _bo, _bp in [('yes', p_btts_yes), ('no', p_btts_no)]:
                        if _bo in odds_btts:
                            _pr, _mu = odds_btts[_bo]
                            _edge = _bp * _pr - 1
                            _skip_this_btts = _skip_btts_yes and _bo == 'yes'
                            if not _skip_this_btts and _edge >= WC_MIN_EDGE and _bp >= 0.55 and _edge < 0.45 and _bp < 0.92:
                                picks.append({
                                    'match': match_label, 'league': intl_league,
                                    'event_id': eid, 'market_url': _mu,
                                    'price': _pr,
                                    'label': f'BTTS {"Yes" if _bo == "yes" else "No"} (xG {_xg_h:.1f}/{_xg_a:.1f})',
                                    'model_prob': _bp, 'edge': _edge,
                                    'sport': 'soccer', 'intl': True, '_wc_phase_c': True,
                                })

                # WC 2H Goals O/U 1.5 edges (DC xG × 0.45 — live prices since May 27)
                if intl_league == 'FIFA_WC' and p_o15_2h is not None:
                    for _ho, _hp in [('over', p_o15_2h), ('under', p_u15_2h)]:
                        if _ho in odds_2h:
                            _pr, _mu = odds_2h[_ho]
                            _edge = _hp * _pr - 1
                            if _edge >= WC_MIN_EDGE and _hp >= 0.55 and _edge < 0.45 and _hp < 0.92:
                                picks.append({
                                    'match': match_label, 'league': intl_league,
                                    'event_id': eid, 'market_url': _mu,
                                    'price': _pr,
                                    'label': f'Goals 2H {_ho.title()} 1.5 (xG {_xg_h*0.45:.1f}+{_xg_a*0.45:.1f})',
                                    'model_prob': _hp, 'edge': _edge,
                                    'sport': 'soccer', 'intl': True, '_wc_phase_c': True,
                                })

    # Record odds for monitoring
    try:
        from oraculo_odds_monitor import record_odds
        _all_events = []
        for _lk in list(CB_COMPS.keys()):
            _evs = api.get_odds(CB_COMPS.get(_lk, ''))
            if _evs:
                _all_events.extend(_evs)
        if _all_events:
            _n_rec = record_odds(api, _all_events)
            log.debug('Odds recorded: %d snapshots', _n_rec)
    except Exception as e:
        log.debug('Odds recording error: %s', e)

    # ── STEAM MOVE SCAN (line movement signal) ──────────────────────────────
    # Scan all recorded events for significant odds movement (>8% drop in 6h)
    # A steam move = sharp money → bet the same direction, independent of model
    try:
        from oraculo_odds_monitor import detect_steam_moves
        _steam_count = 0
        _seen_steam = set()
        for _ev in _all_events:
            _eid = str(_ev.get('id', ''))
            _home = (_ev.get('home') or {}).get('name', '')
            _away = (_ev.get('away') or {}).get('name', '')
            if not _eid or not _home:
                continue
            # Skip in-play events (cutoff passed = match already started)
            _ev_cutoff = _ev.get('cutoffTime', '')
            if _ev_cutoff:
                try:
                    from datetime import timezone as _tz
                    _ct = datetime.fromisoformat(_ev_cutoff.replace('Z', '+00:00'))
                    if _ct <= datetime.now(tz=_tz.utc):
                        continue
                except Exception:
                    pass
            _markets = _ev.get('markets', {})
            for _mk_name, _mk_data in _markets.items():
                for _sub in _mk_data.get('submarkets', {}).values():
                    for _sel in _sub.get('selections', []):
                        _murl = _sel.get('marketUrl', '')
                        _outcome = _sel.get('outcome', '')
                        _price = float(_sel.get('price', 0) or 0)
                        if not _murl or _price < 1.15:
                            continue
                        _STEAM_OK = ('soccer.asian_handicap', 'soccer.total_goals',
                                     'soccer.match_winner', 'soccer.both_teams_to_score',
                                     'soccer.double_chance')
                        if not any(_murl.startswith(m) for m in _STEAM_OK):
                            continue
                        _key = (_eid, _murl, _outcome)
                        if _key in _seen_steam:
                            continue
                        _seen_steam.add(_key)
                        _move = detect_steam_moves(_eid, _murl, threshold=0.08, window_hours=6)
                        if _move and _move['direction'] == 'DROP':
                            # Odds dropped = sharp money on this side → follow
                            # Only bet if movement is meaningful (>8%) and current price OK
                            _mag = _move['magnitude']
                            _from = _move['from_price']
                            _steam_edge = _mag * 0.5  # conservative: half the movement as edge estimate
                            if _steam_edge < 0.04:
                                continue
                            # Find league
                            _steam_league = 'UNK'
                            for _lk, _ck in CB_COMPS.items():
                                if _ck and _ev.get('competitionId', '') in _ck:
                                    _steam_league = _lk
                                    break
                            if STEAM_ENABLED:
                                picks.append({
                                    'match': f'{_home} vs {_away}',
                                    'league': _steam_league,
                                    'event_id': _eid,
                                    'market_url': _murl,
                                    'price': _price,
                                    'label': f'STEAM {_outcome} ({_mag:.0%} drop)',
                                    'model_prob': min(0.9, (1/_price) + _steam_edge),
                                    'edge': _steam_edge,
                                    'sport': 'soccer',
                                    'signal': 'steam',
                                    'steam_from': _from,
                                    'steam_to': _price,
                                    'steam_mag': _mag,
                                })
                                _steam_count += 1
                            log.info('STEAM: %s vs %s | %s %s | %.0f%% drop %.2f→%.2f',
                                     _home, _away, _mk_name, _outcome,
                                     _mag*100, _from, _price)
        if _steam_count:
            log.info('Steam moves detected: %d picks', _steam_count)
    except Exception as _e:
        log.debug('Steam scan error: %s', _e)

    # WC correlated picks cap: keep top-2 per match (avoid over-betting correlated outcomes)
    _wc_by_match = {}
    for _pi, _pp in enumerate(picks):
        if not _pp.get('_wc_phase_c'):
            continue
        _mk = _pp.get('match', '')
        _wc_by_match.setdefault(_mk, []).append(_pi)
    _capped = set()
    for _mk, _idxs in _wc_by_match.items():
        if len(_idxs) <= 2:
            continue
        # Sort by edge descending, drop everything after top 2
        _idxs_sorted = sorted(_idxs, key=lambda i: picks[i].get('edge', 0), reverse=True)
        for _drop_i in _idxs_sorted[2:]:
            _capped.add(_drop_i)
            log.info('WC picks cap: dropped %s %s (corr)', picks[_drop_i].get('match','?')[:25], picks[_drop_i].get('label','?'))
    if _capped:
        picks = [p for i, p in enumerate(picks) if i not in _capped]
        log.info('WC picks cap: removed %d correlated picks', len(_capped))

    # WC Pinnacle blend: 30% DC model + 70% Pinnacle no-vig for 1X2 picks
    _pinn_blended = 0
    try:
        from oraculo_clv import get_novig_prob as _gnp
        for _pi, _pp in enumerate(picks):
            if not _pp.get('_wc_phase_c'):
                continue
            _lbl = _pp.get('label', '')
            if 'BTTS' in _lbl or '1.5' in _lbl or '2.5' in _lbl:
                continue  # blend 1X2 only
            _pinn_p = _gnp(_pp.get('match', ''), 'soccer', _lbl, 'soccer-international-world-cup')
            if _pinn_p and 0.05 < _pinn_p < 0.95:
                _b_prob = round(0.30 * _pp['model_prob'] + 0.70 * _pinn_p, 4)
                _b_edge = round(_b_prob * _pp['price'] - 1, 4)
                picks[_pi] = {**_pp, 'model_prob': _b_prob, 'edge': _b_edge,
                               '_pinn_blend': True, '_dc_prob': _pp['model_prob'],
                               '_pinn_prob': round(_pinn_p, 4)}
                _pinn_blended += 1
    except Exception as _pe:
        log.debug('WC Pinnacle blend error: %s', _pe)
    if _pinn_blended:
        log.info('WC Pinnacle blend: %d picks updated', _pinn_blended)

    # Higher minimum edge for soccer (8%) to avoid marginal bets
    _pre_filter = len(picks)
    picks = [p for p in picks if p.get("edge", 0) >= 0.08]
    if len(picks) < _pre_filter:
        log.info("Football: filtered %d marginal picks (edge < 8%%)", _pre_filter - len(picks))
    log.info('Football: %d value picks found (%d steam)', len(picks),
             sum(1 for p in picks if p.get('signal') == 'steam'))
    for _p in picks:
        log.info('  [FB] %s | %s | edge=%.1f%% conf=%.0f%% @%.2f', _p.get('match','?')[:35], _p.get('label','?'), _p.get('edge',0)*100, _p.get('model_prob',0)*100, _p.get('price',0))
    if _FRESH_ENABLED:
        picks = _fresh_check(picks)
        _fs = _fresh_summary(picks)
        if _fs: log.info(_fs)
    # Benter trick: recalibrar model_prob con implied del bookmaker
    if _BENTER_ENABLED:
        picks = _BENTER.apply_batch(picks, book='cloudbet')
    # Benter trick
    if _BENTER_ENABLED:
        picks = _BENTER.apply_batch(picks, book='cloudbet')
    # Benter trick: recalibrar model_prob con implied del bookmaker
    if _BENTER_ENABLED:
        picks = _BENTER.apply_batch(picks, book='cloudbet')
    # Benter trick
    if _BENTER_ENABLED:
        picks = _BENTER.apply_batch(picks, book='cloudbet')
    if _FRESH_ENABLED:
        picks = _fresh_check(picks)
        _fs = _fresh_summary(picks)
        if _fs: log.info(_fs)
    if _RLM_ENABLED:
        _RLM.record_batch(picks, book='cloudbet')
        picks = _RLM.tag_picks(picks)
    if _SIBILA_ENABLED:
        for _sp in picks:
            _sibila_record(_sp)
    return picks

# ---------------------------------------------------------------------------
# Tennis market scanner
# ---------------------------------------------------------------------------
def scan_tennis(api, state, dry_run=False):
    _cal = _load_calibration()
    """Scan Cloudbet tennis markets, find value bets."""
    log.info('=== SCANNING TENNIS MARKETS ===')
    picks = []

    # Load dynamic filters from oraculo_filters.json
    try:
        import json as _json
        _fpath = os.path.join(SCRIPT_DIR, 'oraculo_filters.json')
        with open(_fpath) as _ff:
            _dyn_filters = _json.load(_ff)
        _dyn_tennis_blocks = set(_dyn_filters.get('tennis', {}).get('blocked_comp_substrings', []))
        _dyn_tennis_odds  = _dyn_filters.get('tennis', {}).get('max_odds', {})
    except Exception as _fe:
        log.warning('Could not load oraculo_filters.json: %s', _fe)
        _dyn_tennis_blocks = set()
        _dyn_tennis_odds   = {}

    # Surface map by competition key substring
    COMP_SURFACE = {
        'monte-carlo': 'clay', 'barcelona': 'clay', 'madrid': 'clay',
        'rome': 'clay', 'roland-garros': 'clay', 'french-open': 'clay',
        'hamburg': 'clay', 'munich': 'clay', 'budapest': 'clay',
        'estoril': 'clay', 'marrakech': 'clay', 'casablanca': 'clay',
        'wimbledon': 'grass', 'queens': 'grass', 'halle': 'grass',
        'eastbourne': 'grass', 's-hertogenbosch': 'grass',
        'australian-open': 'hard', 'us-open': 'hard', 'miami': 'hard',
        'indian-wells': 'hard', 'canada': 'hard', 'cincinnati': 'hard',
        'shanghai': 'hard', 'paris': 'hard', 'vienna': 'hard',
        'sofia': 'hard', 'moscow': 'hard', 'linz': 'hard',
        'challenger': 'hard',  # Most challengers are hard
    }

    # Load tennis Elo
    tennis_elo = None
    try:
        from oraculo_tennis import TennisElo
        tennis_elo = TennisElo()
        # Phase 2: warm-start Elo from pre-trained tennis_v1.pkl (26y ATP + 16y WTA)
        try:
            import pickle as _pk
            _v1_path = os.path.join(SCRIPT_DIR, 'models', 'tennis_v1.pkl')
            if os.path.exists(_v1_path):
                with open(_v1_path, 'rb') as _f:
                    _v1 = _pk.load(_f)
                for _p, _r in _v1['elo']['overall'].items():
                    tennis_elo.overall[_p] = _r
                for _surf in ('hard', 'clay', 'grass'):
                    for _p, _r in _v1['elo'].get(_surf, {}).items():
                        if _surf in tennis_elo.by_surface:
                            tennis_elo.by_surface[_surf][_p] = _r
                log.info('Tennis ELO warm-start: %d players from tennis_v1.pkl', len(_v1['elo']['overall']))
        except Exception as _e_v1:
            log.debug('tennis_v1 warm-start skipped: %s', _e_v1)
        # Phase 3: cache GBC model from tennis_v1.pkl for probability blending
        try:
            import pickle as _pk3
            _v1p3 = os.path.join(SCRIPT_DIR, 'models', 'tennis_v1.pkl')
            with open(_v1p3, 'rb') as _f3:
                _v1c3 = _pk3.load(_f3)
            scan_tennis._gbc_v1 = _v1c3['gbc']
            log.info('Tennis GBC v1 loaded (%.1f%% acc, %d samples)', _v1c3['meta']['gbc_acc'], _v1c3['meta']['ml_samples'])
        except Exception as _e3:
            scan_tennis._gbc_v1 = None
            log.debug('GBC v1 load skipped: %s', _e3)
        # Phase 4: build form + H2H lookups from cached match history
        try:
            import collections as _coll4
            _form_db4 = _coll4.defaultdict(list)
            _h2h_db4 = _coll4.defaultdict(lambda: _coll4.defaultdict(int))
            _cache_dir4 = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis')
            _json_files4 = sorted([
                _fn for _fn in os.listdir(_cache_dir4)
                if (_fn.startswith('atp_') or _fn.startswith('wta_')) and _fn.endswith('.json')
            ]) if os.path.isdir(_cache_dir4) else []
            for _fn4 in _json_files4:
                with open(os.path.join(_cache_dir4, _fn4)) as _f4:
                    _ms4 = json.load(_f4)
                if not isinstance(_ms4, list):
                    continue
                for _m4 in _ms4:
                    _w4 = _m4.get('winner', '')
                    _l4 = _m4.get('loser', '')
                    _d4 = _m4.get('date', '')
                    _s4 = _m4.get('surface', 'hard').lower()
                    if not _w4 or not _l4:
                        continue
                    _form_db4[_w4].append((_d4, _s4, True))
                    _form_db4[_l4].append((_d4, _s4, False))
                    _key4 = tuple(sorted([_w4, _l4]))
                    _h2h_db4[_key4][_w4] += 1
            scan_tennis._form_db = dict(_form_db4)
            scan_tennis._h2h_db = {k: dict(v) for k, v in _h2h_db4.items()}
            log.info('Phase 4: form/H2H built for %d players (%d pairs)', len(scan_tennis._form_db), len(scan_tennis._h2h_db))
        except Exception as _e4:
            scan_tennis._form_db = {}
            scan_tennis._h2h_db = {}
            log.debug('Phase 4 build skipped: %s', _e4)
        # Phase 4b: load current ATP+WTA rankings from Sackmann (cached 7 days)
        try:
            import urllib.request as _ureq4
            import csv as _csv4
            import io as _io4
            import time as _tr4
            _rank_db4 = {}
            _cd4 = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis')
            for _tour4, _repo4 in [('atp', 'tennis_atp'), ('wta', 'tennis_wta')]:
                _pc4 = os.path.join(_cd4, f'{_tour4}_players.csv')
                _rc4 = os.path.join(_cd4, f'{_tour4}_rankings_current.csv')
                _base4 = f'https://raw.githubusercontent.com/JeffSackmann/{_repo4}/master'
                for _url4, _dst4 in [(_base4+f'/{_tour4}_players.csv', _pc4),
                                      (_base4+f'/{_tour4}_rankings_current.csv', _rc4)]:
                    if not os.path.exists(_dst4) or (_tr4.time() - os.path.getmtime(_dst4)) > 604800:
                        _ureq4.urlretrieve(_url4, _dst4)
                with open(_pc4) as _fp4:
                    _id2n4 = {}
                    for _pr4 in _csv4.DictReader(_fp4):
                        _fn4 = (_pr4.get('name_first') or '').strip()
                        _ln4 = (_pr4.get('name_last') or '').strip()
                        if _fn4 and _ln4:
                            _id2n4[_pr4['player_id']] = f'{_ln4} {_fn4[0]}.'
                with open(_rc4) as _fr4:
                    _seen4 = set()
                    for _rr4 in reversed(list(_csv4.DictReader(_fr4))):
                        _pid4 = _rr4['player']
                        if _pid4 not in _seen4 and _pid4 in _id2n4:
                            try:
                                _rank_db4[_id2n4[_pid4]] = int(_rr4['rank'])
                            except (ValueError, KeyError):
                                pass
                            _seen4.add(_pid4)
            scan_tennis._rank_db = _rank_db4
            log.info('Phase 4b: rank data loaded for %d players (ATP+WTA)', len(_rank_db4))
        except Exception as _e4b:
            scan_tennis._rank_db = {}
            log.debug('Phase 4b rank load skipped: %s', _e4b)
        # Load cached Elo state
        elo_path_atp = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis', 'elo_atp.pkl')
        elo_path_wta = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis', 'elo_wta.pkl')
        cache_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis')
        _elo_fresh = False
        if os.path.exists(elo_path_atp):
            import time as _t
            _age = _t.time() - os.path.getmtime(elo_path_atp)
            if _age < 86400:
                tennis_elo.load_state(elo_path_atp)
                _elo_fresh = True
                log.info('Tennis ATP Elo loaded from cache (%d players)', len(tennis_elo.overall))
        if not _elo_fresh:
            for fname in sorted(os.listdir(cache_dir)) if os.path.isdir(cache_dir) else []:
                if fname.startswith('atp_') and fname.endswith('.json'):
                    with open(os.path.join(cache_dir, fname)) as f:
                        matches = json.load(f)
                    if isinstance(matches, list):
                        tennis_elo.process_matches(matches)
            tennis_elo.save_state(elo_path_atp)
            log.info('Tennis ATP Elo trained & saved (%d players)', len(tennis_elo.overall))
        # ServeReturn Elo (Newton-Keller model)
        _sr_elo_path = os.path.join(cache_dir, 'sr_elo_atp.pkl')
        _sr_elo = None
        try:
            from oraculo_tennis import ServeReturnElo
            _sr_elo = ServeReturnElo()
            _sr_loaded = _sr_elo.load_state(_sr_elo_path)
            if not _sr_loaded or not _elo_fresh:
                # Retrain from ATP data
                for fname in sorted(os.listdir(cache_dir)) if os.path.isdir(cache_dir) else []:
                    if fname.startswith('atp_') and fname.endswith('.json'):
                        with open(os.path.join(cache_dir, fname)) as _f:
                            _ms = json.load(_f)
                        if isinstance(_ms, list):
                            _sr_elo.process_matches(_ms)
                _sr_elo.save_state(_sr_elo_path)
            _n_sr = sum(1 for c in _sr_elo.serve_count.values() if c >= _sr_elo.MIN_MATCHES)
            log.info('ServeReturn Elo: %d players with serve data', _n_sr)
            scan_tennis._sr_elo = _sr_elo
        except Exception as _e_sr:
            log.debug('ServeReturnElo unavailable: %s', _e_sr)
            scan_tennis._sr_elo = None
        # WTA Elo — also warm-started from tennis_v1.pkl
        wta_elo = TennisElo()
        try:
            import pickle as _pk2
            _v1_path2 = os.path.join(SCRIPT_DIR, 'models', 'tennis_v1.pkl')
            if os.path.exists(_v1_path2):
                with open(_v1_path2, 'rb') as _f2:
                    _v1b = _pk2.load(_f2)
                for _p2, _r2 in _v1b['elo']['overall'].items():
                    wta_elo.overall[_p2] = _r2
                for _surf2 in ('hard', 'clay', 'grass'):
                    for _p2, _r2 in _v1b['elo'].get(_surf2, {}).items():
                        if _surf2 in wta_elo.by_surface:
                            wta_elo.by_surface[_surf2][_p2] = _r2
                log.info('WTA ELO warm-start: %d players from tennis_v1.pkl', len(_v1b['elo']['overall']))
        except Exception as _e_v1b:
            log.debug('tennis_v1 WTA warm-start skipped: %s', _e_v1b)
        _wta_fresh = False
        if os.path.exists(elo_path_wta):
            import time as _t
            _age = _t.time() - os.path.getmtime(elo_path_wta)
            if _age < 86400:
                wta_elo.load_state(elo_path_wta)
                _wta_fresh = True
                log.info('Tennis WTA Elo loaded from cache (%d players)', len(wta_elo.overall))
        if not _wta_fresh:
            try:
                from oraculo_tennis import download_wta_data
                download_wta_data()
                log.info('WTA Sackmann data refreshed')
            except Exception as _ewta:
                log.debug('WTA download skipped: %s', _ewta)
            for fname in sorted(os.listdir(cache_dir)) if os.path.isdir(cache_dir) else []:
                if fname.startswith('wta_') and fname.endswith('.json'):
                    with open(os.path.join(cache_dir, fname)) as f:
                        matches = json.load(f)
                    if isinstance(matches, list):
                        wta_elo.process_matches(matches)
            wta_elo.save_state(elo_path_wta)
            log.info('Tennis WTA Elo trained & saved (%d players)', len(wta_elo.overall))
        scan_tennis._wta_elo = wta_elo
        scan_tennis._elo = tennis_elo
        # Save top Elo ratings to state for display
        try:
            _elo_snapshot = dict(sorted(tennis_elo.overall.items(), key=lambda x: x[1], reverse=True)[:50])
            state['tennis_elo'] = _elo_snapshot
        except Exception:
            pass
        # Load advanced tennis features (fatigue, retirement, surface form)
        try:
            from oraculo_tennis_advanced import TennisAdvanced
            _tn_adv_atp = TennisAdvanced(base_elo=tennis_elo)
            _tn_adv_wta = TennisAdvanced(base_elo=wta_elo)
            _cache_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis')
            _tn_adv_atp.load_match_history(_cache_dir)
            _tn_adv_wta.load_match_history(_cache_dir)
            scan_tennis._adv_atp = _tn_adv_atp
            scan_tennis._adv_wta = _tn_adv_wta
            gc.collect()
        except Exception as e:
            log.debug('TennisAdvanced unavailable: %s', e)
    except Exception as e:
        log.warning('Tennis Elo unavailable: %s', e)
        return picks

    if not tennis_elo or len(tennis_elo.overall) < 50:
        log.warning('Tennis Elo has too few players, skipping')
        return picks

    # Auto-discover active tennis competitions
    tennis_comps = list(CB_TENNIS)  # Start with known keys
    try:
        import requests as _rq
        _cfg = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))
        _ts = _rq.Session()
        _ts.headers.update({'Authorization': 'Bearer ' + _cfg['api_key'], 'Accept': 'application/json'})
        # Use working sports endpoint to discover all categories with live competitions
        _r = _ts.get(CB_BASE + '/pub/v2/odds/sports/tennis', timeout=10,
                     params={'markets': 'tennis.match_winner', 'limit': 50})
        if _r.status_code == 200:
            for _cat in _r.json().get('categories', []):
                for comp in _cat.get('competitions', []):
                    ck = comp.get('key', '')
                    if (ck and ck not in tennis_comps
                            and 'double' not in ck       # catches double + doubles
                            and 'simulated' not in ck
                            and 'srl' not in ck
                            and 'itf' not in ck           # low-quality Elo data
                            and 'international' not in ck  # UTR/team events
                            and 'utr' not in ck            # UTR events not under international prefix
                            and 'federation' not in ck    # team tennis
                            and 'monte-carlo' not in ck): # RESTRICTED for sharp accounts
                        tennis_comps.append(ck)
    except Exception:
        pass  # Fall back to hardcoded list

    for comp_key in tennis_comps:
        # Dynamic AutoAnalyzer blocks (from oraculo_filters.json)
        if any(b in comp_key for b in _dyn_tennis_blocks if b != 'wta'):
            log.info('  [SKIP] AutoAnalyzer block (%s): %s', 
                     next(b for b in _dyn_tennis_blocks if b in comp_key and b != 'wta'), comp_key[:40])
            continue
        # Block WTA: Sibila 62.1% WR break-even (29 picks) — shadow-only until 50+ clean picks
        if 'wta' in comp_key:
            log.debug('  [SKIP] WTA blocked (shadow-only): %s', comp_key)
            continue
        events = api.get_odds(comp_key)
        if not events:
            continue
        log.info('  %s: %d events', comp_key, len(events))
        # Record odds snapshots for CLV tracking
        try:
            from oraculo_odds_monitor import record_odds as _rec_tn
            _rec_tn(api, events)
        except Exception:
            pass

        # Filter: only events within 48h
        cutoff_limit_tn = (datetime.now(timezone.utc) + timedelta(hours=168)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for ev in events:
            if not ev or not isinstance(ev, dict):
                continue
            # Skip events beyond 48h window
            ev_cutoff = ev.get('cutoffTime', '')
            if ev_cutoff and ev_cutoff > cutoff_limit_tn:
                continue
            home = (ev.get('home') or {}).get('name', '')
            away = (ev.get('away') or {}).get('name', '')
            eid = str(ev.get('id', ''))
            markets = ev.get('markets', {})
            if not home or not away:
                continue

            # Get tennis prediction (enhanced with fatigue/surface/retirement)
            try:
                _is_wta = 'wta' in comp_key.lower()
                _adv = getattr(scan_tennis, '_adv_wta' if _is_wta else '_adv_atp', None)
                # Detect surface from competition key
                _surf = 'hard'
                for _sk, _sv in COMP_SURFACE.items():
                    if _sk in comp_key.lower():
                        _surf = _sv
                        break
                if _adv:
                    _epred = _adv.predict_enhanced(home, away, surface=_surf)
                    prob_home = _epred['prob_a']
                else:
                    _use_elo = getattr(scan_tennis, '_wta_elo', tennis_elo) if _is_wta else tennis_elo
                    pred = _use_elo.predict(home, away, surface=_surf)
                    prob_home = pred if isinstance(pred, float) else pred.get('prob_a', 0.5)
                # Blend with ServeReturn Elo (Newton-Keller) when data available
                _sr = getattr(scan_tennis, '_sr_elo', None)
                if _sr and not _is_wta:
                    _sr_prob = _sr.predict(home, away, surface=_surf)
                    if _sr_prob is not None:
                        prob_home = 0.55 * prob_home + 0.45 * _sr_prob
                        log.debug('  SR blend: elo=%.3f sr=%.3f final=%.3f %s',
                                  pred if isinstance(pred, float) else prob_home, _sr_prob, prob_home, home[:15])
            except Exception:
                continue

            _gbc_ph = None; _elo_prob_pre = prob_home  # defaults if GBC unavailable
            # Phase 3: blend GBC probability (trained on 26y data + pre-trained ELO features)
            _gbc_v1 = getattr(scan_tennis, '_gbc_v1', None)
            if _gbc_v1 is not None:
                try:
                    import numpy as _np_gbc
                    _eh = tennis_elo.overall.get(home, 1500)
                    _ea = tennis_elo.overall.get(away, 1500)
                    _sh = tennis_elo.by_surface.get(_surf, {}).get(home, 1500)
                    _sa = tennis_elo.by_surface.get(_surf, {}).get(away, 1500)
                    # Phase 4: real form + H2H features
                    _fdb4 = getattr(scan_tennis, '_form_db', {})
                    _hdb4 = getattr(scan_tennis, '_h2h_db', {})
                    _hist_h = _fdb4.get(home, [])
                    _hist_a = _fdb4.get(away, [])
                    _ov_h = [_w for _, _, _w in _hist_h[-10:]]
                    _ov_a = [_w for _, _, _w in _hist_a[-10:]]
                    _sf_h = [_w for _, _ss, _w in _hist_h if _ss == _surf][-10:]
                    _sf_a = [_w for _, _ss, _w in _hist_a if _ss == _surf][-10:]
                    _form_ph = sum(_ov_h)/len(_ov_h) if _ov_h else 0.5
                    _form_pa = sum(_ov_a)/len(_ov_a) if _ov_a else 0.5
                    _sf_ph = sum(_sf_h)/len(_sf_h) if _sf_h else 0.5
                    _sf_pa = sum(_sf_a)/len(_sf_a) if _sf_a else 0.5
                    _hkey4 = tuple(sorted([home, away]))
                    _h2h4 = _hdb4.get(_hkey4, {})
                    _haw4 = float(_h2h4.get(home, 0))
                    _hbw4 = float(_h2h4.get(away, 0))
                    _h2h_rate4 = _haw4/(_haw4+_hbw4) if (_haw4+_hbw4) > 0 else 0.5
                    _rdb4 = getattr(scan_tennis, '_rank_db', {})
                    _rh4 = _rdb4.get(home, 0)
                    _ra4 = _rdb4.get(away, 0)
                    _rdiff4 = float(_ra4 - _rh4) if (_rh4 > 0 and _ra4 > 0) else 0.0
                    # B: fatigue features — days since last match + matches in last 7d
                    try:
                        from datetime import datetime as _fdt
                        _today_f = _fdt.now().strftime('%Y-%m-%d')
                        _hist_hs = sorted(_hist_h, key=lambda x: x[0])
                        _hist_as = sorted(_hist_a, key=lambda x: x[0])
                        def _days_since_f(hist, today):
                            if not hist: return 30
                            try: return min(max((_fdt.fromisoformat(today)-_fdt.fromisoformat(hist[-1][0])).days,0),30)
                            except: return 30
                        def _m7d_f(hist, today):
                            try:
                                _cut = _fdt.fromisoformat(today).toordinal()-7
                                return sum(1 for d,_,_ in hist if _fdt.fromisoformat(d).toordinal()>=_cut)
                            except: return 0
                        _days_h = float(_days_since_f(_hist_hs, _today_f))
                        _days_a = float(_days_since_f(_hist_as, _today_f))
                        _m7d_h = float(_m7d_f(_hist_hs, _today_f))
                        _m7d_a = float(_m7d_f(_hist_as, _today_f))
                    except Exception:
                        _days_h = _days_a = 30.0; _m7d_h = _m7d_a = 0.0
                    _feat_v = [_eh-_ea, prob_home, _sh-_sa, _rdiff4, _form_ph-_form_pa,
                               _form_ph, _form_pa, _sf_ph, _sf_pa, _h2h_rate4, _haw4, _hbw4,
                               _days_h, _days_a, _m7d_h, _m7d_a]  # 16 features (B: fatigue added)
                    _gbc_ph = float(_gbc_v1.predict_proba([_feat_v])[0][1])
                    _elo_prob_pre = prob_home  # pre-blend ELO prob (for C logging)
                    prob_home = 0.55 * _gbc_ph + 0.45 * prob_home
                except Exception as _eg:
                    log.debug('GBC blend err: %s', _eg)

            # Platt calibration: corrects 15%% systematic overestimation (fitted 2026-05-22 on 124 Sibila picks)
            try:
                import math as _pm
                _lgt = _pm.log(max(prob_home, 1e-6) / max(1 - prob_home, 1e-6))
                prob_home = 1 / (1 + _pm.exp(-(TENNIS_PLATT_A * _lgt + TENNIS_PLATT_B)))
            except Exception:
                pass
            # Scan winner market — try 3 market keys:
            # 1. tennis.match_odds (classic), 2. tennis.winner (alt), 3. tennis.exact_sets (Rome/clay)
            winner_data = markets.get('tennis.match_odds', markets.get('tennis.winner', {}))
            _using_exact_sets = False
            # DISABLED 2026-05-22: exact_sets WR=23%% on 30 picks, skip fallback
            #             if not winner_data.get('submarkets'):
            # DISABLED 2026-05-22: exact_sets WR=23%% on 30 picks, skip fallback
            #                 _es = markets.get('tennis.exact_sets', {})
            # DISABLED 2026-05-22: exact_sets WR=23%% on 30 picks, skip fallback
            #                 if _es.get('submarkets'):
            # DISABLED 2026-05-22: exact_sets WR=23%% on 30 picks, skip fallback
            #                     winner_data = _es
            # DISABLED 2026-05-22: exact_sets WR=23%% on 30 picks, skip fallback
            #                     _using_exact_sets = True
            # E: pre-compute overround for vig-adjusted edge
            _mkt_prices = {}
            for _sub_v in winner_data.get('submarkets', {}).values():
                for _sel_v in _sub_v.get('selections', []):
                    _oc_v = _sel_v.get('outcome', '')
                    _pr_v = float(_sel_v.get('price', 0))
                    if _oc_v in ('home', 'away') and _pr_v >= 1.1:
                        _mkt_prices[_oc_v] = _pr_v
            _overround = (1/_mkt_prices['home'] + 1/_mkt_prices['away']) if len(_mkt_prices) == 2 else 1.0
            for sub in winner_data.get('submarkets', {}).values():
                for sel in sub.get('selections', []):
                    if sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                        if sel.get('status') and sel.get('status') != 'SELECTION_ENABLED':
                            continue
                    price = sel.get('price', 0)
                    murl = sel.get('marketUrl', '')
                    outcome = sel.get('outcome', '')
                    if price < 1.1 or not murl:
                        continue
                    # tennis.exact_sets: outcome=2 → home wins, outcome=3 → away wins
                    if _using_exact_sets:
                        if outcome == 'outcome=2':
                            outcome = 'home'
                        elif outcome == 'outcome=3':
                            outcome = 'away'
                        else:
                            continue
                    elif outcome not in ('home', 'away'):
                        continue
                    prob = prob_home if outcome == 'home' else (1 - prob_home)
                    player = home if outcome == 'home' else away
                    # Kelly-style edge: prob * price - 1 (consistent with MLB/NBA)
                    edge = round(prob * float(price) - 1.0, 4)
                    _fair_implied = (1/float(price)) / _overround
                    _edge_va = round(prob - _fair_implied, 4)  # vig-adjusted edge
                    # Require minimum Elo match history for confidence
                    player_matches = (tennis_elo._match_count.get(player, 0) or
                                      tennis_elo._match_count.get(player.replace('-', ' '), 0))
                    opp = away if player == home else home
                    opponent_matches = (tennis_elo._match_count.get(opp, 0) or
                                        tennis_elo._match_count.get(opp.replace('-', ' '), 0))
                    if player_matches < 20 or opponent_matches < 20:
                        log.debug('  [SKIP] Insufficient Elo data: %s (%d) vs %s (%d)',
                                  player, player_matches, opp, opponent_matches)
                        continue
                    # Block ATP1000+Hamburg: Sibila — ATP1000 43.4% WR -$186 (83 picks), Hamburg 54.5% WR -$198 (22 picks)
                    if 'madrid' in comp_key or 'rome' in comp_key or 'hamburg' in comp_key or 'atp1000' in comp_key or 'masters' in comp_key:
                        log.info('  [SKIP] ATP1000 blocked (%s): WR=43.4%% Sibila', comp_key[:35])
                        continue
                    # Cap odds 2.00: Sibila 33% WR -$3.06 on odds>=2.00 (15 picks); prior cap was 2.10
                    _max_odds_winner = _dyn_tennis_odds.get('tennis_winner', 2.00)
                    if float(price) > _max_odds_winner:
                        log.info('  [SKIP] h2h odds cap @%.2f>=2.00 (WR=33%% Sibila 15 picks)', float(price))
                        continue
                    if edge > TENNIS_MIN_EDGE and _edge_va > 0 and prob > TENNIS_MIN_CONF and edge <= TENNIS_MAX_EDGE and prob < 0.92:
                        picks.append({
                            'match': f'{home} vs {away}', 'league': comp_key,
                            'event_id': eid, 'market_url': murl,
                            'price': float(price), 'label': f'Winner: {player}',
                            'model_prob': round(prob, 4),
                            'raw_model_prob_uncal': round(prob, 4),
                            'confidence': round(prob, 4),
                            'edge': edge, 'sport': 'tennis', 'player': player,
                            '_gbc_prob': round(_gbc_ph, 4) if _gbc_ph is not None else None,
                            '_elo_prob': round(_elo_prob_pre, 4),
                            '_edge_va': _edge_va, '_vig': round(_overround - 1.0, 4),
                            'surface': _surf,
                            '_max_stake': 12.00,  # 2026-05-29: daily 0 (WR 70%%)
                            '_features': {'elo_diff':round(_eh-_ea,3),'form_a':round(_form_ph,3),'form_b':round(_form_pa,3),'h2h_rate':round(_h2h_rate4,3)},
                        })

    # Scan set handicap markets (total sets over/under)
    _adv = getattr(scan_tennis, '_adv_atp', None)
    if _adv:
        for comp_key in tennis_comps:
            # 2026-06-03: block same comps as h2h (wta-foggia, wta-rome-italy, etc.)
            if any(b in comp_key for b in _dyn_tennis_blocks):
                log.info('  [SKIP win_set] blocked comp (%s): %s',
                         next((b for b in _dyn_tennis_blocks if b in comp_key), '?'), comp_key[:40])
                continue
            events = api.get_odds(comp_key)
            if not events:
                continue
            _is_wta = 'wta' in comp_key.lower()
            _use_adv = getattr(scan_tennis, '_adv_wta' if _is_wta else '_adv_atp', _adv)
            cutoff_sets = (datetime.now(timezone.utc) + timedelta(hours=168)).strftime('%Y-%m-%dT%H:%M:%SZ')
            for ev in events:
                if not ev or not isinstance(ev, dict):
                    continue
                ev_cutoff = ev.get('cutoffTime', '')
                if ev_cutoff and ev_cutoff > cutoff_sets:
                    continue
                home = (ev.get('home') or {}).get('name', '')
                away = (ev.get('away') or {}).get('name', '')
                eid = str(ev.get('id', ''))
                if not home or not away:
                    continue
                markets = ev.get('markets', {})
                # Check for total sets market
                sets_mkt = markets.get('tennis.total_sets', markets.get('tennis.exact_sets', {}))
                for sv in sets_mkt.get('submarkets', {}).values():
                    for sel in sv.get('selections', []):
                        price = float(sel.get('price', 0) or 0)
                        murl = sel.get('marketUrl', '')
                        outcome = sel.get('outcome', '')
                        params = str(sel.get('params', ''))
                        if price < 1.1 or not murl:
                            continue
                        # Map tennis.exact_sets outcomes: outcome=2 -> under, outcome=3 -> over
                        if outcome == 'outcome=2':
                            outcome = 'under'
                        elif outcome == 'outcome=3':
                            outcome = 'over'
                        if '2.5' in params or outcome in ('under', 'over'):
                            try:
                                _clay_keys = ['roland','clay','madrid','rome','barcelona','monte','hamburg','geneva','lyon','strasbourg','rabat','marrakech','munich']
                                _grass_keys = ['wimbledon','grass','eastbourne','halle','queens']
                                _surf_su = ('clay' if any(x in comp_key for x in _clay_keys)
                                            else ('grass' if any(x in comp_key for x in _grass_keys)
                                            else 'hard'))
                                set_pred = _use_adv.predict_sets(home, away, _surf_su, 3)
                                if outcome == 'over':
                                    prob = set_pred.get('over_25_sets', 0.5)
                                elif outcome == 'under':
                                    prob = set_pred.get('under_25_sets', 0.5)
                                else:
                                    continue
                                if prob < 0.01:
                                    continue
                                edge = prob * price - 1
                                if (edge > TENNIS_MIN_EDGE and prob >= 0.74 and edge < 0.40
                                        and prob < 0.92 and price <= 1.65):
                                    picks.append({
                                        'match': f'{home} vs {away}',
                                        'league': comp_key,
                                        'event_id': eid,
                                        'market_url': murl,
                                        'price': price,
                                        'label': f'Sets {outcome.title()} 2.5',
                                        'model_prob': prob,
                                        'raw_model_prob_uncal': prob,
                                        'edge': edge,
                                        'sport': 'tennis',
                                        'market_type': 'sets_under',
                                        'surface': _surf_su,
                                    })
                            except Exception:
                                pass

    # --- Episodic memory: penalize players with recent loss history ---
    _mem = _build_episodic_memory(days_back=21)
    if _mem:
        for _p in picks:
            _mstr = _p.get('match', '')
            if ' vs ' not in _mstr:
                continue
            _pa, _pb = [x.strip().lower() for x in _mstr.split(' vs ', 1)]
            _ma = _mem.get(_pa, {})
            _mb = _mem.get(_pb, {})
            _losses = _ma.get('losses', 0) + _mb.get('losses', 0)
            _wins   = _ma.get('wins', 0)   + _mb.get('wins', 0)
            # Only penalize if net negative (more losses than wins)
            if _losses > _wins:
                if _losses >= 3:
                    # 3+ net losses → skip entirely
                    _p['_skip_memory'] = True
                    log.info('  [MEM SKIP] %s | losses=%d wins=%d',
                             _mstr[:40], _losses, _wins)
                elif _losses >= 2:
                    # 2 net losses → halve stake
                    _p['_llm_reduce'] = True
                    log.info('  [MEM REDUCE] %s | losses=%d wins=%d',
                             _mstr[:40], _losses, _wins)
        picks = [p for p in picks if not p.get('_skip_memory')]


    # -- FASE 2: Nuevos mercados tennis (Cloudbet actual) ------------------
    # Markets: tennis.exact_sets / tennis.winner_and_total / tennis.team_to_win_a_set
    import math as _math

    def _norm_cdf(_x, _mu, _sigma):
        return 0.5 * (1 + _math.erf((_x - _mu) / (_sigma * _math.sqrt(2))))

    _GAMES_2SET = {'clay': 23.5, 'hard': 21.5, 'grass': 19.0}
    _GAMES_3SET = {'clay': 33.0, 'hard': 30.5, 'grass': 27.5}

    _tn_adv = getattr(scan_tennis, '_adv_atp', None)
    if _tn_adv:
        _p2_comps = []
        try:
            import requests as _rq2
            _cfg2 = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))
            _ts2 = _rq2.Session()
            _ts2.headers.update({'Authorization': 'Bearer ' + _cfg2['api_key'], 'Accept': 'application/json'})
            _r2 = _ts2.get(CB_BASE + '/pub/v2/odds/sports/tennis', timeout=10,
                           params={'markets': 'tennis.exact_sets', 'limit': 100})
            if _r2.status_code == 200:
                for _cat in _r2.json().get('categories', []):
                    for _comp in _cat.get('competitions', []):
                        _ck = _comp.get('key', '')
                        if (_ck and 'double' not in _ck and 'srl' not in _ck
                                and 'simulated' not in _ck
                                and 'international' not in _ck
                                and 'monte-carlo' not in _ck):
                            _p2_comps.append(_ck)
        except Exception as _e2:
            log.debug('Phase2 comp discovery error: %s', _e2)
        if not _p2_comps:
            _p2_comps = [c for c in tennis_comps if 'itf' not in c]

        log.info('[TN P2] Scanning %d comps for exact_sets/winner_and_total', len(_p2_comps))
        _p2_picks_added = 0

        for comp_key in _p2_comps:
            # 2026-06-03: block same comps as h2h (wta-foggia, wta-rome-italy, etc.)
            if any(b in comp_key for b in _dyn_tennis_blocks):
                log.info('  [SKIP TN-P2] blocked comp (%s): %s',
                         next((b for b in _dyn_tennis_blocks if b in comp_key), '?'), comp_key[:40])
                continue
            events = api.get_odds(comp_key)
            if not events:
                continue
            _is_wta = 'wta' in comp_key.lower()
            _use_adv = getattr(scan_tennis, '_adv_wta' if _is_wta else '_adv_atp', _tn_adv)
            _use_elo = getattr(scan_tennis, '_wta_elo', tennis_elo) if _is_wta else tennis_elo
            cutoff_p2 = (datetime.now(timezone.utc) + timedelta(hours=168)).strftime('%Y-%m-%dT%H:%M:%SZ')
            for ev in events:
                if not ev or not isinstance(ev, dict):
                    continue
                if (ev.get('cutoffTime') or '') > cutoff_p2:
                    continue
                home = (ev.get('home') or {}).get('name', '')
                away = (ev.get('away') or {}).get('name', '')
                eid  = str(ev.get('id', ''))
                if not home or not away:
                    continue
                home_mc = (_use_elo._match_count.get(home, 0) or
                            _use_elo._match_count.get(home.replace('-', ' '), 0))
                away_mc = (_use_elo._match_count.get(away, 0) or
                            _use_elo._match_count.get(away.replace('-', ' '), 0))
                if home_mc < 15 or away_mc < 15:
                    continue
                try:
                    _surf_p2 = 'hard'
                    for _sk, _sv in COMP_SURFACE.items():
                        if _sk in comp_key.lower():
                            _surf_p2 = _sv
                            break
                    _is_slam = any(s in comp_key.lower() for s in (
                        'roland-garros', 'french-open', 'wimbledon', 'us-open', 'australian-open'))
                    _bo = 5 if (_is_slam and not _is_wta) else 3
                    set_pred = _use_adv.predict_sets(home, away, _surf_p2, _bo)
                    if _bo == 3:
                        p_h20 = set_pred.get('a_2_0', 0)
                        p_h21 = set_pred.get('a_2_1', 0)
                        p_a20 = set_pred.get('b_2_0', 0)
                        p_a21 = set_pred.get('b_2_1', 0)
                        p_home_wins = p_h20 + p_h21
                        p_exact2 = p_h20 + p_a20
                        p_exact3_raw = p_h21 + p_a21
                        # Calibrate: model overshoots real 3-set base rates.
                        # Sackmann 2024-26: clay=37.3%, hard=35.2%, grass=39.6%
                        _P3_BASE_RATE = {'clay': 0.373, 'hard': 0.352, 'grass': 0.396}
                        _p3_base = _P3_BASE_RATE.get(_surf_p2, 0.370)
                        # 30% model signal, 70% base rate
                        p_exact3 = 0.30 * p_exact3_raw + 0.70 * _p3_base
                        mu2 = _GAMES_2SET.get(_surf_p2, 21.5)
                        mu3 = _GAMES_3SET.get(_surf_p2, 30.5)
                    else:
                        p_h30 = set_pred.get('a_3_0', 0)
                        p_h31 = set_pred.get('a_3_1', 0)
                        p_h32 = set_pred.get('a_3_2', 0)
                        p_a30 = set_pred.get('b_3_0', 0)
                        p_a31 = set_pred.get('b_3_1', 0)
                        p_a32 = set_pred.get('b_3_2', 0)
                        p_home_wins = p_h30 + p_h31 + p_h32
                        p_exact2 = p_h30 + p_a30
                        p_exact3 = p_h31 + p_a31
                        mu2 = _GAMES_2SET.get(_surf_p2, 21.5) * 1.6
                        mu3 = _GAMES_3SET.get(_surf_p2, 30.5) * 1.6
                except Exception:
                    continue

                match_s = '%s vs %s' % (home, away)
                mkts = ev.get('markets', {})

                # 2.1: tennis.exact_sets
                # DISABLED 2026-05-26: ROI -64% on exact_sets market
                es_mkt = {}  # disabled
                for _sub_k, _sub_v in es_mkt.get('submarkets', {}).items():
                    for sel in (_sub_v.get('selections') or []):
                        if sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                            continue
                        _price = float(sel.get('price', 0) or 0)
                        _murl  = sel.get('marketUrl', '')
                        _oc    = str(sel.get('outcome', ''))
                        if _price < 2.75 or not _murl:  # min @2.75: real base rate only gives edge when book underprices
                            continue
                        try:
                            n_sets = int(_oc.replace('outcome=', ''))
                        except Exception:
                            continue
                        if _bo == 3:
                            _prob = p_exact2 if n_sets == 2 else p_exact3
                        else:
                            if n_sets == 3:   _prob = p_exact2
                            elif n_sets == 4: _prob = p_exact3
                            elif n_sets == 5: _prob = p_h32 + p_a32
                            else: continue
                        _edge = _prob * _price - 1.0
                        if _edge > MIN_EDGE and _prob > 0.38 and _edge < 0.40:
                            picks.append({
                                'match': match_s, 'league': comp_key,
                                'event_id': eid, 'market_url': _murl,
                                'price': _price,
                                'label': 'Exact %d sets' % n_sets,
                                'model_prob': round(_prob, 4),
                                'edge': round(_edge, 4),
                                'sport': 'tennis',
                                'market_type': 'tennis_exact_sets',
                                'surface': _surf_p2,
                                '_max_stake': 1.50,
                            })
                            _p2_picks_added += 1

                # 2.2: tennis.winner_and_total (BO3 only — p_h20/p_a20 undefined for BO5/Slams)
                wt_mkt = {}  # DISABLED 2026-06-05: WR=16.3%% on 49 picks Sibila
                for _sub_k, _sub_v in wt_mkt.get('submarkets', {}).items():
                    for sel in (_sub_v.get('selections') or []):
                        if sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                            continue
                        _price  = float(sel.get('price', 0) or 0)
                        _murl   = sel.get('marketUrl', '')
                        _oc     = str(sel.get('outcome', ''))
                        _params = str(sel.get('params', ''))
                        if _price < 1.15 or not _murl:
                            continue
                        try:
                            _line = float(_params.split('total=')[-1])
                        except Exception:
                            continue
                        if 'home' in _oc and 'under' in _oc:
                            _prob = (p_h20 * _norm_cdf(_line, mu2, 3.0) +
                                     p_h21 * _norm_cdf(_line, mu3, 4.0))
                        elif 'home' in _oc and 'over' in _oc:
                            _prob = (p_h20 * (1 - _norm_cdf(_line, mu2, 3.0)) +
                                     p_h21 * (1 - _norm_cdf(_line, mu3, 4.0)))
                        elif 'away' in _oc and 'under' in _oc:
                            _prob = (p_a20 * _norm_cdf(_line, mu2, 3.0) +
                                     p_a21 * _norm_cdf(_line, mu3, 4.0))
                        elif 'away' in _oc and 'over' in _oc:
                            _prob = (p_a20 * (1 - _norm_cdf(_line, mu2, 3.0)) +
                                     p_a21 * (1 - _norm_cdf(_line, mu3, 4.0)))
                        else:
                            continue
                        _edge = _prob * _price - 1.0
                        # Cap odds dynamic (oraculo_filters.json tennis_winner_and_total, default 2.50)
                        _p2_wt_cap = _dyn_tennis_odds.get('tennis_winner_and_total', 2.50)
                        if _edge > TENNIS_MIN_EDGE and _prob > 0.45 and _edge < 0.35 and _price <= _p2_wt_cap:
                            picks.append({
                                'match': match_s, 'league': comp_key,
                                'event_id': eid, 'market_url': _murl,
                                'price': _price,
                                'label': 'W+Total %s %.1f' % (_oc, _line),
                                'model_prob': round(_prob, 4),
                                'edge': round(_edge, 4),
                                'sport': 'tennis',
                                'market_type': 'tennis_winner_and_total',
                                '_max_stake': 8.00,
                            })
                            _p2_picks_added += 1

                # 2.3: tennis.team_to_win_a_set
                ts_mkt = mkts.get('tennis.team_to_win_a_set', {}) if _bo == 3 else {}
                for _sub_k, _sub_v in ts_mkt.get('submarkets', {}).items():
                    _team = ('home' if 'team=home' in _sub_k
                             else ('away' if 'team=away' in _sub_k else None))
                    if not _team:
                        continue
                    for sel in (_sub_v.get('selections') or []):
                        if sel.get('status') not in ('SELECTION_ENABLED', None, ''):
                            continue
                        _price = float(sel.get('price', 0) or 0)
                        _murl  = sel.get('marketUrl', '')
                        _oc    = str(sel.get('outcome', ''))
                        if _price < 1.15 or not _murl or _oc not in ('yes', 'no'):
                            continue
                        if _team == 'home' and _oc == 'yes':
                            _prob = 1.0 - p_a20
                        elif _team == 'home' and _oc == 'no':
                            _prob = p_a20
                        elif _team == 'away' and _oc == 'yes':
                            _prob = 1.0 - p_h20
                        elif _team == 'away' and _oc == 'no':
                            _prob = p_h20
                        else:
                            continue
                        _edge = _prob * _price - 1.0
                        # Cap odds dynamic (oraculo_filters.json tennis_team_win_set, default 1.80)
                        _p2_ws_cap = _dyn_tennis_odds.get('tennis_team_win_set', 1.80)
                        if _edge > TENNIS_MIN_EDGE and _prob >= TENNIS_MIN_CONF and _prob < 0.93 and _edge < 0.35 and _price <= _p2_ws_cap and _price >= 1.40:
                            _player = home if _team == 'home' else away
                            picks.append({
                                'match': match_s, 'league': comp_key,
                                'event_id': eid, 'market_url': _murl,
                                'price': _price,
                                'label': '%s wins set (%s)' % (_player[:16], _oc),
                                'model_prob': round(_prob, 4),
                                'edge': round(_edge, 4),
                                'sport': 'tennis',
                                'market_type': 'tennis_team_win_set',
                                'surface': _surf_p2,
                                '_max_stake': 12.00,
                            })
                            _p2_picks_added += 1

        log.info('[TN P2] %d additional picks from exact_sets/winner_total/team_set', _p2_picks_added)

    log.info('Tennis: %d value picks found', len(picks))
    for _tp in picks:
        log.info('  [TN] %s | %s | edge=%.1f%% conf=%.0f%% @%.2f',
                 _tp.get('match','?')[:35], _tp.get('label','?'),
                 _tp.get('edge',0)*100, _tp.get('model_prob',0)*100, _tp.get('price',0))
    if _SIBILA_ENABLED:
        for _sp in picks:
            _sibila_record(_sp)

    # --- NBA SCANNING ---
    # NBA game markets only active during season (Oct-Apr). Check if game markets exist.
    _nba_game_markets_active = False
    try:
        _cfg_key = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json'))).get('api_key', '')
        _rnba = requests.get(CB_BASE + '/pub/v2/odds/competitions/basketball-usa-nba',
                             headers={'X-API-Key': _cfg_key}, timeout=5)
        if _rnba.status_code == 200:
            for _nev in _rnba.json().get('events', []):
                if 'basketball.1x2' in _nev.get('markets', {}):
                    _nba_game_markets_active = True
                    break
    except Exception:
        pass
    try:
        from oraculo_nba import train_nba_elo, scan_nba
        _nba_elo = train_nba_elo()
        # NBA active: basketball.1x2 confirmed available (moneyline was restricted)
        if _nba_game_markets_active and _nba_elo and len(_nba_elo.ratings) >= 20:
            _nba_picks = scan_nba(api, state, _nba_elo, shadow=False)
            if _nba_picks:
                log.info('[NBA] %d picks:', len(_nba_picks))
                for _np in _nba_picks:
                    log.info('  [NBA] %s | %s | edge=%.1f%% conf=%.0f%% @%.3f',
                             _np['match'], _np['label'], _np['edge']*100,
                             _np['model_prob']*100, _np['price'])
                if _SIBILA_ENABLED:
                    for _np in _nba_picks:
                        _sibila_record(_np)
                picks.extend(_nba_picks)
    except Exception as e:
        log.debug('NBA scan error: %s', e)

    # --- WNBA SCANNING ---
    # WNBA season May-Sep. Uses basketball.1x2 market. Shadow=True until 40+ picks validated.
    _wnba_active = False
    try:
        _rwnba = requests.get(CB_BASE + '/pub/v2/odds/competitions/basketball-usa-wnba',
                              headers={'Authorization': f'Bearer {api.api_key}'}, timeout=5)
        if _rwnba.status_code == 200:
            for _wev in _rwnba.json().get('events', []):
                if (_wev or {}).get('type') != 'EVENT_TYPE_OUTRIGHT' and 'basketball.1x2' in (_wev or {}).get('markets', {}):
                    _wnba_active = True
                    break
    except Exception:
        pass
    try:
        from oraculo_wnba import train_wnba_elo, scan_wnba
        _wnba_elo = train_wnba_elo()
        if _wnba_active and _wnba_elo and len(_wnba_elo.ratings) >= 8:
            _wnba_picks = scan_wnba(api, state, _wnba_elo, shadow=True)
            if _wnba_picks:
                log.info('[WNBA] %d picks (SHADOW):', len(_wnba_picks))
                for _wp in _wnba_picks:
                    log.info('  [WNBA] %s | %s | edge=%.1f%% conf=%.0f%% @%.3f',
                             _wp['match'][:35], _wp['label'], _wp['edge']*100,
                             _wp['model_prob']*100, _wp['price'])
                if _SIBILA_ENABLED:
                    for _wp in _wnba_picks:
                        _sibila_record(_wp)
                # shadow=True so these never reach place_bets
                picks.extend(_wnba_picks)
    except Exception as e:
        log.debug('WNBA scan error: %s', e)

    # --- NHL SCANNING ---
    # NHL regular season Oct-Apr. Uses hockey.1x2 market. Shadow=True until 40+ picks validated.
    _nhl_active = False
    try:
        _rnhl = requests.get(CB_BASE + '/pub/v2/odds/competitions/hockey-usa-nhl',
                             headers={'Authorization': f'Bearer {api.api_key}'}, timeout=5)
        if _rnhl.status_code == 200:
            for _nev in _rnhl.json().get('events', []):
                if (_nev or {}).get('type') != 'EVENT_TYPE_OUTRIGHT' and 'hockey.1x2' in (_nev or {}).get('markets', {}):
                    _nhl_active = True
                    break
    except Exception:
        pass
    try:
        from oraculo_nhl import train_nhl_elo, scan_nhl
        _nhl_elo = train_nhl_elo()
        if _nhl_active and _nhl_elo and len(_nhl_elo.ratings) >= 28:
            _nhl_picks = scan_nhl(api, state, _nhl_elo, shadow=True)
            if _nhl_picks:
                log.info('[NHL] %d picks (SHADOW):', len(_nhl_picks))
                for _np in _nhl_picks:
                    log.info('  [NHL] %s | %s | edge=%.1f%% conf=%.0f%% @%.3f',
                             _np['match'][:35], _np['label'], _np['edge']*100,
                             _np['model_prob']*100, _np['price'])
                if _SIBILA_ENABLED:
                    for _np in _nhl_picks:
                        _sibila_record(_np)
                picks.extend(_nhl_picks)
    except Exception as e:
        log.debug('NHL scan error: %s', e)
    return picks

# ---------------------------------------------------------------------------
# Build tennis parlays from individual picks
# ---------------------------------------------------------------------------
def build_parlays(tennis_picks, state=None):
    """Generate 2-leg parlay combos from top tennis picks.
    Only uses players with positive profit history.
    Blacklists players with net negative profit."""
    parlays = []
    if not PARLAYS_ENABLED:
        return parlays

    # Build player profit history from predictions log
    _whitelist = set()  # players with 2+ wins and positive profit
    _blacklist = set()  # players with net negative profit
    try:
        import json as _json
        _plines = open(PREDICTIONS_FILE).readlines()
        _pbets = [_json.loads(l) for l in _plines
                  if _json.loads(l).get('result') in ('WIN','LOSS')
                  and 'PARLAY' not in _json.loads(l).get('label','').upper()]
        _pstats = {}
        for _b in _pbets:
            _lbl = _b.get('label','')
            _player = _lbl.replace('Winner:','').strip() if 'Winner:' in _lbl else ''
            if not _player:
                continue
            if _player not in _pstats:
                _pstats[_player] = {'wins':0,'losses':0,'profit':0.0}
            _wl = float(_b.get('win_loss',0))
            _sk = float(_b.get('stake',0))
            if _b['result'] == 'WIN':
                _pstats[_player]['wins'] += 1
                _pstats[_player]['profit'] += _wl
            else:
                _pstats[_player]['losses'] += 1
                _pstats[_player]['profit'] -= _sk
        for _player, _d in _pstats.items():
            if _d['profit'] < -1.0:  # net loser: blacklist
                _blacklist.add(_player.lower())
            elif _d['wins'] >= 2 and _d['profit'] > 0:  # proven winner: whitelist
                _whitelist.add(_player.lower())
    except Exception:
        pass

    # Filter: high confidence, not blacklisted
    strong = []
    for p in tennis_picks:
        if p['model_prob'] < PARLAY_MIN_CONF:
            continue
        _pname = (p.get('player','') or p.get('match','').split(' vs ')[0]).lower()
        if _pname in _blacklist:
            log.debug('Parlay: skip blacklisted %s', _pname)
            continue
        # Boost sort score for whitelisted players
        p['_parlay_score'] = p['model_prob'] * p['edge'] * (1.3 if _pname in _whitelist else 1.0)
        strong.append(p)

    # Sort by combined score: prefer whitelisted high-edge high-prob
    strong.sort(key=lambda x: x.get('_parlay_score', x['model_prob']), reverse=True)

    if len(strong) < PARLAY_MIN_LEGS:
        return parlays

    # Only 2-leg combos from top candidates
    for combo in combinations(strong[:8], 2):  # max top-8 candidates
        combined_odds = 1.0
        combined_prob = 1.0
        sels = []
        for p in combo:
            combined_odds *= p['price']
            combined_prob *= p['model_prob']
            sels.append({
                'eventId': p['event_id'], 'marketUrl': p['market_url'],
                'price': p['price'],
                'player': p.get('player','') or p.get('match','').split(' vs ')[0],
            })
        implied = 1.0 / combined_odds
        edge = combined_prob - implied
        # Tighter filters for 2-leg parlays
        if edge <= 0.05 or combined_prob < 0.45:
            continue
        # Dedup: skip if same combo or any leg already active
        combo_key = frozenset(str(p['event_id']) for p in combo)
        if state:
            _active_keys = set()
            _straight_eids = set()
            for _ab in state.get('active_bets', []):
                _ab_eids = _ab.get('event_ids', [])
                if _ab_eids:
                    _active_keys.add(frozenset(str(e) for e in _ab_eids))
                elif _ab.get('event_id'):
                    _straight_eids.add(str(_ab['event_id']))
            if combo_key in _active_keys:
                continue
            if combo_key & _straight_eids:
                continue
        parlays.append({
            'selections': sels,
            'combined_odds': round(combined_odds, 2),
            'combined_prob': combined_prob,
            'edge': edge,
            'n_legs': 2,
            'label': ' + '.join(s['player'] for s in sels),
        })

    parlays.sort(key=lambda x: x['edge'], reverse=True)
    return parlays[:2]  # Top 2 parlays max (was 3)

# ---------------------------------------------------------------------------
# Daily football parlay (best confidence picks)
# ---------------------------------------------------------------------------
FOOTBALL_PARLAY_MIN_CONF = 0.70   # Min 70% confidence per leg
FOOTBALL_PARLAY_LEGS = 3          # 3-leg parlay
FOOTBALL_PARLAY_STAKE_PCT = 0.03  # 3% of bankroll

def build_daily_football_parlay(football_picks, state):
    """Build one daily parlay from highest-confidence football picks.
    Only runs once per day (tracked in state)."""
    if not PARLAYS_ENABLED:
        return None
    if state.get('football_parlay_placed_today'):
        return None

    # Filter: high confidence, different matches, sorted by confidence
    strong = [p for p in football_picks if p['model_prob'] >= FOOTBALL_PARLAY_MIN_CONF]
    # Deduplicate by match (keep highest confidence per match)
    seen_matches = {}
    for p in sorted(strong, key=lambda x: x['model_prob'], reverse=True):
        key = p['match']
        if key not in seen_matches:
            seen_matches[key] = p
    unique = list(seen_matches.values())

    if len(unique) < FOOTBALL_PARLAY_LEGS:
        return None

    # Take top N by confidence
    top = unique[:FOOTBALL_PARLAY_LEGS]
    combined_odds = 1.0
    combined_prob = 1.0
    sels = []
    for p in top:
        combined_odds *= p['price']
        combined_prob *= p['model_prob']
        sels.append({
            'eventId': p['event_id'], 'marketUrl': p['market_url'],
            'price': p['price'], 'match': p['match'], 'label': p['label'],
        })

    implied = 1.0 / combined_odds
    edge = combined_prob - implied
    if edge < 0.03:
        return None

    stake = state['bankroll'] * FOOTBALL_PARLAY_STAKE_PCT
    if stake < MIN_STAKE:
        return None

    parlay = {
        'selections': sels,
        'combined_odds': round(combined_odds, 2),
        'combined_prob': combined_prob,
        'edge': edge,
        'n_legs': FOOTBALL_PARLAY_LEGS,
        'stake': round(stake, 2),
        'label': ' + '.join(s['label'] for s in sels),
        'matches': ' | '.join(s['match'][:25] for s in sels),
        'sport': 'soccer',
    }
    log.info('DAILY FOOTBALL PARLAY: %d legs @%.2f (prob %.1f%%, edge +%.1f%%)',
             FOOTBALL_PARLAY_LEGS, combined_odds, combined_prob * 100, edge * 100)
    for s in sels:
        log.info('  Leg: %s | %s @%.3f', s['match'][:35], s['label'], s['price'])
    return parlay

# ---------------------------------------------------------------------------
# Daily mixed parlay (2 football + 2 tennis)
# ---------------------------------------------------------------------------
MIXED_PARLAY_STAKE_PCT = 0.05   # 5% of bankroll

def build_mixed_parlay(football_picks, tennis_picks, state):
    """Build daily mixed parlay: 2 best football + 2 best tennis."""
    if not PARLAYS_ENABLED:
        return None
    if state.get('mixed_parlay_placed_today'):
        return None

    # Top 2 football by confidence (different matches)
    fb_seen = {}
    for p in sorted(football_picks, key=lambda x: x['model_prob'], reverse=True):
        if p['match'] not in fb_seen and p['model_prob'] >= 0.65:
            fb_seen[p['match']] = p
        if len(fb_seen) >= 2:
            break
    fb_top = list(fb_seen.values())

    # Top 2 tennis by confidence (different matches)
    tn_seen = {}
    for p in sorted(tennis_picks, key=lambda x: x['model_prob'], reverse=True):
        if p['match'] not in tn_seen and p['model_prob'] >= 0.65:
            tn_seen[p['match']] = p
        if len(tn_seen) >= 2:
            break
    tn_top = list(tn_seen.values())

    if len(fb_top) < 2 or len(tn_top) < 2:
        return None

    legs = fb_top + tn_top
    combined_odds = 1.0
    combined_prob = 1.0
    sels = []
    for p in legs:
        combined_odds *= p['price']
        combined_prob *= p['model_prob']
        sels.append({
            'eventId': p['event_id'], 'marketUrl': p['market_url'],
            'price': p['price'], 'match': p['match'], 'label': p['label'],
            'player': p.get('player', '') or p.get('match', '').split(' vs ')[0],
        })

    implied = 1.0 / combined_odds
    edge = combined_prob - implied
    if edge < 0.02:
        return None

    stake = state['bankroll'] * MIXED_PARLAY_STAKE_PCT
    if stake < MIN_STAKE:
        return None

    parlay = {
        'selections': sels,
        'combined_odds': round(combined_odds, 2),
        'combined_prob': combined_prob,
        'edge': edge,
        'n_legs': 4,
        'stake': round(stake, 2),
        'label': ' + '.join(s.get('label', s.get('match', '?'))[:20] for s in sels),
        'matches': ' | '.join(s['match'][:25] for s in sels),
        'sport': 'mixed',
    }
    log.info('DAILY MIXED PARLAY: 4 legs (2fb+2tn) @%.2f (prob %.1f%%, edge +%.1f%%)',
             combined_odds, combined_prob * 100, edge * 100)
    for s in sels:
        log.info('  Leg: %s | %s @%.3f', s['match'][:35], s['label'], s['price'])
    return parlay

# Track rejected event+market combos to avoid resubmitting within same session
_rejected_keys = set()


# ---------------------------------------------------------------------------
# Currency alternation: use USDT for even-numbered bets, USDC for odd
# ---------------------------------------------------------------------------
def _choose_currency(state):
    bbc = state.get("bankroll_by_currency", {})
    usdt_bal = bbc.get("USDT", 0)
    active_bets = state.get("active_bets", [])
    _bets_iter = list(active_bets.values()) if isinstance(active_bets, dict) else active_bets
    usdt_pending = sum(b.get("stake", 0) for b in _bets_iter
                       if b.get("currency", "USDT") == "USDT" and b.get("status", "open") == "open")
    if usdt_bal - usdt_pending < 5.0:
        return "USDC"
    n = state.get("bets_placed_today", 0)
    return "USDT" if n % 2 == 0 else "USDC"

# ---------------------------------------------------------------------------
# Place bets
# ---------------------------------------------------------------------------
def place_bets(api, state, picks, parlays, dry_run=False):
    """Place straight bets + parlays, respecting limits."""
    # Apply per-market-type calibration to model_prob before Kelly sizing
    # _cal factors: actual_win_rate / avg_model_prob per market type
    try:
        _cal_factors = _load_calibration()
        if _cal_factors:
            for _p in picks:
                _mtype = _p.get("market_type") or _p.get("sport") or "unknown"
                _cf = _cal_factors.get(_mtype, _cal_factors.get("unknown", 1.0))
                if isinstance(_cf, (int, float)) and 0.5 < _cf < 2.0 and _cf != 1.0:
                    _p["raw_model_prob"] = _p["model_prob"]  # preserve for logging
                    _p["model_prob"] = min(0.95, max(0.05, float(_p["model_prob"]) * _cf))
    except Exception:
        pass
    # Filtrar picks con learned_rules.json
    picks = _apply_learned_rules(list(picks))

    bankroll = state['bankroll']
    if bankroll < CIRCUIT_BREAKER:
        log.warning('CIRCUIT BREAKER: bankroll $%.2f < $%.2f, NOT placing bets',
                     bankroll, CIRCUIT_BREAKER)
        return 0

    # Hard cap: total pending exposure across ALL days
    # Exclude long-horizon WC pre-event bets (cutoff >5d away) — they have their own wc_reserve pool
    _today_d = datetime.utcnow().date()
    def _is_long_horizon(b):
        ct = b.get('cutoff_time', '')
        if not ct:
            return False
        try:
            return (datetime.strptime(str(ct)[:10], '%Y-%m-%d').date() - _today_d).days > 5
        except Exception:
            return False
    total_pending = sum(b.get('stake', 0) for b in state.get('active_bets', [])
                        if not _is_long_horizon(b))
    wc_pending = sum(b.get('stake', 0) for b in state.get('active_bets', [])
                     if _is_long_horizon(b))
    if wc_pending > 0:
        log.debug('WC pre-event excluded from exposure cap: $%.2f (%d bets)',
                  wc_pending, sum(1 for b in state.get('active_bets', []) if _is_long_horizon(b)))
    max_exposure = bankroll * MAX_TOTAL_EXPOSURE
    if total_pending >= max_exposure:
        log.info('EXPOSURE CAP: $%.2f pending >= $%.2f max (%.0f%% of bankroll)',
                 total_pending, max_exposure, MAX_TOTAL_EXPOSURE * 100)
        return 0

    # Daily budget proporcional: 25%% del bankroll, sube y baja con el bankroll
    # 00->0 | 00->5 | 00->00 | 00->25 | <00 -> min 0
    _daily_budget = max(10.0, bankroll * 0.25)
    max_today = _daily_budget
    # Respect football ceiling if set (to reserve budget for tennis)
    ceiling = state.get('_football_ceiling')
    if ceiling is not None:
        effective_max = min(max_today, ceiling)
    else:
        effective_max = max_today
    remaining = effective_max - state['daily_staked']
    exposure_headroom = max_exposure - total_pending
    remaining = min(remaining, exposure_headroom)
    if remaining < MIN_STAKE:
        log.info('Limit reached (daily=$%.2f/$%.2f, exposure=$%.2f/$%.2f)',
                 state['daily_staked'], effective_max, total_pending, max_exposure)
        return 0

    # Build dedup sets: per match+label AND per match (exposure tracking)
    active_keys = set()
    active_ev_markets = set()  # event_id|market_url — stable dedup (label can change between scans)
    active_matches = {}  # match_name -> total staked
    active_events = {}   # event_id -> total staked (cross-cycle)
    for ab in state.get('active_bets', []):
        ak = f"{ab.get('match','')}|{ab.get('label','')}"
        active_keys.add(ak)
        _ab_eid = ab.get('event_id', '')
        _ab_murl = ab.get('market_url', '')
        if _ab_eid and _ab_murl:
            active_ev_markets.add(f"{_ab_eid}|{_ab_murl}")
        mn = ab.get('match', '')
        if mn and 'PARLAY' not in mn and '(legacy)' not in mn:
            active_matches[mn] = active_matches.get(mn, 0) + ab.get('stake', 0)
        eid = _ab_eid
        if eid:
            active_events[eid] = active_events.get(eid, 0) + ab.get('stake', 0)

    # Sort by edge
    picks.sort(key=lambda p: p['edge'], reverse=True)
    placed = 0
    match_bets_this_cycle = {}  # match -> count placed this cycle

    # --- Straight bets ---
    # Reserve 10% of budget for parlays
    parlay_reserve = remaining * 0.10 if parlays else 0
    straight_remaining = remaining - parlay_reserve
    consecutive_fails = 0


    for p in picks[:MAX_BETS_PER_SCAN]:
        if straight_remaining < MIN_STAKE:
            break
        # Skip shadow-only picks (WNBA, NHL until validated)
        if p.get('shadow'):
            log.debug('  [SKIP-SHADOW] %s', p.get('label', ''))
            continue
        # Skip previously rejected combos
        dedup_key = f"{p['event_id']}|{p.get('market_url', '')}|{p['label']}"
        if dedup_key in _rejected_keys:
            continue
        # Skip events and markets restricted by Cloudbet for this account
        _murl_pick = p.get('market_url', '')
        _restricted_pfx = state.get('restricted_market_prefixes', [])
        _restricted_evs = state.get('restricted_event_ids', [])
        if any(_murl_pick.startswith(_pfx) for _pfx in _restricted_pfx):
            log.debug('  [SKIP] Restricted market prefix: %s', _murl_pick[:40])
            continue
        if p.get('event_id') and str(p.get('event_id')) in _restricted_evs:
            log.debug('  [SKIP] Restricted event: %s', p.get('event_id'))
            continue
        # Skip if already have active bet on same match+label
        active_key = f"{p['match']}|{p['label']}"
        if active_key in active_keys:
            log.info('  [SKIP] Already active: %s | %s', p['match'][:35], p['label'])
            continue
        # Skip if already bet same event+market (label can differ between scans, e.g. xG value changes)
        _ev_mkt_key = f"{p.get('event_id','')}|{p.get('market_url','')}"
        if _ev_mkt_key and _ev_mkt_key in active_ev_markets:
            log.info('  [SKIP-EVMKT] Duplicate event+market: %s', _ev_mkt_key[:60])
            continue
        # Skip if already hit max bets for this match (1 per match)
        match_name = p['match']
        if match_bets_this_cycle.get(match_name, 0) >= MAX_PER_MATCH:
            log.info('  [SKIP] Max %d bet(s) per match: %s', MAX_PER_MATCH, match_name[:35])
            continue
        # Skip if event exposure exceeds limit (cross-cycle, by event_id)
        _ev_id = p.get('event_id', '')
        if _ev_id:
            _ev_exp = active_events.get(_ev_id, 0)
            _ev_max = bankroll * MAX_EXPOSURE_PER_EVENT
            if _ev_exp + MIN_STAKE > _ev_max:
                log.info('  [SKIP] Event %s exposure $%.2f >= max $%.2f', _ev_id, _ev_exp, _ev_max)
                continue

        # Skip if match exposure exceeds limit
        match_exposure = active_matches.get(match_name, 0)
        max_match_exposure = bankroll * MAX_EXPOSURE_PER_MATCH
        if match_exposure + MIN_STAKE > max_match_exposure:
            log.info('  [SKIP] Match exposure $%.2f >= max $%.2f: %s',
                     match_exposure, max_match_exposure, match_name[:35])
            continue
        # --- Aprendizajes de errores (2026-04-28) ---
        # 1. Match Winner con odds > 2.0: ROI -51%, skip
        _is_winner_mkt = any(w in (p.get('label') or '') for w in ('Winner:', 'winner/', 'home', 'away', 'moneyline'))
        _odds_float = float(p.get('price', 0) or 0)
        if _is_winner_mkt and _odds_float > 2.0:
            log.info('  [SKIP] Match Winner odds %.2f > 2.0 cap: %s', _odds_float, p.get('match','')[:35])
            continue
        # 3. tennis_exact_sets: real WR=0% en 5 bets resueltas (2026-05), desactivado
        if p.get('market_type') == 'tennis_exact_sets':
            log.info('  [SKIP] tennis_exact_sets disabled real WR=0%%: %s', p.get('match','')[:35])
            continue
        # 4. tennis_winner_and_total (Games O/U): capped at $1 per bet
        # 2. Match Winner: stake reducido al 50% vs Sets Under
        _stake_factor = 0.5 if _is_winner_mkt else 1.0
        # 5. Skip if model_prob is 0 or negative (model failed to compute)
        if float(p.get('model_prob', 0) or 0) < 0.01:
            log.info('  [SKIP] model_prob~0 (model error): %s', p.get('match','')[:35])
            continue

        # Portfolio Kelly: correlation-aware sizing
        try:
            from oraculo_portfolio import PortfolioKelly
            _pk = PortfolioKelly(bankroll, MAX_TOTAL_EXPOSURE, MAX_EXPOSURE_PER_MATCH, KELLY_FRAC)
            _pk_result = _pk.optimize([p], state.get('active_bets', []))
            stake = _pk_result[0][1] if _pk_result else 0
        except Exception:
            stake = kelly_stake(bankroll, p['model_prob'], p['price'],
                               state.get('consecutive_losses', 0),
                               sport=p.get('sport', ''))
        stake = round(stake * _stake_factor, 2)
        if stake <= 0:
            if p.get('_max_stake'):
                # Validation bet: force _max_stake even if portfolio is full
                stake = p['_max_stake']
                log.debug('_max_stake forced: PortfolioKelly=0, using $%.2f validation stake', stake)
            else:
                continue
        # Hard cap per pick (applied BEFORE dead-zone so cap is respected)
        if p.get('_max_stake'):
            stake = min(stake, p['_max_stake'])
        # WC Fase C: fixed 1% of wc_reserve stake
        if p.get('_wc_phase_c') and WC_ENABLED:
            _wc_res = state.get('wc_reserve', 0) or bankroll * 0.53
            stake = round(max(_wc_res * WC_STAKE_PCT, 1.00), 2)
            log.info('[WC_FaseC] Fixed stake: $%.2f (1%% of wc_reserve $%.2f)', stake, _wc_res)
        # Per-sport Kelly fractions replace the old dead-zone binary logic
        # LLM said REDUCE: cut stake by 50%
        if p.get('_llm_reduce'):
            stake = round(stake * 0.5, 2)
            log.info('  LLM REDUCE: stake halved to $%.2f', stake)
        stake = min(stake, straight_remaining)

        if dry_run:
            log.info('  [DRY] %s | %s @%.3f | $%.2f | edge +%.1f%%',
                     p['match'][:35], p['label'], p['price'], stake, p['edge'] * 100)
            placed += 1
            continue

        _bet_currency = _choose_currency(state)
        # Fallback: switch currency if chosen one has insufficient balance for this stake
        _bbc_check = state.get('bankroll_by_currency', {})
        if _bbc_check.get(_bet_currency, 0) < stake:
            _alt_curr = 'USDC' if _bet_currency == 'USDT' else 'USDT'
            if _bbc_check.get(_alt_curr, 0) >= stake:
                log.info('  [CURR] Switching %s->%s (balance $%.2f < stake $%.2f)',
                         _bet_currency, _alt_curr, _bbc_check.get(_bet_currency, 0), stake)
                _bet_currency = _alt_curr
            else:
                log.warning('  [SKIP] Insufficient balance in both currencies for stake $%.2f (USDC=%.2f, USDT=%.2f)',
                            stake, _bbc_check.get('USDC', 0), _bbc_check.get('USDT', 0))
                continue
        _orig_currency = api.currency
        api.currency = _bet_currency
        # Sharp reference filter: compare model_prob vs Pinnacle no-vig probability
        if SHARP_REF_ENABLED:
            _mt_sharp = p.get('market_type', '') or ''
            _sport_sharp = p.get('sport', '') or ''
            # Only filter winner/moneyline markets (not set handicaps, BTTS, totals)
            _apply_sharp = (_mt_sharp in ('tennis', 'tennis_team_win_set') or
                            'result_1x2' in _mt_sharp or 'match_winner' in _mt_sharp or
                            (_sport_sharp == 'soccer' and 'asian' in _mt_sharp))
            if _apply_sharp:
                try:
                    from oraculo_clv import get_novig_prob as _get_nvp
                    _pin_prob = _get_nvp(p['match'], _sport_sharp, p.get('label', ''), p.get('league', ''))
                    if _pin_prob is not None:
                        _model_p = float(p.get('model_prob') or 0.5)
                        _discrepancy = abs(_model_p - _pin_prob)
                        if _discrepancy > 0.10:
                            log.info('  [SKIP/SHARP] %s model=%.1f%% pin_novig=%.1f%% diff=%.1f%% — discrepancy too large',
                                     p['match'][:30], _model_p * 100, _pin_prob * 100, _discrepancy * 100)
                            _rejected_keys.add(dedup_key)
                            continue
                        log.debug('  [SHARP OK] model=%.1f%% pin=%.1f%%', _model_p * 100, _pin_prob * 100)
                except Exception as _se:
                    log.debug('Sharp ref error: %s', _se)
        _wc_reserve = state.get('wc_reserve', 0)
        if _wc_reserve > 0 and _bet_currency == 'USDC':
            _usdc_total = state.get('bankroll', 0) - state.get('bankroll_by_currency', {}).get('USDT', 0)  # use total minus USDT; bbc.USDC is stale when deposits bypass it
            _usdc_pending = sum(b.get('stake', 0) for b in state.get('active_bets', [])
                                if b.get('currency', '') == 'USDC' and b.get('status', 'open') == 'open')
            _usdc_effective = _usdc_total - _usdc_pending
            _is_wc_pick = (p.get('cutoff_time', '') or '') >= '2026-06-01' or p.get('league') == 'FIFA_WC'
            if not _is_wc_pick and _usdc_effective - stake < _wc_reserve:
                # Bug fix: try USDT fallback before skipping (don't blacklist — funding constraint ≠ bad pick)
                _usdt_total = state.get('bankroll_by_currency', {}).get('USDT', 0)
                _usdt_pending = sum(b.get('stake', 0) for b in state.get('active_bets', [])
                                    if b.get('currency', '') == 'USDT')
                if _usdt_total - _usdt_pending >= stake:
                    log.info('[WC_RESERVE] USDC blocked (avail=%.2f<reserve=%.2f) — switching to USDT for %s',
                             _usdc_effective, _wc_reserve, p.get('label', '')[:20])
                    _bet_currency = 'USDT'
                    api.currency = 'USDT'
                else:
                    log.info('[WC_RESERVE] Skip: avail_usdc=%.2f avail_usdt=%.2f stake=%.2f reserve=%.2f %s',
                             _usdc_effective, _usdt_total - _usdt_pending, stake, _wc_reserve, p.get('label', '')[:20])
                    api.currency = _orig_currency
                    continue  # no _rejected_keys — retry next cycle
        # Guard pre-placement: detiene fade_teams que escaparon filtros upstream
        if p.get('sport') == 'baseball' and p.get('market_type') == 'mlb_f5_ml':
            _guard_lbl = str(p.get('label', '')).lower()
            _guard_m = re.search(r'f5 ml: (.+?) \(fip', _guard_lbl)
            if _guard_m and _guard_m.group(1).strip() in MLB_FADE_TEAMS_LOWER:
                log.error('[FADE-GUARD] BUG: fade_team "%s" escapo filtros — placement bloqueado', _guard_m.group(1)[:20])
                continue
        log.info('  Placing: %s | %s @%.3f | $%.2f %s', p['match'][:35], p['label'], p['price'], stake, _bet_currency)
        resp = api.place_straight(p['event_id'], p['market_url'], p['price'], stake)
        api.currency = _orig_currency
        if isinstance(resp, dict) and resp.get('RESTRICTED'):
            _ev_id2 = resp.get('event_id', '')
            _blocked_evs2 = state.setdefault('restricted_event_ids', [])
            if _ev_id2 and _ev_id2 not in _blocked_evs2:
                _blocked_evs2.append(_ev_id2)
                if len(_blocked_evs2) > 20:
                    state["restricted_event_ids"] = _blocked_evs2[-20:]
                log.warning('  [BLOCKED] Event %s restricted by Cloudbet', _ev_id2)
            _rejected_keys.add(dedup_key)
            continue
        if resp and isinstance(resp, dict) and resp.get('betId'):
            placed += 1
            consecutive_fails = 0
            state['daily_staked'] += stake
            state['bets_placed_today'] += 1
            straight_remaining -= stake
            bet_id = resp.get('betId', '')
            _save_bet(state, bet_id, p['match'], p['label'], p.get('sport','soccer'),
                      p['price'], stake, edge=p['edge'],
                      event_id=p.get('event_id',''), market_url=p.get('market_url',''),
                      cutoff_time=p.get('cutoff_time',''), currency=_bet_currency,
                      model_prob=p.get('model_prob', 0),
                      market_type=p.get('market_type', ''))
            log.info('  [OK] Bet placed: %s | ID: %s', p['label'], bet_id[:12])
            send_whatsapp(_wa_bet_msg(p, stake, state['daily_pnl']))
            match_bets_this_cycle[p['match']] = match_bets_this_cycle.get(p['match'], 0) + 1
            active_matches[p['match']] = active_matches.get(p['match'], 0) + stake
            if p.get('event_id',''):
                active_events[p['event_id']] = active_events.get(p['event_id'], 0) + stake
            _placed_ev_mkt = f"{p.get('event_id','')}|{p.get('market_url','')}"
            if _placed_ev_mkt.strip('|'):
                active_ev_markets.add(_placed_ev_mkt)
            # save_state called by _save_bet
            # Track prediction for backtest
            _log_prediction(p['match'], p['label'],
                           p.get('raw_model_prob', p['model_prob']),  # always log raw prob
                           p['edge'], p['price'], stake, bet_id, p.get('sport', 'soccer'),
                           signal=p.get('signal', 'model'),
                           league=p.get('league', ''),
                           event_id=p.get('event_id', ''),
                           market_type=p.get('market_type'))  # 2026-06-02 fix
            # Portfolio Kelly: registrar correlacion post-placement (solo tracking — no cancela el bet)
            # NOTE: el sizing real ya fue calculado por PortfolioKelly pre-placement.
            # Bug fix: 'continue' aqui hacia que sibila_placed no se llamara para bets ya colocados.
            if _PORTFOLIO_ENABLED:
                _port_result = _PORTFOLIO.get_adjusted_stake(p, base_stake=stake)
                if _port_result.get('skip'):
                    log.warning('  [Portfolio-post] Would have skipped (bet already placed): %s', _port_result.get('reason',''))
                else:
                    p['portfolio_adj']  = _port_result['stake_adj']
                    p['portfolio_corr'] = _port_result.get('corr_penalty', 0)
                    p['portfolio_capped'] = _port_result.get('capped', False)
            if _SIBILA_ENABLED:
                _sibila_placed(p['match'], p['label'], bet_id, stake)
            # RLM boost: si sharp money confirma nuestro pick -> +20% stake
            if _RLM_ENABLED and p.get('rlm_signal') and p.get('rlm_score', 0) >= 0.5:
                _rlm_boost = min(stake * 1.20, stake + 20)  # max +20 unidades
                log.info('  [RLM] Stake boost %.2f->%.2f (score=%.2f)', stake, _rlm_boost, p.get('rlm_score',0))
                stake = round(_rlm_boost, 2)
        elif isinstance(resp, dict) and resp.get('INSUFFICIENT_FUNDS'):
            # Currency balance exhausted — retry immediately with the other currency
            _retry_curr = 'USDC' if _bet_currency == 'USDT' else 'USDT'
            log.info('  [CURR] INSUFFICIENT_FUNDS on %s — retrying with %s', _bet_currency, _retry_curr)
            api.currency = _retry_curr
            resp2 = api.place_straight(p['event_id'], p['market_url'], p['price'], stake)
            api.currency = _orig_currency
            if resp2 and isinstance(resp2, dict) and resp2.get('betId'):
                placed += 1
                consecutive_fails = 0
                state['daily_staked'] += stake
                state['bets_placed_today'] += 1
                straight_remaining -= stake
                bet_id = resp2.get('betId', '')
                _save_bet(state, bet_id, p['match'], p['label'], p.get('sport','soccer'),
                          p['price'], stake, edge=p['edge'],
                          event_id=p.get('event_id',''), market_url=p.get('market_url',''),
                          cutoff_time=p.get('cutoff_time',''), currency=_retry_curr,
                          model_prob=p.get('model_prob', 0),
                          market_type=p.get('market_type', ''))
                log.info('  [OK] Bet placed (retry %s): %s | ID: %s', _retry_curr, p['label'], bet_id[:12])
                send_whatsapp(_wa_bet_msg(p, stake, state['daily_pnl']))
                match_bets_this_cycle[p['match']] = match_bets_this_cycle.get(p['match'], 0) + 1
                active_matches[p['match']] = active_matches.get(p['match'], 0) + stake
                if p.get('event_id',''):
                    active_events[p['event_id']] = active_events.get(p['event_id'], 0) + stake
                _retry_ev_mkt = f"{p.get('event_id','')}|{p.get('market_url','')}"
                if _retry_ev_mkt.strip('|'):
                    active_ev_markets.add(_retry_ev_mkt)
                if _SIBILA_ENABLED:
                    _sibila_placed(p['match'], p['label'], bet_id, stake)
            else:
                _rejected_keys.add(dedup_key)
                consecutive_fails += 1
                log.warning('  [FAIL] %s — both currencies insufficient (%d consecutive)', p['label'], consecutive_fails)
                if consecutive_fails >= 3:
                    log.error('  ABORT: %d consecutive failures', consecutive_fails)
                    break
        else:
            _rejected_keys.add(dedup_key)
            consecutive_fails += 1
            log.warning('  [FAIL] %s (%d consecutive)', p['label'], consecutive_fails)
            if consecutive_fails >= 3:
                log.error('  ABORT: %d consecutive failures, API likely rate-limited', consecutive_fails)
                break
        time.sleep(2.0)  # Respect Cloudbet rate limit (2 req/s max)

    # Restore remaining: unused straight budget + parlay reserve
    remaining = straight_remaining + parlay_reserve
    # --- Parlays ---
    for par in parlays:
        if remaining < MIN_STAKE:
            break
        # Fixed stake by parlay type
        sport = par.get('sport', 'tennis')
        if sport == 'mixed':
            stake = bankroll * MIXED_PARLAY_STAKE_PCT     # 3%
        elif sport == 'soccer':
            stake = bankroll * FOOTBALL_PARLAY_STAKE_PCT  # 3%
        else:
            stake = bankroll * TENNIS_PARLAY_STAKE_PCT    # 5%
        stake = round(stake, 2)
        if stake < MIN_STAKE:
            continue
        stake = min(stake, remaining)

        if dry_run:
            log.info('  [DRY] PARLAY %d-leg: %s @%.2f | $%.2f | edge +%.1f%%',
                     par['n_legs'], par['label'], par['combined_odds'], stake, par['edge'] * 100)
            placed += 1
            continue

        _bet_currency = _choose_currency(state)
        _orig_currency = api.currency
        api.currency = _bet_currency
        log.info('  Placing PARLAY: %s @%.2f | $%.2f %s', par['label'], par['combined_odds'], stake, _bet_currency)
        resp = api.place_parlay(par['selections'], stake)
        api.currency = _orig_currency
        if resp and isinstance(resp, dict) and resp.get('betId'):
            placed += 1
            consecutive_fails = 0
            state['daily_staked'] += stake
            state['bets_placed_today'] += 1
            remaining -= stake
            bet_id = resp.get('betId', '')
            _parlay_eids = [s.get('eventId', s.get('event_id', '')) for s in par.get('selections', [])]
            _save_bet(state, bet_id, 'PARLAY: '+par['label'], str(par['n_legs'])+'-leg parlay',
                      'tennis', par['combined_odds'], stake, edge=par['edge'],
                      currency=_bet_currency,
                      event_ids=_parlay_eids)  # persisted atomically with save_state
            log.info('  [OK] Parlay placed: %s', bet_id[:12])
            send_whatsapp(
                f"🎰 Parlay ✅ ({par['n_legs']} picks)\n"
                f"{par['label'][:60]}\n"
                f"💰 Odds: {par['combined_odds']:.2f} | Edge: +{par['edge']*100:.1f}%\n"
                f"💵 Apostado: ${stake:.2f} | Ganancia est: +${stake*(par['combined_odds']-1):.2f}\n"
                f"📊 Dia: {'+' if state['daily_pnl'] >= 0 else '-'}${abs(state['daily_pnl']):.2f}"
            )
        time.sleep(2.0)  # Respect rate limit

    log.info('Placed %d bets, staked today: $%.2f / $%.2f',
             placed, state['daily_staked'], max_today)
    return placed

# ---------------------------------------------------------------------------
# Check results via API
# ---------------------------------------------------------------------------
def check_results(api, state):
    """Poll Cloudbet for settled bets, update P&L."""
    log.info('=== CHECKING RESULTS ===')
    # Auto-populate cutoff_time for bets that don't have it yet (one-time backfill)
    _need_ct = [b for b in state.get('active_bets', []) if not b.get('cutoff_time') and b.get('event_id')]
    if _need_ct:
        import requests as _rq2
        _s2 = _rq2.Session()
        _cfg2 = json.load(open(os.path.join(SCRIPT_DIR, 'cloudbet_config.json')))
        _s2.headers.update({'X-API-Key': _cfg2.get('api_key',''), 'Accept': 'application/json'})
        _seen_eids = {}
        for _b2 in _need_ct:
            eid2 = str(_b2.get('event_id',''))
            if eid2 in _seen_eids:
                _b2['cutoff_time'] = _seen_eids[eid2]
                continue
            try:
                _er = _s2.get(CB_BASE + '/pub/v2/odds/events/' + eid2, timeout=6)
                if _er.status_code == 200:
                    _ct = _er.json().get('cutoffTime', '')[:10]
                    _b2['cutoff_time'] = _ct
                    _seen_eids[eid2] = _ct
            except Exception:
                pass
        log.debug('Backfilled cutoff_time for %d bets', len(_need_ct))
    # Warn about stale pending bets (>7 days old)
    from datetime import timedelta
    _today = datetime.now().isoformat()[:10]
    for _ab in state.get('active_bets', []):
        # Use event cutoff_time if available (more accurate than placed_at)
        _event_date = str(_ab.get('cutoff_time', _ab.get('placed_at', _ab.get('placed', ''))))[:10]
        _placed_date = str(_ab.get('placed_at', _ab.get('placed', '')))[:10]
        # Only warn if BOTH: placed >7 days ago AND event date is in the future (genuinely pending)
        _placed_threshold = (datetime.now() - timedelta(days=7)).isoformat()[:10]
        if _placed_date and _placed_date < _placed_threshold and _event_date >= _today:
            log.info('PENDING (pre-event, placed %s, plays %s): %s | %s | $%.2f', 
                     _placed_date, _event_date, _ab.get('match','?')[:30], _ab.get('label','?')[:20], _ab.get('stake',0))
        elif _placed_date and _placed_date < _placed_threshold and (not _event_date or _event_date < _today):
            log.warning('STALE BET - event may have passed: %s | %s | $%.2f | event=%s', 
                        _ab.get('match','?')[:30], _ab.get('label','?')[:20], _ab.get('stake',0), _event_date)
    # In-play monitoring: check active bets for cash-out signals
    try:
        from oraculo_inplay import check_cashout_opportunities
        _ip_alerts = check_cashout_opportunities(api, state)
        if _ip_alerts:
            log.info('[InPlay] %d active bets in danger', len(_ip_alerts))
    except Exception:
        pass
    bets = api.get_bets(limit=500, days_back=120)
    if not bets:
        log.info('No bets found from API')
        return 0

    settled = 0
    known_ids = {b.get('bet_id') for b in state.get('active_bets', [])}
    # Persistent set of all-time processed bet IDs (avoids double-counting across days)
    processed_ids = set(state.get('all_processed_ids', []))

    for b in bets:
        if not b.get('isSettled'):
            continue
        bet_id = b.get('betId', '')
        result = b.get('result', '')
        wl = float(b.get('winLoss', 0))
        stake = float(b.get('stake', 0))

        # Already processed? Check persistent set + today's settlements
        if bet_id in processed_ids:
            continue
        if any(s.get('bet_id') == bet_id for s in state.get('settled_today', [])):
            continue

        # Find in active bets
        matched_active = None
        for ab in state['active_bets']:
            if ab.get('bet_id') == bet_id:
                matched_active = ab
                break

        if result == 'WIN':
            state['bankroll'] += wl
            state['total_pnl'] += wl
            state['daily_pnl'] += wl
            state['wins'] = state.get('wins', 0) + 1
            state['consecutive_losses'] = 0
            # Per-currency tracking
            _curr = matched_active.get('currency', 'USDC') if matched_active else 'USDC'
            _bbc = state.setdefault('bankroll_by_currency', {'USDC': INITIAL_DEPOSIT_USDC, 'USDT': INITIAL_DEPOSIT_USDT})
            _bbc[_curr] = _bbc.get(_curr, 0) + wl
            log.info('  WIN: $%+.2f | %s', wl,
                     matched_active.get('match', bet_id[:12]) if matched_active else bet_id[:12])
        elif result == 'LOSS':
            # Use actual loss from API (wl is negative); stake is what we placed
            # If partial fill: API stake may differ from local stake — wl is authoritative
            _actual_loss = abs(wl) if wl != 0 else stake
            state['bankroll'] -= _actual_loss
            state['total_pnl'] -= _actual_loss
            state['daily_pnl'] -= _actual_loss
            state['losses'] = state.get('losses', 0) + 1
            state['consecutive_losses'] = state.get('consecutive_losses', 0) + 1
            # Per-currency tracking
            _curr = matched_active.get('currency', 'USDC') if matched_active else 'USDC'
            _bbc = state.setdefault('bankroll_by_currency', {'USDC': INITIAL_DEPOSIT_USDC, 'USDT': INITIAL_DEPOSIT_USDT})
            _bbc[_curr] = _bbc.get(_curr, 0) - _actual_loss
            log.info('  LOSS: $-%.2f | %s', _actual_loss,
                     matched_active.get('match', bet_id[:12]) if matched_active else bet_id[:12])
        elif result in ('VOID', 'PUSH', 'HALF_WIN', 'HALF_LOSS', 'PARTIAL'):
            log.info('  %s: %s', result, bet_id[:12])
            _curr = matched_active.get('currency', 'USDC') if matched_active else 'USDC'
            _bbc = state.setdefault('bankroll_by_currency', {'USDC': INITIAL_DEPOSIT_USDC, 'USDT': INITIAL_DEPOSIT_USDT})
            # HALF_WIN: only profit (wl) added -- stake never removed at placement.
            # HALF_LOSS: winLoss = -stake/2 (stake reduction happens here).
            # VOID/PUSH: stake was never deducted at placement -- no-op.
            if result == 'HALF_WIN' and wl > 0:
                # bankroll model: stake never removed at placement
                # so only add profit (wl), not wl+stake/2
                state['bankroll'] += wl
                state['total_pnl'] += wl
                state['daily_pnl'] += wl
                _bbc[_curr] = round(_bbc.get(_curr, 0) + wl, 5)
            elif result in ('VOID', 'PUSH'):
                # Stake was never removed at placement — returning it is a no-op
                pass
            elif wl != 0:  # HALF_LOSS or PARTIAL
                state['bankroll'] += wl
                state['total_pnl'] += wl
                state['daily_pnl'] += wl
                _bbc[_curr] = round(_bbc.get(_curr, 0) + wl, 5)

        state['settled_today'].append({
            'bet_id': bet_id, 'result': result, 'winLoss': wl,
            'stake': stake, 'settled': datetime.now().isoformat(),
            'match': matched_active.get('match', '') if matched_active else '',
        })
        _log_settlement(bet_id, result, wl)
        if matched_active:
            _write_bet_history(
                bet_id=bet_id,
                match=matched_active.get('match', ''),
                league=matched_active.get('league', ''),
                sport=matched_active.get('sport', ''),
                label=matched_active.get('label', ''),
                market_type=matched_active.get('market_type', ''),
                placed_at=matched_active.get('placed', ''),
                settled_at=datetime.now().isoformat(),
                stake=float(matched_active.get('stake', 0) or 0),
                price=float(matched_active.get('price', 0) or 0),
                model_prob=float(matched_active.get('model_prob', 0) or 0),
                edge=float(matched_active.get('edge', 0) or 0),
                result=result,
                pnl=float(wl),
                currency=matched_active.get('currency', 'USDC'),
            )
        # CLV Oracle: calcular CLV con Betfair/Pinnacle como referencia
        _clv_data = {}
        if _CLV_ORACLE_ENABLED and matched_active:
            try:
                _clv_data = _CLV_ORACLE.resolve_clv_for_pick(
                    matched_active,
                    entry_odds=matched_active.get('price') or matched_active.get('odds'),
                    settled_ts=int(time.time())
                )
            except Exception as _clve:
                log.warning('[CLV] resolve error: %s', _clve)
        if _SIBILA_ENABLED:
            _closing = (_clv_data.get('closing_odds_betfair')
                        or _clv_data.get('closing_odds_pinnacle'))
            _sibila_resolve(
                bet_id=bet_id, result=result,
                closing_odds=_closing,
            )
            if matched_active:
                _n_shad = _sibila_resolve_shadow(
                    match=matched_active.get('match', ''),
                    side=matched_active.get('label', ''),
                    result=result,
                    closing_odds=_closing,
                )
                if _n_shad:
                    log.debug('[Sibila] %d shadow picks resolved for %s',
                              _n_shad, matched_active.get('match', '')[:30])
                _fm_fade = re.search(r'F5 ML: (.+?) \(FIP', matched_active.get('label', ''))
                if _fm_fade:
                    _n_fade = _sibila_resolve_fade(
                        match=matched_active.get('match', ''),
                        bet_team=_fm_fade.group(1).strip(),
                        result=result,
                        closing_odds=_closing,
                    )
                    if _n_fade:
                        log.info('[Sibila Fade] %d fade picks resolved for %s',
                                 _n_fade, matched_active.get('match', '')[:30])
        if _PORTFOLIO_ENABLED:
            _PORTFOLIO.invalidate_cache()  # bet cerrada -> recalcular portafolio
        # Telegram notification for each settlement
        try:
            _tg_emoji = chr(9989) if result == 'WIN' else chr(10060) if result == 'LOSS' else chr(9208)
            _tg_match = matched_active.get('match', bet_id[:12]) if matched_active else bet_id[:12]
            _tg_label = matched_active.get('label', '') if matched_active else ''
            _tg_pnl = wl if result == 'WIN' else -stake if result == 'LOSS' else wl
            _tg_clv = matched_active.get('clv') if matched_active else None
            _tg_clv_str = f' | CLV: {_tg_clv:+.1%}' if _tg_clv is not None else ''
            _tg_prob = matched_active.get('model_prob') if matched_active else None
            _tg_odds = matched_active.get('odds', matched_active.get('price', 0)) if matched_active else 0
            _tg_prob_str = f' @{_tg_odds:.2f} (model {_tg_prob:.0%})' if _tg_prob else f' @{_tg_odds:.2f}'
            send_telegram(
                f'{_tg_emoji} *{result}*: ${_tg_pnl:+.2f}{_tg_clv_str} | {_tg_match}\n'
                f'{_tg_label}{_tg_prob_str}\n'
                f'Bankroll: ${state["bankroll"]:.2f} | Today: ${state["daily_pnl"]:+.2f}'
            )
            send_whatsapp(
                f"{_tg_emoji} {result}\n"
                f"{_tg_match}\n"
                f"📌 {_tg_label}{_tg_prob_str}\n"
                f"💰 Resultado: {'+' if _tg_pnl >= 0 else '-'}${abs(_tg_pnl):.2f}\n"
                f"📊 Dia: {'+' if state['daily_pnl'] >= 0 else '-'}${abs(state['daily_pnl']):.2f}"
            )
        except Exception as _tg_err:
            log.debug('Telegram settlement notification failed: %s', _tg_err)
        # Learn tennis results for Elo update
        if matched_active and matched_active.get('sport') == 'tennis':
            _learn_tennis_result(
                matched_active.get('match', ''),
                result,
                matched_active.get('label', ''))
        # LLM post-match analysis (async, non-blocking)
        if matched_active:
            _llm_postmatch_learn(matched_active, result, wl)
        # Remove from active
        state['active_bets'] = [ab for ab in state['active_bets'] if ab.get('bet_id') != bet_id]
        processed_ids.add(bet_id)
        # Persist to bet_history (used for per-sport settled count, auto-tune, etc.)
        _hist_entry = {
            'bet_id': bet_id,
            'result': result,
            'winLoss': wl,
            'stake': stake,
            'match': matched_active.get('match', '') if matched_active else '',
            'label': matched_active.get('label', '') if matched_active else '',
            'sport': matched_active.get('sport', '') if matched_active else '',
            'market_type': matched_active.get('market_type', '') if matched_active else '',
            'odds': matched_active.get('odds', 0) if matched_active else 0,
            'edge': matched_active.get('edge', 0) if matched_active else 0,
            'model_prob': matched_active.get('model_prob', 0) if matched_active else 0,
            'clv': matched_active.get('clv') if matched_active else None,
            'settled': datetime.now().isoformat(),
        }
        bh = state.setdefault('bet_history', [])
        bh.append(_hist_entry)
        if len(bh) > 500:
            state['bet_history'] = bh[-500:]
        settled += 1

    # Persist processed IDs (keep last 200 to avoid unbounded growth)
    all_ids = list(processed_ids)
    state['all_processed_ids'] = all_ids[-1000:] if len(all_ids) > 1000 else all_ids

    if settled:
        log.info('Settled %d bets | P&L today: $%+.2f | Bankroll: $%.2f',
                 settled, state['daily_pnl'], state['bankroll'])
    else:
        log.info('No new settlements')
    # CLV semaphore check (every cycle)
    try:
        from oraculo_sibila import clv_report as _clv_report
        _clv = _clv_report(window=20, min_picks=5)
        for _alert in _clv.get("alerts", []):
            log.warning("[CLV ALERT] %s — CLV below -3%%, model may have lost edge", _alert)
            try:
                _send_telegram("*[CLV ALERTA]* " + _alert + " — revisar calibracion del scanner")
            except Exception:
                pass
    except Exception as _e:
        log.debug("CLV report error: %s", _e)

        # Update CLV for settled bets (non-blocking)
        if _CLV_ENABLED:
            try:
                _n_clv = record_closing_for_settled(PREDICTIONS_FILE)
                if _n_clv:
                    log.info('CLV: updated %d bets with closing odds', _n_clv)
            except Exception as _e_clv:
                log.debug('CLV update error: %s', _e_clv)

    state['last_result_check'] = datetime.now().isoformat()
    # Full recalculation of ROI stats after settlements
    if settled:
        _recalculate_roi_by_type()
        _recalibrate_model()
    return settled

# ---------------------------------------------------------------------------
# Bankroll reconciliation
# ---------------------------------------------------------------------------
def reconcile_bankroll(api, state):
    """Reconcile local bankroll with Cloudbet actual balance."""
    try:
        bets = api.get_bets(limit=500, days_back=120)
        if not bets:
            return
        # Sum all pending stakes
        pending_stake = sum(float(b.get('stake', 0)) for b in bets if not b.get('isSettled'))
        # Sum all settled P&L
        settled_pnl = sum(float(b.get('winLoss', 0)) for b in bets if b.get('isSettled'))
        settled_stakes = sum(float(b.get('stake', 0)) for b in bets
                            if b.get('isSettled') and b.get('result') == 'LOSS')

        # Log discrepancy if any
        local_bankroll = state['bankroll']
        local_active = sum(b.get('stake', 0) for b in state.get('active_bets', []))
        if abs(local_active - pending_stake) > 1.0:
            log.warning('RECONCILE: local active=$%.2f vs API pending=$%.2f (diff=$%.2f)',
                        local_active, pending_stake, local_active - pending_stake)
        # Store for dashboard
        state['_reconcile'] = {
            'api_pending_stake': round(pending_stake, 2),
            'api_settled_pnl': round(settled_pnl, 2),
            'local_bankroll': round(local_bankroll, 2),
            'local_active_stake': round(local_active, 2),
            'checked': datetime.now().isoformat(),
        }

        # Auto-correct bankroll if drift > $0.25
        INITIAL_DEPOSIT = 57.03 + state.get('extra_deposits', 0)
        all_settled_pnl = sum(float(b.get('winLoss', 0)) for b in bets if b.get('isSettled'))
        all_pending_stake = sum(float(b.get('stake', 0)) for b in bets if not b.get('isSettled'))
        correct_bankroll = INITIAL_DEPOSIT + all_settled_pnl + all_pending_stake + state.get('cumulative_void_returns', 0)
        drift = abs(state['bankroll'] - correct_bankroll)
        if drift > 1.50:  # raised from 0.25 to kill ±$3 tennis-bet oscillation
            log.warning('RECONCILE: Auto-correcting bankroll $%.2f -> $%.2f (drift=$%.2f)',
                        state['bankroll'], correct_bankroll, drift)
            if drift > 5.0:
                _rmsg = ('Oraculo RECONCILE ALERT | drift=$%.2f'
                         ' local=$%.2f -> correct=$%.2f'
                         ' settled=$%.2f pending=$%.2f') % (
                         drift, state['bankroll'], correct_bankroll,
                         all_settled_pnl, all_pending_stake)
                send_telegram(_rmsg)
            state['bankroll'] = round(correct_bankroll, 2)
            # Sync total_pnl to match API-derived settled PnL (avoids silent drift accumulation)
            api_total_pnl = round(all_settled_pnl, 2)
            local_pnl = round(state.get('total_pnl', 0), 2)
            pnl_drift = abs(local_pnl - api_total_pnl)
            if pnl_drift > 0.25:
                log.warning('RECONCILE: total_pnl drift local=$%.2f vs api=$%.2f — correcting',
                            local_pnl, api_total_pnl)
                state['total_pnl'] = api_total_pnl
        # bankroll_by_currency is maintained by settlement code (WIN/LOSS adjustments).
        # Reconciler does NOT overwrite it — the per-currency formula requires per-currency
        # initial deposit constants that are structurally inconsistent with the total.
        # Only log for diagnostics.
        _bbc = state.get('bankroll_by_currency', {})
        for curr in ('USDC', 'USDT'):
            c_pending = sum(float(b.get('stake', 0)) for b in bets
                           if not b.get('isSettled') and b.get('currency') == curr)
            c_free = _bbc.get(curr, 0) - c_pending
            log.debug('RECONCILE bbc[%s]: wallet=%.2f pending=%.2f free=%.2f',
                      curr, _bbc.get(curr, 0), c_pending, c_free)

        # --- Ghost bet pruning: remove local bets that API no longer knows about ---
        # Cloudbet silently voids markets (e.g. tennis.exact_sets) returning 404 with no history entry.
        # Threshold: >1 day old + absent from API (500-bet window) = treat as VOID, return stake.
        api_bet_ids = {b.get('betId', '') for b in bets}
        now = datetime.now()
        pruned = []
        kept = []
        for ab in state.get('active_bets', []):
            placed_str = ab.get('placed', '') or ab.get('placed_at', '')
            try:
                placed_dt = datetime.fromisoformat(placed_str.replace('Z', '+00:00').split('+')[0])
            except (ValueError, TypeError):
                placed_dt = now  # keep if we can't parse date
            age_hours = (now - placed_dt.replace(tzinfo=None)).total_seconds() / 3600
            bet_id = ab.get('bet_id', '')
            # If bet is >24h old and NOT in API response (neither pending nor settled), it was voided
            if age_hours > 24 and bet_id not in api_bet_ids:
                pruned.append(ab)
            else:
                kept.append(ab)
        if pruned:
            state['active_bets'] = kept
            pruned_stake = sum(p.get('stake', 0) for p in pruned)
            for p in pruned:
                age_h = (now - datetime.fromisoformat(p.get('placed', now.isoformat()))).total_seconds() / 3600
                log.warning('PRUNE ghost bet (VOID+stake_returned): %s | %s | age=%.0fh | stake=$%.2f',
                            p.get('bet_id', '')[:12], p.get('match', ''), age_h, p.get('stake', 0))
                # Stake never removed at placement -- bankroll unchanged on ghost void.
                # cumulative_void_returns compensates for ghost disappearing from API pending list.
                stake_back = float(p.get('stake', 0))
                state['cumulative_void_returns'] = round(state.get('cumulative_void_returns', 0) + stake_back, 5)
            _pruned_ids = [p.get('bet_id', '') for p in pruned if p.get('bet_id')]
            _pids2 = list(set(state.get('all_processed_ids', []) + _pruned_ids))
            state['all_processed_ids'] = _pids2[-1000:]
            log.info('Pruned %d ghost bets — stake $%.2f returned (VOID) | new bankroll: $%.2f',
                     len(pruned), pruned_stake, state['bankroll'])
        # --- Auto-void bets stuck PENDING in API >20 days ---
        api_pending_by_id = {b.get('betId',''): b for b in bets if not b.get('isSettled')}
        voided = []
        kept2 = []
        for ab in state.get('active_bets', []):
            placed_str = ab.get('placed', '') or ab.get('placed_at', '')
            try:
                placed_dt = datetime.fromisoformat(placed_str.replace('Z', '+00:00').split('+')[0])
                age_days = (now - placed_dt.replace(tzinfo=None)).days
            except Exception:
                age_days = 0
            bet_id = ab.get('bet_id', '')
            api_bet = api_pending_by_id.get(bet_id)
            if api_bet and age_days > 20:
                voided.append(ab)
                log.warning('AUTO-VOID stuck bet: %s | %s | age=%dd | stake=$%.2f',
                            bet_id[:12], ab.get('match', '')[:30], age_days, ab.get('stake', 0))
            else:
                kept2.append(ab)
        if voided:
            state['active_bets'] = kept2
            _voided_ids = [v.get('bet_id', '') for v in voided if v.get('bet_id')]
            _pids = list(set(state.get('all_processed_ids', []) + _voided_ids))
            state['all_processed_ids'] = _pids[-1000:]
            log.info('Auto-voided %d stuck bets (total stake=$%.2f) — treated as VOID, no P&L change',
                     len(voided), sum(v.get('stake', 0) for v in voided))
        # --- Detect bets in API but missing locally ---
        local_bet_ids = {ab.get('bet_id', '') for ab in state.get('active_bets', [])}
        for b in bets:
            if b.get('isSettled'):
                continue
            bid = b.get('betId', '')
            if bid and bid not in local_bet_ids:
                sel = b.get('selection', {})
                _match = sel.get('eventName', sel.get('eventId', ''))
                if _save_bet(state, bid, _match, sel.get('marketUrl',''), 'soccer',
                             float(b.get('price',0)), float(b.get('stake',0)),
                             source='api_sync_reconcile'):
                    log.info('RECONCILE: added missing bet %s (stake=$%.2f)', bid[:12], float(b.get('stake',0)))
    except Exception as e:
        log.debug('Reconcile bankroll failed: %s', e)




def _llm_postmatch_learn(bet_info, result, win_loss):
    """
    Ask LLM to analyze why a bet won or lost.
    Saves insights to llm_postmatch_log.jsonl.
    Non-blocking: runs in background thread.
    """
    import threading
    def _analyze():
        try:
            from oraculo_llm import OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT
            import requests as _rq, re as _re
            match = bet_info.get('match', '?')
            label = bet_info.get('label', '?')
            odds = bet_info.get('odds', 0)
            stake = bet_info.get('stake', 0)
            edge = bet_info.get('edge', 0)
            model_prob = bet_info.get('model_prob', 0)
            sport = bet_info.get('sport', 'unknown')
            outcome = 'WON' if result == 'WIN' else 'LOST'
            implied = (1/odds) if odds > 0 else 0
            prompt = (
                "You are a sports betting analyst. Analyze why this bet " + outcome + ".\n"
                "Be concise (2-3 sentences). Focus on what the model got right or missed.\n\n"
                "BET: " + match + "\nSELECTION: " + label + "\nSPORT: " + sport + "\n"
                "MODEL PROB: " + str(round(model_prob*100, 1)) + "% | "
                "IMPLIED: " + str(round(implied*100, 1)) + "% | "
                "EDGE: " + str(round(edge*100, 1)) + "%\n"
                "ODDS: " + str(odds) + " | STAKE: $" + str(stake) +
                " | OUTCOME: " + outcome + " (P&L: $" + str(round(win_loss, 2)) + ")\n\n"
                "Format: LESSON: <your analysis>"
            )
            r = _rq.post(OLLAMA_URL, json={
                'model': OLLAMA_MODEL,
                'prompt': prompt,
                'stream': False,
                'options': {'num_predict': 120, 'temperature': 0.3}
            }, timeout=OLLAMA_TIMEOUT)
            if r.status_code != 200:
                return
            text = r.json().get('response', '').strip()
            m = _re.search(r'LESSON:\s*(.+)', text, _re.IGNORECASE | _re.DOTALL)
            lesson = m.group(1).strip() if m else text[:200]
            entry = {
                'ts': datetime.now().isoformat(),
                'match': match, 'label': label, 'sport': sport,
                'odds': odds, 'model_prob': model_prob, 'edge': edge,
                'stake': stake, 'outcome': outcome, 'pnl': win_loss,
                'lesson': lesson,
            }
            log_file = os.path.join(SCRIPT_DIR, 'llm_postmatch_log.jsonl')
            with open(log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
            log.info('[LLM Learn] %s %s -> %s', outcome, match[:35], lesson[:80])
            if outcome == 'LOST' and abs(win_loss) >= 3.0:
                send_telegram(
                    'LLM Post-Match\n'
                    'PERDIDA: ' + match + '\n'
                    'Leccion: ' + lesson[:200]
                )
        except Exception as e:
            log.debug('LLM postmatch failed: %s', e)
    threading.Thread(target=_analyze, daemon=True).start()

def _learn_tennis_result(match_name, result, label):
    """Feed settled tennis result back into Elo cache."""
    try:
        if "Winner" not in label:
            return
        if " vs " not in match_name:
            return
        parts = match_name.split(" vs ")
        if len(parts) != 2:
            return
        player_a = parts[0].strip()
        player_b = parts[1].strip()

        picked = label.replace("Winner: ", "").replace("Winner:", "").strip()

        # Normalize: check if picked name matches player_a (either is substring of other)
        _pa_low = player_a.lower()
        _pb_low = player_b.lower()
        _pk_low = picked.lower()
        # picked matches player_a if any word of picked appears as last name in player_a
        _pk_words = set(_pk_low.split())
        _pa_words = set(_pa_low.split())
        _matched_a = bool(_pk_words & _pa_words) or (_pk_low in _pa_low) or (_pa_low in _pk_low)
        if result == "WIN":
            winner = picked
            loser = player_b if _matched_a else player_a
        elif result == "LOSS":
            loser = picked
            winner = player_b if _matched_a else player_a
        else:
            return

        results_file = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis', 'recent_results.json')
        os.makedirs(os.path.dirname(results_file), exist_ok=True)
        results = []
        if os.path.exists(results_file):
            try:
                results = json.load(open(results_file))
            except Exception:
                results = []

        today = datetime.now().strftime('%Y-%m-%d')
        key = winner + '|' + loser + '|' + today
        existing_keys = set(r['winner'] + '|' + r['loser'] + '|' + r['date'] for r in results)
        if key in existing_keys:
            return

        results.append({
            'winner': winner, 'loser': loser,
            'date': today, 'surface': 'hard',
            'source': 'cloudbet_settled',
        })
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2)
        log.info('Tennis Elo learned: %s beat %s', winner, loser)
    except Exception as e:
        log.debug('Tennis learn failed: %s', e)

# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = '1521532947'
TELEGRAM_ENABLED = True

def send_telegram(msg):
    """Send alert via Telegram. Fire-and-forget."""
    if not TELEGRAM_ENABLED:
        return
    try:
        import requests as _req
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        _req.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                             'parse_mode': 'Markdown'}, timeout=5)
    except Exception:
        pass

def _wa_bet_msg(p, stake, daily_pnl=None):
    """Build WA notification card — one field per line, matches reference format."""
    sport = p.get('sport', 'soccer')
    sport_icons = {'soccer': '⚽ Futbol', 'tennis': '🎾 Tennis', 'baseball': '⚾ Beisbol'}
    hdr = '🏆 WC' if p.get('_wc_phase_c') else sport_icons.get(sport, '🎰 Apuesta')
    league = p.get('league', '')
    # date from cutoff_time
    ct = p.get('cutoff_time', '')
    try:
        from datetime import datetime as _dt
        _d = _dt.strptime(ct[:19], '%Y-%m-%dT%H:%M:%S')
        date_str = _d.strftime('%-d %b · %H:%M')
    except Exception:
        date_str = ''
    label = p.get('label', '')
    odds  = p.get('price', 0)
    edge  = p.get('edge', 0) * 100
    conf  = p.get('model_prob', 0) * 100
    ev    = stake * (odds - 1)
    lines = [f'{hdr} ✅', p['match'][:50]]
    if league or date_str:
        loc_line = ''
        if league:
            loc_line += f'🏟 {league}'
        if date_str:
            loc_line += f' · 🕐 {date_str}'
        lines.append(loc_line.strip())
    elo = p.get('elo_diff')
    if elo:
        lines.append(f'📊 ELO diff: {elo:.0f} pts')
    lines += [
        f'📌 {label}',
        f'💰 Odds: {odds:.2f} (Cloudbet)',
        f'📈 Edge: +{edge:.2f}%',
        f'🎯 Conf: {conf:.1f}%',
        f'💵 Apostado: ${stake:.2f}',
        f'📊 Ganancia est: +${ev:.2f} (retorno: ${stake+ev:.2f})',
    ]
    if daily_pnl is not None:
        _ds = '+' if daily_pnl >= 0 else '-'
        lines.append(f'📊 Dia: {_ds}${abs(daily_pnl):.2f}')
    return '\n'.join(lines)

def send_whatsapp(msg):
    """Send alert to WhatsApp group via Baileys :3001. Fire-and-forget."""
    try:
        import requests as _wq
        plain = msg.replace("*", "")
        _wq.post(
            "http://127.0.0.1:3001/send",
            json={"chatId": "120363427170639397@g.us", "message": plain},
            timeout=5)
    except Exception:
        pass

def check_alerts(state):
    """Send Telegram alerts for important events."""
    # Alert on loss streak
    streak = state.get('consecutive_losses', 0)
    if streak >= 3 and not state.get('_alerted_streak'):
        send_telegram(f'⚠️ *Oraculo Alert*: {streak} consecutive losses! '
                      f'Bankroll: ${state["bankroll"]:.2f}')
        state['_alerted_streak'] = True
    elif streak < 3:
        state.pop('_alerted_streak', None)

    # Alert on circuit breaker (trigger + recovery)
    cb_now = state['bankroll'] < CIRCUIT_BREAKER
    cb_was = state.get('_cb_active', False)
    if cb_now and not cb_was:
        send_telegram(f'CIRCUIT BREAKER: Bankroll ${state["bankroll"]:.2f} < ${CIRCUIT_BREAKER:.2f}. Betting STOPPED.')
        send_whatsapp(f'🚨 CIRCUIT BREAKER: BK ${state["bankroll"]:.2f} — apuestas PAUSADAS')
        state['_cb_active'] = True
    elif not cb_now and cb_was:
        send_telegram(f'RECOVERED: Bankroll ${state["bankroll"]:.2f} >= ${CIRCUIT_BREAKER:.2f}. Betting RESUMED.')
        state['_cb_active'] = False

    # Alert on big win
    daily_pnl = state.get('daily_pnl', 0)
    if daily_pnl > 10 and not state.get('_alerted_big_win'):
        send_telegram(f'🎉 *Oraculo*: Gran dia! P&L: ${daily_pnl:+.2f} | '
                      f'Bankroll: ${state["bankroll"]:.2f}')
        state['_alerted_big_win'] = True

    # Daily summary at first cycle after midnight reset
    if state.get('daily_date') != datetime.now().strftime('%Y-%m-%d'):
        yesterday_pnl = state.get('daily_pnl', 0)
        if abs(yesterday_pnl) > 0.01:
            wins = state.get('wins', 0)
            losses = state.get('losses', 0)
            send_telegram(f'📊 *Oraculo Daily Summary*\n'
                          f'P&L ayer: ${yesterday_pnl:+.2f}\n'
                          f'Bankroll: ${state["bankroll"]:.2f}\n'
                          f'Record: {wins}W/{losses}L\n'
                          f'Active: {len(state.get("active_bets", []))} bets')
            send_whatsapp(
                '📊 Oraculo resumen del dia\n'
                + f'P&L: ${yesterday_pnl:+.2f} | BK: ${state["bankroll"]:.2f}\n'
                + f'Record: {wins}W/{losses}L | Pendientes: {len(state.get("active_bets", []))}'
            )

# ---------------------------------------------------------------------------
# Prediction tracking (for backtest validation)
# ---------------------------------------------------------------------------


def _capture_closing_odds(api, state):
    """Capture closing odds for active bets near kickoff (within 2h).
    Stores closing_odds on the bet entry for CLV calculation later."""
    try:
        now = datetime.now(timezone.utc)
        for bet in state.get('active_bets', []):
            if bet.get('closing_odds'):
                continue  # Already captured
            eid = bet.get('event_id', '')
            if not eid:
                continue
            # Only capture if we have the placed odds
            placed_odds = bet.get('odds', 0)
            if placed_odds <= 1:
                continue
            # Try to get current odds from API
            try:
                events = api.get_odds_by_event(eid) if hasattr(api, 'get_odds_by_event') else None
                if not events:
                    # Fallback: query via v2
                    r = api.v2.get(f'{CB_BASE}/pub/v2/odds/events/{eid}', timeout=10)
                    if r.status_code != 200:
                        continue
                    ev = r.json()
                else:
                    ev = events
                # Find matching market
                market_url = bet.get('market_url', bet.get('market', ''))
                if not market_url:
                    continue
                # Parse market from selection
                markets = ev.get('markets', {})
                for mk_name, mk_data in markets.items():
                    for sv in mk_data.get('submarkets', {}).values():
                        for sel in sv.get('selections', []):
                            if sel.get('marketUrl', '') == market_url:
                                current_price = float(sel.get('price', 0))
                                if current_price > 1.0:
                                    bet['closing_odds'] = current_price
                                    bet['clv'] = round((placed_odds / current_price) - 1, 4)
                                    break
            except Exception:
                continue
    except Exception as e:
        log.debug('CLV capture error: %s', e)


def _compute_clv_stats(state):
    """Compute aggregate CLV statistics from settled bets."""
    try:
        if not os.path.exists(PREDICTIONS_FILE):
            return None
        lines = open(PREDICTIONS_FILE).readlines()
        clv_data = []
        for ln in lines:
            try:
                e = json.loads(ln)
                if e.get('result') and e.get('closing_odds') and e.get('odds'):
                    placed = float(e['odds'])
                    closing = float(e['closing_odds'])
                    clv = (placed / closing) - 1
                    clv_data.append(clv)
            except Exception:
                pass
        if len(clv_data) < 5:
            return None
        avg_clv = sum(clv_data) / len(clv_data)
        positive = sum(1 for c in clv_data if c > 0)
        return {
            'avg_clv': round(avg_clv, 4),
            'clv_positive_pct': round(positive / len(clv_data), 3),
            'sample': len(clv_data),
        }
    except Exception:
        return None

PREDICTIONS_FILE = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
import threading as _threading
try:
    from oraculo_clv import record_closing_for_settled, format_clv_telegram
    _CLV_ENABLED = True
except ImportError:
    _CLV_ENABLED = False
    def record_closing_for_settled(f): return 0
    def format_clv_telegram(f): return "CLV module no disponible"
try:
    from oraculo_sibila import record_pick as _sibila_record, mark_placed as _sibila_placed, resolve_pick as _sibila_resolve, resolve_shadow_picks as _sibila_resolve_shadow, format_telegram as _sibila_fmt
    try:
        from oraculo_sibila import resolve_fade_shadow_picks as _sibila_resolve_fade
    except ImportError:
        def _sibila_resolve_fade(*a, **kw): return 0
    try:
        from soccer_sibila_resolver import resolve_all_pending as _soccer_resolve_shadows
    except ImportError:
        def _soccer_resolve_shadows(**kw): return (0, 0, 0)
    try:
        from mlb_sibila_resolver import resolve_all_pending as _mlb_resolve_shadows
    except ImportError:
        def _mlb_resolve_shadows(**kw): return (0, 0, 0)
    try:
        from tennis_sibila_resolver import resolve_all_pending as _tennis_resolve_shadows
    except ImportError:
        def _tennis_resolve_shadows(**kw): return (0, 0, 0)
    try:
        from wnba_sibila_resolver import resolve_wnba_pending as _wnba_resolve_shadows
        from nhl_sibila_resolver import resolve_nhl_pending as _nhl_resolve_shadows
        from nba_sibila_resolver import resolve_nba_pending as _nba_resolve_shadows
    except ImportError:
        def _wnba_resolve_shadows(*a, **kw): return 0
        def _nba_resolve_shadows(*a, **kw): return 0
    _SIBILA_ENABLED = True
except ImportError:
    _SIBILA_ENABLED = False
    def _sibila_record(*a, **kw): pass
    def _sibila_placed(*a, **kw): pass
    def _sibila_resolve(*a, **kw): pass
    def _sibila_resolve_shadow(*a, **kw): return 0
    def _sibila_resolve_fade(*a, **kw): return 0
    def _sibila_fmt(**kw): return 'Sibila no disponible'
# ── Learned Rules (generado por _learn_from_losses.py) ──────────────────
import json as _json
_LEARNED_RULES       = []
_LEARNED_RULES_MTIME = 0.0
_LEARNED_RULES_PATH  = os.path.join(os.path.dirname(__file__), 'learned_rules.json')

def _load_learned_rules():
    global _LEARNED_RULES, _LEARNED_RULES_MTIME
    try:
        mtime = os.path.getmtime(_LEARNED_RULES_PATH)
        if mtime == _LEARNED_RULES_MTIME:
            return
        data  = _json.load(open(_LEARNED_RULES_PATH))
        rules = data.get('rules', data) if isinstance(data, dict) else data
        _LEARNED_RULES = rules if isinstance(rules, list) else []
        _LEARNED_RULES_MTIME = mtime
        log.info('[Rules] %d reglas aprendidas cargadas desde learned_rules.json', len(_LEARNED_RULES))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning('[Rules] Error cargando learned_rules.json: %s', exc)

def _apply_learned_rules(picks: list) -> list:
    """Filtra/reduce picks segun reglas aprendidas de historial."""
    _load_learned_rules()
    if not _LEARNED_RULES:
        return picks
    out = []
    for p in picks:
        if p.get('_skip_rules'):
            out.append(p)
            continue
        price  = float(p.get('odds', p.get('price', 0)) or 0)
        edge   = float(p.get('edge', 0) or 0)
        market = str(p.get('market_type', p.get('market', '')) or '').lower()
        level  = str(p.get('level', '') or '').lower()
        sport  = str(p.get('sport', '') or '').lower()
        skip   = False
        factor = 1.0
        for r in _LEARNED_RULES:
            dim = r.get('dimension', '')
            val = r.get('value', '')
            act = r.get('action', 'skip')
            match = False
            if dim == 'price':
                if val == '1.20-1.50' and 1.20 <= price < 1.50: match = True
                elif val == '1.50-2.00' and 1.50 <= price < 2.00: match = True
                elif val == '2.00-2.50' and 2.00 <= price < 2.50: match = True
                elif val == '2.50-3.00' and 2.50 <= price < 3.00: match = True
                elif val == '3.00+' and price >= 3.00: match = True
            elif dim == 'edge':
                ep = edge * 100
                if val == '1-2%' and 1 <= ep < 2: match = True
                elif val == '2-4%' and 2 <= ep < 4: match = True
                elif val == '4-7%' and 4 <= ep < 7: match = True
                elif val == '7-12%' and 7 <= ep < 12: match = True
                elif val == '12%+' and ep >= 12: match = True
            elif dim == 'market' and val.lower() in market: match = True
            elif dim == 'level'  and val.lower() in level:  match = True
            # Solo aplicar si la regla aplica al deporte del pick
            rule_sport = r.get('sport', '')
            if rule_sport and rule_sport != sport:
                continue
            if match:
                if act == 'skip':
                    skip = True
                    log.info('[Rules] SKIP %s | %s @ %.2f — regla: %s %s ROI %.1f%%',
                             p.get('match','?'), p.get('label','?'), price, dim, val,
                             r.get('roi_observed', 0))
                    break
                elif act in ('reduce_stake_50pct', 'stake_x0.5'):
                    factor = min(factor, 0.5)
        if not skip:
            if factor < 1.0:
                p['_rules_stake_factor'] = factor
            out.append(p)
    removed = len(picks) - len(out)
    if removed:
        log.info('[Rules] %d picks filtrados por learned_rules', removed)
    return out

try:
    from oraculo_fresh_line import check_picks as _fresh_check, fresh_summary as _fresh_summary
    _FRESH_ENABLED = True
except ImportError:
    _FRESH_ENABLED = False
    def _fresh_check(p): return p
    def _fresh_summary(p): return ''
try:
    from oraculo_rlm import get_tracker as _rlm_tracker
    _RLM = _rlm_tracker()
    _RLM_ENABLED = True
except Exception:
    _RLM_ENABLED = False
    class _FakeRLM:
        def record_batch(self, *a, **kw): pass
        def tag_picks(self, p): return p
        def purge_old(self, **kw): pass
    _RLM = _FakeRLM()
try:
    from oraculo_portfolio import PortfolioManager as _PortfolioManager
    _PORTFOLIO = _PortfolioManager(
        predictions_file=PREDICTIONS_FILE,
        bankroll=1000.0,
    )
    _PORTFOLIO_ENABLED = True
except Exception as _pe:
    _PORTFOLIO_ENABLED = False
    log.warning('Portfolio Kelly no disponible: %s', _pe)
    class _FakePort:
        def get_adjusted_stake(self, p, base_stake=None):
            return {'stake_adj': base_stake or 0, 'skip': False, 'capped': False, 'reason': ''}
        def invalidate_cache(self): pass
        def format_status(self): return 'Portfolio no disponible'
    _PORTFOLIO = _FakePort()
try:
    from oraculo_clv import CLVOracle as _CLVOracle
    _ODDS_API_KEY = os.environ.get('ODDS_API_KEY', '')
    _CLV_ORACLE = _CLVOracle(odds_api_key=_ODDS_API_KEY, rlm_tracker=_RLM if _RLM_ENABLED else None)
    _CLV_ORACLE_ENABLED = True
except Exception as _ce:
    _CLV_ORACLE_ENABLED = False
    log.warning('CLV Oracle no disponible: %s', _ce)
    class _FakeCLV:
        def fetch_and_record(self, *a, **kw): return 0
        def resolve_clv_for_pick(self, *a, **kw): return {}
        def clv_quality_label(self, clv): return ''
    _CLV_ORACLE = _FakeCLV()
try:
    from oraculo_benter import get_calibrator as _benter_cal
    _BENTER = _benter_cal(
        sibila_db=os.path.join(os.path.dirname(__file__), 'sibila.db')
    )
    _BENTER_ENABLED = True
    log.info('Benter Calibrator cargado (alpha football=%.2f tennis=%.2f mlb=%.2f)',
             _BENTER._alphas.get('soccer',0.60),
             _BENTER._alphas.get('tennis',0.65),
             _BENTER._alphas.get('mlb',0.55))
except Exception as _be:
    _BENTER_ENABLED = False
    log.warning('Benter no disponible: %s', _be)
    class _FakeBenter:
        _alphas = {}
        def apply_batch(self, picks, **kw): return picks
        def recalibrate(self, **kw): return False
    _BENTER = _FakeBenter()
_predictions_lock = _threading.Lock()  # Thread-safe writes to predictions_log.jsonl

def _build_episodic_memory(days_back=21):
    """Build player-level win/loss memory from llm_postmatch_log.jsonl.
    Returns: {player_name_lower: {'losses': int, 'wins': int, 'markets': [str]}}
    Used to penalize picks against players we have recent bad history with."""
    memory = {}
    try:
        log_file = os.path.join(SCRIPT_DIR, 'llm_postmatch_log.jsonl')
        if not os.path.exists(log_file):
            return memory
        cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
        with open(log_file) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get('ts', '') < cutoff:
                        continue
                    match = e.get('match', '')
                    outcome = e.get('outcome', '')
                    market = e.get('label', e.get('market_type', ''))
                    if ' vs ' not in match:
                        continue  # skip parlays
                    players = [p.strip() for p in match.split(' vs ', 1)]
                    for player in players:
                        key = player.lower()
                        if key not in memory:
                            memory[key] = {'losses': 0, 'wins': 0, 'markets': []}
                        if outcome == 'LOST':
                            memory[key]['losses'] += 1
                            memory[key]['markets'].append(market)
                        elif outcome == 'WON':
                            memory[key]['wins'] += 1
                except Exception:
                    continue
    except Exception as e:
        log.debug('Episodic memory load failed: %s', e)
    return memory


def _classify_market_type(label, sport):
    """Classify bet into market category for ROI tracking."""
    label_low = label.lower()
    if sport == 'tennis':
        return 'tennis'
    if 'over' in label_low or 'under' in label_low:
        return 'over_under'
    if 'asian' in label_low or 'handicap' in label_low or 'ah' in label_low:
        return 'asian_handicap'
    if 'btts' in label_low or 'both teams' in label_low:
        return 'btts'
    return 'result_1x2'

def _log_prediction(match, label, model_prob, edge, odds, stake, bet_id, sport, signal='model', league='', event_id='', conf=None, market_type=None):
    """Append prediction to JSONL file for backtest analysis."""
    try:
        entry = {
            'ts': datetime.now().isoformat(),
            'match': match, 'label': label,
            'model_prob': round(model_prob, 4),
            'edge': round(edge, 4),
            'odds': odds, 'stake': stake,
            'bet_id': bet_id, 'sport': sport,
            'league': league, 'event_id': event_id,
            'conf': round(conf, 4) if conf is not None else round(model_prob, 4),
            'market_type': market_type or _classify_market_type(label, sport),
            'signal': signal,
            'result': None,  # filled later by _log_settlement
        }
        with open(PREDICTIONS_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def _save_bet(state, bet_id, match, label, sport, odds, stake, edge=0,
              event_id='', market_url='', cutoff_time='', source='model',
              currency='USDC', **extra):
    """Single authoritative function for saving a placed bet to active_bets.

    Returns True if saved, False if rejected (empty bet_id or duplicate).
    Guards:
    - bet_id must be non-empty string
    - bet_id must not already exist in active_bets
    - Updates daily_staked and bets_placed_today
    - Atomically saves state to disk
    """
    if not bet_id or not isinstance(bet_id, str):
        log.warning('[_save_bet] Rejected: empty or invalid bet_id (match=%s label=%s)', match[:30], label[:20])
        return False
    if stake <= 0:
        log.warning('[_save_bet] Rejected: stake=%.4f <= 0 (match=%s label=%s)', stake, match[:30], label[:20])
        return False

    existing = {b.get('bet_id') for b in state.get('active_bets', [])}
    if bet_id in existing:
        log.debug('[_save_bet] Skipped duplicate bet_id %s', bet_id[:12])
        return False

    entry = {
        'bet_id': bet_id,
        'match': match,
        'label': label,
        'sport': sport,
        'odds': odds,
        'stake': stake,
        'edge': edge,
        'placed': datetime.now().isoformat(),
        'event_id': event_id,
        'market_url': market_url,
        'cutoff_time': cutoff_time,
        'source': source,
        'currency': currency,
    }
    entry.update(extra)
    state['active_bets'].append(entry)
    save_state(state)
    log.info('[_save_bet] Saved bet %s | %s | $%.2f', bet_id[:12], label[:25], stake)
    return True

def _write_bet_history(bet_id, match, league, sport, label, market_type,
                       placed_at, settled_at, stake, price, model_prob,
                       edge, result, pnl, currency):
    """Append a settled bet to bet_history table in oraculo.db."""
    try:
        import sqlite3 as _sq3
        _db = os.path.join(SCRIPT_DIR, 'oraculo.db')
        _con = _sq3.connect(_db, timeout=10)
        _con.execute('''
            CREATE TABLE IF NOT EXISTS bet_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id      TEXT UNIQUE,
                match       TEXT,
                league      TEXT,
                sport       TEXT,
                label       TEXT,
                market_type TEXT,
                placed_at   TEXT,
                settled_at  TEXT,
                stake       REAL,
                price       REAL,
                model_prob  REAL,
                edge        REAL,
                result      TEXT,
                pnl         REAL,
                currency    TEXT
            )''')
        _con.execute('''
            INSERT OR IGNORE INTO bet_history
            (bet_id,match,league,sport,label,market_type,placed_at,settled_at,
             stake,price,model_prob,edge,result,pnl,currency)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (bet_id, match, league, sport, label, market_type, placed_at, settled_at,
             stake, price, model_prob, edge, result, pnl, currency))
        _con.commit()
        _con.close()
    except Exception as _e:
        log.debug('bet_history write failed: %s', _e)


def _log_settlement(bet_id, result, win_loss):
    """Update prediction log with result. Thread-safe write with lock."""
    try:
        if not os.path.exists(PREDICTIONS_FILE):
            return
        with _predictions_lock:
            lines = open(PREDICTIONS_FILE).readlines()
            updated = False
            new_lines = []
            settled_entry = None
            for line in lines:
                try:
                    entry = json.loads(line.strip())
                except Exception:
                    new_lines.append(line)
                    continue
                if entry.get('bet_id') == bet_id and entry.get('result') is None:
                    entry['result'] = result
                    entry['win_loss'] = win_loss
                    entry['settled_ts'] = datetime.now().isoformat()
                    updated = True
                    settled_entry = entry
                new_lines.append(json.dumps(entry) + '\n')
            if updated:
                with open(PREDICTIONS_FILE, 'w') as f:
                    f.writelines(new_lines)
        if settled_entry:
            _update_roi_by_type(settled_entry)
    except Exception:
        pass

def _recalculate_roi_by_type():
    """Full recalculation of ROI by market type from predictions log."""
    try:
        pred_path = os.path.join(SCRIPT_DIR, "predictions_log.jsonl")
        if not os.path.exists(pred_path):
            return
        preds = []
        with open(pred_path) as f:
            for line in f:
                if line.strip():
                    try:
                        preds.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        by_type = {}
        for p in preds:
            mt = p.get("market_type", "unknown")
            if mt not in by_type:
                by_type[mt] = {"bets": 0, "wins": 0, "losses": 0, "staked": 0.0, "profit": 0.0}
            by_type[mt]["bets"] += 1
            by_type[mt]["staked"] += p.get("stake", 0)
            r = p.get("result")
            # Use actual win_loss if available (consistent with incremental path)
            _p_stake = p.get('stake', 0)
            _p_wl = p.get('win_loss',
                          _p_stake * (p.get('odds', p.get('price', 1)) - 1) if r == 'WIN' else -_p_stake)
            if r == "WIN":
                by_type[mt]["wins"] += 1
                by_type[mt]["profit"] += _p_wl
            elif r == "LOSS":
                by_type[mt]["losses"] += 1
                by_type[mt]["profit"] += _p_wl  # wl is negative for LOSS
            # Also track by signal
            sig = p.get("signal", "model")
            sig_key = "signal_" + sig
            if sig_key not in by_type:
                by_type[sig_key] = {"bets": 0, "wins": 0, "losses": 0, "staked": 0.0, "profit": 0.0}
            by_type[sig_key]["bets"] += 1
            by_type[sig_key]["staked"] += _p_stake
            if r == "WIN":
                by_type[sig_key]["wins"] += 1
                by_type[sig_key]["profit"] += _p_wl
            elif r == "LOSS":
                by_type[sig_key]["losses"] += 1
                by_type[sig_key]["profit"] += _p_wl
        for mt, d in by_type.items():
            d["staked"] = round(d["staked"], 2)
            d["profit"] = round(d["profit"], 2)
            d["roi_pct"] = round(d["profit"] / d["staked"] * 100, 2) if d["staked"] else 0
        by_type["_updated"] = datetime.now().isoformat()
        roi_file = os.path.join(SCRIPT_DIR, "roi_by_type.json")
        with open(roi_file, "w") as f:
            json.dump(by_type, f, indent=2)
        log.info("ROI by type recalculated: %d market types", len([k for k in by_type if not k.startswith("_")]))
    except Exception as e:
        log.debug("ROI recalculation failed: %s", e)


def _recalibrate_model():
    """Compute calibration factors from actual results vs predicted probs."""
    try:
        pred_path = os.path.join(SCRIPT_DIR, "predictions_log.jsonl")
        if not os.path.exists(pred_path):
            return
        preds = []
        with open(pred_path) as f:
            for line in f:
                if line.strip():
                    try:
                        preds.append(json.loads(line))
                    except Exception:
                        continue
        by_type = {}
        for p in preds:
            r = p.get("result")
            if r not in ("WIN", "LOSS"):
                continue
            mt = p.get("market_type", "unknown")
            if mt not in by_type:
                by_type[mt] = {"wins": 0, "total": 0, "sum_prob": 0.0}
            by_type[mt]["total"] += 1
            by_type[mt]["sum_prob"] += p.get("model_prob", 0.5)
            if r == "WIN":
                by_type[mt]["wins"] += 1
        cal = {}
        for mt, d in by_type.items():
            if d["total"] < 10:
                continue
            actual_wr = d["wins"] / d["total"]
            avg_prob = d["sum_prob"] / d["total"]
            if avg_prob > 0:
                cal[mt] = round(actual_wr / avg_prob, 4)
        cal["_updated"] = datetime.now().isoformat()
        cal_path = os.path.join(SCRIPT_DIR, "calibration.json")
        with open(cal_path, "w") as f:
            json.dump(cal, f, indent=2)
        log.info("Calibration updated: %s", {k: v for k, v in cal.items() if k != "_updated"})
    except Exception as e:
        log.debug("Calibration failed: %s", e)


def _load_calibration():
    """Load calibration factors. Returns dict of market_type -> factor."""
    try:
        cal_path = os.path.join(SCRIPT_DIR, "calibration.json")
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}
def _update_roi_by_type(entry):
    """Update rolling ROI statistics per market type."""
    try:
        roi_file = os.path.join(SCRIPT_DIR, 'roi_by_type.json')
        roi = {}
        if os.path.exists(roi_file):
            roi = json.load(open(roi_file))
        mtype = entry.get('market_type', 'unknown')
        stake = float(entry.get('stake', 0))
        wl = float(entry.get('win_loss', 0))
        won = entry.get('result') == 'WIN'
        if mtype not in roi:
            roi[mtype] = {'bets': 0, 'wins': 0, 'losses': 0,
                          'staked': 0.0, 'profit': 0.0, 'roi_pct': 0.0}
        roi[mtype]['bets'] += 1
        roi[mtype]['staked'] = round(roi[mtype]['staked'] + stake, 2)
        roi[mtype]['profit'] = round(roi[mtype]['profit'] + wl, 2)
        if won:
            roi[mtype]['wins'] += 1
        else:
            roi[mtype]['losses'] += 1
        if roi[mtype]['staked'] > 0:
            roi[mtype]['roi_pct'] = round(roi[mtype]['profit'] / roi[mtype]['staked'] * 100, 2)
        # Also track by signal type (steam vs model)
        sig = entry.get('signal', 'model')
        sig_key = 'signal_' + sig
        if sig_key not in roi:
            roi[sig_key] = {'bets': 0, 'wins': 0, 'losses': 0, 'staked': 0.0, 'profit': 0.0, 'roi_pct': 0.0}
        roi[sig_key]['bets'] += 1
        roi[sig_key]['staked'] = round(roi[sig_key]['staked'] + stake, 2)
        roi[sig_key]['profit'] = round(roi[sig_key]['profit'] + wl, 2)
        if won:
            roi[sig_key]['wins'] += 1
        else:
            roi[sig_key]['losses'] += 1
        if roi[sig_key]['staked'] > 0:
            roi[sig_key]['roi_pct'] = round(roi[sig_key]['profit'] / roi[sig_key]['staked'] * 100, 2)
        roi['_updated'] = datetime.now().isoformat()
        with open(roi_file, 'w') as f:
            json.dump(roi, f, indent=2)
        log.info('ROI by type: %s -> %.1f%% ROI (%d bets)',
                 mtype, roi[mtype]['roi_pct'], roi[mtype]['bets'])
    except Exception as e:
        log.debug('ROI update failed: %s', e)

def get_roi_summary():
    """Return formatted ROI summary by market type."""
    try:
        roi_file = os.path.join(SCRIPT_DIR, 'roi_by_type.json')
        if not os.path.exists(roi_file):
            return "No ROI data yet."
        roi = json.load(open(roi_file))
        lines = ["ROI por tipo de apuesta:"]
        order = ['tennis', 'over_under', 'result_1x2', 'btts']
        for mtype in order + [k for k in roi if k not in order and not k.startswith('_')]:
            if mtype not in roi or mtype.startswith('_'):
                continue
            d = roi[mtype]
            wr = d['wins'] / d['bets'] * 100 if d['bets'] > 0 else 0
            emoji = 'UP' if d['roi_pct'] > 0 else 'DOWN'
            lines.append(f"[{emoji}] {mtype}: {d['bets']} bets | WR {wr:.0f}% | ROI {d['roi_pct']:+.1f}%")
        return '\n'.join(lines)
    except Exception:
        return "Error reading ROI data."

# ---------------------------------------------------------------------------
# Obsidian sync
# ---------------------------------------------------------------------------
def sync_obsidian(state):
    """Update Obsidian betting log."""
    try:
        os.makedirs(OBSIDIAN_DIR, exist_ok=True)
        today = datetime.now().strftime('%Y-%m-%d')
        path = os.path.join(OBSIDIAN_DIR, f'Oraculo Betting - {today}.md')

        settled_list = state.get('settled_today', [])
        active_list = state.get('active_bets', [])
        wins = state.get('wins', 0)
        losses = state.get('losses', 0)
        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0

        lines = [
            f'# Oraculo Betting Log - {today}\n',
            f'> Actualizado: {datetime.now():%Y-%m-%d %H:%M} (auto-runner)\n',
            f'## Bankroll',
            f'- **Balance**: ${state["bankroll"]:.2f} USDC',
            f'- **Profit total**: ${state["total_pnl"]:+.2f}',
            f'- **Hoy**: ${state["daily_pnl"]:+.2f} ({state["bets_placed_today"]} bets)',
            f'- **Record**: {wins}W / {losses}L ({wr:.1f}%)',
            f'- **Pendientes**: {len(active_list)}',
            '',
        ]

        if settled_list:
            lines.append('## Resueltas hoy\n')
            lines.append('| Match | Result | P&L |')
            lines.append('|-------|--------|-----|')
            for s in settled_list:
                lines.append(f'| {s.get("match", "?")[:40]} | {s["result"]} | ${s["winLoss"]:+.2f} |')
            lines.append('')

        if active_list:
            lines.append('## Pendientes\n')
            lines.append('| Match | Market | Odds | Stake |')
            lines.append('|-------|--------|------|-------|')
            for a in active_list:
                lines.append(f'| {a.get("match", "?")[:35]} | {a.get("label", "")} | {a["odds"]:.3f} | ${a["stake"]:.2f} |')
            lines.append('')

        lines.append('---')
        lines.append('**Auto-sync por oraculo_runner_auto.py**')

        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        log.info('Obsidian synced: %s', path)
    except Exception as e:
        log.error('Obsidian sync failed: %s', e)

# ---------------------------------------------------------------------------
# Main scan cycle
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------
HEARTBEAT_FILE  = os.path.join(SCRIPT_DIR, '.oraculo_heartbeat')
DASHBOARD_FILE = os.path.join(SCRIPT_DIR, 'dashboard.html')

def generate_dashboard(state):
    """Generate a simple HTML dashboard."""
    try:
        wins = state.get('wins', 0)
        losses = state.get('losses', 0)
        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0
        active = state.get('active_bets', [])
        settled = state.get('settled_today', [])
        reconcile = state.get('_reconcile', {})

        active_rows = ""
        for b in sorted(active, key=lambda x: x.get('placed', ''), reverse=True):
            edge_pct = b.get('edge', 0) * 100
            active_rows += f"""<tr>
                <td>{b.get('match','?')[:40]}</td>
                <td>{b.get('label','?')}</td>
                <td>{b.get('odds',0):.2f}</td>
                <td>${b.get('stake',0):.2f}</td>
                <td>{edge_pct:.1f}%</td>
                <td>{b.get('placed','?')[:16]}</td>
            </tr>"""

        settled_rows = ""
        for s in settled:
            wl = s.get('winLoss', 0)
            cls = 'win' if s.get('result') == 'WIN' else 'loss'
            settled_rows += f"""<tr class="{cls}">
                <td>{s.get('match','?')[:40]}</td>
                <td>{s.get('result','?')}</td>
                <td>${wl:+.2f}</td>
                <td>{s.get('settled','?')[:16]}</td>
            </tr>"""

        # Load prediction log stats
        pred_stats = ""
        pred_file = os.path.join(SCRIPT_DIR, 'predictions_log.jsonl')
        if os.path.exists(pred_file):
            lines = open(pred_file).readlines()
            total_pred = len(lines)
            settled_pred = sum(1 for l in lines if '"result":' in l and '"result": null' not in l)
            pred_stats = f"Predictions logged: {total_pred} ({settled_pred} settled)"

        # WC 2026 progress section
        from datetime import date as _date
        _wc_start = _date(2026, 6, 12)
        _today = _date.today()
        _wc_days_left = (_wc_start - _today).days
        _wc_countdown = (f"{_wc_days_left} days to kick-off" if _wc_days_left > 0
                         else ("LIVE" if _wc_days_left == 0 else "IN PROGRESS"))
        _wc_reserve = state.get('wc_reserve', 0)
        _wc_bets = [b for b in active
                    if b.get('cutoff_time', '') >= '2026-06-01'
                    or b.get('league') == 'FIFA_WC']
        _wc_rows = ''
        for _wb in sorted(_wc_bets, key=lambda x: x.get('cutoff_time', '')):
            _wc_rows += (f'<tr><td>{_wb.get("match","?")[:40]}</td>'
                         f'<td>{_wb.get("label","?")[:30]}</td>'
                         f'<td>{_wb.get("odds",0):.2f}</td>'
                         f'<td>${_wb.get("stake",0):.2f}</td>'
                         f'<td>{_wb.get("cutoff_time","?")[:10]}</td></tr>')
        _wc_table = (f'''<table><tr><th>Match</th><th>Pick</th><th>Odds</th>
                     <th>Stake</th><th>Date</th></tr>{_wc_rows}</table>'''
                     if _wc_rows else '<p style="color:#888">No WC bets placed yet.</p>')
        wc_section = f'''<h2>&#127942; World Cup 2026 ({_wc_countdown})</h2>
<div class="kpi">
  <div class="kpi-box"><div class="label">WC Reserve</div>
    <div class="value">${_wc_reserve:.2f}</div></div>
  <div class="kpi-box"><div class="label">WC Active Bets</div>
    <div class="value">{len(_wc_bets)}</div></div>
  <div class="kpi-box"><div class="label">WC Stake In Flight</div>
    <div class="value">${sum(b.get("stake",0) for b in _wc_bets):.2f}</div></div>
</div>
{_wc_table}'''

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Oraculo Dashboard</title>
<meta http-equiv="refresh" content="120">
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; margin: 20px; }}
  h1 {{ color: #00d4ff; }} h2 {{ color: #7b68ee; border-bottom: 1px solid #333; padding-bottom: 5px; }}
  .kpi {{ display: flex; gap: 20px; flex-wrap: wrap; margin: 20px 0; }}
  .kpi-box {{ background: #16213e; border-radius: 10px; padding: 15px 25px; min-width: 150px; }}
  .kpi-box .label {{ font-size: 0.8em; color: #888; }} .kpi-box .value {{ font-size: 1.8em; font-weight: bold; }}
  .pos {{ color: #00ff88; }} .neg {{ color: #ff4444; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
  th {{ background: #16213e; padding: 8px; text-align: left; }} td {{ padding: 6px 8px; border-bottom: 1px solid #222; }}
  tr.win td {{ background: rgba(0,255,136,0.05); }} tr.loss td {{ background: rgba(255,68,68,0.05); }}
  .footer {{ margin-top: 30px; color: #555; font-size: 0.8em; }}
</style></head><body>
<h1>Oraculo V2 Dashboard</h1>
<div class="kpi">
  <div class="kpi-box"><div class="label">Bankroll</div><div class="value">${state['bankroll']:.2f}</div></div>
  <div class="kpi-box"><div class="label">Total P&L</div><div class="value {'pos' if state.get('total_pnl',0)>=0 else 'neg'}">${state.get('total_pnl',0):+.2f}</div></div>
  <div class="kpi-box"><div class="label">Today P&L</div><div class="value {'pos' if state.get('daily_pnl',0)>=0 else 'neg'}">${state.get('daily_pnl',0):+.2f}</div></div>
  <div class="kpi-box"><div class="label">Record</div><div class="value">{wins}W / {losses}L</div></div>
  <div class="kpi-box"><div class="label">Win Rate</div><div class="value">{wr:.1f}%</div></div>
  <div class="kpi-box"><div class="label">Active Bets</div><div class="value">{len(active)}</div></div>
  <div class="kpi-box"><div class="label">Loss Streak</div><div class="value">{state.get('consecutive_losses',0)}</div></div>
</div>

<h2>Active Bets ({len(active)})</h2>
<table><tr><th>Match</th><th>Label</th><th>Odds</th><th>Stake</th><th>Edge</th><th>Placed</th></tr>
{active_rows}</table>

<h2>Settled Today ({len(settled)})</h2>
<table><tr><th>Match</th><th>Result</th><th>P&L</th><th>Settled</th></tr>
{settled_rows}</table>

{wc_section}

<div class="footer">
  Last scan: {state.get('last_scan', 'never')[:19]}<br>
  Last check: {state.get('last_result_check', 'never')[:19]}<br>
  {pred_stats}<br>
  Reconcile: {json.dumps(reconcile) if reconcile else 'N/A'}<br>
  Auto-refresh every 2 minutes
</div>
</body></html>"""

        with open(DASHBOARD_FILE, 'w') as f:
            f.write(html)
    except Exception as e:
        log.debug('Dashboard generation failed: %s', e)


# ---------------------------------------------------------------------------
# Manual bet queue (user-requested bets)
# ---------------------------------------------------------------------------
MANUAL_BETS_FILE = os.path.join(SCRIPT_DIR, 'manual_bets.json')

def process_manual_bets(api, state):
    """Process user-requested manual bets from manual_bets.json.

    File format:
    {
      "bets": [
        {"match": "Player A vs Player B", "pick": "Player A", "sport": "tennis",
         "stake": 5.0, "competition": "tennis-atp-atp-miami-usa-men-singles"},
        {"match": "Team A vs Team B", "pick": "over", "market": "over25",
         "sport": "soccer", "stake": 3.0, "competition": "soccer-england-premier-league"}
      ]
    }

    After processing, bets are moved to "processed" list with result.
    """
    if not os.path.exists(MANUAL_BETS_FILE):
        return 0

    try:
        data = json.load(open(MANUAL_BETS_FILE))
    except Exception as e:
        log.warning('Manual bets file invalid: %s', e)
        return 0

    pending = data.get('bets', [])
    if not pending:
        return 0

    if state['bankroll'] < CIRCUIT_BREAKER:
        log.warning('Manual bets skipped: circuit breaker active')
        return 0

    processed = data.get('processed', [])
    placed = 0
    remaining = []

    for bet in pending:
        match_name = bet.get('match', '')
        pick = bet.get('pick', '')
        sport = bet.get('sport', 'tennis')
        stake = float(bet.get('stake', 0))
        comp = bet.get('competition', '')

        if not match_name or not pick or stake <= 0:
            log.warning('Manual bet invalid: %s', bet)
            processed.append({**bet, 'status': 'INVALID', 'ts': datetime.now().isoformat()})
            continue

        currency = bet.get('currency', 'USDT')  # Manual bets default to USDT
        # Warn if manual bet pushes exposure too high
        total_active = sum(b.get('stake', 0) for b in state.get('active_bets', []))
        if total_active + stake > state['bankroll'] * 2.0:
            log.warning('  Manual bet WARNING: total exposure $%.2f would exceed 2x bankroll $%.2f',
                        total_active + stake, state['bankroll'])
        log.info('=== MANUAL BET: %s | %s | $%.2f %s ===', match_name, pick, stake, currency)

        # Find the event on Cloudbet
        event_id = None
        market_url = None
        price = None

        if sport == 'tennis':
            # Search tennis competitions
            comps_to_check = [comp] if comp else CB_TENNIS
            for ck in comps_to_check:
                events = api.get_odds(ck)
                for ev in events:
                    home = (ev.get('home') or {}).get('name', '')
                    away = (ev.get('away') or {}).get('name', '')
                    # Match by player names
                    if (pick.lower() in home.lower() or pick.lower() in away.lower()):
                        eid = str(ev.get('id', ''))
                        markets = ev.get('markets', {})
                        winner_mkt = markets.get('tennis.winner', {})
                        for sk, sub in winner_mkt.get('submarkets', {}).items():
                            for sel in sub.get('selections', []):
                                if sel.get('status') != 'SELECTION_ENABLED':
                                    continue
                                outcome = sel.get('outcome', '')
                                sel_price = sel.get('price', 0)
                                # Match: if pick is home and outcome is home, or pick in name
                                if ((pick.lower() in home.lower() and outcome == 'home') or
                                    (pick.lower() in away.lower() and outcome == 'away')):
                                    event_id = eid
                                    market_url = sel.get('marketUrl', '')
                                    price = sel_price
                                    break
                        if event_id:
                            break
                if event_id:
                    break

        elif sport == 'soccer':
            if not SOCCER_ENABLED:
                log.info('Manual soccer bet skipped -- SOCCER_ENABLED=False: %s', match_name)
                remaining.append(bet)
                continue
            market_key = bet.get('market', 'asian_handicap')
            comps_to_check = [comp] if comp else list(CB_COMPS.values())
            for ck in comps_to_check:
                events = api.get_odds(ck)
                for ev in events:
                    home = (ev.get('home') or {}).get('name', '')
                    away = (ev.get('away') or {}).get('name', '')
                    if match_name.lower() in (home + ' vs ' + away).lower():
                        eid = str(ev.get('id', ''))
                        markets = ev.get('markets', {})
                        # Find the right market
                        from oraculo_bet_engine import CB_MARKETS
                        if market_key in CB_MARKETS:
                            mkt_base, options = CB_MARKETS[market_key]
                            mkt_data = markets.get(mkt_base, {})
                            for sk, sub in mkt_data.get('submarkets', {}).items():
                                for sel in sub.get('selections', []):
                                    if sel.get('status') != 'SELECTION_ENABLED':
                                        continue
                                    outcome = sel.get('outcome', '')
                                    if pick.lower() in outcome.lower():
                                        event_id = eid
                                        market_url = sel.get('marketUrl', '')
                                        price = sel.get('price', 0)
                                        break
                                if event_id:
                                    break
                        if event_id:
                            break
                if event_id:
                    break

        if not event_id or not market_url or not price:
            log.warning('  Manual bet: event not found on Cloudbet')
            remaining.append(bet)  # Keep for retry next cycle
            continue

        log.info('  Found: event=%s market=%s price=%.3f', event_id, market_url, price)
        # Use bet-specific currency (USDT for friend, USDC for own)
        orig_currency = api.currency
        api.currency = currency
        resp = api.place_straight(event_id, market_url, price, stake)
        api.currency = orig_currency

        if resp:
            bet_id = resp.get('betId', '')
            _save_bet(state, bet_id, match_name,
                      'Winner: '+pick if sport=='tennis' else pick,
                      sport, price, stake, source='manual', currency=currency,
                      owner=bet.get('owner','friend'))
            state['daily_staked'] += stake
            state['bets_placed_today'] = state.get('bets_placed_today', 0) + 1
            log.info('  [OK] Manual bet placed: %s | ID: %s', pick, bet_id[:12])
            send_telegram('Manual bet placed: {} | {} @{:.2f} | ${:.2f}'.format(
                match_name, pick, price, stake))
            send_whatsapp(
                f'⚽ Manual ✅\n'
                f'{match_name[:50]}\n'
                f'📌 {pick}\n'
                f'💰 Odds: {price:.2f}\n'
                f'💵 Apostado: ${stake:.2f} | Ganancia est: +${stake*(price-1):.2f}\n'
                f'📊 Dia: {"+" if state["daily_pnl"] >= 0 else "-"}${abs(state["daily_pnl"]):.2f}'
            )
            processed.append({**bet, 'status': 'PLACED', 'bet_id': bet_id,
                            'price': price, 'ts': datetime.now().isoformat()})
            placed += 1
        else:
            log.warning('  [FAIL] Manual bet rejected by Cloudbet')
            processed.append({**bet, 'status': 'REJECTED', 'ts': datetime.now().isoformat()})

        time.sleep(2.0)

    # Save updated file
    data['bets'] = remaining
    data['processed'] = processed[-50:]  # Keep last 50
    with open(MANUAL_BETS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    # Process manual parlays
    for parlay in data.get('parlays', []):
        picks_list = parlay.get('picks', [])
        stake = float(parlay.get('stake', 0))
        if len(picks_list) < 2 or stake <= 0:
            continue

        log.info('=== MANUAL PARLAY: %d legs | $%.2f ===', len(picks_list), stake)
        selections = []
        all_found = True

        for pick_info in picks_list:
            pick_name = pick_info.get('pick', '')
            comp = pick_info.get('competition', '')
            found = False
            comps_to_check = [comp] if comp else CB_TENNIS
            for ck in comps_to_check:
                events = api.get_odds(ck)
                for ev in events:
                    home = (ev.get('home') or {}).get('name', '')
                    away = (ev.get('away') or {}).get('name', '')
                    if pick_name.lower() in home.lower() or pick_name.lower() in away.lower():
                        eid = str(ev.get('id', ''))
                        markets = ev.get('markets', {})
                        winner_mkt = markets.get('tennis.winner', {})
                        for sk, sub in winner_mkt.get('submarkets', {}).items():
                            for sel in sub.get('selections', []):
                                if sel.get('status') != 'SELECTION_ENABLED':
                                    continue
                                outcome = sel.get('outcome', '')
                                if ((pick_name.lower() in home.lower() and outcome == 'home') or
                                    (pick_name.lower() in away.lower() and outcome == 'away')):
                                    selections.append({
                                        'eventId': eid,
                                        'marketUrl': sel.get('marketUrl', ''),
                                        'price': sel.get('price', 0),
                                    })
                                    log.info('  Leg: %s @%.2f', pick_name, sel.get('price', 0))
                                    found = True
                                    break
                            if found:
                                break
                    if found:
                        break
                if found:
                    break
            if not found:
                log.warning('  Parlay leg not found: %s', pick_name)
                all_found = False
                break

        if all_found and len(selections) == len(picks_list):
            parlay_currency = parlay.get('currency', 'USDT')
            orig_currency = api.currency
            api.currency = parlay_currency
            resp = api.place_parlay(selections, stake)
            api.currency = orig_currency
            if resp:
                bet_id = resp.get('betId', '')
                label = ' + '.join(p['pick'] for p in picks_list)
                _save_bet(state, bet_id, 'PARLAY: '+label, str(len(picks_list))+'-leg parlay',
                          'tennis', 0, stake, source='manual', currency=parlay_currency,
                          owner=parlay.get('owner','friend'))
                state['daily_staked'] += stake
                state['bets_placed_today'] = state.get('bets_placed_today', 0) + 1
                log.info('  [OK] Manual parlay placed: %s', bet_id[:12])
                send_telegram('Manual parlay placed: {} @${:.2f}'.format(label, stake))
                send_whatsapp(
                    f'🎰 Parlay Manual ✅\n'
                    f'{label[:60]}\n'
                    f'💵 Apostado: ${stake:.2f}\n'
                    f'📊 Dia: {"+" if state["daily_pnl"] >= 0 else "-"}${abs(state["daily_pnl"]):.2f}'
                )
                placed += 1
            else:
                log.warning('  [FAIL] Manual parlay rejected')
            time.sleep(2.0)

    # Clear processed parlays
    data['parlays'] = []

    if placed:
        save_state(state)
    return placed


# ---------------------------------------------------------------------------
# Interactive Telegram Bot (polling)
# ---------------------------------------------------------------------------
import threading

def _telegram_bot_loop():
    """Poll Telegram for commands: /status, /picks, /apostar"""
    import requests as _req
    last_update_id = 0
    bot_url = 'https://api.telegram.org/bot' + TELEGRAM_BOT_TOKEN

    while True:
        try:
            r = _req.get(bot_url + '/getUpdates',
                        params={'offset': last_update_id + 1, 'timeout': 30}, timeout=35)
            if r.status_code != 200:
                time.sleep(5)
                continue
            updates = r.json().get('result', [])
            for upd in updates:
                last_update_id = upd['update_id']
                msg = upd.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = msg.get('chat', {}).get('id')
                if not text or not chat_id:
                    continue
                if str(chat_id) != TELEGRAM_CHAT_ID:
                    continue

                try:
                    reply = _handle_telegram_command(text)
                    if reply:
                        _req.post(bot_url + '/sendMessage',
                                 json={'chat_id': TELEGRAM_CHAT_ID, 'text': reply,
                                       'parse_mode': 'Markdown'}, timeout=5)
                except Exception as e:
                    log.debug('Telegram command error: %s', e)

        except Exception:
            time.sleep(10)

def _handle_telegram_command(text):
    """Handle a Telegram command and return reply text."""
    cmd = text.lower().split()
    if not cmd:
        return None

    if cmd[0] in ('/status', '/estado'):
        state = load_state()
        wins = state.get('wins', 0)
        losses = state.get('losses', 0)
        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0
        active = len(state.get('active_bets', []))
        return ('*Oraculo Status*\n'
                'Bankroll: ${:.2f}\n'
                'P&L: ${:+.2f}\n'
                'Record: {}W/{}L ({:.0f}%)\n'
                'Active: {} bets\n'
                'Last scan: {}'.format(
                    state['bankroll'], state.get('total_pnl', 0),
                    wins, losses, wr, active,
                    state.get('last_scan', 'never')[:16]))

    elif cmd[0] in ('/picks', '/recomendaciones'):
        # Run a quick scan
        try:
            api = CloudbetAPI()
            state = load_state()
            picks = scan_tennis(api, state, dry_run=True)
            if not picks:
                return 'No value picks found right now.'
            lines = ['*Tennis Picks*\n']
            for p in picks[:5]:
                lines.append('{} @{:.2f} | prob={:.0f}% | edge={:.0f}%'.format(
                    p['label'], p['price'], p['model_prob']*100, p['edge']*100))
            return '\n'.join(lines)
        except Exception as e:
            return 'Error scanning: {}'.format(e)

    elif cmd[0] in ('/apostar', '/bet'):
        # /apostar Fils 5 USDT
        # /apostar Humbert 3
        if len(cmd) < 3:
            return 'Uso: /apostar <jugador> <monto> [USDT/USDC]'
        player = cmd[1]
        try:
            stake = float(cmd[2])
        except ValueError:
            return 'Monto invalido: {}'.format(cmd[2])
        currency = cmd[3].upper() if len(cmd) > 3 else 'USDT'
        if currency not in ('USDT', 'USDC'):
            return 'Currency debe ser USDT o USDC'

        # Find matching player in current events
        try:
            api = CloudbetAPI()
            found_match = None
            found_player = None
            for comp in CB_TENNIS:
                events = api.get_odds(comp)
                for ev in events:
                    home = (ev.get('home') or {}).get('name', '')
                    away = (ev.get('away') or {}).get('name', '')
                    if player.lower() in home.lower():
                        found_match = '{} vs {}'.format(home, away)
                        found_player = home
                        break
                    elif player.lower() in away.lower():
                        found_match = '{} vs {}'.format(home, away)
                        found_player = away
                        break
                if found_match:
                    break

            if not found_match:
                return 'Player "{}" not found in active events'.format(player)

            # Add to manual bets
            mb_path = os.path.join(SCRIPT_DIR, 'manual_bets.json')
            mb = {'bets': [], 'parlays': [], 'processed': []}
            if os.path.exists(mb_path):
                try:
                    mb = json.load(open(mb_path))
                except Exception:
                    pass

            mb.setdefault('bets', []).append({
                'match': found_match,
                'pick': found_player,
                'sport': 'tennis',
                'stake': stake,
                'currency': currency,
                'owner': 'telegram',
            })
            with open(mb_path, 'w') as f:
                json.dump(mb, f, indent=2)

            return ('Queued: {} | {} | ${:.2f} {}\n'
                    'Will be placed next cycle (max 1h)'.format(
                        found_match, found_player, stake, currency))

        except Exception as e:
            return 'Error: {}'.format(e)

    elif cmd[0] in ('/bets', '/apuestas'):
        state = load_state()
        active = state.get('active_bets', [])
        if not active:
            return 'No active bets'
        lines = ['*Active Bets ({})*\n'.format(len(active))]
        for b in active[-10:]:
            lines.append('{} | {} | ${:.2f}'.format(
                b.get('match', '?')[:30], b.get('label', '?'), b.get('stake', 0)))
        if len(active) > 10:
            lines.append('... and {} more'.format(len(active) - 10))
        return '\n'.join(lines)

    elif cmd[0] == '/roi':
        return get_roi_summary()
    elif cmd[0] == '/clv':
        _clv_base = format_clv_telegram(PREDICTIONS_FILE)
        if _CLV_ORACLE_ENABLED:
            _clv_base += '\n\n_Referencia: Betfair Exchange + Pinnacle_'
        return _clv_base
    elif cmd[0] == '/sibila':
        _days = int(cmd[1]) if len(cmd) > 1 and cmd[1].isdigit() else 30
        return _sibila_fmt(days=_days)
    elif cmd[0] == '/portfolio':
        return _PORTFOLIO.format_status() if _PORTFOLIO_ENABLED else 'Portfolio no disponible'
    elif cmd[0] == '/benter':
        if not _BENTER_ENABLED:
            return 'Benter no disponible'
        _ba = _BENTER._alphas
        lines = ['*Benter Calibrator*', '']
        lines.append('Alpha por deporte (modelo vs mercado):')
        for _sp, _al in sorted(_ba.items()):
            if _sp == 'default': continue
            lines.append(f'  {_sp}: {_al:.2f} ({_al*100:.0f}% modelo / {(1-_al)*100:.0f}% mercado)')
        lines.append('')
        lines.append('_Alpha 1.0 = solo modelo | 0.0 = solo mercado_')
        return '\n'.join(lines)
    elif cmd[0] == '/kelly':
        frac = _get_dynamic_kelly_fraction()
        return 'Kelly: ' + str(round(frac,3)) + ' (' + str(round(frac*100,1)) + '%) | Base: ' + str(KELLY_FRAC)
    elif cmd[0] == '/lessons':
        import json as _jj
        lf = os.path.join(SCRIPT_DIR, 'llm_postmatch_log.jsonl')
        if not os.path.exists(lf):
            return 'No lessons yet.'
        items = [_jj.loads(x) for x in open(lf).readlines()[-5:]]
        parts = ['Last LLM lessons:']
        for e in items:
            parts.append(('WIN ' if e['outcome']=='WON' else 'LOSS ') + e['match'][:30] + chr(10) + e['lesson'][:100])
        return (chr(10)+chr(10)).join(parts)
    elif cmd[0] in ('/metrics', '/metricas'):
        try:
            import sqlite3 as _sq3
            _db = _sq3.connect(os.path.join(SCRIPT_DIR, 'sibila.db'))
            lines = ['*Oraculo Metrics*']
            # Sport breakdown
            _rows = _db.execute("""
                SELECT sport,
                    COUNT(*) as n,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END),
                    ROUND(SUM(pnl),2),
                    ROUND(AVG(odds),2)
                FROM sibila_picks WHERE placed=1
                GROUP BY sport ORDER BY n DESC""").fetchall()
            lines.append('\n*Por deporte (reales):*')
            for r in _rows:
                sp, n, w, l, pnl, ao = r
                s2 = (w or 0)+(l or 0)
                wr = (w or 0)/s2*100 if s2 else 0
                lines.append(f'  {sp}: {n} bets | WR={wr:.0f}% | PnL=${pnl or 0:+.2f} | @{ao or 0:.2f}')
            # Prob calibration buckets
            _cal = _db.execute("""
                SELECT CAST(ROUND(prob_model*10) AS INT)*10 as pb,
                    COUNT(*), SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)
                FROM sibila_picks
                WHERE placed=1 AND result IN ('WIN','LOSS') AND prob_model > 0.01
                GROUP BY pb ORDER BY pb""").fetchall()
            if _cal:
                lines.append('\n*Calibracion por prob:*')
                for pb, n, w in _cal:
                    wr = (w or 0)/n*100 if n else 0
                    bar = '█'*int(wr/10) + '░'*(10-int(wr/10))
                    lines.append(f'  {pb}%: {n:2d} bets | WR={wr:.0f}% {bar}')
            # AutoTune params
            _at = state.get('_autotune', {}) if 'state' in dir() else {}
            if _at:
                lines.append('\n*AutoTune:*')
                lines.append(f'  edge={_at.get("min_edge",0)*100:.0f}% conf={_at.get("min_conf",0)*100:.0f}%')
                lines.append(f'  exposure={_at.get("max_exposure",0)*100:.0f}% sample={_at.get("sample_size",0)}')
            # SPORT_KELLY
            lines.append('\n*Kelly por deporte:*')
            for sp, fr in SPORT_KELLY.items():
                lines.append(f'  {sp}: {fr*100:.0f}%')
            _db.close()
            return '\n'.join(lines)
        except Exception as _e:
            return f'Metrics error: {_e}'

    elif cmd[0] == '/help':
        return ('*Oraculo Commands*\n'
                '/status - Balance and record\n'
                '/picks - Current value picks\n'
                '/apostar <player> <amount> [USDT] - Place bet\n'
                '/bets - Active bets\n'
                '/metrics - Calibracion y estadisticas\n'
                '/clv - Closing Line Value (edge real?)\n'
                '/sibila - Libro sombra (ROI sin limites)\n'
                '/portfolio - Estado del portafolio Kelly\n'
                '/benter - Alphas modelo vs mercado\n'
                '/help - This message')

    return None

def _start_telegram_bot():
    """Start Telegram bot in background thread."""
    t = threading.Thread(target=_telegram_bot_loop, daemon=True)
    t.start()
    log.info('Telegram interactive bot started')


def run_cycle(dry_run=False):
    """Single scan + place + check cycle."""
    reconcile_engine_state()
    state = load_state()
    api = CloudbetAPI()

    log.info('=' * 60)
    log.info('ORACULO AUTO CYCLE | Bankroll: $%.2f | Staked today: $%.2f',
             state['bankroll'], state['daily_staked'])
    log.info('=' * 60)

    # 0. Auto-tune strategy parameters
    _auto_tune_strategy(state)

    # 1. Check results first
    check_results(api, state)

    # 1a. Process manual bets (user-requested, bypasses exposure cap)
    manual_placed = process_manual_bets(api, state)
    if manual_placed:
        log.info('Placed %d manual bets', manual_placed)

    # 1b. Reconcile bankroll with Cloudbet
    reconcile_bankroll(api, state)

    # 2. Scan football — always scan (records to Sibila shadow); bet only if SOCCER_ENABLED
    # 2026-05-13: scan with dry_run=True so picks populate Sibila even when betting is off.
    # Then discard football_picks=[] so place_bets / parlay builders don't try to bet.
    football_picks = scan_football(api, state, dry_run=dry_run)  # 2026-05-29: F2 enable live
    if not SOCCER_ENABLED:
        football_picks = []
    # Fase B 2026-05-22: drop over/under picks below raw 0.65 model_prob
    # Calibration N=107: <0.65 WR=33.8% ROI=-13.0% | >=0.65 WR=69.2% ROI=+23.5%
    if football_picks:
        _pre_ou = len(football_picks)
        football_picks = [p for p in football_picks
                          if not ('over' in (p.get('label') or '').lower() or 'under' in (p.get('label') or '').lower())
                          or float(p.get('raw_model_prob_uncal') or p.get('model_prob') or 0) >= 0.65]
        _dropped_ou = _pre_ou - len(football_picks)
        if _dropped_ou:
            log.info('[FaseB] over/under threshold 0.65: dropped %d low-conf picks', _dropped_ou)

    # Fase 2 soccer filter v2 (2026-05-30): two-bucket approach — sibila analysis
    # Bucket A: conf>=85% + edge>=5%   → 99% WR (low edge = model+market agree)
    # Bucket B: conf 75-85% + edge<10% → 98% WR (market agrees with model)
    # Blocked:  conf 75-85% + edge>=10% → 54% WR (market disagrees = market right in soccer)
    if football_picks:
        _pre_f2 = len(football_picks)
        football_picks = [
            p for p in football_picks
            if (p.get('market_type') or '') not in ('over', 'under', 'ou', 'total')
            and (
                (float(p.get('confidence') or p.get('model_prob') or 0) >= 0.85
                 and float(p.get('edge') or 0) >= 0.05
                 and float(p.get('price') or p.get('odds') or 0) <= 1.50)  # 2026-06-02: backtest n=39 WR=84.6% cliff at odds 1.52
                or
                (0.75 <= float(p.get('confidence') or p.get('model_prob') or 0) < 0.85
                 and float(p.get('edge') or 0) >= 0.05
                 and float(p.get('edge') or 0) < 0.10
                 and float(p.get('price') or p.get('odds') or 0) <= 1.50)  # 2026-06-02: backtest n=39 WR=84.6% cliff at odds 1.52
            )
        ]
        for _p in football_picks:
            _p['_max_stake'] = 2.00  # 2026-06-02: raised, backtest ROI +34% at odds<1.50
        _dropped_f2 = _pre_f2 - len(football_picks)
        if _dropped_f2:
            log.info('[F2] Soccer live filter v2: kept %d/%d (dropped %d)',
                     len(football_picks), _pre_f2, _dropped_f2)
    mlb_picks = []

    # 3. Scan tennis
    tennis_picks = scan_tennis(api, state, dry_run)
    # Exclude tennis_exact_sets: 0W/6L (-71.6% ROI in Sibila realistic, pure variance drain)
    # sets_under (Sets Under 2.5): BLOCKED pending calibration — N=12 contaminated (Kelly runaway+prob=0 picks)
    #   Revisit when Sibila shadow has 30+ clean picks (prob>0, edge>=8%, stake<=2)
    # tennis_team_win_set: tactical pool mode — edge>=8% AND odds>=1.35
    # (relaxed from 18%/1.40 while WC reserve locks USDC; $40 USDT tactical pool rotates Roland Garros picks)
    tennis_picks = [p for p in tennis_picks
                    if p.get('market_type') not in ('tennis_exact_sets', 'sets_under',
                                                    'tennis_winner_and_total')  # w+total: 0W/1L, complex market
                    # tennis_team_win_set: edge>=10% AND odds 1.40-1.90
                    # EXCLUIR 0.15-0.18: valle de la muerte WR=42.9% ROI=-34% n=7 (2026-06-02)
                    # 0.12-0.15 WR=75% y 0.18+ WR=75% son buenos; 0.15-0.18 es anomalia del modelo
                    and not (p.get('market_type') == 'tennis_team_win_set'
                             and (float(p.get('edge', 0) or 0) < 0.10
                                  or float(p.get('price', 0) or 0) < 1.40
                                  or float(p.get('price', 0) or 0) > 1.90
                                  or (0.15 <= float(p.get('edge', 0) or 0) < 0.18)))
                    # tennis_team_win_set (no): favor wins set — floor 1.50 (50% WR at odds<=1.50, 75% at >1.50)
                    and not (p.get('market_type') == 'tennis_team_win_set'
                             and '(no)' in str(p.get('label', '') or p.get('side', '') or '')
                             and float(p.get('price', 0) or 0) <= 1.50)
                    # h2h: DESACTIVADO 2026-06-02 — WR=52.6%, ROI=-7.6% Sibila n=38 placed; win_set (WR=65.7%) es el mercado
                    and p.get('market_type') not in ('', None)]
    # Platt calibration shadow log — N=54 Sibila tws, A=0.357 B=0.088 — log only, no placement effect
    try:
        from oraculo_tws_calibrator import shadow_log_platt as _tws_platt_shadow
        _tws_platt_shadow(tennis_picks, log)
    except Exception as _e:
        log.debug('TWS Platt shadow skipped: %s', _e)
    # 3c. Scan MLB
    try:
        if not MLB_ENABLED:
            raise ImportError('MLB_ENABLED=False — scan skipped (WR 36.3% < implied 43.6%)')
        from oraculo_mlb import scan_mlb, train_mlb_elo
        _mlb_elo = train_mlb_elo(days_back=45)
        if _mlb_elo and len(_mlb_elo.ratings) >= 20:
            # Scan amplio: genera TODOS los picks del modelo para Sibila
            _all_mlb = scan_mlb(api, state, _mlb_elo, dry_run, min_edge=0.02, min_conf=0.40)
            # Platt calibration sobre TODOS (aprende de todo el espectro)
            try:
                from oraculo_mlb_calibrator import calibrate_picks, train_platt
                train_platt()
                _all_mlb = calibrate_picks(_all_mlb)
            except Exception as _ce:
                log.debug('MLB calibrator skipped: %s', _ce)
            # Sibila graba TODOS antes de filtros
            if _SIBILA_ENABLED:
                for _sp in _all_mlb:
                    _sibila_record(_sp)
            # 2026-06-03: Fade shadow — cuando modelo elige equipo con WR<30%, registrar oponente
            if _SIBILA_ENABLED and MLB_FADE_TEAMS:
                _ev_map = {}
                for _sp in _all_mlb:
                    _eid2 = _sp.get('event_id', '')
                    if _eid2 not in _ev_map:
                        _ev_map[_eid2] = []
                    _ev_map[_eid2].append(_sp)
                import re as _re2
                for _eid2, _eps in _ev_map.items():
                    for _fp in _eps:
                        if _fp.get('market_type') != 'mlb_f5_ml':
                            continue
                        _fm2 = _re2.search(r'F5 ML: (.+?) \(FIP', str(_fp.get('label', '')))
                        if not _fm2:
                            continue
                        _fteam = _fm2.group(1).strip()
                        if _fteam not in MLB_FADE_TEAMS:
                            continue
                        # Fix Jun-04: scan_mlb only generates ONE side per game
                        # (opponent has negative edge => not in _all_mlb). Extract from match string.
                        _match_teams = str(_fp.get('match', '')).split(' vs ')
                        if len(_match_teams) != 2:
                            continue
                        _t1, _t2 = _match_teams[0].strip(), _match_teams[1].strip()
                        _opp_team = _t2 if _t1 == _fteam else _t1
                        _fp_prob = float(_fp.get('model_prob') or 0.5)
                        _opp_prob = round(1.0 - _fp_prob, 4)
                        _opp_price = round(1.0 / max(0.1, _opp_prob), 2)
                        _fade = {
                            'match': _fp.get('match', ''),
                            'event_id': _eid2,
                            'market_url': _fp.get('market_url', ''),
                            'price': _opp_price,
                            'label': 'F5 ML [FADE vs ' + _fteam + ']: ' + _opp_team,
                            'model_prob': _opp_prob,
                            'edge': 0.0,
                            'sport': 'baseball',
                            'league': 'MLB',
                            'market_type': 'mlb_f5_ml_fade',
                            '_shadow_only': True,
                            '_source': 'fade_shadow',
                        }
                        _sibila_record(_fade)
                        log.info('[MLB Fade] shadow %s vs [FADE]%s @%.2f', _opp_team, _fteam, _opp_price)
            # 2026-06-04: counter shadow removed -- scanner blocks Over<6.0 at source,
            # so Over 5.0/5.5 never reach _all_mlb. Data already validated (n=795, WR=30-31%).
            # Solo picks reales para apostar (post-calibracion)
            def _mlb_line_ok(p):
                # v4: Over 6.5+, Under 4.0-5.0, ML pass through
                lbl = (p.get('label') or '').lower()
                m = re.search(r'(over|under)\s+([\d.]+)', lbl)
                if not m:
                    return True  # ML or other — pass through
                direction, line = m.group(1), float(m.group(2))
                if direction == 'over':
                    return line >= 4.5  # v5: backtest OOS Over4.5+ ROI=+30.8%
                if direction == 'under':
                    return False  # v5: Under desactivado (OOS WR=47%)
                return True

            # P2.4: apply global calibration factor to correct systematic overestimation
            if MLB_PROB_CALIBRATION != 1.0:
                for _cp in _all_mlb:
                    # Only calibrate full-game ML; F5 model is separately validated (WR=60%)
                    if _cp.get('market_type') not in ('mlb_f5_ml', 'mlb_f5_total'):
                        _cp['model_prob'] = round(float(_cp.get('model_prob') or 0) * MLB_PROB_CALIBRATION, 4)
                        if _cp.get('odds'):
                            _cp['edge'] = round(_cp['model_prob'] - 1.0/float(_cp['odds']), 4)
            # Filter uses calibrated edge/prob (MLB_PROB_CALIBRATION already applied above)
            # 2026-05-22: added edge > 0 guard — historical bets showed neg-edge picks slipping through
            # 2026-06-01: shadow-only for now — block h2h full-game (WR=41%, ROI=-12%)
            # f5_ml/f5_total: edge threshold relaxed (calibration artifact gives edge~0)
            # 2026-06-02: f5_ml ACTIVADO LIVE — WR=58.1% n=210 supera umbral; MLB_F5_ML_MIN_EDGE=0.08
            mlb_picks = [p for p in _all_mlb
                         if p.get('market_type') in ('mlb_f5_ml', 'mlb_f5_total')
                         and float(p.get('edge') or 0) > 0
                         and float(p.get('edge') or 0) >= (MLB_F5_ML_MIN_EDGE if p.get('market_type') == 'mlb_f5_ml' else MLB_MIN_EDGE)
                         and float(p.get('model_prob') or 0) >= 0.55]
            log.info('MLB: %d candidatos totales, %d pasan pre-filtro v5 (prob>=60%%)',
                     len(_all_mlb), len(mlb_picks))
            # De-correlate: keep best-edge pick per (event_id, market_type)
            # Prevents Kelly inflation from 4+ correlated F5 picks per game
            if mlb_picks:
                _mlb_game_best = {}
                for _p in mlb_picks:
                    _k = (_p.get('event_id',''), _p.get('market_type',''))
                    if _k not in _mlb_game_best or float(_p.get('edge',0)) > float(_mlb_game_best[_k].get('edge',0)):
                        _mlb_game_best[_k] = _p
                _n_before_decorr = len(mlb_picks)
                mlb_picks = list(_mlb_game_best.values())
                if _n_before_decorr != len(mlb_picks):
                    log.info('MLB [de-corr]: %d -> %d picks (best-edge per game/market)',
                             _n_before_decorr, len(mlb_picks))
            # Dead-man switch: reset AutoTune si 3+ ciclos sin picks MLB
            if len(mlb_picks) == 0:
                state['_mlb_zero_cycles'] = state.get('_mlb_zero_cycles', 0) + 1
                if state['_mlb_zero_cycles'] >= 3:
                    state['_mlb_zero_cycles'] = 0
                    log.info('[MLB-Reset] 3 ciclos sin picks MLB (filtro v5 activo — normal en temporada baja)')
            else:
                state['_mlb_zero_cycles'] = 0
    except ImportError:
        log.debug("oraculo_mlb not available")
    except Exception as e:
        log.warning("MLB scan error: %s", e)

    # 3d. Scan soccer corners/bookings (UCL + PL + BL1 + FL1 + SA + PD)
    # v2: form rolling-5 + referee multiplier + league base rate consensus
    sc_picks = []
    try:
        from oraculo_soccer_v2 import scan_soccer_v2, SoccerModelV2
        if not hasattr(scan_soccer_v2, '_sc_model'):
            scan_soccer_v2._sc_model = SoccerModelV2().load()
        sc_picks = scan_soccer_v2(api, state, model=scan_soccer_v2._sc_model, dry_run=dry_run)
        if _SIBILA_ENABLED:
            _sc_prob_key = chr(39)+chr(114)+chr(97)+chr(119)+chr(95)+chr(109)+chr(111)+chr(100)+chr(101)+chr(108)+chr(95)+chr(112)+chr(114)+chr(111)+chr(98)+chr(39)
            for _sp in sc_picks:
                _sp[_sc_prob_key] = float(_sp.get('raw_model_prob') or _sp.get('model_prob') or _sp.get('base_rate_prob') or 0)
                _sp.setdefault('market_type', 'soccer_corners')  # 2026-06-02: tag Sibila tracking
                _sibila_record(_sp)
    except ImportError as _sie:
        log.warning('Soccer v2 import error: %s', _sie)
    except Exception as _sce:
        log.warning('Soccer corners scan error: %s', _sce)


    # 3e. Scan darts (Premier League + Modus)
    try:
        from oraculo_darts import train_darts_elo, scan_darts
        _darts_elo = train_darts_elo()
        _darts_picks = scan_darts(api, state, _darts_elo, shadow=False)
        if _darts_picks:
            log.info('[Darts] %d picks:', len(_darts_picks))
            for _dp in _darts_picks:
                log.info('  [Darts] %s | %s | edge=%.1f%% conf=%.0f%% @%.3f',
                         _dp.get("match"), _dp.get("label"),
                         _dp.get("edge", 0) * 100, _dp.get("confidence", 0) * 100,
                         _dp.get("price", 0))
            if _SIBILA_ENABLED:
                for _dp in _darts_picks:
                    _sibila_record(_dp)
            picks.extend(_darts_picks)
        else:
            log.debug("Darts: 0 value picks")
    except Exception as _de:
        log.debug("Darts scan error: %s", _de)

    # 3b. Filter tennis picks through LLM (quality gate)
    if tennis_picks:
        try:
            from oraculo_llm import filter_picks_with_llm
            original_count = len(tennis_picks)
            _elo = getattr(scan_tennis, '_elo', None)
            tennis_picks = filter_picks_with_llm(
                tennis_picks, _elo,
                surface='hard', tourney='ATP Miami')
            if len(tennis_picks) < original_count:
                log.info('LLM vetoed %d/%d tennis picks',
                         original_count - len(tennis_picks), original_count)
        except ImportError:
            log.debug('oraculo_llm not available, skipping LLM filter')
        except Exception as e:
            log.warning('LLM filter error (continuing without): %s', e)

    # 4. Build tennis parlays (filter restricted events first)
    _restricted_evs_p = state.get('restricted_event_ids', [])
    _restricted_pfx_p = state.get('restricted_market_prefixes', [])
    tennis_picks_ok = [p for p in tennis_picks
                       if str(p.get('event_id','')) not in _restricted_evs_p
                       and not any(p.get('market_url','').startswith(x) for x in _restricted_pfx_p)]
    if not state.get("tennis_parlay_placed_today"):
        parlays = build_parlays(tennis_picks_ok, state)
    else:
        parlays = []
    if parlays:
        log.info('Tennis parlays: %d combos generated', len(parlays))

    # 4b. Build daily football parlay (once per day, best confidence picks)
    fb_parlay = build_daily_football_parlay(football_picks, state)
    if fb_parlay:
        parlays.append(fb_parlay)
        log.info('Daily football parlay added: %d legs @%.2f', fb_parlay['n_legs'], fb_parlay['combined_odds'])

    # 4c. Build daily mixed parlay (2 football + 2 tennis, 5% bankroll)
    mixed = None
    if not state.get('mixed_parlay_placed_today'):
        mixed = build_mixed_parlay(football_picks, tennis_picks, state)
        if mixed:
            parlays.append(mixed)
            log.info('Daily mixed parlay added: %d legs @%.2f', mixed['n_legs'], mixed['combined_odds'])

    # 5. Place football bets first (capped to leave room for tennis)
    placed = 0
    if football_picks or (parlays and not tennis_picks):
        # Cap football budget: leave TENNIS_BUDGET_RESERVE for tennis if tennis picks exist
        bankroll = state['bankroll']
        # Daily budget proporcional: 25%% del bankroll, escala en ambas direcciones
        # 00->0 | 00->5 | 00->00 | 00->25 | <00 -> min 0
        _daily_budget = max(10.0, bankroll * 0.25)
        max_today = _daily_budget
        if tennis_picks:
            tennis_reserve = max_today * TENNIS_BUDGET_RESERVE
            football_cap = max_today - tennis_reserve
            already_staked = state['daily_staked']
            football_remaining = max(0, football_cap - already_staked)
            # Temporarily reduce daily limit for football placement
            orig_daily_pct = MAX_DAILY_PCT
            # Set effective ceiling as already_staked + football_remaining
            state['_football_ceiling'] = already_staked + football_remaining
        fb_parlays = [p for p in parlays if p.get('sport') in ('soccer', 'mixed')]
        placed += place_bets(api, state, football_picks, fb_parlays, dry_run)
        state.pop('_football_ceiling', None)

    # 6. Place tennis bets with remaining budget
    if tennis_picks or parlays:
        tn_parlays = [p for p in parlays if p.get('sport') not in ('soccer', 'mixed')]
        placed += place_bets(api, state, tennis_picks, tn_parlays, dry_run)
        # Mark parlays as placed today
        if fb_parlay and not dry_run:
            state['football_parlay_placed_today'] = True
        if mixed and not dry_run:
            state['mixed_parlay_placed_today'] = True
        state["tennis_parlay_placed_today"] = True
    else:
        log.info('No value bets found this cycle')
        placed = 0

    # 6b. Place MLB bets — with quality filters (2026-05-04)
    if mlb_picks:
        _mlb_settled = sum(1 for h in state.get('bet_history', [])
                           if h.get('sport') == 'baseball' and h.get('result') in ('WIN', 'LOSS'))

        # FILTROS MLB v4 (2026-05-07) — patron mining sobre 3077 picks Sibila
        # OVER 6.5+: prob>=70% WR=100% (n=24) vs prob<65% WR=49% — exigir 70%
        # UNDER 4.5: prob>=65% WR=100% (n=18) — exigir 65%
        # Lunes: WR=0% PnL=-$253 — no apostar
        # Odds 1.90-2.10: WR=34% dentro de v3 — evitar
        # Edge >=25%: WR=96% PnL=+$2019 — priorizar
        import datetime as _dt
        _dow = _dt.datetime.utcnow().weekday()  # 0=Mon
        _active_matches = set(b.get('match', '') for b in state.get('active_bets', []))
        _filtered_mlb = []

        # Regla global: no apostar los lunes (WR=0% historico)
        if _dow == 0:
            log.info('MLB [lunes-skip]: WR historico=0%% en lunes — no se apuesta hoy')
            mlb_picks = []
        else:
            for _mp in mlb_picks:
                _lbl = str(_mp.get('label', '') or _mp.get('side', '')).lower()
                _mch = str(_mp.get('match', ''))
                _lm = re.search(r'(?:over|under)\s+([\d.]+)', _lbl)
                _line = float(_lm.group(1)) if _lm else 0.0
                _odds = float(_mp.get('price') or _mp.get('odds') or 2.0)
                _edge = float(_mp.get('edge') or 0)
                _is_over  = 'over'  in _lbl
                _is_under = 'under' in _lbl
                _is_ml    = 'ml'    in _lbl
                _conf = float(_mp.get('raw_model_prob_uncal') or _mp.get('raw_model_prob') or _mp.get('model_prob') or _mp.get('confidence') or 0)
                _mkt_type = str(_mp.get('market_type', ''))

                # Filtro 0: PROB minima 0.60 (O/U) — bucket 55-59% destruye $13k
                # Under 4.5: ML model validated threshold is 0.58
                # Filtro v5 (2026-05-11): backtest OOS 1218 juegos, ROI +30.8%
                # Over >= 4.5, prob >= 0.65, odds >= 1.75 | Under DESACTIVADO

                # Filtro 0: prob minima 0.63 para O/U (no ML)
                if _conf < 0.63 and not _is_ml:
                    log.info('MLB [prob-skip %.0f%%<63%%]: bajo threshold v5', _conf * 100)
                    continue

                # Filtro 1: 1 apuesta por partido
                if _mch in _active_matches:
                    log.info('MLB [1-per-match]: %s ya activo', _mch[:30])
                    continue

                # Filtro 1b: NUNCA apostar en FADE_TEAMS — modelo los sobreestima (WR=22-38%)
                # FADE_TEAMS en live = EV negativo: breakeven > WR real historico
                if _is_ml and _mkt_type == 'mlb_f5_ml':
                    import re as _re_fade
                    _picked_m = _re_fade.search(r'f5 ml: (.+?) \(fip', _lbl)
                    if _picked_m:
                        _picked_team = _picked_m.group(1).strip()
                        if _picked_team in MLB_FADE_TEAMS_LOWER:
                            log.info('MLB [fade-block %s]: WR historico<breakeven — no apostar en vivo', _picked_team[:15])
                            continue
                        else:
                            log.debug('MLB [fade-ok %s]: no en fade_teams, pasa', _picked_team[:15])

                # Filtro 2 v6: OVER — solo lineas 6.5+ (Fase3: 4.5-6.0 WR=30-44% Sibila 1230p -> BLOQUEADO)
                if _is_over:
                    if _line <= 6.0:
                        log.info('MLB [over-skip line%.1f]: linea<=6.0 bloqueada (4.5-6.0 WR=30-44%% Sibila)', _line)
                        continue
                    if _conf < 0.63:
                        log.info('MLB [over-prob-skip %.0f%%<63%%]: requiere prob>=63%%', _conf * 100)
                        continue
                    if _odds < 1.75:
                        log.info('MLB [over-odds-skip @%.2f<1.75]: odds insuficientes', _odds)
                        continue

                # Filtro 3 v5: UNDER — solo linea 4.5 habilitada
                # F5 Under 4.5: WR=67% n=246 Sibila (2026-06-03) — re-enable selectivo
                # Otras lineas (2.5/3.0/3.5): WR=31-41% — siguen bloqueadas
                # Blanket ban 2026-05-26 era para todos; ahora 4.5 separado
                _mkt_type = str(_mp.get('market_type',''))
                if _is_under and not _is_ml:
                    if _line == 4.5 and _mkt_type == 'mlb_f5_total' and _conf >= 0.65:
                        log.info('[MLB Under4.5 LIVE] WR=67%% Sibila n=246 @%.2f conf=%.0f%%',
                                 _odds, _conf * 100)
                    else:
                        log.info('MLB [under-disabled line=%.1f conf=%.0f%%]: solo Under4.5 con conf>=65%%',
                                 _line, _conf * 100)
                        continue

                # Filtro 4: solo F5 — full-game ML/total desactivado
                # Real: 91 bets -10.3% ROI | Sibila: 2996 bets -6.2% ROI (2026-05-19)
                if _mkt_type in ('mlb_full_ml', 'mlb_full_total'):
                    log.info('MLB [fullgame-disabled %s]: ROI -10.3%% real / -6.2%% Sibila', _mkt_type)
                    continue

                # Filtro 5: FIP gate — skip F5 ML si oponente (fade team) tiene pitcher elite ese dia
                # Cuando fade team tiene FIP<=3.20 vs nuestro pick, el modelo subestima su pitching
                # Ejemplo: TB 0-DET 8 (DET fade team, FIP 3.43 — justo por encima del threshold)
                if _is_ml and _mkt_type == 'mlb_f5_ml':
                    _fip_m = re.search(r'fip ([\d.]+)/([\d.]+)', _lbl)
                    _match_parts = str(_mch).split(' vs ')
                    if _fip_m and len(_match_parts) == 2:
                        _home_fip  = float(_fip_m.group(1))
                        _away_fip  = float(_fip_m.group(2))
                        _home_team = _match_parts[0].strip()
                        _picked_m  = re.search(r'f5 ml: (.+?) \(fip', _lbl)
                        if _picked_m:
                            _picked   = _picked_m.group(1).strip()
                            _opp_name = _match_parts[1].strip() if _picked.lower() == _home_team.lower() else _match_parts[0].strip()
                            _opp_fip  = _away_fip if _picked.lower() == _home_team.lower() else _home_fip
                            if _opp_name in MLB_FADE_TEAMS and _opp_fip <= 3.20:
                                log.info('MLB [fip-fade-gate %s]: oponente fade %s FIP=%.2f<=3.20 — skip',
                                         _picked[:12], _opp_name[:12], _opp_fip)
                                continue

                _filtered_mlb.append(_mp)
                _active_matches.add(_mch)

            _n_removed = len(mlb_picks) - len(_filtered_mlb)
            if _n_removed:
                log.info('MLB filters v5: %d/%d removidos', _n_removed, len(mlb_picks))
            mlb_picks = _filtered_mlb

        # Stake cap: F5 O/U $2.00 (shadow-validated 3000+ picks), F5 ML $1.00, Under4.5 $1.00 (new)
        # 2026-06-04: boost teams (MIL/WAS WR>=89% n>=14) get $2.00 cap (doble del normal f5_ml)
        for _mp in mlb_picks:
            if _mp.get('raw_model_prob_uncal'):
                _mp['model_prob'] = _mp['raw_model_prob_uncal']  # raw prob -> Kelly positivo
            _lbl_lower = str(_mp.get('label', '')).lower()
            _is_boost = any(t.lower() in _lbl_lower for t in MLB_BOOST_TEAMS)
            if _mp.get('market_type') == 'mlb_f5_ml' and _is_boost:
                _mp['_max_stake'] = 15.00  # 2026-06-04: boost (MIL/WAS WR>=89% n>=14, daily 0)
            elif _mp.get('market_type') == 'mlb_f5_ml' and float(_mp.get('_min_fip', 99)) <= 2.0:
                _mp['_max_stake'] = 12.00  # elite pitching min_fip<=2.0 WR=74-86%% Sibila Jun-04
            elif _mp.get('market_type') == 'mlb_f5_ml':
                _mp['_max_stake'] = 10.00  # 2026-06-02: daily 0 normal
            elif 'under 4.5' in _lbl_lower:
                _mp['_max_stake'] = 8.00  # 2026-06-03: Under4.5 daily 0
            else:
                _mp['_max_stake'] = 12.00  # F5 O/U daily 0
        # F5 Under dome shadow (2026-06-05): WR=60% n=10 -- accumulate before enabling live
        _dome_under = [p for p in mlb_picks
                       if "under" in str(p.get("label", "") or p.get("side", "")).lower()
                       and "[dome]" in str(p.get("label", "") or p.get("side", "")).lower()]
        for _dp in _dome_under:
            _dp["_shadow_only"] = True
            try:
                _sibila_record(_dp)
                log.info("[MLB F5-Under Dome] shadow %s @%.2f", _dp.get("match", "?"), _dp.get("price", 0))
            except Exception as _e:
                log.warning("[MLB F5-Under Dome] sibila_record err: %s", _e)
        mlb_picks = [p for p in mlb_picks if 'under' not in str(p.get('label', '') or p.get('side', '')).lower()]  # 2026-06-05: F5 Under WR=40.3% n=77 block
        if mlb_picks:
            _caps = {p.get('match','?'): p.get('_max_stake',2.0) for p in mlb_picks}
            log.info('MLB: %d picks -> place_bets | caps=%s | settled=%d',
                     len(mlb_picks), _caps, _mlb_settled)
        placed += place_bets(api, state, mlb_picks, [], dry_run)

    # 7. Sync Obsidian
    sync_obsidian(state)

    # 8. Check alerts
    check_alerts(state)

    # 9. Save state
    state['last_scan'] = datetime.now().isoformat()
    save_state(state)

    # 10. Generate dashboard
    generate_dashboard(state)

    # 6c. Soccer corners/bookings — SHADOW MODE PERMANENTE hasta nuevo modelo
    # BACKTEST 2026-05-06: correlation=0.020 (sin señal real). Poisson medio
    # predice siempre ~40bp sin discriminar partidos. Modelo NO apto para live.
    # ROI simulado Over45.5=-34.7%, Under35.5=-26.6%. Requiere: referee data +
    # form + table position antes de reactivar.

    # Soccer goals model runs independently (no referee required)
    try:
        from oraculo_soccer_v2 import scan_soccer_goals as _scan_goals
        _goals_comps = [c for c in (
            state.get('_soccer_comps') or [
                'soccer-england-premier-league', 'soccer-germany-bundesliga',
                'soccer-italy-serie-a',  # re-enabled 2026-05-26: shadow WR 80% (10 clean picks); prev -3.4% was pre-dedup contamination
                'soccer-spain-laliga',
                'soccer-france-ligue-1', 'soccer-netherlands-eredivisie',
                'soccer-portugal-primeira-liga', 'soccer-international-clubs-uefa-champions-league',
                # New leagues (added 2026-05-11)
                'soccer-england-championship', 'soccer-germany-2-bundesliga',
                'soccer-spain-laliga-2', 'soccer-france-ligue-2',
                'soccer-italy-serie-b', 'soccer-scotland-premiership',
                'soccer-belgium-first-division-a',
                # FIFA World Cup 2026 — added 2026-05-19
                'soccer-international-world-cup',
                # CONMEBOL — added 2026-05-22 (stricter thresholds applied post-filter)
                'soccer-international-clubs-copa-libertadores',
                'soccer-international-clubs-copa-sudamericana',
                # Intl shadow 2026-06-02 -- validar post-WC antes de apostar
                'soccer-international-conmebol-copa-america',
                'soccer-international-uefa-nations-league',
            ]
        ) if 'soccer' in c or 'international' in c]
        _goal_picks = _scan_goals(api, state, comp_keys=_goals_comps, dry_run=dry_run, min_edge=0.12, min_conf=0.70)
        if _goal_picks:
            log.info('[Soccer Goals] %d picks found', len(_goal_picks))
            if _SIBILA_ENABLED:
                for _gp in _goal_picks:
                    _sibila_record(_gp)
            if SOCCER_GOALS_ENABLED:
                # Only bet picks where CSV form data was available (not odds proxy)
                # Exception: WC picks allowed via odds-proxy in Sibila shadow (no CSV for internationals)
                _CONMEBOL_COMPS = {
                    'soccer-international-clubs-copa-libertadores',
                    'soccer-international-clubs-copa-sudamericana',
                }
                # Shadow-only intl comps: registrar en Sibila, no apostar hasta validar WC
                _INTL_SHADOW_COMPS = {
                    'soccer-international-conmebol-copa-america',
                    'soccer-international-uefa-nations-league',
                }
                # 2026-06-03: Goals 2H Under — relajar CSV gate para ligas domésticas
                # Sibila: Goals 2H Under 2.5 WR=96% n=273, Under 1.5 WR=81% n=167
                # Ligas domésticas aprobadas (tienen odds history como señal alternativa)
                _GOALS2H_DOMESTIC = {
                    'soccer-england-premier-league', 'soccer-germany-bundesliga',
                    'soccer-italy-serie-a', 'soccer-spain-laliga', 'soccer-france-ligue-1',
                    'soccer-netherlands-eredivisie', 'soccer-portugal-primeira-liga',
                    'soccer-england-championship', 'soccer-germany-2-bundesliga',
                    'soccer-spain-laliga-2', 'soccer-scotland-premiership',
                    'soccer-belgium-first-division-a',
                }
                def _goals2h_under_ok(p):
                    import re as _re_xg
                    lbl = str(p.get('label', '') or p.get('side', '')).lower()
                    # Goals 2H Over: WR=0% n=3 shadow -- solo Under tiene senal
                    if 'goals 2h' in lbl and 'over' in lbl:
                        return False
                    if 'goals 2h' not in lbl or 'under' not in lbl:
                        return False
                    # xG gate (Sibila 2026-06-05):
                    # Under 1.5: xG<=0.4 WR=98% (n=55), xG 0.5-0.6 WR=20% (n=5) -- BLOCK 0.5+
                    # Under 2.5: xG<=1.2 WR=100% (n=20), xG>1.2 WR=66% (n=6) -- BLOCK 1.3+
                    _m_xg = _re_xg.search(r'xg ([0-9.]+)', lbl)
                    if _m_xg:
                        _xg = float(_m_xg.group(1))
                        if 'under 1.5' in lbl and _xg > 0.4:
                            return False
                        if 'under 2.5' in lbl and _xg > 1.2:
                            return False
                    return (p.get("league") in _GOALS2H_DOMESTIC
                        and float(p.get("edge", 0) or 0) >= 0.12
                        and float(p.get("confidence", p.get("conf", 0)) or 0) >= 0.70)

                def _goals_over_line_ok(p):
                    # 2026-06-04 Fase3: FT Over 4.5-6.0 shadow WR=30-44% -$8928 -- BLOCK
                    # Keep: Over<=3.5 (WR=56-76%), Over>=6.5 (WR=58-89%)
                    import re as _re2
                    lbl = str(p.get('label', '')).lower()
                    if p.get('market_type') != 'soccer_goals':
                        return True
                    if 'over' not in lbl or 'goals 2h' in lbl:
                        return True
                    m = _re2.search(r'over\s*(\d+\.?\d*)', lbl)
                    if not m:
                        return True
                    line = float(m.group(1))
                    if 4.5 <= line <= 6.0:
                        log.debug('[Soccer Goals] BLOCKED FT Over %.1f (WR=30-44%% gate): %s',
                                  line, p.get('match', '?'))
                        return False
                    return True

                _gp_csv = [p for p in _goal_picks
                           if (p.get('_csv_form')
                               or p.get('league') == 'soccer-international-world-cup'
                               or (p.get('league') in _CONMEBOL_COMPS
                                   and p.get('league') not in _INTL_SHADOW_COMPS
                                   and p.get('edge', 0) >= 0.14
                                   and p.get('conf', 0) >= 0.72)
                               or _goals2h_under_ok(p))
                           and _goals_over_line_ok(p)]
                # Skip picks with kickoff >48h away (allows next-day matches, prevents weeks-long capital lock-up)
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _now_utc = _dt.now(_tz.utc)
                def _kicks_within_48h(p):
                    ct = p.get('cutoff_time', '')
                    if not ct:
                        return True
                    try:
                        _ko = _dt.fromisoformat(ct.replace('Z', '+00:00'))
                        _delta = (_ko - _now_utc).total_seconds()
                        if _delta > 48 * 3600:
                            log.debug('[Soccer Goals] skip far-future match (%.0fh): %s', _delta/3600, p.get('match','?'))
                            return False
                        return True
                    except Exception:
                        return True
                _gp_csv = [p for p in _gp_csv if _kicks_within_48h(p)]
                if _gp_csv:
                    global MAX_TOTAL_EXPOSURE
                    _saved_exp = MAX_TOTAL_EXPOSURE
                    MAX_TOTAL_EXPOSURE = min(0.70, MAX_TOTAL_EXPOSURE + 0.10)
                    # Tag under picks for higher Kelly (soccer_under: 0.25 vs soccer: 0.20)
                    for _gp2 in _gp_csv:
                        if "under" in str(_gp2.get("label","")).lower():
                            _gp2["sport"] = "soccer_under"
                    # Goals 2H Under doméstico sin CSV: cap $3 (WR=90%+ Sibila, conservador)
                    # Otros sin CSV (intl): cap $5
                    for _gp2 in _gp_csv:
                        if not _gp2.get("_csv_form"):
                            _lbl2 = str(_gp2.get("label","")).lower()
                            if 'goals 2h' in _lbl2 and 'under' in _lbl2 \
                                    and _gp2.get("league") in _GOALS2H_DOMESTIC:
                                _gp2.setdefault("_max_stake", 12.00)
                                log.debug("[Soccer Goals 2H Under] dom cap $3: %s", _gp2.get("match","?"))
                            else:
                                _gp2.setdefault("_max_stake", 5.00)
                                log.debug("[Soccer Goals] intl cap $5: %s", _gp2.get("match","?"))
                    placed += place_bets(api, state, _gp_csv, [], dry_run)
                    MAX_TOTAL_EXPOSURE = _saved_exp
                    log.info('[Soccer Goals] %d csv-backed picks placed', len(_gp_csv))
                else:
                    log.debug('[Soccer Goals] no csv-backed picks this cycle')
            else:
                log.debug('[Soccer Goals] SOCCER_GOALS_ENABLED=False — shadow only, %d picks', len(_goal_picks))
    except Exception as _ge:
        log.debug('Soccer goals scan error: %s', _ge)

    # Copa Libertadores/Sudamericana — SHADOW ONLY (sin CSV form, thresholds reducidos)
    # Objetivo: acumular datos en Sibila para validar WR antes de habilitar real
    try:
        from oraculo_soccer_v2 import scan_soccer_goals as _scan_goals_copa
        _copa_comps = [
            'soccer-international-clubs-copa-libertadores',
            'soccer-international-clubs-copa-sudamericana',
        ]
        _copa_picks = _scan_goals_copa(api, state, comp_keys=_copa_comps, dry_run=True,
                                        min_edge=0.06, min_conf=0.62)
        if _copa_picks and _SIBILA_ENABLED:
            _copa_new = 0
            for _cp in _copa_picks:
                _cp['_shadow_only'] = True
                _cp['_source'] = 'copa_shadow'
                _sibila_record(_cp)
                _copa_new += 1
            if _copa_new:
                log.info('[Copa Shadow] %d picks registrados en Sibila (shadow)', _copa_new)
    except Exception as _ce:
        log.debug('Copa shadow scan error: %s', _ce)

    if sc_picks:
        # Referee-filtered picks go to place_bets; unfiltered shadow only
        from oraculo_soccer_v2 import _is_high_card_ref
        _sc_real = [p for p in sc_picks if _is_high_card_ref(p.get('_referee',''))[0]]
        _sc_shadow = [p for p in sc_picks if not _is_high_card_ref(p.get('_referee',''))[0]]
        if _sc_real and SOCCER_ENABLED:
            log.info('[Soccer] %d picks con arbitro alta-tarjeta -> place_bets', len(_sc_real))
            for _scp in _sc_real:
                log.info('  [SC-real] %s | %s | ref=%s | edge=%.1f%% conf=%.0f%% @%.2f',
                         _scp.get('match','?')[:30], _scp.get('label','?')[:25],
                         _scp.get('_referee','?')[:15],
                         _scp.get('edge',0)*100, _scp.get('model_prob',0)*100, _scp.get('price',0))
            placed += place_bets(api, state, _sc_real, [], dry_run)
        elif _sc_real:
            log.debug('[Soccer V2] SOCCER_ENABLED=False — shadow only, %d picks', len(_sc_real))
        if _sc_shadow:
            log.info('[Soccer] SHADOW (sin arbitro confirmado): %d picks', len(_sc_shadow))
            for _scp in _sc_shadow:
                log.info('  [SC-shadow] %s | %s | edge=%.1f%% conf=%.0f%% @%.2f',
                         _scp.get('match','?')[:35], _scp.get('label','?')[:30],
                         _scp.get('edge',0)*100, _scp.get('model_prob',0)*100, _scp.get('price',0))

    # CLV: update closing odds snapshot for all active bets
    if _CLV_ORACLE_ENABLED:
        try:
            _CLV_ORACLE.record_cloudbet_clv(api, state.get('active_bets', []),
                os.path.join(SCRIPT_DIR, 'sibila.db'))
        except Exception as _clve:
            log.debug('CLV cloudbet update failed: %s', _clve)
    else:
        # 2026-06-02: record_cloudbet_clv usa cloudbet_config.json, no ODDS_API_KEY
        try:
            from oraculo_clv import CLVOracle as _CLVOracleCB
            _cb_clv = _CLVOracleCB(odds_api_key='')
            _cb_clv.record_cloudbet_clv(api, state.get('active_bets', []),
                os.path.join(SCRIPT_DIR, 'sibila.db'))
        except Exception as _clve2:
            log.debug('CLV cloudbet (standalone) failed: %s', _clve2)

    log.info('Cycle complete: %d picks found (%d fb + %d tn + %d mlb + %d sc), %d placed | Bankroll: $%.2f',
             len(football_picks) + len(tennis_picks) + len(mlb_picks) + len(sc_picks), len(football_picks), len(tennis_picks), len(mlb_picks), len(sc_picks),
             placed, state['bankroll'])
    return state



def _prune_odds_history():
    """Delete rows older than 14 days from odds_history.db. Runs at startup."""
    db_path = os.path.join(SCRIPT_DIR, ".oraculo_cache", "odds_history.db")
    if not os.path.exists(db_path):
        return
    size_mb = os.path.getsize(db_path) / 1048576
    if size_mb < 50:
        return
    try:
        import sqlite3 as _sq
        con = _sq.connect(db_path, timeout=30)
        cur = con.cursor()
        cur.execute("DELETE FROM odds_snapshots WHERE timestamp < datetime('now', '-14 days')")
        deleted = cur.rowcount
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        if deleted:
            new_mb = os.path.getsize(db_path) / 1048576
            log.info('odds_history pruned: %d rows deleted, %.0f MB remaining', deleted, new_mb)
    except Exception as _e:
        log.warning('odds_history prune failed: %s', _e)

# ---------------------------------------------------------------------------
# 24/7 loop
# ---------------------------------------------------------------------------
def run_loop():
    """Run autonomous loop forever."""
    _prune_odds_history()
    log.info('*' * 60)
    log.info('  ORACULO AUTONOMOUS RUNNER STARTED')
    log.info('  Scan interval: %ds | Result check: %ds', SCAN_INTERVAL, RESULT_INTERVAL)
    log.info('  Min edge: %.0f%% | Min conf: %.0f%%', MIN_EDGE * 100, MIN_CONF * 100)
    log.info('  Circuit breaker: $%.2f', CIRCUIT_BREAKER)
    log.info('*' * 60)

    # Start interactive Telegram bot
    if TELEGRAM_ENABLED:
        _start_telegram_bot()

    # Start dashboard HTTP server
    try:
        import threading
        from http.server import HTTPServer, SimpleHTTPRequestHandler
        class _DashHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=SCRIPT_DIR, **kwargs)
            def log_message(self, fmt, *args):
                pass
        def _serve_dashboard():
            try:
                srv = HTTPServer(('0.0.0.0', 8889), _DashHandler)
                srv.serve_forever()
            except Exception:
                pass
        _dt = threading.Thread(target=_serve_dashboard, daemon=True)
        _dt.start()
        log.info('Dashboard server started on port 8889')
    except Exception as e:
        log.debug('Dashboard server failed: %s', e)

    last_scan = 0
    last_check = 0
    last_soccer_resolve = 0
    last_mlb_resolve = 0
    last_tennis_resolve = 0
    last_wnba_resolve       = 0
    last_exposure_rebalance = 0
    last_nhl_resolve        = 0
    last_nba_resolve        = 0
    last_tennis_update = 0
    TENNIS_UPDATE_INTERVAL = 86400  # Once per day
    NEWS_REFRESH_INTERVAL = 7200   # Refresh tennis news every 2 hours
    last_news_refresh = 0

    # Dead-man switch: check if a previous crash left a stale heartbeat
    _hb_stale = False
    try:
        if os.path.exists(HEARTBEAT_FILE):
            _hb_age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
            if _hb_age > 1800:  # > 30 min
                _hb_stale = True
                log.warning('HEARTBEAT STALE: last beat %.0f min ago — runner was down!',
                            _hb_age / 60)
                if TELEGRAM_ENABLED:
                    send_telegram(
                        f'\u26a0\ufe0f Oraculo restarted after {_hb_age/60:.0f} min downtime')
    except Exception:
        pass

    while True:
        now_t = time.time()
        # Write heartbeat file every iteration (dead-man switch)
        try:
            open(HEARTBEAT_FILE, 'w').write(str(now_t))
        except Exception:
            pass
        # Refresh tennis news for LLM context
        if now_t - last_news_refresh >= NEWS_REFRESH_INTERVAL:
            try:
                from oraculo_llm import fetch_tennis_news
                news = fetch_tennis_news()
                if news:
                    log.info('Tennis news refreshed: %d headlines', len(news))
                last_news_refresh = now_t
            except Exception:
                last_news_refresh = now_t

        # --- Auto-retrain Poisson/ELO models (weekly) ---
        # Weekly odds_history prune (in case service runs for weeks without restart)
        _PRUNE_INTERVAL = 86400 * 7  # 7 days
        if not hasattr(run_loop, "_last_prune"):
            run_loop._last_prune = now_t
        elif now_t - run_loop._last_prune >= _PRUNE_INTERVAL:
            _prune_odds_history()
            run_loop._last_prune = now_t

        POISSON_RETRAIN_INTERVAL = 86400 * 7  # 7 days
        if not hasattr(scan_football, "_last_poisson_train"):
            scan_football._last_poisson_train = 0
        if now_t - scan_football._last_poisson_train >= POISSON_RETRAIN_INTERVAL:
            try:
                import pickle as _pk
                from collections import defaultdict as _dd
                from oraculo_models_advanced import PoissonGoalModel, EloRating
                try:
                    from oraculo_xg import fetch_footballdata_results
                    fetch_footballdata_results(force_refresh=True)
                    log.info("Refreshed football-data.co.uk results for retrain")
                except Exception as _fe:
                    log.warning("Failed to refresh football data: %s", _fe)
                _xg_cache = os.path.join(SCRIPT_DIR, ".oraculo_cache", "xg_matches.json")
                if os.path.exists(_xg_cache):
                    with open(_xg_cache) as _f:
                        _matches_raw = json.load(_f)
                    _matches = [{
                        "home_team": _m.get("home", _m.get("home_team", "")),
                        "away_team": _m.get("away", _m.get("away_team", "")),
                        "home_score": _m.get("home_goals", _m.get("home_score")),
                        "away_score": _m.get("away_goals", _m.get("away_score")),
                        "utc_date": _m.get("date", ""),
                    } for _m in _matches_raw]
                    if len(_matches) >= 50:
                        _poisson = PoissonGoalModel()
                        _poisson.fit(_matches)
                        _elo = EloRating()
                        _elo.process_matches(_matches)
                        _mdir = os.path.join(SCRIPT_DIR, "models")
                        os.makedirs(_mdir, exist_ok=True)
                        def _ddd(obj):
                            if isinstance(obj, _dd): return {k: _ddd(v) for k, v in obj.items()}
                            if isinstance(obj, dict): return {k: _ddd(v) for k, v in obj.items()}
                            if isinstance(obj, list): return [_ddd(i) for i in obj]
                            return obj
                        with open(os.path.join(_mdir, "poisson_state.pkl"), "wb") as _f:
                            _pk.dump(_ddd(_poisson.__dict__), _f)
                        with open(os.path.join(_mdir, "elo_state.pkl"), "wb") as _f:
                            _pk.dump(_ddd(_elo.__dict__), _f)
                        log.info("Poisson+ELO retrained from %d matches", len(_matches))
                        scan_football._last_poisson_train = now_t
            except Exception as _e:
                log.warning("Poisson/ELO retrain failed: %s", _e)

        # --- Refresh NBA results cache (weekly) ---
        NBA_REFRESH_INTERVAL = 21600  # 6h - matches fetch_nba_results TTL
        if not hasattr(scan_football, "_last_nba_refresh"):
            scan_football._last_nba_refresh = 0
        if now_t - scan_football._last_nba_refresh >= NBA_REFRESH_INTERVAL:
            try:
                from oraculo_nba import fetch_nba_results, train_nba_elo as _nba_retrain
                fetch_nba_results(force_refresh=True)
                _nba_retrain(force=True)  # rebuild Elo from fresh results
                scan_football._last_nba_refresh = now_t
                log.info('NBA results + Elo refreshed')
                log.info("NBA results cache refreshed")
            except Exception as _e:
                log.warning("NBA refresh skipped: %s", _e, exc_info=True)

        # Daily tennis Elo data refresh
        if now_t - last_tennis_update >= TENNIS_UPDATE_INTERVAL:
            try:
                from update_tennis_data import update_cache
                if update_cache():
                    log.info('Tennis Elo data updated from GitHub')
                last_tennis_update = now_t
            except Exception as e:
                log.debug('Tennis data update skipped: %s', e)
                last_tennis_update = now_t  # Don't retry immediately
        now = time.time()
        try:
            # Full scan cycle
            if now - last_scan >= SCAN_INTERVAL:
                run_cycle()
                last_scan = now
                last_check = now  # cycle already checks results

            # Intermediate result check
            elif now - last_check >= RESULT_INTERVAL:
                state = load_state()
                api = CloudbetAPI()
                # Capture closing odds for CLV before checking results
                try:
                    _capture_closing_odds(api, state)
                except Exception as _clv_e:
                    log.debug('CLV capture skipped: %s', _clv_e)
                settled = check_results(api, state)
                # Soccer shadow resolver (runs when CSV data available)
                if now - last_soccer_resolve >= 3600:
                    try:
                        _n_res, _n_skip, _n_nf = _soccer_resolve_shadows()
                        if _n_res:
                            log.info('Soccer shadow resolver: %d resolved', _n_res)
                    except Exception as _sr_e:
                        log.debug('Soccer resolve error: %s', _sr_e)
                    last_soccer_resolve = now
                if now - last_mlb_resolve >= 3600:
                    try:
                        _n_res, _n_skip, _n_nf = _mlb_resolve_shadows()
                        if _n_res:
                            log.info('MLB shadow resolver: %d resolved', _n_res)
                    except Exception as _mr_e:
                        log.debug('MLB resolve error: %s', _mr_e)
                    last_mlb_resolve = now
                if now - last_tennis_resolve >= 3600:
                    try:
                        _n_res, _n_skip, _n_nf = _tennis_resolve_shadows()
                        if _n_res:
                            log.info('Tennis shadow resolver: %d resolved', _n_res)
                    except Exception as _tr_e:
                        log.debug('Tennis resolve error: %s', _tr_e)
                    last_tennis_resolve = now
                if now - last_wnba_resolve >= 3600:
                    try:
                        import sqlite3 as _sq3
                        _wconn = _sq3.connect(os.path.join(SCRIPT_DIR, 'sibila.db'))
                        _n_wres = _wnba_resolve_shadows(_wconn)
                        _wconn.close()
                        if _n_wres:
                            log.info('WNBA shadow resolver: %d resolved', _n_wres)
                    except Exception as _wr_e:
                        log.debug('WNBA resolve error: %s', _wr_e)
                    last_wnba_resolve = now
                if now - last_nhl_resolve >= 3600:
                    try:
                        import sqlite3 as _sq3n
                        _nconn = _sq3n.connect(os.path.join(SCRIPT_DIR, 'sibila.db'))
                        _n_nres = _nhl_resolve_shadows(_nconn)
                        _nconn.close()
                        if _n_nres:
                            log.info('NHL shadow resolver: %d resolved', _n_nres)
                    except Exception as _nr_e:
                        log.debug('NHL resolve error: %s', _nr_e)
                    last_nhl_resolve = now
                if now - last_nba_resolve >= 3600:
                    try:
                        import sqlite3 as _sq3b
                        _bconn = _sq3b.connect(os.path.join(SCRIPT_DIR, 'sibila.db'))
                        _n_bres = _nba_resolve_shadows(_bconn)
                        _bconn.close()
                        if _n_bres:
                            log.info('NBA shadow resolver: %d resolved', _n_bres)
                    except Exception as _br_e:
                        log.debug('NBA resolve error: %s', _br_e)
                    last_nba_resolve = now
                # Auto-rebalance: lower exposure cap when pending bets drop below 30% of bankroll
                if now - last_exposure_rebalance >= 3600:
                    try:
                        _br   = float(state.get('bankroll', 200))
                        _pend = sum(b.get('stake', 0) for b in state.get('active_bets', []))
                        _pend_pct = _pend / _br if _br > 0 else 0
                        _cur_exp  = float(state.get('persisted_max_exposure', MAX_TOTAL_EXPOSURE))
                        # If pending < 30% of bankroll AND current cap > 0.35, step down toward 0.35
                        if _pend_pct < 0.30 and _cur_exp > 0.36:
                            _new_exp = round(max(0.35, _cur_exp - 0.05), 2)
                            state['persisted_max_exposure'] = _new_exp
                            MAX_TOTAL_EXPOSURE = _new_exp
                            log.info('AUTO-REBALANCE: exposure %.0f%%->%.0f%% (pending=%.0f%% of BR, BR=$%.2f)',
                                     _cur_exp*100, _new_exp*100, _pend_pct*100, _br)
                        # If exposure < 0.35 and we have very little pending, lock at 0.35
                        elif _cur_exp < 0.34:
                            state['persisted_max_exposure'] = 0.35
                            MAX_TOTAL_EXPOSURE = 0.35
                            log.info('AUTO-REBALANCE: exposure floor enforced at 35%%')
                    except Exception as _rb_e:
                        log.debug('Auto-rebalance error: %s', _rb_e)
                    last_exposure_rebalance = now
                if settled or True:  # always save to persist closing_odds
                    sync_obsidian(state)
                    save_state(state)
                last_check = now

        except KeyboardInterrupt:
            log.info('Shutting down...')
            break
        except Exception as e:
            log.error('Loop error: %s', e, exc_info=True)

        # Sleep in small increments for responsive shutdown
        for _ in range(60):
            time.sleep(1)

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------
def show_status():
    state = load_state()
    wins = state.get('wins', 0)
    losses = state.get('losses', 0)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    print(f'''
ORACULO AUTONOMOUS RUNNER - STATUS
===================================
Bankroll:     ${state["bankroll"]:.2f} USDC
Total P&L:    ${state["total_pnl"]:+.2f}
Today P&L:    ${state["daily_pnl"]:+.2f}
Record:       {wins}W / {losses}L ({wr:.1f}%)
Active bets:  {len(state.get("active_bets", []))}
Settled today:{len(state.get("settled_today", []))}
Staked today: ${state["daily_staked"]:.2f} / ${state["bankroll"] * MAX_DAILY_PCT:.2f}
Last scan:    {state.get("last_scan", "never")}
Last check:   {state.get("last_result_check", "never")}
Loss streak:  {state.get("consecutive_losses", 0)}
''')
    for ab in state.get('active_bets', []):
        print(f'  [{ab.get("sport","?")}] {ab.get("match","?")[:40]} | {ab.get("label","")} @{ab["odds"]:.3f} ${ab["stake"]:.2f}')

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Oraculo Autonomous Runner')
    parser.add_argument('--once', action='store_true', help='Single scan cycle')
    parser.add_argument('--status', action='store_true', help='Show current state')
    parser.add_argument('--results', action='store_true', help='Check/settle bets only')
    parser.add_argument('--dry-run', action='store_true', help='Scan without placing')
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.results:
        state = load_state()
        api = CloudbetAPI()
        check_results(api, state)
        sync_obsidian(state)
        save_state(state)
    elif args.once or args.dry_run:
        run_cycle(dry_run=args.dry_run)
    else:
        run_loop()