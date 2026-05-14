"""Soccer corners + bookings model for Oráculo v2.

Markets: corner_nr (who wins corner N), last_corner, booking_nr (first booking),
         total_booking_points O/U.

Data source: football-data.co.uk CSVs (home/away split per team).
"""
import os, json, math, logging
from collections import defaultdict

log = logging.getLogger('oraculo.soccer')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_DIR    = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv')

LEAGUE_FILES = ['E0_2526.json', 'D1_2526.json', 'F1_2526.json',
                'SP1_2526.json', 'I1_2526.json']

CB_TO_CSV = {
    'Bayern Munich':             'FC Bayern Munchen',
    'Paris Saint Germain':       'Paris Saint-Germain FC',
    'Paris SG':                  'Paris Saint-Germain FC',
    'PSG':                       'Paris Saint-Germain FC',
    'Liverpool':                 'Liverpool FC',
    'Chelsea':                   'Chelsea FC',
    'Arsenal':                   'Arsenal FC',
    'Manchester City':           'Manchester City FC',
    'Man City':                  'Manchester City FC',
    'Manchester United':         'Manchester United FC',
    'Man United':                'Manchester United FC',
    'Tottenham':                 'Tottenham Hotspur FC',
    'Tottenham Hotspur':         'Tottenham Hotspur FC',
    'Real Madrid':               'Real Madrid CF',
    'Barcelona':                 'FC Barcelona',
    'Atletico Madrid':           'Club Atletico de Madrid',
    'Inter Milan':               'FC Internazionale Milano',
    'AC Milan':                  'AC Milan',
    'Juventus':                  'Juventus FC',
    'Borussia Dortmund':         'Borussia Dortmund',
    'RB Leipzig':                'RB Leipzig',
    'Bayer Leverkusen':          'Bayer 04 Leverkusen',
}

UCL_CARD_MULTIPLIER  = 1.45
UCL_CORNER_HOME_BOOST = 1.05


class SoccerModel:
    def __init__(self):
        self._home = defaultdict(lambda: dict(cf=0, ca=0, y=0, r=0, fo=0, n=0))
        self._away = defaultdict(lambda: dict(cf=0, ca=0, y=0, r=0, fo=0, n=0))

    def load(self):
        for fname in LEAGUE_FILES:
            fpath = os.path.join(CSV_DIR, fname)
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath) as f:
                    data = json.load(f)
                for m in data:
                    ht = m.get('home_team', '')
                    at = m.get('away_team', '')
                    if not ht or not at:
                        continue
                    hc = m.get('home_corners', 0) or 0
                    ac = m.get('away_corners', 0) or 0
                    hy = m.get('home_yellow', 0) or 0
                    ay = m.get('away_yellow', 0) or 0
                    hr = m.get('home_red', 0) or 0
                    ar = m.get('away_red', 0) or 0
                    hf = m.get('home_fouls', 0) or 0
                    af = m.get('away_fouls', 0) or 0
                    h = self._home[ht]; a = self._away[at]
                    h['cf'] += hc; h['ca'] += ac
                    h['y'] += hy; h['r'] += hr; h['fo'] += hf; h['n'] += 1
                    a['cf'] += ac; a['ca'] += hc
                    a['y'] += ay; a['r'] += ar; a['fo'] += af; a['n'] += 1
            except Exception as e:
                log.debug('CSV load error %s: %s', fname, e)
        log.info('SoccerModel: %d teams loaded', len(self._home))
        return self

    def _norm(self, name):
        mapped = CB_TO_CSV.get(name, name)
        if mapped in self._home:
            return mapped
        for csv_name in self._home:
            if name.lower() in csv_name.lower() or csv_name.lower() in name.lower():
                return csv_name
        return mapped

    def _team_stats(self, name, as_home):
        csv_name = self._norm(name)
        src = self._home[csv_name] if as_home else self._away[csv_name]
        if src['n'] == 0:
            return None
        n = src['n']
        return {
            'avg_corners_for':     src['cf'] / n,
            'avg_corners_against': src['ca'] / n,
            'avg_yellows': src['y'] / n,
            'avg_reds':    src['r'] / n,
            'avg_fouls':   src['fo'] / n,
            'n': n,
        }

    def predict_corners(self, home_name, away_name, is_ucl=False):
        hs  = self._team_stats(home_name, as_home=True)
        as_ = self._team_stats(away_name, as_home=False)
        if not hs or not as_:
            return None
        lh = (hs['avg_corners_for'] + as_['avg_corners_against']) / 2.0
        la = (as_['avg_corners_for'] + hs['avg_corners_against']) / 2.0
        if is_ucl:
            lh *= UCL_CORNER_HOME_BOOST
        total = lh + la if (lh + la) > 0 else 1.0
        p_home = lh / total
        return {
            'lambda_home': round(lh, 2), 'lambda_away': round(la, 2),
            'total_expected': round(total, 2),
            'p_home': round(p_home, 4), 'p_away': round(1 - p_home, 4),
            'n_home': hs['n'], 'n_away': as_['n'],
        }

    def predict_bookings(self, home_name, away_name, is_ucl=False):
        hs  = self._team_stats(home_name, as_home=True)
        as_ = self._team_stats(away_name, as_home=False)
        if not hs or not as_:
            return None
        mult = UCL_CARD_MULTIPLIER if is_ucl else 1.0
        h_y = hs['avg_yellows'] * mult
        a_y = as_['avg_yellows'] * mult
        h_r = hs['avg_reds']    * mult
        a_r = as_['avg_reds']   * mult
        exp_bp = 10 * (h_y + a_y) + 25 * (h_r + a_r)
        var_bp = 100 * (h_y + a_y) + 625 * (h_r + a_r)
        std_bp = math.sqrt(var_bp) if var_bp > 0 else 5.0
        rate_h = h_y + 2 * h_r
        rate_a = a_y + 2 * a_r
        total_rate = rate_h + rate_a
        p_home_first = rate_h / total_rate if total_rate > 0 else 0.5
        return {
            'exp_bp': round(exp_bp, 1), 'std_bp': round(std_bp, 1),
            'h_yellows': round(h_y, 2), 'a_yellows': round(a_y, 2),
            'h_reds': round(h_r, 3),    'a_reds': round(a_r, 3),
            'p_home_first_booking': round(p_home_first, 4),
            'p_away_first_booking': round(1 - p_home_first, 4),
            'n_home': hs['n'], 'n_away': as_['n'],
        }

    def p_over_booking_points(self, pred, line):
        if not pred:
            return 0.5
        z = (line - pred['exp_bp']) / pred['std_bp']
        return round(_norm_cdf(-z), 4)


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def scan_soccer_corners(api, state, model=None, dry_run=False,
                        min_edge=0.12, min_conf=0.52):
    """Scan UCL and PL for corner_nr, last_corner, booking_nr, booking_pts value picks."""
    picks = []
    if model is None:
        model = SoccerModel().load()

    COMPS = [
        ('soccer-international-clubs-uefa-champions-league', True),
        ('soccer-england-premier-league', False),
    ]

    for comp_key, is_ucl in COMPS:
        try:
            events = api.get_odds(comp_key)
        except Exception as e:
            log.debug('Soccer fetch %s: %s', comp_key, e)
            continue
        if not events:
            continue

        for ev in events:
            if not ev or not isinstance(ev, dict):
                continue
            if ev.get('type') == 'EVENT_TYPE_OUTRIGHT':
                continue
            home_obj = ev.get('home') or {}
            away_obj = ev.get('away') or {}
            home = home_obj.get('name', '') if isinstance(home_obj, dict) else str(home_obj)
            away = away_obj.get('name', '') if isinstance(away_obj, dict) else str(away_obj)
            if not home or not away:
                continue
            eid     = str(ev.get('id', ''))
            markets = ev.get('markets', {})
            match_s = '%s vs %s' % (home, away)

            cpred = model.predict_corners(home, away, is_ucl=is_ucl)
            bpred = model.predict_bookings(home, away, is_ucl=is_ucl)

            # corner_nr
            for mk_name in ('soccer.corner_nr', 'soccer.last_corner'):
                mkt = markets.get(mk_name, {})
                lbl_prefix = 'Corner N' if 'corner_nr' in mk_name else 'Last corner'
                for sv in mkt.get('submarkets', {}).values():
                    for sel in sv.get('selections', []):
                        if sel.get('status') != 'SELECTION_ENABLED':
                            continue
                        price   = float(sel.get('price', 0) or 0)
                        murl    = sel.get('marketUrl', '')
                        outcome = sel.get('outcome', '')
                        if price < 1.05 or not murl or not cpred:
                            continue
                        if outcome == 'home':
                            prob = cpred['p_home']; lbl = '%s: %s' % (lbl_prefix, home)
                        elif outcome == 'away':
                            prob = cpred['p_away']; lbl = '%s: %s' % (lbl_prefix, away)
                        else:
                            continue
                        edge = prob * price - 1
                        if edge >= min_edge and prob >= min_conf:
                            picks.append({
                                'match': match_s, 'league': comp_key,
                                'event_id': eid, 'market_url': murl,
                                'price': price, 'label': lbl,
                                'model_prob': prob, 'raw_model_prob': prob,
                                'edge': edge, 'sport': 'soccer',
                                'market': mk_name.split('.')[-1],
                            })

            # booking_nr
            bnr = markets.get('soccer.booking_nr', {})
            for sv in bnr.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    if sel.get('status') != 'SELECTION_ENABLED':
                        continue
                    price   = float(sel.get('price', 0) or 0)
                    murl    = sel.get('marketUrl', '')
                    outcome = sel.get('outcome', '')
                    if price < 1.05 or not murl or not bpred:
                        continue
                    if outcome == 'home':
                        prob = bpred['p_home_first_booking']
                        lbl  = 'First booking: %s' % home
                    elif outcome == 'away':
                        prob = bpred['p_away_first_booking']
                        lbl  = 'First booking: %s' % away
                    else:
                        continue
                    edge = prob * price - 1
                    if edge >= min_edge and prob >= min_conf:
                        picks.append({
                            'match': match_s, 'league': comp_key,
                            'event_id': eid, 'market_url': murl,
                            'price': price, 'label': lbl,
                            'model_prob': prob, 'raw_model_prob': prob,
                            'edge': edge, 'sport': 'soccer',
                            'market': 'booking_nr',
                        })

            # total_booking_points
            tbp = markets.get('soccer.total_booking_points', {})
            for sv in tbp.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    if sel.get('status') != 'SELECTION_ENABLED':
                        continue
                    price   = float(sel.get('price', 0) or 0)
                    murl    = sel.get('marketUrl', '')
                    outcome = sel.get('outcome', '')
                    params  = str(sel.get('params', ''))
                    if price < 1.05 or not murl or not bpred:
                        continue
                    try:
                        line = float(params.split('total=')[-1])
                    except Exception:
                        continue
                    if outcome == 'over':
                        prob = model.p_over_booking_points(bpred, line)
                        lbl  = 'Booking pts Over %.1f' % line
                    elif outcome == 'under':
                        prob = 1.0 - model.p_over_booking_points(bpred, line)
                        lbl  = 'Booking pts Under %.1f' % line
                    else:
                        continue
                    edge = prob * price - 1
                    if edge >= 0.15 and prob >= 0.55:
                        picks.append({
                            'match': match_s, 'league': comp_key,
                            'event_id': eid, 'market_url': murl,
                            'price': price, 'label': lbl,
                            'model_prob': prob, 'raw_model_prob': prob,
                            'edge': edge, 'sport': 'soccer',
                            'market': 'booking_pts',
                        })

    log.info('Soccer corners/bookings: %d value picks', len(picks))
    for p in picks:
        log.info('  [SC] %s | %s | edge=%.1f%% conf=%.0f%% @%.2f',
                 p['match'][:35], p['label'][:30],
                 p['edge'] * 100, p['model_prob'] * 100, p['price'])
    return picks


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    m = SoccerModel().load()
    for home, away, is_ucl, desc in [
        ('Bayern Munich', 'Paris Saint Germain', True, 'UCL SF'),
        ('Liverpool', 'Chelsea', False, 'PL'),
    ]:
        print('\n=== %s: %s vs %s ===' % (desc, home, away))
        cp = m.predict_corners(home, away, is_ucl)
        bp = m.predict_bookings(home, away, is_ucl)
        if cp:
            print('Corners: lh=%.2f la=%.2f  P(home)=%.1f%%  P(away)=%.1f%%  (n=%d/%d)' % (
                cp['lambda_home'], cp['lambda_away'],
                cp['p_home']*100, cp['p_away']*100, cp['n_home'], cp['n_away']))
        if bp:
            print('Bookings: exp_bp=%.1f std=%.1f  P(home_1st)=%.1f%%' % (
                bp['exp_bp'], bp['std_bp'], bp['p_home_first_booking']*100))
            for line in [25.5, 35.5, 45.5]:
                p_ov = m.p_over_booking_points(bp, line)
                print('  Over %.1f: %.1f%%  Under: %.1f%%' % (line, p_ov*100, (1-p_ov)*100))
