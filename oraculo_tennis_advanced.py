"""Advanced tennis features: fatigue, retirement risk, set handicap, surface form."""
import os, json, logging, math
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class TennisAdvanced:
    """Enhanced tennis prediction with fatigue, retirement, and set modeling."""

    def __init__(self, base_elo=None):
        self.base_elo = base_elo
        self._match_log = defaultdict(list)
        self._retirements = defaultdict(int)
        self._total_matches = defaultdict(int)

    def load_match_history(self, cache_dir):
        """Load match history for fatigue and retirement tracking."""
        for fname in sorted(os.listdir(cache_dir)):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(cache_dir, fname)) as f:
                    matches = json.load(f)
                if not isinstance(matches, list):
                    continue
                for m in matches:
                    winner = m.get('winner', '')
                    loser = m.get('loser', '')
                    date = m.get('date', '')
                    surface = m.get('surface', 'hard')
                    score = m.get('score', '')
                    retired = 'ret' in score.lower() or 'w/o' in score.lower() if score else False
                    sets = score.count('-') if score else 2

                    _serve_keys = ('w_svpt','w_1stIn','w_1stWon','w_2ndWon',
                                   'w_bpSaved','w_bpFaced','w_ace','w_df',
                                   'l_svpt','l_1stIn','l_1stWon','l_2ndWon',
                                   'l_bpSaved','l_bpFaced','l_ace','l_df')
                    _extra = {k: m[k] for k in _serve_keys if k in m}
                    if winner:
                        entry_w = {'date': date, 'surface': surface,
                                   'sets': sets, 'won': True, 'retired_opp': retired}
                        entry_w.update(_extra)
                        self._match_log[winner].append(entry_w)
                        self._total_matches[winner] += 1
                    if loser:
                        entry_l = {'date': date, 'surface': surface,
                                   'sets': sets, 'won': False, 'retired': retired}
                        entry_l.update(_extra)
                        self._match_log[loser].append(entry_l)
                        self._total_matches[loser] += 1
                        if retired:
                            self._retirements[loser] += 1
            except Exception:
                continue
        log.info('TennisAdvanced: %d players, %d with retirements',
                 len(self._match_log), len(self._retirements))

    def get_fatigue(self, player, ref_date=None, window_days=14):
        """Fatigue score 0=fresh, 1=exhausted. Based on matches+sets in last N days."""
        if ref_date is None:
            ref_date = datetime.utcnow().strftime('%Y-%m-%d')
        try:
            cutoff = (datetime.strptime(ref_date[:10], '%Y-%m-%d') - timedelta(days=window_days)).strftime('%Y-%m-%d')
        except Exception:
            return 0.0

        recent = [m for m in self._match_log.get(player, [])
                  if cutoff <= m.get('date', '') <= ref_date]
        if not recent:
            return 0.0

        n_matches = len(recent)
        total_sets = sum(m.get('sets', 2) for m in recent)
        match_fatigue = min(1.0, n_matches / 7)
        set_fatigue = min(1.0, total_sets / 21)

        last_7 = [m for m in recent
                  if m.get('date', '') >= (datetime.strptime(ref_date[:10], '%Y-%m-%d') - timedelta(days=7)).strftime('%Y-%m-%d')]
        density = min(1.0, len(last_7) / 4)

        return min(1.0, 0.4 * match_fatigue + 0.3 * set_fatigue + 0.3 * density)

    def get_retirement_risk(self, player):
        """Retirement probability 0-0.15 based on historical rate."""
        total = self._total_matches.get(player, 0)
        rets = self._retirements.get(player, 0)
        if total < 20:
            return 0.02
        return min(0.15, rets / total)

    def get_surface_form(self, player, surface='hard', n_recent=15):
        """Win rate on specific surface (last N matches)."""
        matches = [m for m in self._match_log.get(player, [])
                   if m.get('surface', '').lower() == surface.lower()]
        if not matches:
            return None
        recent = matches[-n_recent:]
        return sum(1 for m in recent if m.get('won')) / len(recent)

    def get_serve_dominance(self, player, n=30):
        """SPW / (1-RPW) dominance ratio from match history. >1 = serve-dominant."""
        matches = self._match_log.get(player, [])[-n:]
        if not matches:
            return None
        spw_list, rpw_list = [], []
        for m in matches:
            svpt  = m.get('w_svpt')  if m.get('won') else m.get('l_svpt')
            w1st  = m.get('w_1stWon') if m.get('won') else m.get('l_1stWon')
            w2nd  = m.get('w_2ndWon') if m.get('won') else m.get('l_2ndWon')
            # Return points won = opponent's serve points lost
            o_svpt = m.get('l_svpt')  if m.get('won') else m.get('w_svpt')
            o_1st  = m.get('l_1stWon') if m.get('won') else m.get('w_1stWon')
            o_2nd  = m.get('l_2ndWon') if m.get('won') else m.get('w_2ndWon')
            if svpt and svpt > 0 and w1st is not None and w2nd is not None:
                spw_list.append((w1st + w2nd) / svpt)
            if o_svpt and o_svpt > 0 and o_1st is not None and o_2nd is not None:
                rpw_list.append(1.0 - (o_1st + o_2nd) / o_svpt)
        if not spw_list or not rpw_list:
            return None
        spw = sum(spw_list) / len(spw_list)
        rpw = sum(rpw_list) / len(rpw_list)
        dom = spw / max(1 - rpw, 0.01)
        return round(dom, 4)

    def predict_enhanced(self, player_a, player_b, surface='hard', ref_date=None):
        """Enhanced prediction: Elo + fatigue + surface form + retirement risk."""
        base_prob = 0.5
        if self.base_elo:
            base_prob = self.base_elo.predict(player_a, player_b, surface, include_h2h=False)

        # Fatigue (-10% max)
        fat_a = self.get_fatigue(player_a, ref_date)
        fat_b = self.get_fatigue(player_b, ref_date)
        fatigue_adj = (fat_b - fat_a) * 0.10

        # Surface form (-8% max)
        sf_a = self.get_surface_form(player_a, surface)
        sf_b = self.get_surface_form(player_b, surface)
        surface_adj = 0
        if sf_a is not None and sf_b is not None:
            surface_adj = (sf_a - sf_b) * 0.08

        # Retirement risk
        ret_a = self.get_retirement_risk(player_a)
        ret_b = self.get_retirement_risk(player_b)
        ret_adj = (ret_b - ret_a) * 0.5

        # H2H adjustment: up to +/-6% if strong head-to-head record (min 4 matches)
        h2h_adj = 0.0
        h2h = [0, 0]
        if self.base_elo and hasattr(self.base_elo, 'get_h2h'):
            h2h = self.base_elo.get_h2h(player_a, player_b)
            h2h_total = sum(h2h) if h2h else 0
            if h2h_total >= 4:
                h2h_wr_a = h2h[0] / h2h_total
                # Shrink toward 0.5 to avoid overfit on small samples
                h2h_shrink = min(1.0, h2h_total / 20)  # 20% weight at 4 matches, 100% at 20+
                h2h_adj = (h2h_wr_a - 0.5) * 0.12 * h2h_shrink

        # Serve dominance: high SPW/RPW ratio → harder to break → boost slightly
        dom_a = self.get_serve_dominance(player_a)
        dom_b = self.get_serve_dominance(player_b)
        dom_adj = 0.0
        if dom_a is not None and dom_b is not None:
            # Adjust by max ±4% based on dominance ratio difference
            dom_adj = (dom_a - dom_b) * 0.08
            dom_adj = max(-0.04, min(0.04, dom_adj))

        final_prob = max(0.05, min(0.95, base_prob + fatigue_adj + surface_adj + ret_adj + h2h_adj + dom_adj))

        return {
            'prob_a': final_prob, 'prob_b': 1 - final_prob,
            'base_elo': base_prob,
            'fatigue_a': fat_a, 'fatigue_b': fat_b, 'fatigue_adj': fatigue_adj,
            'surface_form_a': sf_a, 'surface_form_b': sf_b, 'surface_adj': surface_adj,
            'retirement_risk_a': ret_a, 'retirement_risk_b': ret_b, 'ret_adj': ret_adj,
            'h2h': h2h, 'h2h_adj': h2h_adj,
            'dom_a': dom_a, 'dom_b': dom_b, 'dom_adj': dom_adj,
        }

    def predict_sets(self, player_a, player_b, surface='hard', best_of=3):
        """Set-related predictions: total sets, set handicap, exact score."""
        pred = self.predict_enhanced(player_a, player_b, surface)
        p = pred['prob_a']

        if best_of == 3:
            p_set = 0.5 + (p - 0.5) * 0.85
            p_20 = p_set * p_set
            p_02 = (1 - p_set) * (1 - p_set)
            p_21 = p_set * (1 - p_set) * p_set * 2
            p_12 = (1 - p_set) * p_set * (1 - p_set) * 2

            total = p_20 + p_02 + p_21 + p_12
            if total > 0:
                p_20, p_02, p_21, p_12 = p_20/total, p_02/total, p_21/total, p_12/total

            return {
                'a_2_0': p_20, 'a_2_1': p_21,
                'b_2_0': p_02, 'b_2_1': p_12,
                'over_25_sets': p_21 + p_12,
                'under_25_sets': p_20 + p_02,
                'a_handicap_m15': p_20,
                'a_handicap_p15': p_20 + p_21 + p_12,
                'b_handicap_m15': p_02,
                'b_handicap_p15': p_02 + p_12 + p_21,
            }

        if best_of == 5:
            p_set = 0.5 + (p - 0.5) * 0.80
            q = 1 - p_set
            p_30 = p_set**3
            p_03 = q**3
            p_31 = 3 * p_set**3 * q
            p_13 = 3 * q**3 * p_set
            p_32 = 6 * p_set**3 * q**2
            p_23 = 6 * q**3 * p_set**2

            total = p_30 + p_03 + p_31 + p_13 + p_32 + p_23
            if total > 0:
                p_30, p_03 = p_30/total, p_03/total
                p_31, p_13 = p_31/total, p_13/total
                p_32, p_23 = p_32/total, p_23/total

            return {
                'a_3_0': p_30, 'a_3_1': p_31, 'a_3_2': p_32,
                'b_3_0': p_03, 'b_3_1': p_13, 'b_3_2': p_23,
                'over_35_sets': p_32 + p_23 + p_31 + p_13,
                'under_35_sets': p_30 + p_03,
                'over_45_sets': p_32 + p_23,
            }
        return {}
