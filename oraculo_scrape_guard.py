"""Scraping resilience — monitors scraper health, fallbacks, and alerts."""
import os, json, logging, time
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_FILE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'scrape_health.json')


class ScrapeGuard:
    """Monitor scraper health and provide fallbacks."""

    def __init__(self):
        self.health = self._load_health()

    def _load_health(self):
        if os.path.exists(HEALTH_FILE):
            try:
                with open(HEALTH_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_health(self):
        os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
        with open(HEALTH_FILE, 'w') as f:
            json.dump(self.health, f, indent=2)

    def record_success(self, source, items_count):
        """Record successful scrape."""
        self.health[source] = {
            'status': 'OK',
            'last_success': datetime.utcnow().isoformat(),
            'items': items_count,
            'consecutive_failures': 0,
        }
        self._save_health()

    def record_failure(self, source, error_msg):
        """Record failed scrape."""
        prev = self.health.get(source, {})
        fails = prev.get('consecutive_failures', 0) + 1
        self.health[source] = {
            'status': 'FAILED',
            'last_failure': datetime.utcnow().isoformat(),
            'last_success': prev.get('last_success', 'never'),
            'error': str(error_msg)[:200],
            'consecutive_failures': fails,
        }
        self._save_health()

        if fails >= 3:
            log.error('[ScrapeGuard] %s has failed %d consecutive times: %s',
                     source, fails, error_msg)

    def is_healthy(self, source, max_age_hours=48):
        """Check if a source is healthy (had recent success)."""
        info = self.health.get(source, {})
        if info.get('status') != 'OK':
            return False
        last = info.get('last_success', '')
        if not last:
            return False
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            return age < max_age_hours * 3600
        except Exception:
            return False

    def get_status_report(self):
        """Get health status of all scrapers."""
        report = {}
        for source, info in self.health.items():
            report[source] = {
                'status': info.get('status', 'UNKNOWN'),
                'last_success': info.get('last_success', 'never'),
                'failures': info.get('consecutive_failures', 0),
            }
        return report


def guarded_fetch(source_name, fetch_fn, *args, **kwargs):
    """Wrapper that records success/failure for any scraping function."""
    guard = ScrapeGuard()
    try:
        result = fetch_fn(*args, **kwargs)
        if result:
            count = len(result) if isinstance(result, (list, dict)) else 1
            guard.record_success(source_name, count)
        else:
            guard.record_failure(source_name, 'Empty result')
        return result
    except Exception as e:
        guard.record_failure(source_name, str(e))
        log.warning('[ScrapeGuard] %s failed: %s', source_name, e)
        return None
