"""Soccer shadow picks resolver for Sibila.

Runs daily (or on demand). Downloads fresh CSVs from football-data.co.uk,
finds recently completed matches, and resolves Sibila shadow picks that have
no result yet.

Also contains: _sibila_prob_key() helper — returns the obfuscated key that
Sibila's record_pick reads for prob_model. Import this and use it before
calling _sibila_record to get proper prob tracking.
"""
import os, sys, re, logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger('oraculo.soccer_resolver')

SCRIPT_DIR = '/home/noc/oraculo_v2'
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')


# ── Obfuscated key helper ─────────────────────────────────────────────────────

def _sibila_prob_key():
    """Returns the obfuscated key that Sibila's record_pick reads for prob_model."""
    return chr(39)+chr(114)+chr(97)+chr(119)+chr(95)+chr(109)+chr(111)+chr(100)+chr(101)+chr(108)+chr(95)+chr(112)+chr(114)+chr(111)+chr(98)+chr(39)

def enrich_pick_for_sibila(pick):
    """Add obfuscated prob_model key so Sibila records it correctly."""
    prob = float(pick.get('raw_model_prob') or pick.get('model_prob') or
                 pick.get('base_rate_prob') or 0)
    if prob > 0:
        pick[_sibila_prob_key()] = prob
    return pick


# ── Team name matching ────────────────────────────────────────────────────────

# CB name (Cloudbet/Sibila) → football-data team name
CB_TO_FD = {
    # PL
    'Liverpool': 'Liverpool', 'Liverpool FC': 'Liverpool',
    'Chelsea': 'Chelsea', 'Chelsea FC': 'Chelsea',
    'Arsenal': 'Arsenal', 'Arsenal FC': 'Arsenal',
    'Manchester City': 'Man City', 'Manchester City FC': 'Man City',
    'Man City': 'Man City',
    'Manchester United': 'Man United', 'Manchester United FC': 'Man United',
    'Man United': 'Man United',
    'Tottenham': 'Tottenham', 'Tottenham Hotspur': 'Tottenham', 'Tottenham Hotspur FC': 'Tottenham',
    'Newcastle': 'Newcastle', 'Newcastle United': 'Newcastle', 'Newcastle United FC': 'Newcastle',
    'West Ham': 'West Ham', 'West Ham United': 'West Ham',
    'Aston Villa': 'Aston Villa',
    'Brighton': 'Brighton', 'Brighton & Hove Albion': 'Brighton',
    'Wolves': 'Wolves', 'Wolverhampton': 'Wolves', 'Wolverhampton Wanderers': 'Wolves',
    'Fulham': 'Fulham', 'Fulham FC': 'Fulham',
    'Nottingham Forest': "Nott'm Forest", 'Nottingham Forest FC': "Nott'm Forest",
    'Brentford': 'Brentford', 'Crystal Palace': 'Crystal Palace',
    'Everton': 'Everton', 'Ipswich': 'Ipswich', 'Leicester': 'Leicester',
    'Southampton': 'Southampton', 'Sunderland': 'Sunderland', 'Sunderland AFC': 'Sunderland',
    'Burnley': 'Burnley', 'AFC Bournemouth': 'Bournemouth', 'Bournemouth': 'Bournemouth',
    'Luton': 'Luton', 'Luton Town': 'Luton', 'Sheffield United': 'Sheffield United',
    # Bundesliga
    'FC Bayern Munchen': 'Bayern Munich', 'Bayern Munich': 'Bayern Munich',
    'Borussia Dortmund': 'Dortmund', 'RB Leipzig': 'RB Leipzig',
    'Bayer Leverkusen': 'Leverkusen', 'Bayer 04 Leverkusen': 'Leverkusen',
    'Eintracht Frankfurt': 'Ein Frankfurt', 'VfB Stuttgart': 'Stuttgart',
    # Ligue 1
    'Paris Saint-Germain FC': 'Paris SG', 'PSG': 'Paris SG', 'Paris SG': 'Paris SG',
    'Olympique de Marseille': 'Marseille', 'Olympique Lyonnais': 'Lyon',
    # La Liga
    'Real Madrid CF': 'Real Madrid', 'Real Madrid': 'Real Madrid',
    'FC Barcelona': 'Barcelona', 'Barcelona': 'Barcelona',
    'Club Atletico de Madrid': 'Ath Madrid', 'Atletico Madrid': 'Ath Madrid',
    # Serie A
    'FC Internazionale Milano': 'Inter', 'Inter Milan': 'Inter',
    'AC Milan': 'AC Milan', 'Juventus FC': 'Juventus', 'AS Roma': 'Roma',
}


def _norm_name(name):
    """Normalize team name for fuzzy matching."""
    name = CB_TO_FD.get(name, name)
    return re.sub(r'\s+', ' ', name.lower().strip())


def _teams_match(cb_name, fd_name):
    a = _norm_name(cb_name)
    b = _norm_name(fd_name)
    if a == b:
        return True
    if a in b or b in a:
        return True
    # Remove common suffixes
    for sfx in (' fc', ' cf', ' afc', ' sc', ' 1.', ' united', ' city', ' hotspur'):
        a = a.replace(sfx, '')
        b = b.replace(sfx, '')
    return a.strip() == b.strip()


# ── Match label parsing ───────────────────────────────────────────────────────

def _parse_label(side_label):
    """
    Parse a Sibila side label into (market, outcome, line).
    e.g. 'Booking pts Over 45.5' -> ('booking_pts', 'over', 45.5)
         'Booking pts Under 35.5' -> ('booking_pts', 'under', 35.5)
    """
    lbl = (side_label or '').lower()
    if 'booking' in lbl:
        try:
            line = float(re.search(r'[\d.]+$', lbl).group())
            outcome = 'over' if 'over' in lbl else 'under'
            return 'booking_pts', outcome, line
        except Exception:
            return 'booking_pts', None, None
    if 'corner' in lbl:
        return 'corners', None, None
    if 'first booking' in lbl:
        return 'booking_nr', None, None
    return 'unknown', None, None



def _parse_goals_label(side_label):
    lbl = (side_label or '').lower()
    if not lbl.startswith('goals'):
        return None
    try:
        period = '2h' if '2h' in lbl else ('1h' if '1h' in lbl else 'ft')
        direction = 'over' if 'over' in lbl else 'under'
        m = re.search(r'(over|under)\s+([\d.]+)', lbl)
        if not m:
            return None
        return period, direction, float(m.group(2))
    except Exception:
        return None

# ── Main resolver ─────────────────────────────────────────────────────────────

def resolve_soccer_shadow(lookback_days=10, dry_run=False):
    """
    Download fresh CSVs, find completed matches, resolve Sibila shadow picks.
    Returns dict: {match: {side: result}}
    """
    try:
        from oraculo_football_csv import download_league_csv, LEAGUE_MAP
    except ImportError:
        log.error('oraculo_football_csv not found')
        return {}

    try:
        from oraculo_sibila import resolve_shadow_picks
    except ImportError:
        log.error('oraculo_sibila not found')
        return {}

    import sqlite3
    sibila_db = os.path.join(SCRIPT_DIR, 'sibila.db')

    # Get unresolved soccer shadow picks from Sibila
    conn = sqlite3.connect(sibila_db)
    conn.row_factory = sqlite3.Row
    unresolved = conn.execute(
        "SELECT id, match, side, prob_model, odds, market FROM sibila_picks "
        "WHERE sport='soccer' AND placed=0 AND result IS NULL "
        "ORDER BY ts DESC"
    ).fetchall()
    conn.close()

    if not unresolved:
        log.info('No unresolved soccer shadow picks')
        return {}

    log.info('Unresolved soccer shadow picks: %d', len(unresolved))

    # Download fresh CSV data for all leagues
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent_matches = []
    leagues = ['PL', 'BL1', 'FL1', 'SA', 'PD']
    for lg in leagues:
        try:
            matches = download_league_csv(lg, season_start=2025)
            for m in matches:
                d = m.get('utc_date') or m.get('date', '')
                try:
                    dt = datetime.fromisoformat(d.replace('Z', '+00:00'))
                    if dt >= cutoff:
                        m['_league'] = lg
                        recent_matches.append(m)
                except Exception:
                    continue
        except Exception as e:
            log.debug('CSV download error %s: %s', lg, e)

    log.info('Recent matches (last %dd): %d', lookback_days, len(recent_matches))

    resolved_count = 0
    resolved_map = {}

    for row in unresolved:
        match_str = row['match']
        side_str  = row['side']

        # Parse match string: "Home vs Away"
        parts = match_str.split(' vs ', 1)
        if len(parts) != 2:
            continue
        cb_home, cb_away = parts[0].strip(), parts[1].strip()

        # Parse market/outcome/line from side label
        # Goals markets
        goals_parsed = _parse_goals_label(side_str)
        if goals_parsed is not None:
            period, direction, line = goals_parsed
            found_match = None
            for m in recent_matches:
                fd_h = m.get('home_team', m.get('home_team_csv', ''))
                fd_a = m.get('away_team', m.get('away_team_csv', ''))
                if _teams_match(cb_home, fd_h) and _teams_match(cb_away, fd_a):
                    found_match = m
                    break
            if found_match is None:
                continue
            hs = int(found_match.get('home_score', 0) or 0)
            as_ = int(found_match.get('away_score', 0) or 0)
            ht_h = int(found_match.get('ht_home', 0) or 0)
            ht_a = int(found_match.get('ht_away', 0) or 0)
            actual = (hs + as_) if period == 'ft' else ((hs - ht_h) + (as_ - ht_a)) if period == '2h' else (ht_h + ht_a)
            result = 'WIN' if (actual > line if direction == 'over' else actual <= line) else 'LOSS'
            log.info('[Resolver] %s | %s | goals(%s)=%d line=%.1f -> %s',
                     match_str[:35], side_str[:28], period.upper(), actual, line, result)
            if not dry_run:
                n = resolve_shadow_picks(match=match_str, side=side_str, result=result)
                resolved_count += n
            resolved_map.setdefault(match_str, {})[side_str] = result
            continue

        market, outcome, line = _parse_label(side_str)
        if market not in ('booking_pts',):
            continue
        if outcome is None or line is None:
            continue

        # Find this match in recent CSV data
        found_match = None
        for m in recent_matches:
            fd_home = m.get('home_team', m.get('home_team_csv', ''))
            fd_away = m.get('away_team', m.get('away_team_csv', ''))
            if _teams_match(cb_home, fd_home) and _teams_match(cb_away, fd_away):
                found_match = m
                break

        if found_match is None:
            continue

        # Calculate actual booking pts
        hy = found_match.get('home_yellow', 0) or 0
        ay = found_match.get('away_yellow', 0) or 0
        hr = found_match.get('home_red', 0) or 0
        ar = found_match.get('away_red', 0) or 0
        actual_bp = 10 * (hy + ay) + 25 * (hr + ar)

        # Determine result
        if outcome == 'over':
            result = 'WIN' if actual_bp > line else 'LOSS'
        else:
            result = 'WIN' if actual_bp <= line else 'LOSS'

        fd_match_str = '%s vs %s' % (
            found_match.get('home_team', fd_home),
            found_match.get('away_team', fd_away),
        )

        log.info('[Soccer Resolver] %s | %s | actual_bp=%d | line=%.1f | %s -> %s',
                 match_str[:35], side_str[:28], actual_bp, line, outcome.upper(), result)

        if not dry_run:
            n = resolve_shadow_picks(match=match_str, side=side_str, result=result)
            resolved_count += n

        resolved_map.setdefault(match_str, {})[side_str] = result

    log.info('Soccer shadow resolver: resolved %d picks from %d matches',
             resolved_count, len(resolved_map))
    return resolved_map


if __name__ == '__main__':
    import logging, sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    dry = '--dry' in sys.argv
    if dry:
        print('DRY RUN — no changes written to Sibila')
    results = resolve_soccer_shadow(lookback_days=10, dry_run=dry)
    print('\nResolved:', len(results), 'match/side pairs')
    for match, sides in results.items():
        for side, result in sides.items():
            print(f'  {match[:40]} | {side[:30]} -> {result}')
