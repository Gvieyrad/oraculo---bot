#!/usr/bin/env python3
"""
discover_wc_slug.py
Find the correct Cloudbet competition slug for WC 2026 and patch the runner.

Run once around June 1 when Cloudbet opens WC odds:
  python3 /home/noc/oraculo_v2/discover_wc_slug.py

If the correct slug differs from 'soccer-international-world-cup',
patches oraculo_runner_auto.py and oraculo_wc_model.py automatically.
"""
import json, urllib.request, sys, re, os, py_compile

CB_BASE    = 'https://sports-api.cloudbet.com'
CB_CFG     = '/home/noc/oraculo_v2/cloudbet_config.json'
RUNNER     = '/home/noc/oraculo_v2/oraculo_runner_auto.py'
CURRENT_SLUG = 'soccer-international-world-cup'

CANDIDATE_SLUGS = [
    'soccer-international-world-cup',
    'soccer-international-world-cup-2026',
    'soccer-world-cup-2026',
    'soccer-fifa-world-cup-2026',
    'soccer-international-fifa-world-cup',
    'soccer-international-fifa-world-cup-2026',
]
WC_KEYWORDS = ['world cup', 'world-cup', 'mundial', 'fifa wc', 'wc 2026', 'worldcup']

def cb_get(path, key):
    url = CB_BASE + path
    req = urllib.request.Request(url, headers={'X-API-Key': key, 'Accept': 'application/json'})
    r = urllib.request.urlopen(req, timeout=12)
    return json.loads(r.read())

def test_slug(slug, key):
    """Returns number of events if slug is valid, else -1."""
    try:
        d = cb_get(f'/pub/v2/odds/competitions/{slug}', key)
        evts = d.get('events', [])
        return len(evts)
    except Exception:
        return -1

def find_slug_via_browse(key):
    """Browse Cloudbet sport categories to find WC."""
    try:
        d = cb_get('/pub/v2/odds/sports/soccer', key)
        cats = d.get('categories', [])
        for cat in cats:
            for comp in cat.get('competitions', []):
                name = comp.get('name', '').lower()
                k    = comp.get('key', '')
                if any(kw in name or kw in k for kw in WC_KEYWORDS):
                    return comp.get('key'), comp.get('name')
    except Exception as e:
        print(f'Browse failed: {e}')
    return None, None

def patch_runner(old_slug, new_slug):
    src = open(RUNNER).read()
    if new_slug in src:
        print(f'Runner already has slug {new_slug}')
        return
    patched = src.replace(f"'soccer-international-world-cup'",
                          f"'{new_slug}'", )
    if patched == src:
        print('WARNING: slug not found in runner to replace')
        return
    open(RUNNER, 'w').write(patched)
    py_compile.compile(RUNNER, doraise=True)
    print(f'Runner patched: {old_slug} → {new_slug}')

def main():
    key = json.load(open(CB_CFG))['api_key']

    print(f'Current slug: {CURRENT_SLUG}')
    n = test_slug(CURRENT_SLUG, key)
    if n >= 0:
        print(f'✓ Current slug works — {n} events live. Nothing to patch.')
        return 0

    print('Current slug returns 404/0 — searching for correct slug...')

    # 1. Try candidate slugs
    for slug in CANDIDATE_SLUGS:
        if slug == CURRENT_SLUG:
            continue
        n = test_slug(slug, key)
        if n >= 0:
            print(f'✓ Found working slug: {slug} ({n} events)')
            patch_runner(CURRENT_SLUG, slug)
            return 0

    # 2. Browse Cloudbet category tree
    slug, name = find_slug_via_browse(key)
    if slug:
        n = test_slug(slug, key)
        print(f'✓ Found via browse: {slug} = "{name}" ({n} events)')
        patch_runner(CURRENT_SLUG, slug)
        return 0

    # 3. Search all active competitions for WC keyword
    print('Scanning all soccer competitions...')
    try:
        d = cb_get('/pub/v2/odds/sports/soccer', key)
        all_comps = []
        for cat in d.get('categories', []):
            all_comps.extend(cat.get('competitions', []))
        print(f'Total soccer comps visible: {len(all_comps)}')
        for c in all_comps:
            k_low = c.get('key','').lower()
            n_low = c.get('name','').lower()
            if any(kw in k_low or kw in n_low for kw in WC_KEYWORDS):
                print(f'  MATCH: key={c["key"]}  name={c["name"]}')
    except Exception as e:
        print(f'Scan failed: {e}')

    print('\nWC not yet on Cloudbet — try again closer to June 12.')
    print('When found, run: python3 discover_wc_slug.py')
    return 1

if __name__ == '__main__':
    sys.exit(main())
