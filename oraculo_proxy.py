"""Proxy routing for Cloudflare-protected sites."""
import os, json, logging

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'proxy_config.json')


def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def proxied_get(url, timeout=30):
    """Fetch URL through proxy to bypass Cloudflare."""
    cfg = _load_config()
    provider = cfg.get('proxy_provider', '')

    if provider == 'scraperapi' and cfg.get('scraperapi_key'):
        key = cfg['scraperapi_key']
        proxy_url = f'http://api.scraperapi.com?api_key={key}&url={url}'
        return _fetch(proxy_url, timeout)

    if provider == 'scrapfly' and cfg.get('scrapfly_key'):
        key = cfg['scrapfly_key']
        proxy_url = f'https://api.scrapfly.io/scrape?key={key}&url={url}&render_js=true&country=us'
        import urllib.request
        try:
            req = urllib.request.Request(proxy_url)
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read())
            return data.get('result', {}).get('content', '')
        except Exception as e:
            log.debug('ScrapFly error: %s', e)
            return None

    if provider == 'zenrows' and cfg.get('zenrows_key'):
        key = cfg['zenrows_key']
        proxy_url = f'https://api.zenrows.com/v1/?apikey={key}&url={url}&js_render=true'
        return _fetch(proxy_url, timeout)

    return _fetch(url, timeout)


def _fetch(url, timeout=30):
    import urllib.request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.debug('Fetch error for %s: %s', url[:60], e)
        return None


def is_configured():
    cfg = _load_config()
    provider = cfg.get('proxy_provider', '')
    if provider == 'scraperapi':
        return bool(cfg.get('scraperapi_key'))
    if provider == 'scrapfly':
        return bool(cfg.get('scrapfly_key'))
    if provider == 'zenrows':
        return bool(cfg.get('zenrows_key'))
    return False
