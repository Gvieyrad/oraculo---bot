#!/usr/bin/env python3
"""
wc_player_intel.py
Player intelligence for WC 2026: injuries, form, team cohesion.

Pipeline:
  1. Google News RSS per team  → recent headlines (injuries, squad issues)
  2. Ollama gemma4             → structured injury extraction
  3. Key player weights        → compute team xG factor (0.70 - 1.05)
  4. Write wc_player_factors.json

Run daily from cron (same window as update_wc_standings.py):
  0 */6 * * * cd /home/noc/oraculo_v2 && python3 wc_player_intel.py >> logs/wc_intel.log 2>&1
"""
import json, os, re, time, logging, datetime, urllib.request, urllib.parse, unicodedata

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('wc_intel')

BASE          = '/home/noc/oraculo_v2/wc2026'
FACTORS_FILE  = os.path.join(BASE, 'wc_player_factors.json')
GROUPS_FILE   = os.path.join(BASE, 'wc2026_groups.json')
NEWS_CACHE    = os.path.join(BASE, 'wc_news_cache.json')


# ── Key players per team ──────────────────────────────────────────────────────
# Format: {team: [{name, role, weight, notes}]}
# role: 'attack' | 'defense' | 'mid'
# weight: fraction of team's attack/defense lost if absent (0.0–0.30)
KEY_PLAYERS = {
    "Argentina":        [{"name": "Julián Álvarez",   "role": "attack",  "weight": 0.20},
                         {"name": "Lionel Messi",     "role": "attack",  "weight": 0.15},
                         {"name": "Rodrigo De Paul",  "role": "mid",     "weight": 0.10}],
    "France":           [{"name": "Kylian Mbappé",    "role": "attack",  "weight": 0.25},
                         {"name": "Antoine Griezmann","role": "mid",     "weight": 0.12},
                         {"name": "Aurélien Tchouaméni","role":"mid",    "weight": 0.08}],
    "Brazil":           [{"name": "Vinícius Jr",      "role": "attack",  "weight": 0.22},
                         {"name": "Rodrygo",          "role": "attack",  "weight": 0.12},
                         {"name": "Lucas Paquetá",    "role": "mid",     "weight": 0.10}],
    "England":          [{"name": "Harry Kane",       "role": "attack",  "weight": 0.20},
                         {"name": "Jude Bellingham",  "role": "mid",     "weight": 0.18},
                         {"name": "Bukayo Saka",      "role": "attack",  "weight": 0.10}],
    "Spain":            [{"name": "Lamine Yamal",     "role": "attack",  "weight": 0.20},
                         {"name": "Pedri",            "role": "mid",     "weight": 0.12},
                         {"name": "Álvaro Morata",    "role": "attack",  "weight": 0.10}],
    "Germany":          [{"name": "Florian Wirtz",    "role": "attack",  "weight": 0.20},
                         {"name": "Jamal Musiala",    "role": "mid",     "weight": 0.15},
                         {"name": "Kai Havertz",      "role": "attack",  "weight": 0.10}],
    "Netherlands":      [{"name": "Cody Gakpo",       "role": "attack",  "weight": 0.18},
                         {"name": "Virgil van Dijk",  "role": "defense", "weight": 0.15},
                         {"name": "Memphis Depay",    "role": "attack",  "weight": 0.10}],
    "Portugal":         [{"name": "Rafael Leão",      "role": "attack",  "weight": 0.20},
                         {"name": "Bernardo Silva",   "role": "mid",     "weight": 0.15},
                         {"name": "Cristiano Ronaldo","role": "attack",  "weight": 0.10}],
    "Morocco":          [{"name": "Youssef En-Nesyri","role": "attack",  "weight": 0.20},
                         {"name": "Hakim Ziyech",     "role": "attack",  "weight": 0.15},
                         {"name": "Sofyan Amrabat",   "role": "mid",     "weight": 0.10}],
    "Japan":            [{"name": "Takefusa Kubo",    "role": "attack",  "weight": 0.20},
                         {"name": "Kaoru Mitoma",     "role": "attack",  "weight": 0.18},
                         {"name": "Wataru Endō",      "role": "mid",     "weight": 0.10}],
    "Belgium":          [{"name": "Romelu Lukaku",    "role": "attack",  "weight": 0.18},
                         {"name": "Kevin De Bruyne",  "role": "mid",     "weight": 0.20},
                         {"name": "Jeremy Doku",      "role": "attack",  "weight": 0.10}],
    "USA":              [{"name": "Christian Pulisic","role": "attack",  "weight": 0.22},
                         {"name": "Gio Reyna",        "role": "mid",     "weight": 0.12},
                         {"name": "Tim Weah",         "role": "attack",  "weight": 0.10}],
    "Mexico":           [{"name": "Santiago Giménez", "role": "attack",  "weight": 0.22},
                         {"name": "Hirving Lozano",   "role": "attack",  "weight": 0.12},
                         {"name": "Edson Álvarez",    "role": "mid",     "weight": 0.10}],
    "Canada":           [{"name": "Alphonso Davies",  "role": "attack",  "weight": 0.25},
                         {"name": "Jonathan David",   "role": "attack",  "weight": 0.20},
                         {"name": "Tajon Buchanan",   "role": "attack",  "weight": 0.10}],
    "Australia":        [{"name": "Mathew Leckie",    "role": "attack",  "weight": 0.18},
                         {"name": "Mitchell Duke",    "role": "attack",  "weight": 0.12}],
    "Croatia":          [{"name": "Luka Modrić",      "role": "mid",     "weight": 0.20},
                         {"name": "Bruno Petković",   "role": "attack",  "weight": 0.12},
                         {"name": "Ivan Perišić",     "role": "attack",  "weight": 0.10}],
    "Uruguay":          [{"name": "Darwin Núñez",     "role": "attack",  "weight": 0.22},
                         {"name": "Federico Valverde","role": "mid",     "weight": 0.18},
                         {"name": "Luis Suárez",      "role": "attack",  "weight": 0.08}],
    "Colombia":         [{"name": "Luis Díaz",        "role": "attack",  "weight": 0.22},
                         {"name": "James Rodríguez",  "role": "mid",     "weight": 0.15},
                         {"name": "Jhon Córdoba",     "role": "attack",  "weight": 0.10}],
    "Senegal":          [{"name": "Sadio Mané",       "role": "attack",  "weight": 0.22},
                         {"name": "Nicolas Jackson",  "role": "attack",  "weight": 0.15},
                         {"name": "Idrissa Gana Gueye","role":"mid",    "weight": 0.10}],
    "Ecuador":          [{"name": "Enner Valencia",   "role": "attack",  "weight": 0.20},
                         {"name": "Moisés Caicedo",   "role": "mid",     "weight": 0.15}],
    "Switzerland":      [{"name": "Breel Embolo",     "role": "attack",  "weight": 0.18},
                         {"name": "Granit Xhaka",     "role": "mid",     "weight": 0.15},
                         {"name": "Xherdan Shaqiri",  "role": "attack",  "weight": 0.10}],
    "Austria":          [{"name": "Marcel Sabitzer",  "role": "mid",     "weight": 0.18},
                         {"name": "Marko Arnautovic", "role": "attack",  "weight": 0.15}],
    "Norway":           [{"name": "Erling Haaland",   "role": "attack",  "weight": 0.35},
                         {"name": "Martin Ødegaard",  "role": "mid",     "weight": 0.18}],
    "Sweden":           [{"name": "Victor Lindelöf",  "role": "defense", "weight": 0.12},
                         {"name": "Alexander Isak",   "role": "attack",  "weight": 0.22},
                         {"name": "Dejan Kulusevski", "role": "attack",  "weight": 0.12}],
    "Scotland":         [{"name": "Andy Robertson",   "role": "defense", "weight": 0.15},
                         {"name": "Scott McTominay",  "role": "mid",     "weight": 0.15},
                         {"name": "Che Adams",        "role": "attack",  "weight": 0.12}],
    "Algeria":          [{"name": "Islam Slimani",    "role": "attack",  "weight": 0.15},
                         {"name": "Riyad Mahrez",     "role": "attack",  "weight": 0.22}],
    "Egypt":            [{"name": "Mohamed Salah",    "role": "attack",  "weight": 0.35},
                         {"name": "Omar Marmoush",    "role": "attack",  "weight": 0.15}],
    "Tunisia":          [{"name": "Wahbi Khazri",     "role": "attack",  "weight": 0.18},
                         {"name": "Youssef Msakni",   "role": "attack",  "weight": 0.12}],
    "Ghana":            [{"name": "Jordan Ayew",      "role": "attack",  "weight": 0.15},
                         {"name": "Mohammed Kudus",   "role": "mid",     "weight": 0.20}],
    "Ivory Coast":      [{"name": "Sébastien Haller", "role": "attack",  "weight": 0.18},
                         {"name": "Nicolas Pépé",     "role": "attack",  "weight": 0.12},
                         {"name": "Franck Kessié",    "role": "mid",     "weight": 0.12}],
    "South Africa":     [{"name": "Percy Tau",        "role": "attack",  "weight": 0.20},
                         {"name": "Lyle Foster",      "role": "attack",  "weight": 0.15}],
    "DR Congo":         [{"name": "Cédric Bakambu",   "role": "attack",  "weight": 0.18},
                         {"name": "Chancel Mbemba",   "role": "defense", "weight": 0.12}],
    "Iran":             [{"name": "Sardar Azmoun",    "role": "attack",  "weight": 0.20},
                         {"name": "Mehdi Taremi",     "role": "attack",  "weight": 0.18}],
    "Saudi Arabia":     [{"name": "Salem Al-Dawsari", "role": "attack",  "weight": 0.20},
                         {"name": "Firas Al-Buraikan","role": "attack",  "weight": 0.15}],
    "Republic of Korea":[{"name": "Son Heung-min",    "role": "attack",  "weight": 0.30},
                         {"name": "Lee Kang-in",      "role": "mid",     "weight": 0.15}],
    "Qatar":            [{"name": "Akram Afif",       "role": "attack",  "weight": 0.22},
                         {"name": "Almoez Ali",       "role": "attack",  "weight": 0.15}],
    "Iraq":             [{"name": "Aymen Hussein",    "role": "attack",  "weight": 0.18},
                         {"name": "Amjed Attwan",     "role": "mid",     "weight": 0.12}],
    "Jordan":           [{"name": "Yazan Al-Naimat",  "role": "attack",  "weight": 0.18},
                         {"name": "Ahmad Hayel",      "role": "mid",     "weight": 0.12}],
    "Uzbekistan":       [{"name": "Eldor Shomurodov", "role": "attack",  "weight": 0.22},
                         {"name": "Jaloliddin Masharipov","role":"mid",  "weight": 0.15}],
    "New Zealand":      [{"name": "Chris Wood",       "role": "attack",  "weight": 0.25},
                         {"name": "Clayton Lewis",    "role": "mid",     "weight": 0.12}],
    "Cape Verde":       [{"name": "Garry Rodrigues",  "role": "attack",  "weight": 0.20},
                         {"name": "Ryan Mendes",      "role": "attack",  "weight": 0.15}],
    "Panama":           [{"name": "Ismael Díaz",      "role": "attack",  "weight": 0.18},
                         {"name": "Rolando Blackburn","role": "attack",  "weight": 0.12}],
    "Haiti":            [{"name": "Frantzdy Pierrot", "role": "attack",  "weight": 0.18},
                         {"name": "James Léandre",    "role": "mid",     "weight": 0.12}],
    "Paraguay":         [{"name": "Miguel Almirón",   "role": "mid",     "weight": 0.20},
                         {"name": "Antonio Sanabria", "role": "attack",  "weight": 0.18}],
    "Czech Republic":   [{"name": "Tomáš Souček",     "role": "mid",     "weight": 0.18},
                         {"name": "Patrik Schick",    "role": "attack",  "weight": 0.22}],
    "Turkey":           [{"name": "Hakan Çalhanoğlu", "role": "mid",     "weight": 0.20},
                         {"name": "Arda Güler",       "role": "attack",  "weight": 0.20},
                         {"name": "Kerem Aktürkoğlu", "role": "attack",  "weight": 0.12}],
    "Bosnia & Herzegovina":[{"name":"Edin Džeko",     "role": "attack",  "weight": 0.18},
                         {"name": "Miralem Pjanić",   "role": "mid",     "weight": 0.15}],
    "Curacao":          [{"name": "Cuco Martina",     "role": "defense", "weight": 0.12},
                         {"name": "Leandro Bacuna",   "role": "mid",     "weight": 0.15}],
}

# ── News fetching ─────────────────────────────────────────────────────────────
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) WC-intel-bot/1.0'}

# Alternate team names for Google News search
SEARCH_NAMES = {
    'Republic of Korea': 'South Korea',
    'USA':               'United States soccer',
    'Bosnia & Herzegovina': 'Bosnia Herzegovina football',
    'DR Congo':          'Congo DR football',
    'Ivory Coast':       'Ivory Coast football Cote d\'Ivoire',
    'Curacao':           'Curaçao football',
}

def fetch_team_news(team, max_articles=8):
    """Fetch recent news headlines for a WC team via Google News RSS."""
    search_name = SEARCH_NAMES.get(team, team)
    query = urllib.parse.quote(f'{search_name} injury World Cup 2026')
    url   = f'https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        r   = urllib.request.urlopen(req, timeout=10)
        txt = r.read().decode('utf-8', errors='replace')
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', txt)
        if not titles:
            titles = re.findall(r'<title>(.*?)</title>', txt)
        # Filter relevant ones
        kws = ['injur', 'doubt', 'miss', 'out', 'ruled', 'suspend', 'ban',
               'withdraw', 'concern', 'fit', 'problem', 'squad', 'world cup']
        relevant = [t for t in titles[1:] if any(k in t.lower() for k in kws)]
        return relevant[:max_articles]
    except Exception as e:
        log.debug('News fetch failed for %s: %s', team, e)
        return []

# ── Fast keyword extraction ───────────────────────────────────────────────────
INJURY_KWS = [
    'injur', 'ruled out', 'miss', 'doubt', 'suspen', 'fitness concern',
    'not available', 'sidelined', 'unavailable', 'withdrawal', 'withdrew',
    'pulled out', 'fracture', 'torn', 'hamstring', 'ankle', 'knee',
    'surgery', 'operation', 'personal issue', 'family issue',
    'out of world cup', 'out of squad', 'leaves squad',
]
RULED_OUT_KWS = [
    'ruled out', 'confirmed out', 'will not play', "won't play",
    'misses tournament', 'out of world cup', 'withdraws from squad',
    'not in squad', 'leaves the squad', 'definitely out',
]

def _strip_accents(s):
    # 2026-06-16: normaliza acentos — 'mbappe' matchea 'mbappe' en titulares en ingles
    return ''.join(c for c in unicodedata.normalize('NFD', (s or '').lower())
                   if unicodedata.category(c) != 'Mn')

def extract_injuries_llm(team, headlines, key_players):
    """Keyword-based injury extraction — fast, no LLM required."""
    if not headlines:
        return []
    results = []
    for kp in key_players:
        name_parts = [_strip_accents(p) for p in kp['name'].split() if len(p) > 3]
        hits = []
        for h in headlines:
            h_low = _strip_accents(h)
            if any(part in h_low for part in name_parts):
                if any(kw in h_low for kw in INJURY_KWS):
                    hits.append(h)
        if not hits:
            continue
        is_ruled_out = any(any(kw in h.lower() for kw in RULED_OUT_KWS) for h in hits)
        status     = 'injured' if is_ruled_out else 'doubtful'
        confidence = min(0.90, 0.50 + len(hits) * 0.10)
        results.append({
            'player':     kp['name'],
            'status':     status,
            'confidence': confidence,
            'note':       f'{len(hits)} hit(s): {hits[0][:80]}',
        })
    return results

# ── Factor calculation ────────────────────────────────────────────────────────
def calculate_team_factor(team, injury_data, key_players):
    """
    Compute (attack_factor, defense_factor) for a team.
    attack_factor:  1.0 = full strength, 0.70 = heavily weakened
    defense_factor: 1.0 = full strength, 0.80 = weakened defense

    attack_factor applied to home_xg/away_xg.
    defense_factor applied to OPPONENT's xg (weakened defense = more goals conceded).
    """
    attack_factor  = 1.0
    defense_factor = 1.0
    concerns       = []

    injured_names = {
        r['player'].lower(): r
        for r in injury_data
        if r.get('status') in ('injured', 'doubtful', 'suspended')
        and r.get('confidence', 0) >= 0.5
    }

    for kp in key_players:
        name_low = kp['name'].lower()
        # Check if any injury report matches this player
        matched = None
        for iname, irec in injured_names.items():
            # Fuzzy: last name match or full name
            parts = name_low.split()
            if any(p in iname for p in parts if len(p) > 3):
                matched = irec
                break

        if matched:
            status = matched.get('status', 'unknown')
            conf   = matched.get('confidence', 0.5)
            w      = kp['weight'] * conf  # scale by confidence

            if kp['role'] == 'attack':
                attack_factor  *= (1.0 - w)
            elif kp['role'] == 'defense':
                defense_factor *= (1.0 - w)
            elif kp['role'] == 'mid':
                # Midfield: split impact
                attack_factor  *= (1.0 - w * 0.6)
                defense_factor *= (1.0 - w * 0.4)

            concerns.append({
                'player': kp['name'],
                'status': status,
                'confidence': conf,
                'impact': f'-{w*100:.0f}% {"attack" if kp["role"]!="defense" else "defense"}',
                'note': matched.get('note', ''),
            })
            log.info('  %s [%s]: %s %s conf=%.0f%% → factor %.3f/%.3f',
                     team, kp['name'], status, kp['role'],
                     conf*100, attack_factor, defense_factor)

    # Clamp to sane range
    attack_factor  = max(0.60, min(1.05, attack_factor))
    defense_factor = max(0.75, min(1.05, defense_factor))

    return round(attack_factor, 4), round(defense_factor, 4), concerns

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    groups = json.load(open(GROUPS_FILE))
    all_teams = sorted({t for teams in groups.values() for t in teams})

    # Load existing factors (to preserve manual overrides)
    existing = {}
    if os.path.exists(FACTORS_FILE):
        existing = json.load(open(FACTORS_FILE))

    # Load or init news cache
    news_cache = {}
    if os.path.exists(NEWS_CACHE):
        try:
            nc = json.load(open(NEWS_CACHE))
            # Invalidate if > 6h old
            if (datetime.datetime.now() - datetime.datetime.fromisoformat(nc.get('ts','2000-01-01'))).total_seconds() < 21600:
                news_cache = nc.get('data', {})
                log.info('Using cached news (%d teams)', len(news_cache))
        except Exception:
            pass

    factors = {}
    refresh_news = len(news_cache) < 5  # fetch fresh if cache empty

    for team in all_teams:
        kp = KEY_PLAYERS.get(team, [])

        # Fetch news if needed
        if refresh_news or team not in news_cache:
            headlines = fetch_team_news(team)
            news_cache[team] = headlines
            time.sleep(0.5)  # gentle rate limit
        else:
            headlines = news_cache.get(team, [])

        # Extract injuries via LLM
        injury_data = []
        if headlines and kp:
            injury_data = extract_injuries_llm(team, headlines, kp)

        # Calculate factor
        if kp:
            atk_f, def_f, concerns = calculate_team_factor(team, injury_data, kp)
        else:
            atk_f, def_f, concerns = 1.0, 1.0, []

        # Respect manual override flag
        prev = existing.get(team, {})
        if prev.get('manual_override'):
            factors[team] = prev
            log.info('%s: manual override kept (factor %.2f)', team, prev.get('attack_factor', 1.0))
            continue

        factors[team] = {
            'attack_factor':  atk_f,
            'defense_factor': def_f,
            'concerns':       concerns,
            'headlines':      headlines[:4],
            'key_players':    [p['name'] for p in kp],
            'updated':        datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        }

        if concerns:
            log.info('%s: atk=%.2f def=%.2f concerns=%s',
                     team, atk_f, def_f, [c['player'] for c in concerns])

    # Save news cache
    with open(NEWS_CACHE, 'w') as f:
        json.dump({'ts': datetime.datetime.now().isoformat(), 'data': news_cache}, f)

    # Save factors
    with open(FACTORS_FILE, 'w') as f:
        json.dump(factors, f, indent=2, ensure_ascii=False)
    log.info('Wrote %s (%d teams)', FACTORS_FILE, len(factors))

    # Summary
    alerts = [(t, d) for t, d in factors.items() if d.get('attack_factor', 1.0) < 0.90 or d.get('concerns')]
    if alerts:
        log.info('=== INJURY ALERTS ===')
        for team, d in sorted(alerts, key=lambda x: x[1].get('attack_factor', 1.0)):
            log.info('  %-25s atk=%.2f  def=%.2f  %s',
                     team, d.get('attack_factor',1.0), d.get('defense_factor',1.0),
                     [c['player'] for c in d.get('concerns',[])])
    else:
        log.info('No injury alerts at this time.')

    return 0

if __name__ == '__main__':
    import sys
    sys.exit(main())
