"""
ganagol_standings.py
League standings + motivation labels via football-data.org TIER_ONE API.
Covers: PL, PD, SA, BL1, FL1, DED, PPL, BRA (=BSA on fd.org).
"""
import json, os, time, urllib.request
from difflib import get_close_matches
from pathlib import Path

# ganagol league code  →  football-data.org competition code
_FD_MAP = {
    'PL':  'PL',
    'PD':  'PD',
    'SA':  'SA',
    'BL1': 'BL1',
    'FL1': 'FL1',
    'DED': 'DED',
    'PPL': 'PPL',
    'BRA': 'BSA',
}

_CACHE_PATH = Path(__file__).parent / '.oraculo_cache' / 'standings_cache.json'
_CACHE_TTL  = 6 * 3600  # 6 hours

_KEY = None


def _api_key():
    global _KEY
    if _KEY:
        return _KEY
    k = os.environ.get('FOOTBALL_DATA_ORG_KEY', '')
    if not k:
        env = Path('/home/noc/oraculo/.env')
        if env.exists():
            for ln in env.read_text().splitlines():
                if ln.startswith('FOOTBALL_DATA_ORG_KEY='):
                    k = ln.split('=', 1)[1].strip().strip('"\'')
                    break
    _KEY = k
    return k


def _get(url, key):
    req = urllib.request.Request(url, headers={'X-Auth-Token': key})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _norm(name):
    """Normalize team name for fuzzy matching."""
    n = name.lower()
    for tok in (' fc', ' sc', ' cf', ' ac', ' as ', ' rc ', ' cd ', ' ud ', ' sd '):
        n = n.replace(tok, ' ')
    # strip leading/trailing common prefixes as standalone words
    n = n.strip()
    return ' '.join(n.split())


def _motivation(pos, total, pts, pts_list):
    """Return short motivation label for display."""
    pts_2nd  = pts_list[1]  if len(pts_list) > 1 else pts
    pts_safe = pts_list[total - 4] if total >= 4 else 0   # top of bottom-3

    bottom3  = pos > (total - 3)
    gap_up   = pts_list[0] - pts        # gap to leader
    gap_down = pts - pts_safe           # gap above relegation

    if bottom3:
        return 'REL'
    if pos == 1 and (pts - pts_2nd) >= 12:
        return 'CAMP'           # title already wrapped up
    if pos <= 3:
        return 'TITLE'
    if pos <= 6:
        return 'EUR'
    if gap_down >= 15:
        return 'SAFE'           # comfortable mid-table
    return 'MID'


def load_standings(force=False):
    """
    Fetch standings for all supported leagues.
    Returns dict: norm_name → {'pos': int, 'total': int, 'mot': str,
                                'league': str, 'name': str, 'pts': int}
    Uses a 6-hour file cache.
    """
    now = time.time()
    if not force and _CACHE_PATH.exists():
        if now - _CACHE_PATH.stat().st_mtime < _CACHE_TTL:
            with open(_CACHE_PATH) as f:
                return json.load(f)

    key = _api_key()
    if not key:
        return {}

    result = {}
    for ganagol_code, fd_code in _FD_MAP.items():
        try:
            url  = f'https://api.football-data.org/v4/competitions/{fd_code}/standings'
            data = _get(url, key)

            # Prefer TOTAL standings table; fall back to first
            table = None
            for s in data.get('standings', []):
                if s.get('type') == 'TOTAL':
                    table = s.get('table', [])
                    break
            if not table:
                standings_list = data.get('standings', [])
                table = standings_list[0].get('table', []) if standings_list else []

            if not table:
                continue

            total    = len(table)
            pts_list = [row.get('points', 0) for row in table]

            for row in table:
                pos  = row['position']
                pts  = row.get('points', 0)
                name = row.get('team', {}).get('name', '')
                if not name:
                    continue

                mot  = _motivation(pos, total, pts, pts_list)
                norm = _norm(name)
                result[norm] = {
                    'pos':    pos,
                    'total':  total,
                    'mot':    mot,
                    'league': ganagol_code,
                    'name':   name,
                    'pts':    pts,
                }
        except Exception as e:
            import sys
            print(f'[standings] {ganagol_code}: {e}', file=sys.stderr)

    if result:
        _CACHE_PATH.parent.mkdir(exist_ok=True)
        with open(_CACHE_PATH, 'w') as f:
            json.dump(result, f)

    return result


def _token_prefix_match(query_tokens, candidate):
    """True if every query token is a prefix of some word in candidate."""
    cand_words = candidate.split()
    for qt in query_tokens:
        if not any(cw.startswith(qt) for cw in cand_words):
            return False
    return True


def lookup_team(team_name, cache):
    """
    Returns standings entry or None.
    Tries (in order): exact norm, containment, token-prefix, difflib fuzzy.
    """
    if not cache:
        return None

    norm = _norm(team_name)
    if norm in cache:
        return cache[norm]

    # Containment match (e.g. "barcelona" in "barcelona sc")
    for key, val in cache.items():
        if norm in key or key in norm:
            return val

    # Token-prefix match: "man city" → "manchester city"
    qtokens = norm.split()
    if len(qtokens) >= 2:
        for key, val in cache.items():
            if _token_prefix_match(qtokens, key):
                return val

    # difflib fuzzy (cutoff 0.72 avoids cross-team false positives)
    keys    = list(cache.keys())
    matches = get_close_matches(norm, keys, n=1, cutoff=0.72)
    if matches:
        return cache[matches[0]]

    return None


def format_tag(info):
    """Short display tag: '#5 EUR' or '#18 REL'"""
    if not info:
        return None
    return '#{} {}'.format(info['pos'], info['mot'])
