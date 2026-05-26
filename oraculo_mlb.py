"""MLB betting model for Oraculo — Pitcher-based Elo + run scoring."""
import os, json, logging, time, requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import exp, log as ln

try:
    from oraculo_mlb_f5 import predict_live as _mlb_f5_predict, UNDER45_THRESHOLD as _U45_THR
    _HAS_F5_MODEL = True
except ImportError:
    _HAS_F5_MODEL = False
    _U45_THR = 0.58

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _safe_era(val, default=4.5):
    """Safely parse ERA: handles "--.--" and None from MLB Stats API."""
    try:
        v = float(val)
        return v if 0.0 < v < 20.0 else default
    except (ValueError, TypeError):
        return default

MLB_CACHE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'mlb_teams.json')
OPS_CACHE  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'team_ops.json')
_LEAGUE_AVG_OPS = 0.718   # MLB average OPS 2024-25
# FIP constant 2024-25 (scales FIP to ERA scale)
_FIP_CONSTANT = 3.10
_FIP_SCALE_LO = 2.0
_FIP_SCALE_HI = 5.5

def _calc_fip(hr, bb, hbp, k, ip):
    if not ip or ip < 1:
        return 4.50
    return round((13 * hr + 3 * (bb + hbp) - 2 * k) / ip + _FIP_CONSTANT, 3)

def _fetch_team_ops(season=None):
    """Fetch team batting OPS from MLB Stats API. Cache 24 h."""
    season = season or datetime.now().year
    try:
        if os.path.exists(OPS_CACHE):
            with open(OPS_CACHE) as f:
                cached = json.load(f)
            if (time.time() - cached.get('ts', 0)) < 86400:
                return cached.get('ops', {})
    except Exception:
        pass
    ops = {}
    try:
        r = requests.get(
            f'https://statsapi.mlb.com/api/v1/teams/stats'
            f'?season={season}&group=hitting&stats=season&sportId=1',
            timeout=15)
        for entry in r.json().get('stats', []):
            for split in entry.get('splits', []):
                tname = split.get('team', {}).get('name', '')
                val   = float(split.get('stat', {}).get('ops', 0) or 0)
                if tname and val > 0:
                    ops[tname] = val
        if ops:
            os.makedirs(os.path.dirname(OPS_CACHE), exist_ok=True)
            with open(OPS_CACHE, 'w') as f:
                json.dump({'ops': ops, 'ts': time.time(), 'season': season}, f, indent=2)
            avg = sum(ops.values()) / len(ops)
            log.info('MLB: OPS loaded for %d teams (league avg=%.3f)', len(ops), avg)
    except Exception as e:
        log.debug('MLB OPS fetch failed: %s', e)
    return ops


MLB_API = 'https://statsapi.mlb.com/api/v1'

# ── Team Elo ─────────────────────────────────────────────────────────────
class MLBElo:
    """Elo rating system for MLB with home field advantage."""

    def __init__(self, k_factor=6, initial=1500, home_adv=24):
        self.ratings = defaultdict(lambda: initial)
        self.k = k_factor
        self.initial = initial
        self.home_adv = home_adv
        self._games = defaultdict(int)

    def process_game(self, winner, loser, winner_is_home=True, run_diff=1):
        """Update Elo after a game. Run differential adjustment."""
        # Margin multiplier: blowouts transfer more Elo
        mov = max(1.0, ln(max(run_diff, 1) + 1) * 0.8)
        mov = min(mov, 2.0)

        r_w = self.ratings[winner]
        r_l = self.ratings[loser]

        if winner_is_home:
            r_w_adj = r_w + self.home_adv
            r_l_adj = r_l
        else:
            r_w_adj = r_w
            r_l_adj = r_l + self.home_adv

        exp_w = 1.0 / (1.0 + 10 ** ((r_l_adj - r_w_adj) / 400.0))
        change = self.k * mov * (1 - exp_w)

        self.ratings[winner] += change
        self.ratings[loser] -= change
        self._games[winner] += 1
        self._games[loser] += 1

    def predict(self, home_team, away_team):
        """Return (home_prob, away_prob)."""
        r_h = self.ratings[home_team] + self.home_adv
        r_a = self.ratings[away_team]
        home_prob = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
        return home_prob, 1.0 - home_prob


# ── Pitcher Rating ───────────────────────────────────────────────────────
class PitcherRating:
    """Simple pitcher quality score from ERA, WHIP, K/9."""

    def __init__(self):
        self.pitchers = {}  # pitcher_id -> {era, whip, k9, innings, games}

    def load_from_api(self, team_ids=None):
        """Load pitcher stats from MLB Stats API."""
        try:
            # Get all teams
            r = requests.get(f'{MLB_API}/teams?sportId=1', timeout=10)
            teams = r.json().get('teams', [])

            for team in teams:
                tid = team.get('id')
                tname = team.get('name', '')
                if team_ids and tid not in team_ids:
                    continue
                # Get roster with pitcher stats
                try:
                    r2 = requests.get(
                        f'{MLB_API}/teams/{tid}/roster?rosterType=active',
                        timeout=10)
                    roster = r2.json().get('roster', [])
                    for player in roster:
                        pos = player.get('position', {}).get('abbreviation', '')
                        if pos not in ('P', 'SP', 'RP'):
                            continue
                        pid = player.get('person', {}).get('id')
                        pname = player.get('person', {}).get('fullName', '')
                        if pid:
                            self.pitchers[pid] = {
                                'name': pname, 'team': tname, 'team_id': tid
                            }
                except Exception:
                    continue

            # Now get season stats for each pitcher
            season = datetime.now().year
            for pid in list(self.pitchers.keys()):
                try:
                    r3 = requests.get(
                        f'{MLB_API}/people/{pid}/stats?stats=season&season={season}&group=pitching',
                        timeout=8)
                    stats = r3.json().get('stats', [])
                    if stats:
                        splits = stats[0].get('splits', [])
                        if splits:
                            s = splits[0].get('stat', {})
                            _ip  = float(s.get('inningsPitched', 0) or 0)
                            _hr  = int(s.get('homeRuns', 0) or 0)
                            _bb  = int(s.get('baseOnBalls', 0) or 0)
                            _hbp = int(s.get('hitBatsmen', 0) or 0)
                            _k   = int(s.get('strikeOuts', 0) or 0)
                            _fip = _calc_fip(_hr, _bb, _hbp, _k, _ip)
                            self.pitchers[pid].update({
                                'era':    float(s.get('era', 99) or 99),
                                'whip':   float(s.get('whip', 9) or 9),
                                'k9':     float(s.get('strikeoutsPer9Inn', 0) or 0),
                                'innings': _ip,
                                'games':  int(s.get('gamesPlayed', 0) or 0),
                                'wins':   int(s.get('wins', 0) or 0),
                                'losses': int(s.get('losses', 0) or 0),
                                'hr': _hr, 'bb': _bb, 'hbp': _hbp, 'k': _k,
                                'fip': _fip,
                            })
                    time.sleep(0.1)  # Rate limit
                except Exception:
                    continue

            log.info('MLB: loaded %d pitcher stats', len(self.pitchers))
        except Exception as e:
            log.warning('MLB pitcher load failed: %s', e)

    def get_quality(self, pitcher_id):
        """Return quality score 0-1 (higher = better pitcher)."""
        p = self.pitchers.get(pitcher_id, {})
        era = p.get('era', 4.50)
        whip = p.get('whip', 1.30)
        k9 = p.get('k9', 7.0)
        innings = p.get('innings', 0)

        if innings < 5:
            return 0.5  # Unknown pitcher = average

        # Use FIP when available (better than ERA — removes defense noise)
        fip = p.get('fip', era)  # fallback to ERA if FIP not computed

        # FIP: 2.0=elite, 5.5=bad  (60% weight — primary metric)
        fip_score  = max(0, min(1, (_FIP_SCALE_HI - fip) / (_FIP_SCALE_HI - _FIP_SCALE_LO)))
        # WHIP: 0.9=excellent, 1.5=bad  (20% weight)
        whip_score = max(0, min(1, (1.5 - whip) / 0.6))
        # K/9: 12=excellent, 5=bad  (20% weight)
        k9_score   = max(0, min(1, (k9 - 5.0) / 7.0))

        return fip_score * 0.60 + whip_score * 0.20 + k9_score * 0.20


# ── Train from results ───────────────────────────────────────────────────
def train_mlb_elo(days_back=60):
    """Train MLB Elo from recent game results."""
    elo = MLBElo()
    try:
        end = datetime.now()
        start = end - timedelta(days=days_back)

        # Fetch results in chunks of 7 days
        current = start
        total_games = 0
        while current < end:
            chunk_end = min(current + timedelta(days=7), end)
            date_str = current.strftime('%Y-%m-%d')
            end_str = chunk_end.strftime('%Y-%m-%d')

            r = requests.get(
                f'{MLB_API}/schedule?sportId=1&startDate={date_str}&endDate={end_str}'
                '&gameType=R&fields=dates,games,teams,home,away,team,name,isWinner,score',
                timeout=15)

            for date_entry in r.json().get('dates', []):
                for game in date_entry.get('games', []):
                    home = game.get('teams', {}).get('home', {})
                    away = game.get('teams', {}).get('away', {})

                    home_name = home.get('team', {}).get('name', '')
                    away_name = away.get('team', {}).get('name', '')
                    home_score = home.get('score', 0) or 0
                    away_score = away.get('score', 0) or 0

                    if not home_name or not away_name or (home_score == 0 and away_score == 0):
                        continue

                    if home_score > away_score:
                        elo.process_game(home_name, away_name, True, home_score - away_score)
                    elif away_score > home_score:
                        elo.process_game(away_name, home_name, False, away_score - home_score)
                    total_games += 1

            current = chunk_end + timedelta(days=1)
            time.sleep(0.2)

        log.info('MLB Elo trained: %d teams from %d games', len(elo.ratings), total_games)

        # Load pitcher FIP stats and attach to scan_mlb
        try:
            _pr = PitcherRating()
            _pr.load_from_api()
            scan_mlb._pitcher_rating = _pr
            fip_count = sum(1 for p in _pr.pitchers.values() if p.get('fip'))
            log.info('MLB: %d pitchers loaded (%d with FIP)', len(_pr.pitchers), fip_count)
        except Exception as _ep:
            log.debug('MLB pitcher load skipped: %s', _ep)

        # Load team OPS and attach to scan_mlb
        try:
            _ops = _fetch_team_ops()
            scan_mlb._team_ops = _ops
            log.info('MLB: %d team OPS values loaded', len(_ops))
        except Exception as _eo:
            log.debug('MLB OPS load skipped: %s', _eo)

        # Cache
        os.makedirs(os.path.dirname(MLB_CACHE), exist_ok=True)
        with open(MLB_CACHE, 'w') as f:
            json.dump({
                'ratings': dict(elo.ratings),
                'games': dict(elo._games),
                'updated': datetime.now().isoformat(),
            }, f, indent=2)

        return elo
    except Exception as e:
        log.warning('MLB Elo training failed: %s', e)
        # Try loading from cache
        try:
            with open(MLB_CACHE) as f:
                data = json.load(f)
            for team, rating in data.get('ratings', {}).items():
                elo.ratings[team] = rating
            log.info('MLB Elo loaded from cache: %d teams', len(elo.ratings))
            return elo
        except Exception:
            return None


# ── Scanner ──────────────────────────────────────────────────────────────
def scan_mlb(api, state, elo=None, dry_run=False, min_edge=0.06, min_conf=0.50):
    """Scan MLB markets for value bets. Returns list of picks."""
    log.info('=== SCANNING MLB MARKETS ===')
    picks = []

    if not elo:
        elo = train_mlb_elo()
    if not elo or len(elo.ratings) < 20:
        log.warning('MLB: insufficient Elo data')
        return picks

    # Get today's schedule with probable pitchers
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        r = requests.get(
            f'{MLB_API}/schedule?sportId=1&date={today}'
            '&hydrate=probablePitcher,team',
            timeout=15)
        mlb_games = []
        for date_entry in r.json().get('dates', []):
            mlb_games.extend(date_entry.get('games', []))
    except Exception as e:
        log.warning('MLB schedule fetch failed: %s', e)
        return picks

    log.info('  MLB: %d games today', len(mlb_games))

    # Get Cloudbet MLB odds
    try:
        cb_events = api.get_odds('baseball-usa-mlb')
    except Exception:
        cb_events = []

    if not cb_events:
        log.info('  MLB: no Cloudbet events')
        return picks

    # Ensure OPS data available (fallback if train_mlb_elo used cache path)
    if not getattr(scan_mlb, '_team_ops', None):
        scan_mlb._team_ops = _fetch_team_ops()
        log.info('MLB: OPS fallback loaded %d teams', len(scan_mlb._team_ops))

    now_utc = datetime.now(timezone.utc)

    # Match MLB API games to Cloudbet events
    for ev in cb_events:
        ev_name = ev.get('name', '')
        if ' v ' not in ev_name:
            continue

        eid = str(ev.get('id', ''))
        markets = ev.get('markets', {})
        cutoff = ev.get('cutoffTime', '')

        # Skip events already past cutoff (game started or closed)
        if cutoff:
            try:
                ct = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
                if ct < now_utc:
                    log.debug('  MLB: skip past-cutoff event %s (%s)', ev_name, cutoff)
                    continue
            except Exception:
                pass

        # Parse home/away from Cloudbet name (format: "HOME v AWAY")
        parts = ev_name.split(' v ')
        if len(parts) != 2:
            continue
        cb_home = parts[0].strip()
        cb_away = parts[1].strip()

        # Match to Elo team names
        home_elo = _match_team(cb_home, elo.ratings)
        away_elo = _match_team(cb_away, elo.ratings)

        if not home_elo or not away_elo:
            continue

        # Get Elo prediction
        home_prob, away_prob = elo.predict(home_elo, away_elo)

        # Find matching MLB API game for pitcher data
        pitcher_adj = 0.0
        hp, ap = {}, {}  # default: no pitcher data
        hp_pid = ap_pid = None
        hp_fip = ap_fip = 4.50  # default: league-avg FIP if no game matched
        hp_name = ap_name = '?'
        _game_time = ''
        for mlb_game in mlb_games:
            mlb_home = mlb_game.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
            mlb_away = mlb_game.get('teams', {}).get('away', {}).get('team', {}).get('name', '')
            if mlb_home == home_elo and mlb_away == away_elo:
                _game_time = mlb_game.get('gameDate', '')
                hp = mlb_game.get('teams', {}).get('home', {}).get('probablePitcher', {})
                ap = mlb_game.get('teams', {}).get('away', {}).get('probablePitcher', {})
                hp_era = _safe_era(hp.get('era'))
                ap_era = _safe_era(ap.get('era'))
                # FIP lookup from PitcherRating (more accurate than ERA from schedule)
                hp_pid = hp.get('id')
                ap_pid = ap.get('id')
                if hp_pid and ap_pid and hasattr(scan_mlb, '_pitcher_rating'):
                    _pr = scan_mlb._pitcher_rating
                    hp_fip = _pr.pitchers.get(hp_pid, {}).get('fip', hp_era)
                    ap_fip = _pr.pitchers.get(ap_pid, {}).get('fip', ap_era)
                    hp_name = _pr.pitchers.get(hp_pid, {}).get('name', hp.get('fullName', '?'))
                    ap_name = _pr.pitchers.get(ap_pid, {}).get('name', ap.get('fullName', '?'))
                else:
                    hp_fip, ap_fip = hp_era, ap_era
                    hp_name = hp.get('fullName', '?')
                    ap_name = ap.get('fullName', '?')
                # FIP diff: research shows ~4% win prob per 1.0 FIP unit
                fip_diff = ap_fip - hp_fip  # positive = away pitcher worse
                pitcher_adj = fip_diff * 0.04
                pitcher_adj = max(-0.08, min(0.08, pitcher_adj))  # cap at +/-8%
                log.debug('  MLB pitchers: %s FIP=%.2f vs %s FIP=%.2f adj=%+.1f%%',
                          hp_name, hp_fip, ap_name, ap_fip, pitcher_adj * 100)
                break

        # Adjust probabilities with pitcher data
        home_prob = min(0.95, max(0.05, home_prob + pitcher_adj))
        away_prob = 1.0 - home_prob

        # Park factor: affects expected runs, used for totals markets
        park_factor = _PARK_FACTORS.get(home_elo, 1.0)

        # ── Market: moneyline_innings_1_to_5 (best for pitcher-dependent) ──
        ml5 = markets.get('baseball.moneyline_innings_1_to_5', {})
        for sv in ml5.get('submarkets', {}).values():
            for sel in sv.get('selections', []):
                outcome = sel.get('outcome', '')
                murl = sel.get('marketUrl', '')
                price = float(sel.get('price', 0) or 0)
                if price < 1.05:
                    continue

                if outcome == 'home':
                    prob = home_prob * 0.95  # F5 is slightly less certain
                    team = cb_home
                elif outcome == 'away':
                    prob = away_prob * 0.95
                    team = cb_away
                else:
                    continue  # Skip draw

                implied = 1.0 / price
                edge = prob - implied

                if edge > min_edge and prob > min_conf and edge < 0.40:
                    picks.append({
                        'match': f'{cb_home} vs {cb_away}',
                        'league': 'MLB',
                        'event_id': eid,
                        'market_url': murl,
                        'price': price,
                        'label': f'F5 ML: {team} (FIP {hp_fip:.2f}/{ap_fip:.2f})',
                        'model_prob': prob,
                        'edge': edge,
                        'sport': 'baseball',
                        'market_type': 'mlb_f5_ml',
                    })

        # ── Market: totals_innings_1_to_5 (ML-calibrated model) ──
        tot5 = markets.get('baseball.totals_innings_1_to_5', {})

        # Compute shared signals used by both F5 and full-game totals
        _team_ops   = getattr(scan_mlb, '_team_ops', {})
        home_ops    = _team_ops.get(home_elo, _LEAGUE_AVG_OPS)
        away_ops    = _team_ops.get(away_elo, _LEAGUE_AVG_OPS)
        ops_combined = (home_ops + away_ops) / 2 - _LEAGUE_AVG_OPS
        avg_fip = (hp_fip + ap_fip) / 2
        _weather_adj, _w_info = get_mlb_weather_adj(home_elo, _game_time)
        log.debug('  MLB weather: %s %s', home_elo[:15], _w_info)

        for sv in tot5.get('submarkets', {}).values():
            for sel in sv.get('selections', []):
                outcome = sel.get('outcome', '')
                murl = sel.get('marketUrl', '')
                price = float(sel.get('price', 0) or 0)
                if price < 1.20 or outcome not in ('over', 'under'):
                    continue

                # Parse line from market URL
                try:
                    total_str = murl.split('total=')[-1] if 'total=' in murl else ''
                    line = float(total_str)
                except Exception:
                    continue

                # ML model prediction (calibrated LogisticRegression per line)
                prob = None
                if _HAS_F5_MODEL:
                    raw = _mlb_f5_predict(home_elo, away_elo, hp_pid, ap_pid, line, _weather_adj)
                    if raw is not None and isinstance(raw, dict):
                        prob = raw['prob_under'] if outcome == 'under' else raw['prob_over']

                if prob is None:
                    # Formula fallback when model unavailable or no pitcher data
                    park_factor_loc = _PARK_FACTORS.get(home_elo, 1.0)
                    if outcome == 'over':
                        fip_sig = (avg_fip - 4.6 / park_factor_loc) * 0.06
                        ops_sig = ops_combined * 0.80
                        combined = fip_sig + ops_sig + _weather_adj
                        if combined <= 0:
                            continue
                        prob = min(0.80, 0.55 + combined)
                    else:
                        fip_sig = (3.6 / park_factor_loc - avg_fip) * 0.06
                        ops_sig = -ops_combined * 0.80
                        combined = fip_sig + ops_sig - _weather_adj
                        if combined <= 0:
                            continue
                        prob = min(0.80, 0.55 + combined)

                # Under 4.5 has structural book mispricing -- lower confidence threshold
                is_under45 = (outcome == 'under' and abs(line - 4.5) < 0.01)
                conf_thr = _U45_THR if is_under45 else 0.63

                implied = 1.0 / price
                edge    = prob - implied

                # Block F5 Over dome: Sibila WR=0% on O4-O5.5 in domes (outdoor 100% WR)
                if outcome == 'over' and _w_info == 'dome':
                    log.debug('  MLB F5 Over dome blocked: model unreliable in closed stadiums')
                    continue
                if edge > min_edge and prob >= conf_thr and edge < 0.35:
                    picks.append({
                        'match':       f'{cb_home} vs {cb_away}',
                        'league':      'MLB',
                        'event_id':    eid,
                        'market_url':  murl,
                        'price':       price,
                        'label':       f'F5 {outcome.title()} {total_str} [{_w_info[:12]}]',
                        'model_prob':  round(prob, 4),
                        'edge':        round(edge, 4),
                        'sport':       'baseball',
                        'market_type': 'mlb_f5_total',
                    })
                    log.debug('  MLB F5 %s %.1f: prob=%.0f%% edge=%.1f%% thr=%.0f%% @%.2f%s',
                              outcome, line, prob*100, edge*100, conf_thr*100, price,
                              ' [U4.5]' if is_under45 else '')

    # ── Market: baseball.totals (full 9-inning game total) ──────────────────
    # Same FIP+OPS+weather model but scaled for full game.
    # Starters exit after ~5-6 innings; bullpen adds variance.
    # Full game thresholds are higher (more runs expected).
    tot9 = markets.get('baseball.totals', {})
    if tot9:
        # Bullpen quality proxy: if avg_fip > 4.0 starters likely exit early → more bullpen
        bullpen_var = max(0.0, (avg_fip - 4.0) * 0.015)   # positive = more variance → slight over signal
        over_thresh9  = (8.8  / park_factor) - ops_combined * 1.5  # runs where over starts being likely
        under_thresh9 = (7.8  / park_factor) + ops_combined * 1.5

        for sv in tot9.get('submarkets', {}).values():
            for sel in sv.get('selections', []):
                outcome = sel.get('outcome', '')
                murl    = sel.get('marketUrl', '')
                price   = float(sel.get('price', 0) or 0)
                if price < 1.20 or not murl or outcome not in ('over', 'under'):
                    continue
                try:
                    total_str = murl.split('total=')[-1] if 'total=' in murl else ''
                    line9 = float(total_str)
                except Exception:
                    continue

                if outcome == 'over':
                    fip_sig9 = (avg_fip  - 4.2) * 0.04 + bullpen_var
                    ops_sig9 = ops_combined * 0.70
                    combined9 = fip_sig9 + ops_sig9 + _weather_adj
                    if combined9 <= 0:
                        continue
                    prob9 = min(0.78, 0.54 + combined9)
                elif outcome == 'under':
                    fip_sig9 = (3.8 - avg_fip) * 0.04
                    ops_sig9 = -ops_combined * 0.70
                    combined9 = fip_sig9 + ops_sig9 - _weather_adj
                    if combined9 <= 0:
                        continue
                    prob9 = min(0.78, 0.54 + combined9)

                implied9 = 1.0 / price
                edge9    = prob9 - implied9

                if edge9 > min_edge and edge9 < 0.35:
                    picks.append({
                        'match':       f'{cb_home} vs {cb_away}',
                        'league':      'MLB',
                        'event_id':    eid,
                        'market_url':  murl,
                        'price':       price,
                        'label':       f'Total {outcome.title()} {total_str} [{_w_info[:10]}]',
                        'model_prob':  round(prob9, 4),
                        'edge':        round(edge9, 4),
                        'sport':       'baseball',
                        'market_type': 'mlb_full_total',
                        '_home_ops':   round(home_ops, 3),
                        '_away_ops':   round(away_ops, 3),
                    })

    picks.sort(key=lambda p: p['edge'], reverse=True)
    log.info('MLB: %d value picks found', len(picks))
    for p in picks[:5]:
        log.info('  [MLB] %s | %s | edge=%.1f%% prob=%.0f%% @%.2f',
                 p['match'][:30], p['label'], p['edge']*100, p['model_prob']*100, p['price'])

    return picks


# ── Helpers ──────────────────────────────────────────────────────────────
# Mapping of Cloudbet short names to MLB API full names
_TEAM_MAP = {
    'ARI': 'Arizona Diamondbacks', 'ATL': 'Atlanta Braves',
    'BAL': 'Baltimore Orioles', 'BOS': 'Boston Red Sox',
    'CHC': 'Chicago Cubs', 'CWS': 'Chicago White Sox', 'CHW': 'Chicago White Sox',
    'CIN': 'Cincinnati Reds', 'CLE': 'Cleveland Guardians',
    'COL': 'Colorado Rockies', 'DET': 'Detroit Tigers',
    'HOU': 'Houston Astros', 'KC': 'Kansas City Royals',
    'LAA': 'Los Angeles Angels', 'LAD': 'Los Angeles Dodgers',
    'MIA': 'Miami Marlins', 'MIL': 'Milwaukee Brewers',
    'MIN': 'Minnesota Twins', 'NYM': 'New York Mets',
    'NYY': 'New York Yankees', 'OAK': 'Oakland Athletics',
    'PHI': 'Philadelphia Phillies', 'PIT': 'Pittsburgh Pirates',
    'SD': 'San Diego Padres', 'SF': 'San Francisco Giants',
    'SEA': 'Seattle Mariners', 'STL': 'St. Louis Cardinals',
    'TB': 'Tampa Bay Rays', 'TEX': 'Texas Rangers',
    'TOR': 'Toronto Blue Jays', 'WAS': 'Washington Nationals',
}

_NICKNAME_MAP = {
    'diamondbacks': 'Arizona Diamondbacks', 'braves': 'Atlanta Braves',
    'orioles': 'Baltimore Orioles', 'red sox': 'Boston Red Sox',
    'cubs': 'Chicago Cubs', 'white sox': 'Chicago White Sox',
    'reds': 'Cincinnati Reds', 'guardians': 'Cleveland Guardians',
    'rockies': 'Colorado Rockies', 'tigers': 'Detroit Tigers',
    'astros': 'Houston Astros', 'royals': 'Kansas City Royals',
    'angels': 'Los Angeles Angels', 'dodgers': 'Los Angeles Dodgers',
    'marlins': 'Miami Marlins', 'brewers': 'Milwaukee Brewers',
    'twins': 'Minnesota Twins', 'mets': 'New York Mets',
    'yankees': 'New York Yankees', 'athletics': 'Oakland Athletics',
    'phillies': 'Philadelphia Phillies', 'pirates': 'Pittsburgh Pirates',
    'padres': 'San Diego Padres', 'giants': 'San Francisco Giants',
    'mariners': 'Seattle Mariners', 'cardinals': 'St. Louis Cardinals',
    'rays': 'Tampa Bay Rays', 'rangers': 'Texas Rangers',
    'blue jays': 'Toronto Blue Jays', 'nationals': 'Washington Nationals',
}


# ── Stadium weather data ────────────────────────────────────────────────────
# lat, lon, cf_bearing = direction from home plate toward center field (degrees)
# Wind FROM that direction = headwind (fewer runs); blowing TOWARD = tailwind (more runs)
_STADIUMS = {
    'Arizona Diamondbacks':    (33.445, -112.067, 315),  # Chase Field (dome, irrelevant but included)
    'Atlanta Braves':          (33.891, -84.468,  25),
    'Baltimore Orioles':       (39.284, -76.622, 330),
    'Boston Red Sox':          (42.347, -71.097, 225),  # Fenway: CF is SW
    'Chicago Cubs':            (41.948, -87.655, 270),  # Wrigley: CF is W, wind from lake (E) = out
    'Chicago White Sox':       (41.830, -87.634, 315),
    'Cincinnati Reds':         (39.097, -84.507, 315),
    'Cleveland Guardians':     (41.496, -81.685,  45),
    'Colorado Rockies':        (39.756, -104.994, 285), # Coors: thin air, wind from W often out
    'Detroit Tigers':          (42.339, -83.049, 330),
    'Houston Astros':          (29.757, -95.355,   0),  # Dome
    'Kansas City Royals':      (39.051, -94.480, 315),
    'Los Angeles Angels':      (33.800, -117.883, 315),
    'Los Angeles Dodgers':     (34.074, -118.240, 330),
    'Miami Marlins':           (25.778, -80.220,   0),  # Retractable roof
    'Milwaukee Brewers':       (43.028, -87.971,  25),
    'Minnesota Twins':         (44.981, -93.278,   0),  # Dome
    'New York Mets':           (40.757, -73.846, 315),
    'New York Yankees':        (40.829, -73.926,  15),
    'Oakland Athletics':       (37.752, -122.201,  90), # moved but keep for schedule compat
    'Philadelphia Phillies':   (39.906, -75.166, 330),
    'Pittsburgh Pirates':      (40.447, -80.006,  20),
    'San Diego Padres':        (32.707, -117.157, 300),
    'San Francisco Giants':    (37.779, -122.389, 270), # Oracle: heavy W wind = in from bay
    'Seattle Mariners':        (47.591, -122.333, 320),
    'St. Louis Cardinals':     (38.623, -90.193,  10),
    'Tampa Bay Rays':          (27.768, -82.653,   0),  # Dome
    'Texas Rangers':           (32.751, -97.083, 315),  # Globe Life: retractable roof
    'Toronto Blue Jays':       (43.641, -79.389,   0),  # Dome
    'Washington Nationals':    (38.873, -77.007, 355),
    'Sacramento River Cats':   (38.580, -121.499, 315), # A's temp home
}

_DOME_TEAMS = {
    'Arizona Diamondbacks', 'Houston Astros', 'Miami Marlins',
    'Minnesota Twins', 'Tampa Bay Rays', 'Toronto Blue Jays', 'Texas Rangers',
}

import functools, math as _math, time as _time, urllib.request as _urllib_req

@functools.lru_cache(maxsize=64)
def _fetch_stadium_weather(lat, lon, game_hour_utc, cache_key=''):
    """Fetch hourly wind+temp from Open-Meteo for a stadium. Cached per hour."""
    url = (f'https://api.open-meteo.com/v1/forecast'
           f'?latitude={lat}&longitude={lon}'
           f'&hourly=windspeed_10m,winddirection_10m,temperature_2m'
           f'&forecast_days=2&timezone=UTC')
    try:
        with _urllib_req.urlopen(url, timeout=5) as r:
            data = __import__('json').loads(r.read())
        times   = data['hourly']['time']
        speeds  = data['hourly']['windspeed_10m']
        dirs    = data['hourly']['winddirection_10m']
        temps   = data['hourly']['temperature_2m']
        # Find closest hour to game_hour_utc (format YYYY-MM-DDTHH:00)
        for i, t in enumerate(times):
            if t[:13] == game_hour_utc[:13]:
                return {'wind_kph': speeds[i], 'wind_dir': dirs[i], 'temp_c': temps[i]}
        # fallback: first entry
        return {'wind_kph': speeds[0], 'wind_dir': dirs[0], 'temp_c': temps[0]}
    except Exception:
        return None


def get_mlb_weather_adj(home_team, game_time_utc):
    """
    Returns (weather_adj, info_str) for totals model.
    weather_adj > 0 → favors Over (warm + tailwind)
    weather_adj < 0 → favors Under (cold + headwind)
    Max magnitude: ±0.08
    """
    if home_team in _DOME_TEAMS:
        return 0.0, 'dome'

    stadium = _STADIUMS.get(home_team)
    if not stadium:
        return 0.0, 'no_stadium'

    lat, lon, cf_bearing = stadium

    # Cache key: team + hour
    hour_key = (game_time_utc or '')[:13]
    w = _fetch_stadium_weather(lat, lon, hour_key, cache_key=home_team)
    if not w:
        return 0.0, 'api_err'

    wind_kph  = w['wind_kph']
    wind_dir  = w['wind_dir']   # meteorological: direction wind is coming FROM
    temp_c    = w['temp_c']

    wind_mph  = wind_kph * 0.621371

    # Wind component toward CF: wind coming FROM opposite of cf_bearing = tailwind
    # cf_bearing = direction from HP to CF (wind blowing TOWARD that direction = out)
    # Wind FROM (cf_bearing + 180) = tailwind out
    wind_toward_cf = wind_mph * _math.cos(_math.radians(wind_dir - (cf_bearing + 180)))
    # positive = wind blowing out toward CF (more runs)
    # negative = wind blowing in from CF (fewer runs)

    wind_adj = wind_toward_cf * 0.008   # ~0.08 adj per 10 mph tailwind
    wind_adj = max(-0.06, min(0.06, wind_adj))

    # Temperature: 72°F (22°C) is baseline. Each 10°F colder ≈ -0.3 runs
    temp_adj = (temp_c - 22) * 0.005   # ±~0.05 for ±10°C
    temp_adj = max(-0.05, min(0.03, temp_adj))  # cold matters more than heat

    total_adj = wind_adj + temp_adj
    total_adj = max(-0.08, min(0.08, total_adj))

    info = f'{wind_mph:.0f}mph@{wind_dir:.0f}° {temp_c:.0f}°C adj={total_adj:+.2f}'
    return round(total_adj, 4), info

# Park run factors (2024-25 avg). >1.0 = hitter park, <1.0 = pitcher park
# Applied to adjust expected run scoring for totals markets
_PARK_FACTORS = {
    'Colorado Rockies': 1.38,
    'Cincinnati Reds': 1.12,
    'Boston Red Sox': 1.10,
    'Philadelphia Phillies': 1.09,
    'Texas Rangers': 1.08,
    'Chicago Cubs': 1.07,
    'Atlanta Braves': 1.06,
    'Milwaukee Brewers': 1.05,
    'Toronto Blue Jays': 1.04,
    'Minnesota Twins': 1.04,
    'Houston Astros': 1.03,
    'Baltimore Orioles': 1.02,
    'New York Yankees': 1.01,
    'Kansas City Royals': 1.01,
    'Washington Nationals': 1.00,
    'Chicago White Sox': 1.00,
    'Detroit Tigers': 0.99,
    'Arizona Diamondbacks': 0.98,
    'Los Angeles Angels': 0.98,
    'Cleveland Guardians': 0.97,
    'Pittsburgh Pirates': 0.97,
    'Tampa Bay Rays': 0.97,
    'St. Louis Cardinals': 0.96,
    'New York Mets': 0.96,
    'Seattle Mariners': 0.95,
    'Los Angeles Dodgers': 0.95,
    'Miami Marlins': 0.94,
    'San Diego Padres': 0.93,
    'San Francisco Giants': 0.92,
    'Oakland Athletics': 0.91,
}


def _match_team(cb_name, elo_ratings):
    """Match Cloudbet team name to Elo team name. Uses exact word matching to avoid false positives."""
    cb_words = cb_name.lower().split()
    cb_lower = cb_name.lower()

    # 1. Try exact abbreviation match against first word
    first_word = cb_words[0].upper() if cb_words else ''
    if first_word in _TEAM_MAP:
        full = _TEAM_MAP[first_word]
        if full in elo_ratings:
            return full

    # 2. Try nickname match (two-word then single)
    if len(cb_words) >= 2:
        two_word = ' '.join(cb_words[-2:])
        if two_word in _NICKNAME_MAP:
            full = _NICKNAME_MAP[two_word]
            if full in elo_ratings:
                return full
    last_word = cb_words[-1] if cb_words else ''
    if last_word in _NICKNAME_MAP:
        full = _NICKNAME_MAP[last_word]
        if full in elo_ratings:
            return full

    # 3. Whole-word overlap (distinctive words only, len>4)
    # Collect all candidates; return None if ambiguous to avoid wrong team
    cb_word_set = set(cb_words)
    candidates = []
    for team in elo_ratings:
        team_words = set(team.lower().split())
        overlap = {w for w in team_words & cb_word_set if len(w) > 4}
        if overlap:
            candidates.append(team)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        log.debug('MLB: ambiguous match for %r -> %s (skipping)', cb_name, candidates)

    return None
