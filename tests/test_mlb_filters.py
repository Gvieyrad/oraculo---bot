"""
Regression tests for MLB fade filter bugs.
Covers: Filtro 1b, Filtro 5, pre-placement guard, _is_wc_pick.

Bug context: _lbl is lowercased at scan loop start but the original regexes
used uppercase patterns -> re.search() always returned None -> fade filter
was a silent no-op since it was written.

Run: cd /home/noc/oraculo_v2 && python -m pytest tests/test_mlb_filters.py -v
"""
import re
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Constants mirrored from runner (kept in sync via MLB_FADE_TEAMS_LOWER)
MLB_FADE_TEAMS = {'CHI Cubs', 'TEX Rangers', 'DET Tigers', 'MIA Marlins'}
MLB_FADE_TEAMS_LOWER = {t.lower() for t in MLB_FADE_TEAMS}

RUNNER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'oraculo_runner_auto.py'
)


# Extracted filter logic (matches runner implementation)

def filtro_1b(lbl):
    """Block if label is a fade-team F5 ML pick. Returns team name or None."""
    m = re.search(r'f5 ml: (.+?) \(fip', lbl)
    if m:
        team = m.group(1).strip()
        if team in MLB_FADE_TEAMS_LOWER:
            return team
    return None


def filtro_5(lbl, match_name):
    """Block if opposing pitcher is a fade team with FIP<=3.20. Returns True to block."""
    fip_m = re.search(r'fip ([\d.]+)/([\d.]+)', lbl)
    parts = str(match_name).split(' vs ')
    if not fip_m or len(parts) != 2:
        return False
    home_fip = float(fip_m.group(1))
    away_fip = float(fip_m.group(2))
    home_team = parts[0].strip()
    picked_m = re.search(r'f5 ml: (.+?) \(fip', lbl)
    if not picked_m:
        return False
    picked = picked_m.group(1).strip()
    is_home = picked.lower() == home_team.lower()
    opp_name = parts[1].strip() if is_home else parts[0].strip()
    opp_fip = away_fip if is_home else home_fip
    return opp_name in MLB_FADE_TEAMS and opp_fip <= 3.20


def is_wc_pick(p):
    """True only for soccer/football bets in WC window."""
    return p.get('sport', '') in ('soccer', 'football') and (
        (p.get('cutoff_time', '') or '') >= '2026-06-01' or
        p.get('league') == 'FIFA_WC'
    )


def pre_placement_guard(p):
    """True = BLOCK: fade team escaped all earlier filters."""
    if p.get('sport') == 'baseball' and p.get('market_type') == 'mlb_f5_ml':
        guard_lbl = str(p.get('label', '')).lower()
        m = re.search(r'f5 ml: (.+?) \(fip', guard_lbl)
        if m and m.group(1).strip() in MLB_FADE_TEAMS_LOWER:
            return True
    return False


# MLB_FADE_TEAMS_LOWER sync

class TestFadeTeamsLowerSync:

    @pytest.mark.parametrize('team', list(MLB_FADE_TEAMS))
    def test_each_team_in_lower_set(self, team):
        assert team.lower() in MLB_FADE_TEAMS_LOWER

    def test_non_fade_teams_not_in_lower_set(self):
        assert 'sea mariners' not in MLB_FADE_TEAMS_LOWER
        assert 'lad dodgers' not in MLB_FADE_TEAMS_LOWER
        assert 'ny yankees' not in MLB_FADE_TEAMS_LOWER

    def test_lower_set_size_matches(self):
        assert len(MLB_FADE_TEAMS_LOWER) == len(MLB_FADE_TEAMS)


# Filtro 1b

class TestFiltro1b:

    @pytest.mark.parametrize('team_lower,label', [
        ('mia marlins', 'f5 ml: mia marlins (fip 4.50/3.80)'),
        ('chi cubs',    'f5 ml: chi cubs (fip 3.90/3.10)'),
        ('det tigers',  'f5 ml: det tigers (fip 3.15/3.80)'),
        ('tex rangers', 'f5 ml: tex rangers (fip 4.10/3.50)'),
    ])
    def test_fade_team_blocked(self, team_lower, label):
        assert filtro_1b(label) == team_lower

    @pytest.mark.parametrize('label', [
        'f5 ml: sea mariners (fip 3.20/3.45)',
        'f5 ml: lad dodgers (fip 3.50/4.10)',
        'f5 ml: ny yankees (fip 3.80/4.20)',
        'f5 ou: over 7.5',
        'goals over 2.5',
    ])
    def test_non_fade_team_not_blocked(self, label):
        assert filtro_1b(label) is None

    def test_regression_uppercase_regex_fails_on_lowercase_label(self):
        old_regex = r'F5 ML: (.+?) \(FIP'
        label = 'f5 ml: mia marlins (fip 4.50/3.80)'
        assert re.search(old_regex, label) is None


# Filtro 5

class TestFiltro5:

    def test_fade_opp_good_fip_blocked(self):
        assert filtro_5('f5 ml: sea mariners (fip 3.10/3.45)', 'DET Tigers vs SEA Mariners')

    def test_fade_opp_fip_above_threshold_not_blocked(self):
        assert not filtro_5('f5 ml: sea mariners (fip 3.25/3.45)', 'DET Tigers vs SEA Mariners')

    def test_non_fade_opp_not_blocked(self):
        assert not filtro_5('f5 ml: det tigers (fip 3.45/3.10)', 'DET Tigers vs SEA Mariners')

    def test_missing_fip_not_blocked(self):
        assert not filtro_5('f5 ml: sea mariners', 'DET Tigers vs SEA Mariners')

    def test_malformed_match_name_not_blocked(self):
        assert not filtro_5('f5 ml: sea mariners (fip 3.10/3.45)', 'DET Tigers')


# Pre-placement guard

class TestPrePlacementGuard:

    def test_mia_blocked_original_case_label(self):
        pick = {
            'sport': 'baseball', 'market_type': 'mlb_f5_ml',
            'label': 'F5 ML: MIA Marlins (FIP 4.50/3.80)',
        }
        assert pre_placement_guard(pick)

    def test_chi_blocked(self):
        pick = {
            'sport': 'baseball', 'market_type': 'mlb_f5_ml',
            'label': 'F5 ML: CHI Cubs (FIP 3.80/4.10)',
        }
        assert pre_placement_guard(pick)

    def test_sea_not_blocked(self):
        pick = {
            'sport': 'baseball', 'market_type': 'mlb_f5_ml',
            'label': 'F5 ML: SEA Mariners (FIP 3.20/3.45)',
        }
        assert not pre_placement_guard(pick)

    def test_soccer_not_affected(self):
        pick = {'sport': 'soccer', 'market_type': 'goals_ou', 'label': 'Goals Over 2.5'}
        assert not pre_placement_guard(pick)

    def test_non_f5ml_market_not_affected(self):
        pick = {
            'sport': 'baseball', 'market_type': 'mlb_totals',
            'label': 'F5 ML: MIA Marlins (FIP 4.50/3.80)',
        }
        assert not pre_placement_guard(pick)


# _is_wc_pick sport guard

class TestIsWcPick:

    @pytest.mark.parametrize('sport', ['baseball', 'tennis'])
    def test_non_soccer_june_not_wc(self, sport):
        p = {'sport': sport, 'cutoff_time': '2026-06-05T20:00:00'}
        assert not is_wc_pick(p)

    @pytest.mark.parametrize('sport', ['soccer', 'football'])
    def test_soccer_june_is_wc(self, sport):
        p = {'sport': sport, 'cutoff_time': '2026-06-05T20:00:00'}
        assert is_wc_pick(p)

    def test_soccer_may_not_wc(self):
        p = {'sport': 'soccer', 'cutoff_time': '2026-05-31T20:00:00'}
        assert not is_wc_pick(p)

    def test_soccer_fifa_wc_league_any_date(self):
        p = {'sport': 'soccer', 'league': 'FIFA_WC', 'cutoff_time': ''}
        assert is_wc_pick(p)

    def test_baseball_fifa_wc_not_wc(self):
        p = {'sport': 'baseball', 'league': 'FIFA_WC', 'cutoff_time': '2026-06-12T20:00:00'}
        assert not is_wc_pick(p)


# Runner file text checks (catch regex regressions without re-running all code)

class TestRunnerFileRegex:

    @pytest.fixture(autouse=True)
    def load_runner(self):
        with open(RUNNER_PATH) as f:
            self.content = f.read()

    def test_filtro_1b_scan_loop_uses_lowercase_regex(self):
        # Uppercase regex is legitimate in settlement/shadow where label is stored in original case.
        # The scan filter (_lbl is already lowercase) must use lowercase pattern.
        assert "r'f5 ml: (.+?) \\(fip', _lbl" in self.content, \
               "Filtro 1b scan loop must search lowercase 'f5 ml:' pattern on _lbl"

    def test_filtro_5_no_uppercase_fip_regex(self):
        assert "r'FIP ([\\d.]+)/([\\d.]+)'" not in self.content

    def test_fade_teams_lower_constant_defined(self):
        assert 'MLB_FADE_TEAMS_LOWER' in self.content

    def test_pre_placement_guard_present(self):
        assert 'FADE-GUARD' in self.content
