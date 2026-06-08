#!/usr/bin/env python3
"""
ganagol_retrain.py — Reentrena el modelo Dixon-Coles con ligas globales.

Descarga CSVs de football-data.co.uk para Argentina, Brasil, Rusia y Japón,
los combina con los partidos europeos existentes (xg_matches.json) y
reconstruye el cache dixon_coles.json.

Uso:
  python3 ganagol_retrain.py           # retrain normal
  python3 ganagol_retrain.py --force   # fuerza re-descarga de CSVs
"""
import os, sys, json, logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ganagol_retrain')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
XG_CACHE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'xg_matches.json')
DC_CACHE   = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'dixon_coles.json')

FORCE = '--force' in sys.argv


def load_european_matches():
    if not os.path.exists(XG_CACHE):
        log.warning('xg_matches.json not found — skipping European data')
        return []
    with open(XG_CACHE) as f:
        matches = json.load(f)
    log.info('European matches loaded: %d', len(matches))
    return matches


def load_new_leagues(force=False):
    from oraculo_football_csv import download_new_league_csv, NEW_LEAGUE_CODES
    all_new = []
    for code in NEW_LEAGUE_CODES:
        m = download_new_league_csv(code, force=force)
        log.info('%s: %d matches', code, len(m))
        all_new.extend(m)
    return all_new


def main():
    log.info('=== Ganagol Retrain ===')
    log.info('Force re-download: %s', FORCE)

    eu_matches  = load_european_matches()
    new_matches = load_new_leagues(force=FORCE)

    all_matches = eu_matches + new_matches
    log.info('Total matches for training: %d', len(all_matches))

    if len(all_matches) < 100:
        log.error('Insufficient data (%d matches). Aborting.', len(all_matches))
        sys.exit(1)

    from oraculo_dixon_coles import DixonColesModel
    dc = DixonColesModel()

    # Convert xg_matches format to dixon_coles format (same fields, just verify)
    # xg_matches: {home, away, home_goals, away_goals, date, league}
    # dixon_coles expects the same — no conversion needed

    log.info('Training Dixon-Coles model...')
    dc.train(all_matches, decay=0.004)
    dc.save()
    log.info('Saved to %s', DC_CACHE)

    # Stats
    dc_data = json.load(open(DC_CACHE))
    teams = list(dc_data['attack'].keys())
    log.info('Model covers %d teams', len(teams))

    # Show new teams added
    eu_teams_before = set()
    try:
        import json as _j
        xg = _j.load(open(XG_CACHE))
        eu_teams_before = set(m['home'] for m in xg) | set(m['away'] for m in xg)
    except Exception:
        pass

    new_teams = [t for t in teams if t not in eu_teams_before]
    log.info('New teams added: %d', len(new_teams))
    if new_teams:
        print('\nNew teams in model:')
        for t in sorted(new_teams):
            atk = dc_data['attack'].get(t, 1.0)
            dfs = dc_data['defense'].get(t, 1.0)
            print(f'  {t:<35} atk={atk:.2f}  def={dfs:.2f}')

    print(f'\nDone. Model now covers {len(teams)} teams.')
    print('Run: python3 ganagol.py [matches...] to get predictions.')


if __name__ == '__main__':
    main()
