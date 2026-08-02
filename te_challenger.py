"""ATP/WTA Challenger results fetcher (TennisExplorer.com) for tennis_sibila_resolver.

2026-07-31: tennis_sibila_resolver.py resolves from a static ATP/WTA main-tour
XLSX that has zero Challenger coverage -- confirmed via test (186 shadow picks
in Challenger leagues, 82 falsely VOID from an old bug, 104 stuck NULL forever
because the match genuinely isn't in that XLSX). This module adds
TennisExplorer.com as a fallback source, used ONLY when the main XLSX lookup
misses. Shadow-resolution only -- does not touch live betting behavior,
only lets Challenger sets_under finally accumulate a real, measurable WR
so the block in oraculo_runner_auto.py can eventually be revisited with data.
"""
import os, time, json, logging, unicodedata
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
TE_CACHE_DIR  = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'te_challenger')
TE_HEADERS    = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}


def _norm(name: str) -> str:
    if not name:
        return ''
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_name.lower().strip()


def _fetch_day(date_str: str, force: bool = False) -> list:
    """Fetch and parse one day's Challenger results from TennisExplorer.
    Cached per-day (results don't change once a day is in the past)."""
    os.makedirs(TE_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(TE_CACHE_DIR, f'{date_str}.json')
    if not force and os.path.exists(cache_path):
        try:
            return json.load(open(cache_path))
        except Exception:
            pass

    try:
        import requests
        from bs4 import BeautifulSoup
        y, m, d = date_str.split('-')
        url = (f'https://www.tennisexplorer.com/results/'
               f'?type=challenger&year={y}&month={m}&day={d}')
        r = requests.get(url, headers=TE_HEADERS, timeout=15)
        if r.status_code != 200:
            log.debug('TE fetch %s failed: HTTP %d', date_str, r.status_code)
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        table = soup.find('table', class_='result')
        if not table:
            return []

        matches = []
        rows = table.find_all('tr')
        current_tourney = ''
        i = 0
        while i < len(rows):
            row = rows[i]
            classes = row.get('class') or []
            if 'head' in classes:
                link = row.find('a', href=True)
                current_tourney = link.get_text(strip=True) if link else ''
                i += 1
                continue
            # Match rows come in pairs (player 1, player 2)
            if i + 1 < len(rows) and 'challenger' in current_tourney.lower():
                row2 = rows[i + 1]
                p1_link = row.find('a', href=lambda h: h and '/player/' in h)
                p2_link = row2.find('a', href=lambda h: h and '/player/' in h)
                if p1_link and p2_link:
                    p1 = p1_link.get_text(strip=True)
                    p2 = p2_link.get_text(strip=True)
                    res_cells1 = row.find_all('td', class_='result')
                    res_cells2 = row2.find_all('td', class_='result')
                    score_cells1 = row.find_all('td', class_='score')
                    score_cells2 = row2.find_all('td', class_='score')
                    if res_cells1 and res_cells2 and score_cells1 and score_cells2:
                        try:
                            sets_won1 = int(res_cells1[0].get_text(strip=True))
                            sets_won2 = int(res_cells2[0].get_text(strip=True))
                        except (ValueError, IndexError):
                            i += 2
                            continue
                        sets = []
                        for c1, c2 in zip(score_cells1, score_cells2):
                            t1 = c1.get_text(strip=True)
                            t2 = c2.get_text(strip=True)
                            if not t1 or not t2:
                                continue
                            try:
                                g1 = int(''.join(ch for ch in t1 if ch.isdigit())[:1] or t1[0])
                                g2 = int(''.join(ch for ch in t2 if ch.isdigit())[:1] or t2[0])
                                sets.append((g1, g2))
                            except (ValueError, IndexError):
                                continue
                        if sets and sets_won1 != sets_won2:
                            winner, loser = (p1, p2) if sets_won1 > sets_won2 else (p2, p1)
                            matches.append({
                                'tourney': current_tourney,
                                'date':    date_str,
                                'winner':  winner,
                                'loser':   loser,
                                'winner_norm': _norm(winner),
                                'loser_norm':  _norm(loser),
                                'sets':    sets,
                            })
                i += 2
                continue
            i += 1

        with open(cache_path, 'w') as f:
            json.dump(matches, f)
        return matches
    except Exception as e:
        log.debug('TE fetch error for %s: %s', date_str, e)
        return []


def _last_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[-1] if parts else full_name


def _surname_matches(cb_full_name: str, te_display_name: str) -> bool:
    """TennisExplorer shows 'Surname I.' (e.g. 'Gombos N.'); Cloudbet shows
    'First Last' (e.g. 'Norbert Gombos'). Match on normalized last name."""
    cb_last = _norm(_last_name(cb_full_name))
    te_norm = _norm(te_display_name)
    te_last = te_norm.split()[0] if te_norm.split() else te_norm
    return cb_last == te_last or (len(cb_last) > 3 and cb_last in te_norm)


def find_challenger_result(player1: str, player2: str, ts_str: str) -> dict:
    """Look up a Challenger match result on TennisExplorer for player1 vs
    player2 within +/-3 days of ts_str. Returns a dict with 'winner_norm',
    'sets' etc (same shape as tennis_sibila_resolver's XLSX rows) or None.
    Matches on last name -- TennisExplorer displays 'Surname I.', Cloudbet
    displays 'First Last'."""
    try:
        pick_dt = datetime.strptime(ts_str[:10], '%Y-%m-%d')
    except Exception:
        return None

    for delta in (0, -1, 1, 2, -2, 3, -3):
        day = (pick_dt + timedelta(days=delta)).strftime('%Y-%m-%d')
        for m in _fetch_day(day):
            w, l = m['winner'], m['loser']
            if ((_surname_matches(player1, w) and _surname_matches(player2, l)) or
                    (_surname_matches(player1, l) and _surname_matches(player2, w))):
                return m
    return None
