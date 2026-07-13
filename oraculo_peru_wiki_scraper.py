#!/usr/bin/env python3
"""
oraculo_peru_wiki_scraper.py - Fills the 2025/2026 Liga 1 Peru gap in PER_all.json.

Why: football-data.co.uk free tier has no Peru data past 2024 (see dl_peru.log:
"2025: SKIP -- Free plans do not have access to this season"). This scrapes
Wikipedia's "Sports results" matrix template (no API key, no cost, read-only)
for the season pages and merges into the existing cache, keeping the same
schema as the football-data.co.uk-sourced 2022-2024 records.

Limitations (both fields are decorative -- grep-confirmed unused by
oraculo_peru.build_peru_features/predict_peru, which only read home_team/
away_team/home_score/away_score for form calc, and venue_city only for the
live prediction target, never from historical context records):
  - date: Wikipedia's matrix has no per-match dates. Assigns a placeholder
    (season start of Apertura/Clausura window).
  - venue_name: not available from the matrix. Left blank.
  - venue_city: approximated as the home team's home city (won't capture
    rare neutral-venue matches).
"""
import json
import os
import re
import shutil
import sys
import time
from urllib.request import Request, urlopen
from urllib.parse import quote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'csv', 'PER_all.json')
WIKI_API = 'https://en.wikipedia.org/w/api.php'
SEASONS = (2025, 2026)

# Wikipedia display name -> canonical name used in the existing PER_all.json /
# oraculo_peru.TEAM_CITY (kept identical so historical form-lookup by exact
# string match stays consistent across old and new records).
NAME_ALIASES = {
    'Alianza Atlético': 'Alianza Atletico',
    'Atlético Grau': 'Atletico Grau',
    'Ayacucho': 'Ayacucho FC',
    'Binacional': 'Deportivo Binacional',
    'Los Chankas': 'Club Deportivo Los Chankas',
    'Melgar': 'FBC Melgar',
    'UTC': 'UTC Cajamarca',
    'Juan Pablo II College': 'Juan Pablo II',
    'Cajamarca': 'FC Cajamarca',
}

# Home city per canonical team name (used only for the venue_altitude feature
# of that specific historical match -- not consumed for form lookups).
TEAM_CITY = {
    'ADT': 'Tarma', 'Alianza Atletico': 'Sullana', 'Alianza Lima': 'Lima',
    'Alianza Universidad': 'Huanuco', 'Atletico Grau': 'Piura',
    'Ayacucho FC': 'Ayacucho', 'Deportivo Binacional': 'Juliaca',
    'Cienciano': 'Cusco', 'Comerciantes Unidos': 'Cutervo', 'Cusco': 'Cusco',
    'Deportivo Garcilaso': 'Cusco', 'Juan Pablo II': 'Cajamarca',
    'Club Deportivo Los Chankas': 'Andahuaylas', 'FBC Melgar': 'Arequipa',
    'Sport Boys': 'Callao', 'Sport Huancayo': 'Huancayo',
    'Sporting Cristal': 'Lima', 'Universitario': 'Lima', 'UTC Cajamarca': 'Cajamarca',
    'FC Cajamarca': 'Cajamarca', 'Deportivo Moquegua': 'Moquegua',
}

PHASE_DATE_SUFFIX = {'Apertura': '-03-01', 'Clausura': '-08-15'}

WIKILINK_RE = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
SCORE_RE = re.compile(r'(\d+)[–\-](\d+)')


def fetch_wikitext(page):
    url = f'{WIKI_API}?action=parse&page={quote(page)}&prop=wikitext&format=json&formatversion=2'
    req = Request(url)
    req.add_header('User-Agent', 'oraculo-peru-data-refresh/1.0 (internal research tool)')
    data = json.loads(urlopen(req, timeout=20).read())
    if 'error' in data:
        raise RuntimeError(data['error'])
    return data['parse']['wikitext']


def clean_wikilink(raw):
    """'[[Article|Display]]' or '[[Article]]' -> display text, unwrapping an
    outer '{{nowrap|...}}' template first if present. Plain text passes through."""
    raw = raw.strip()
    if raw.startswith('{{nowrap|') and raw.endswith('}}'):
        raw = raw[len('{{nowrap|'):-2].strip()
    m = WIKILINK_RE.match(raw)
    if not m:
        return raw
    return (m.group(2) or m.group(1)).strip()


def canonical_name(raw_value):
    display = clean_wikilink(raw_value)
    return NAME_ALIASES.get(display, display)


def parse_results_blocks(wikitext, season):
    """Find every {{#invoke:Sports results|main ...}} block, tag it with the
    nearest preceding Apertura/Clausura heading, and extract match results."""
    matches = []
    dropped_unparsed = 0
    for block_m in re.finditer(r'\{\{#invoke:Sports results\|main(.*?)\n\}\}', wikitext, re.S):
        block = block_m.group(1)
        preceding = wikitext[:block_m.start()]
        phase = 'Clausura' if preceding.rfind('Clausura') > preceding.rfind('Apertura') else 'Apertura'

        names = {}
        for nm in re.finditer(r'^\|name_([A-Za-z0-9]+)=(.+)$', block, re.M):
            names[nm.group(1)] = canonical_name(nm.group(2))

        for mm in re.finditer(r'^\|match_([A-Za-z0-9]+)_([A-Za-z0-9]+)=(.+)$', block, re.M):
            home_abbr, away_abbr, raw_score = mm.group(1), mm.group(2), mm.group(3).strip()
            if raw_score.lower() == 'null' or raw_score in ('', '—', '-'):
                continue
            score_text = clean_wikilink(raw_score)
            sm = SCORE_RE.search(score_text)
            if not sm:
                dropped_unparsed += 1
                continue
            home = names.get(home_abbr)
            away = names.get(away_abbr)
            if not home or not away:
                continue
            matches.append({
                'home_team': home,
                'away_team': away,
                'home_score': int(sm.group(1)),
                'away_score': int(sm.group(2)),
                'date': f'{season}{PHASE_DATE_SUFFIX.get(phase, "-01-01")}',
                'venue_city': TEAM_CITY.get(home, ''),
                'venue_name': '',
                'season': season,
            })
    if dropped_unparsed:
        print(f'  ({dropped_unparsed} cell(s) with unparseable score text, skipped)')
    return matches


def scrape_season(season):
    page = f'{season}_Liga_1_(Peru)'
    wt = fetch_wikitext(page)
    return parse_results_blocks(wt, season)


def main():
    dry_run = '--dry-run' in sys.argv

    all_new = []
    for season in SEASONS:
        try:
            m = scrape_season(season)
            print(f'{season}: {len(m)} results parsed')
            all_new.extend(m)
        except Exception as e:
            print(f'{season}: FAILED - {e}')

    if not all_new:
        print('No data scraped from any season -- aborting, cache untouched.')
        return 1

    existing = []
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            existing = json.load(f)
    kept = [m for m in existing if m.get('season') not in SEASONS]
    merged = kept + all_new
    print(f'existing kept (outside {SEASONS}): {len(kept)} | new: {len(all_new)} | total: {len(merged)}')

    if dry_run:
        print('--dry-run: not writing to cache file.')
        for s in SEASONS:
            sample = [m for m in all_new if m['season'] == s][:3]
            print(f'  sample {s}:', sample)
        return 0

    if os.path.exists(CACHE_FILE):
        backup = CACHE_FILE + f'.bak_before_wiki_merge_{int(time.time())}'
        shutil.copy(CACHE_FILE, backup)
        print('backup:', backup)

    with open(CACHE_FILE, 'w') as f:
        json.dump(merged, f)
    print('wrote', CACHE_FILE)
    return 0


if __name__ == '__main__':
    sys.exit(main())
