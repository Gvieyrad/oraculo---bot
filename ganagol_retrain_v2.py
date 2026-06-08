#!/usr/bin/env python3
"""
ganagol_retrain_v2.py — Entrena Dixon-Coles con ligas globales + CHI + URU.
Incluye: Europa (xg_matches), ARG, BRA, RUS, JPN, CHI, URU.

Uso:
  python3 ganagol_retrain_v2.py           # retrain normal
  python3 ganagol_retrain_v2.py --force   # fuerza re-descarga
"""
import os, sys, json, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ganagol_retrain_v2')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
XG_CACHE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_matches.json')
DC_CACHE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'dixon_coles.json')
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')
FORCE = '--force' in sys.argv


def load_european_matches():
    if not os.path.exists(XG_CACHE):
        log.warning('xg_matches.json not found — skipping European data')
        return []
    with open(XG_CACHE) as f:
        matches = json.load(f)
    log.info('European matches (xg_matches): %d', len(matches))
    return matches


def load_new_leagues():
    from oraculo_football_csv import download_new_league_csv, NEW_LEAGUE_CODES
    all_new = []
    for code in NEW_LEAGUE_CODES:
        m = download_new_league_csv(code, force=FORCE)
        log.info('%s: %d matches', code, len(m))
        all_new.extend(m)
    return all_new


def load_cached_leagues():
    """Load all cached league JSON files (api-football + football-data.co.uk) from api-football JSON cache."""
    all_cached = []
    codes = ["CHI", "URU", "PER", "NOR", "IRL", "CYP", "EGY", "BOL", "INTL"]
    for code in codes:
        fname = os.path.join(CSV_DIR, f'new_{code}.json')
        if os.path.exists(fname):
            m = json.load(open(fname))
            log.info('%s: %d matches (cached)', code, len(m))
            all_cached.extend(m)
        else:
            log.warning('%s: cache not found at %s', code, fname)
    return all_cached


def main():
    log.info('=== Ganagol Retrain v2 ===')
    log.info('Force re-download: %s', FORCE)

    eu_matches     = load_european_matches()
    new_matches    = load_new_leagues()
    cached_matches = load_cached_leagues()

    all_matches = eu_matches + new_matches + cached_matches
    log.info('Total matches for training: %d', len(all_matches))

    if len(all_matches) < 100:
        log.error('Insufficient data (%d matches). Aborting.', len(all_matches))
        sys.exit(1)

    from oraculo_dixon_coles import DixonColesModel
    dc = DixonColesModel()

    log.info('Training Dixon-Coles model (date-based per-league decay)...')
    dc.train(all_matches)  # uses _LEAGUE_DECAY dict from oraculo_dixon_coles
    dc.save()
    log.info('Saved to %s', DC_CACHE)

    dc_data = json.load(open(DC_CACHE))
    teams = list(dc_data['attack'].keys())
    log.info('Model covers %d teams', len(teams))

    league_avgs = dc_data.get('league_avgs', {})
    log.info('League avgs: %s', {k: round(v, 2) for k, v in sorted(league_avgs.items())})
    log.info('rho: %.2f', dc_data.get('rho', 0))

    # Sample CHI/URU teams to verify they loaded
    tl = dc_data.get('team_league', {})
    for league in ['CHI', 'URU']:
        lg_teams = [t for t, l in tl.items() if l == league]
        log.info('%s teams in model: %d  %s', league, len(lg_teams), lg_teams[:5])

    log.info('Done. Run: python3 ganagol.py [matches...]')


if __name__ == '__main__':
    main()
