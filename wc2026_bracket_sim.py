#!/usr/bin/env python3
"""
wc2026_bracket_sim.py
Monte Carlo bracket simulator for WC 2026 (48 teams, 12 groups A-L).
Uses oraculo_wc_model.predict_match() + get_player_adjusted_xg() for xG.

Output: wc2026/bracket_probs.json
  {team: {group_win: float, q_r16: float, q_qf: float, q_sf: float,
          q_final: float, win_tournament: float}}

Run: python3 /home/noc/oraculo_v2/wc2026_bracket_sim.py [--sims N]
Default: 10000 simulations (~45s)
"""
import sys, json, os, random, collections, argparse
import numpy as np

sys.path.insert(0, '/home/noc/.local/lib/python3.12/site-packages')
sys.path.insert(0, '/home/noc/oraculo_v2')

BASE = '/home/noc/oraculo_v2'
GROUPS_F   = f'{BASE}/wc2026/wc2026_groups.json'
STANDINGS_F = f'{BASE}/wc2026/wc_standings.json'
OUTPUT_F   = f'{BASE}/wc2026/bracket_probs.json'

# WC 2026 format:
# Group stage: 12 groups × 4 teams, top 2 qualify + 8 best third-place → 32 in R16
# R16 → QF → SF → Final

def load_model():
    import importlib.util
    spec = importlib.util.spec_from_file_location('wc', f'{BASE}/oraculo_wc_model.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def simulate_match(m, home, away, neutral=True, extra_time=False):
    """
    Simulate a single match. Returns (home_goals, away_goals).
    Uses player-adjusted xG from oraculo_wc_model.
    """
    try:
        xg_h, xg_a, _, _ = m.get_player_adjusted_xg(home, away, neutral)
    except Exception:
        res = m.predict_match(home, away, neutral)
        xg_h = res['xg_home']
        xg_a = res['xg_away']

    gh = np.random.poisson(xg_h)
    ga = np.random.poisson(xg_a)

    if extra_time and gh == ga:
        # 30 min extra time: ~30% of normal rate
        et_h = np.random.poisson(xg_h * 0.30)
        et_a = np.random.poisson(xg_a * 0.30)
        gh += et_h; ga += et_a
        if gh == ga:
            # Penalties: 50/50 with slight advantage for first-listed team
            if random.random() < 0.52:
                gh += 1
            else:
                ga += 1
    return int(gh), int(ga)

def simulate_group_stage(m, groups, standings=None):
    """
    Simulate all group matches. Returns dict: {group: [(team, pts, gd, gf)]}
    standings: if provided, seed real results and only simulate unplayed rounds.
    """
    group_results = {}

    # Round-robin schedule for 4 teams (standard WC group scheduling):
    # Round 1: (0v1, 2v3)  Round 2: (0v2, 1v3)  Round 3: (0v3, 1v2)
    ROUNDS = [
        [(0, 1), (2, 3)],
        [(0, 2), (1, 3)],
        [(0, 3), (1, 2)],
    ]

    for grp, teams in groups.items():
        team_stats = {t: {'pts': 0, 'gd': 0, 'gf': 0, '_played': 0} for t in teams}

        # Seed from real standings if available
        played_rounds = 0
        if standings and grp in standings:
            for row in standings[grp]:
                t = row['team']
                if t in team_stats:
                    team_stats[t] = {
                        'pts': row['pts'],
                        'gd': row['gd'],
                        'gf': row['gf'],
                        '_played': row.get('played', 0),
                    }
            # All teams in a group play the same number of rounds;
            # skip as many as the least-played team has completed.
            played_rounds = min(ts['_played'] for ts in team_stats.values())

        # Simulate only unplayed rounds
        for rnd_idx, pairs in enumerate(ROUNDS):
            if rnd_idx < played_rounds:
                continue
            for i, j in pairs:
                home, away = teams[i], teams[j]
                gh, ga = simulate_match(m, home, away, neutral=True, extra_time=False)
                if gh > ga:
                    team_stats[home]['pts'] += 3
                elif gh == ga:
                    team_stats[home]['pts'] += 1
                    team_stats[away]['pts'] += 1
                else:
                    team_stats[away]['pts'] += 3
                team_stats[home]['gd'] += gh - ga
                team_stats[home]['gf'] += gh
                team_stats[away]['gd'] += ga - gh
                team_stats[away]['gf'] += ga

        ranking = sorted(
            teams,
            key=lambda t: (-team_stats[t]['pts'], -team_stats[t]['gd'],
                           -team_stats[t]['gf'], random.random())
        )
        group_results[grp] = [(t, team_stats[t]['pts'], team_stats[t]['gd'],
                                team_stats[t]['gf']) for t in ranking]

    return group_results

def get_r16_bracket(group_results):
    """
    WC 2026 R16 bracket.
    Top 2 from each of 12 groups (24 teams) + 8 best third-place = 32 teams.
    Official R16 seeding TBD; use likely slot assignment.
    Returns list of 16 match tuples (team_a, team_b).
    """
    # Extract top-2 per group
    winners = {}
    runners = {}
    thirds  = []

    for grp in sorted(group_results.keys()):
        ranking = group_results[grp]
        winners[grp] = ranking[0][0]
        runners[grp] = ranking[1][0]
        thirds.append((ranking[2][0], ranking[2][1], ranking[2][2], ranking[2][3], grp))

    # Best 8 third-place teams by pts, gd, gf
    thirds_sorted = sorted(thirds, key=lambda x: (-x[1], -x[2], -x[3]))
    best_thirds = [t[0] for t in thirds_sorted[:8]]
    best_third_groups = [t[4] for t in thirds_sorted[:8]]

    # WC 2026 bracket assignments (approximate based on FIFA announcement pattern)
    # Format: 1A vs 3E/F/G/H, 1C vs 3A/B/C/D, etc.
    # Using the standard WC 2026 slot assignment
    # Slot assignments (simplified symmetric bracket):
    r16 = [
        (winners['A'], runners['B']),
        (winners['C'], runners['D']),
        (winners['E'], runners['F']),
        (winners['G'], runners['H']),
        (winners['B'], runners['A']),
        (winners['D'], runners['C']),
        (winners['F'], runners['E']),
        (winners['H'], runners['G']),
        (winners['I'], runners['J']),
        (winners['K'], runners['L']),
        (winners['J'], runners['I']),
        (winners['L'], runners['K']),
        (best_thirds[0], best_thirds[1]),
        (best_thirds[2], best_thirds[3]),
        (best_thirds[4], best_thirds[5]),
        (best_thirds[6], best_thirds[7]),
    ]
    return r16

def simulate_knockout(m, bracket):
    """
    Simulate a knockout round. Returns list of winners.
    bracket: list of (team_a, team_b) pairs
    """
    winners = []
    for home, away in bracket:
        gh, ga = simulate_match(m, home, away, neutral=True, extra_time=True)
        winners.append(home if gh >= ga else away)
    return winners

def run_simulation(m, groups, n_sims=10000, standings=None):
    """Run n_sims Monte Carlo simulations. Returns advancement probability dict."""
    counters = collections.defaultdict(lambda: collections.defaultdict(int))

    for sim_i in range(n_sims):
        if sim_i % 1000 == 0 and sim_i > 0:
            print(f'  {sim_i}/{n_sims}...', flush=True)

        # Group stage
        group_results = simulate_group_stage(m, groups, standings)

        # Record group-stage outcomes
        for grp, ranking in group_results.items():
            counters[ranking[0][0]]['group_win'] += 1
            for team, _, _, _ in ranking[:2]:
                counters[team]['q_r16'] += 1
            # Third place teams might qualify too
            thirds = [r for i, r in enumerate(ranking) if i == 2]
            for t, _, _, _ in thirds:
                counters[t]['q_r16_third'] += 1  # might qualify

        # R16
        r16 = get_r16_bracket(group_results)
        # Add best third-place teams to r16 count
        for home, away in r16:
            counters[home]['q_r16_actual'] += 1
            counters[away]['q_r16_actual'] += 1

        r16_winners = simulate_knockout(m, r16)

        # QF (16 → 8)
        qf_bracket = [(r16_winners[i*2], r16_winners[i*2+1]) for i in range(8)]
        for t in r16_winners:
            counters[t]['q_qf'] += 1
        qf_winners = simulate_knockout(m, qf_bracket)

        # SF (8 → 4)
        sf_bracket = [(qf_winners[i*2], qf_winners[i*2+1]) for i in range(4)]
        for t in qf_winners:
            counters[t]['q_sf'] += 1
        sf_winners = simulate_knockout(m, sf_bracket)

        # Final (4 → 2 → 1)
        final_bracket = [(sf_winners[0], sf_winners[1]), (sf_winners[2], sf_winners[3])]
        for t in sf_winners:
            counters[t]['q_final'] += 1
        finalists = simulate_knockout(m, final_bracket)

        # Championship final
        champion = simulate_knockout(m, [(finalists[0], finalists[1])])[0]
        counters[champion]['win_tournament'] += 1

    # Normalize
    results = {}
    for team, counts in counters.items():
        results[team] = {
            'group_win':       round(counts['group_win']       / n_sims, 4),
            'q_r16':           round(counts['q_r16']           / n_sims, 4),
            'q_r16_actual':    round(counts['q_r16_actual']    / n_sims, 4),
            'q_qf':            round(counts['q_qf']            / n_sims, 4),
            'q_sf':            round(counts['q_sf']            / n_sims, 4),
            'q_final':         round(counts['q_final']         / n_sims, 4),
            'win_tournament':  round(counts['win_tournament']  / n_sims, 4),
        }
    return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sims', type=int, default=10000)
    args = parser.parse_args()

    print(f'Loading model...')
    m = load_model()

    groups = json.load(open(GROUPS_F))
    print(f'Groups loaded: {len(groups)} groups, {sum(len(v) for v in groups.values())} teams')

    standings = None
    if os.path.exists(STANDINGS_F):
        standings = json.load(open(STANDINGS_F))
        played = sum(r['played'] for grp in standings.values() for r in grp)
        if played > 0:
            print(f'Seeding from real standings ({played} matches played)')

    print(f'Running {args.sims} simulations...')
    np.random.seed(42)   # reproducible
    random.seed(42)       # seed Python random too (tiebreakers, penalties)
    results = run_simulation(m, groups, args.sims, standings)

    # Sort by win probability
    sorted_results = dict(sorted(results.items(),
                                  key=lambda x: -x[1]['win_tournament']))

    json.dump(sorted_results, open(OUTPUT_F, 'w'), indent=2)
    print(f'\nSaved → {OUTPUT_F}')

    # Print top 16
    print('\n=== WC 2026 Win Probability (Top 16) ===')
    print(f'{"Team":<25} {"Group Win":>9} {"R16":>6} {"QF":>6} {"SF":>6} {"Final":>6} {"Win":>6}')
    print('-' * 70)
    for i, (team, probs) in enumerate(sorted_results.items()):
        if i >= 16: break
        print(f'{team:<25} {probs["group_win"]:>9.1%} {probs["q_r16_actual"]:>6.1%} '
              f'{probs["q_qf"]:>6.1%} {probs["q_sf"]:>6.1%} '
              f'{probs["q_final"]:>6.1%} {probs["win_tournament"]:>6.1%}')

if __name__ == '__main__':
    main()
