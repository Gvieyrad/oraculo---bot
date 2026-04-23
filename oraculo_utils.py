#!/usr/bin/env python3
"""
oraculo_utils.py - Shared utilities for the Oráculo prediction system.

Provides:
- FileCache: Reusable JSON file cache with TTL, LRU eviction, and cleanup
- build_features_for_match: Common feature building logic used by daily_predict and backtest
"""

import os
import json
import time
import hashlib
import logging

log = logging.getLogger('oraculo.utils')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# FileCache - Reusable JSON file cache
# ---------------------------------------------------------------------------

class FileCache:
    """JSON file-based cache with TTL, max file limit, and LRU eviction."""

    def __init__(self, namespace, default_ttl=600, max_files=500):
        """
        Args:
            namespace: Subdirectory name under .oraculo_cache/
            default_ttl: Default time-to-live in seconds
            max_files: Maximum number of cache files before LRU eviction
        """
        self.namespace = namespace
        self.default_ttl = default_ttl
        self.max_files = max_files
        self._dir = os.path.join(SCRIPT_DIR, '.oraculo_cache', namespace)

    def _ensure_dir(self):
        if not os.path.exists(self._dir):
            os.makedirs(self._dir)
        return self._dir

    @staticmethod
    def make_key(endpoint, params, exclude_keys=None):
        """Generate cache key from endpoint + params."""
        if exclude_keys:
            params = {k: v for k, v in params.items() if k not in exclude_keys}
        raw = endpoint + '|' + json.dumps(params, sort_keys=True)
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def get(self, key, ttl=None):
        """Return cached data if fresh, else None."""
        ttl = ttl or self.default_ttl
        path = os.path.join(self._ensure_dir(), '%s.json' % key)
        if not os.path.exists(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            age = time.time() - mtime
            if age > ttl:
                log.debug('Cache expired (%ds old, TTL %ds): %s/%s',
                          int(age), ttl, self.namespace, key[:8])
                return None
            log.debug('Cache hit (%ds old): %s/%s', int(age), self.namespace, key[:8])
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def put(self, key, data):
        """Store data in cache, evicting old files if over limit."""
        d = self._ensure_dir()
        path = os.path.join(d, '%s.json' % key)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            log.warning('Cache write failed [%s]: %s', self.namespace, e)
            return
        self._evict_if_needed(d)

    def _evict_if_needed(self, d):
        """Remove oldest files if cache exceeds max_files."""
        try:
            files = [f for f in os.listdir(d) if f.endswith('.json')]
            if len(files) <= self.max_files:
                return
            # Sort by mtime ascending (oldest first)
            full = [(os.path.join(d, f), os.path.getmtime(os.path.join(d, f)))
                    for f in files]
            full.sort(key=lambda x: x[1])
            to_remove = len(full) - self.max_files
            for path, _ in full[:to_remove]:
                try:
                    os.remove(path)
                except Exception:
                    pass
            log.debug('Evicted %d old cache files from %s', to_remove, self.namespace)
        except Exception as e:
            log.debug('Cache eviction check failed: %s', e)

    def cleanup(self, max_age_hours=48):
        """Remove cache files older than max_age_hours."""
        d = self._ensure_dir()
        cutoff = time.time() - (max_age_hours * 3600)
        count = 0
        for fn in os.listdir(d):
            if not fn.endswith('.json'):
                continue
            fp = os.path.join(d, fn)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    count += 1
            except Exception:
                pass
        if count:
            log.info('Cleaned up %d expired cache files from %s', count, self.namespace)
        return count

    def clear(self):
        """Remove all cached files."""
        d = self._ensure_dir()
        count = 0
        for fn in os.listdir(d):
            if fn.endswith('.json'):
                try:
                    os.remove(os.path.join(d, fn))
                    count += 1
                except Exception:
                    pass
        log.info('Cleared %d cache files from %s', count, self.namespace)
        return count

    def stats(self):
        """Return cache stats: file count, total size, oldest/newest mtime."""
        d = self._ensure_dir()
        files = [f for f in os.listdir(d) if f.endswith('.json')]
        if not files:
            return {'count': 0, 'size_kb': 0, 'oldest': None, 'newest': None}
        total_size = 0
        oldest = float('inf')
        newest = 0
        for fn in files:
            fp = os.path.join(d, fn)
            try:
                st = os.stat(fp)
                total_size += st.st_size
                oldest = min(oldest, st.st_mtime)
                newest = max(newest, st.st_mtime)
            except Exception:
                pass
        return {
            'count': len(files),
            'size_kb': round(total_size / 1024, 1),
            'oldest': oldest if oldest != float('inf') else None,
            'newest': newest if newest else None,
        }


# ---------------------------------------------------------------------------
# Shared feature building (used by daily_predict and backtest)
# ---------------------------------------------------------------------------

_standings_cache = {}


def get_standings_cached(comp_code):
    """Get standings with in-memory cache for the run."""
    if comp_code not in _standings_cache:
        from oraculo_football import get_standings
        _standings_cache[comp_code] = get_standings(comp_code)
    return _standings_cache[comp_code]


def find_team_in_standings(standings, team_id):
    """Find a team row in standings by team_id."""
    for row in standings:
        if row.get('team_id') == team_id:
            return row
    return None


def build_features_for_match(match):
    """
    Build complete feature set for a match.
    Fetches team matches, standings, and H2H data.

    Returns:
        (feature_dict, feature_vector) or (None, None) on failure
    """
    from oraculo_football import get_team_matches, get_head_to_head
    from oraculo_football_features import build_match_features, features_to_vector

    home_id = match.get('home_id', 0)
    away_id = match.get('away_id', 0)
    comp_code = match.get('competition_code', '')

    if not home_id or not away_id:
        log.warning('Missing team IDs for match %s', match.get('id'))
        return None, None

    home_matches = get_team_matches(home_id, last_n=10)
    away_matches = get_team_matches(away_id, last_n=10)

    standings = get_standings_cached(comp_code) if comp_code else []
    home_row = find_team_in_standings(standings, home_id)
    away_row = find_team_in_standings(standings, away_id)

    h2h = None
    match_id = match.get('id', 0)
    if match_id:
        h2h = get_head_to_head(match_id)

    try:
        features = build_match_features(
            match, home_matches, away_matches,
            home_row, away_row, h2h
        )
        vector = features_to_vector(features)
        return features, vector
    except Exception as e:
        log.debug('Feature build failed for match %s: %s', match_id, e)
        return None, None
