"""
Oraculo LLM Module - Match quality filter.
Primary: Gemini 2.0 Flash API (fast, free tier 1500 req/day).
Fallback: local Ollama (gemma4:8B).
"""
import json
import logging
import os
import re
import time
from datetime import datetime

import requests

log = logging.getLogger('oraculo_llm')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OLLAMA_URL = 'http://localhost:11434/api/generate'
OLLAMA_MODEL = 'gemma4:latest'
OLLAMA_TIMEOUT = 180
GEMINI_MODEL = 'gemini-2.5-flash'
GEMINI_TIMEOUT = 30
NEWS_CACHE_FILE = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'tennis_news.json')


def _get_gemini_key():
    key = os.environ.get('GEMINI_API_KEY', '')
    if key:
        return key
    cfg = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
    if os.path.exists(cfg):
        try:
            return json.load(open(cfg)).get('gemini_api_key', '')
        except Exception:
            pass
    return ''


def _call_gemini(prompt):
    """Call Gemini REST API. Returns text or None."""
    key = _get_gemini_key()
    if not key:
        return None
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_MODEL}:generateContent?key={key}')
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 120,
            'candidateCount': 1,
        },
    }
    try:
        r = requests.post(url, json=body, timeout=GEMINI_TIMEOUT)
        if r.status_code == 200:
            parts = (r.json()
                     .get('candidates', [{}])[0]
                     .get('content', {})
                     .get('parts', []))
            return parts[0].get('text', '').strip() if parts else None
        log.warning('Gemini HTTP %d: %s', r.status_code, r.text[:120])
        return None
    except Exception as e:
        log.debug('Gemini error: %s', e)
        return None


def _call_ollama(prompt):
    """Call local Ollama. Returns text or None."""
    try:
        r = requests.post(OLLAMA_URL, json={
            'model': OLLAMA_MODEL,
            'prompt': prompt,
            'stream': False,
            'options': {'num_predict': 100, 'temperature': 0.2},
        }, timeout=OLLAMA_TIMEOUT)
        if r.status_code == 200:
            return r.json().get('response', '').strip()
        return None
    except Exception as e:
        log.debug('Ollama error: %s', e)
        return None


# ---------------------------------------------------------------------------
# News scraper
# ---------------------------------------------------------------------------
def fetch_tennis_news():
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
    news = []
    sources = [
        ('https://news.google.com/rss/search?q=ATP+tennis+injury+withdrawal+2026&hl=en', 'google'),
        ('https://news.google.com/rss/search?q=WTA+tennis+injury+withdrawal+2026&hl=en', 'google'),
    ]
    for url, source in sources:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
                if not titles:
                    titles = re.findall(r'<title>(.*?)</title>', r.text)
                for title in titles[:10]:
                    if any(kw in title.lower() for kw in
                           ['tennis', 'atp', 'wta', 'injury', 'withdraw', 'retire',
                            'upset', 'defeat', 'pulls out', 'out of']):
                        news.append({'title': title, 'source': source,
                                     'date': datetime.now().strftime('%Y-%m-%d')})
        except Exception as e:
            log.debug('News fetch failed from %s: %s', source, e)
    try:
        os.makedirs(os.path.dirname(NEWS_CACHE_FILE), exist_ok=True)
        with open(NEWS_CACHE_FILE, 'w') as f:
            json.dump({'news': news, 'fetched': datetime.now().isoformat()}, f, indent=2)
    except Exception:
        pass
    return news


def get_cached_news():
    try:
        if os.path.exists(NEWS_CACHE_FILE):
            data = json.load(open(NEWS_CACHE_FILE))
            fetched = datetime.fromisoformat(data.get('fetched', '2000-01-01'))
            if (datetime.now() - fetched).total_seconds() < 7200:
                return data.get('news', [])
    except Exception:
        pass
    return fetch_tennis_news()


def get_news_context(player_a, player_b):
    news = get_cached_news()
    relevant = []
    for n in news:
        title = n.get('title', '').lower()
        for name in [player_a, player_b]:
            if any(p in title for p in name.lower().split() if len(p) > 3):
                relevant.append(n['title'])
                break
    return relevant[:5]


# ---------------------------------------------------------------------------
# LLM Match Analyzer
# ---------------------------------------------------------------------------
def analyze_match(player_a, player_b, elo_a, elo_b, prob_a, odds_a,
                  form_a=None, form_b=None, h2h_a=0, h2h_b=0,
                  surface='hard', tourney='', matches_a=0, matches_b=0):
    """
    Evaluate a betting opportunity via LLM.
    Returns dict with keys: verdict (BET/SKIP/REDUCE), confidence, reason, source.
    Returns None if all LLM providers unavailable.
    """
    news = get_news_context(player_a, player_b)
    news_section = ''
    if news:
        news_section = '\nRECENT NEWS:\n' + '\n'.join('- ' + n for n in news)

    implied_prob = 1.0 / odds_a if odds_a > 0 else 0
    edge = prob_a - implied_prob

    prompt = f"""You are a professional tennis betting analyst. Your job is to FILTER bad bets, not find good ones. Be analytical — only SKIP when there is a clear specific reason.

MATCH: {player_a} vs {player_b}
TOURNAMENT: {tourney} ({surface} court)

STATISTICS:
- {player_a}: Elo {elo_a:.0f}, {matches_a} matches in database
- {player_b}: Elo {elo_b:.0f}, {matches_b} matches in database
- Form (last 10): {player_a} {form_a:.0%} / {player_b} {form_b:.0%}
- Head-to-head: {player_a} {h2h_a} - {h2h_b} {player_b}
- Model probability: {player_a} {prob_a:.1%}
- Cloudbet odds: {odds_a:.2f} (implied {implied_prob:.1%})
- Calculated edge: {edge:.1%}
{news_section}

RULES FOR YOUR ANALYSIS:
1. If a player has fewer than 10 matches in database, their Elo is UNRELIABLE — lean toward SKIP
2. If model probability and implied probability are within 3%, there is NO real edge — SKIP
3. If the underdog has better recent form (>70%) and favorite has poor form (<40%), REDUCE stake
4. If there are news about injuries or withdrawals for our pick, SKIP
5. If H2H strongly favors the opponent (3+ wins vs 0), consider SKIP
6. Only say BET if the edge is clear and data is reliable

Respond EXACTLY in this format (nothing else):
VERDICT: BET or SKIP or REDUCE
CONFIDENCE: number 0-100
REASON: one sentence"""

    # Try Gemini first (fast cloud API), fall back to local Ollama
    text = _call_gemini(prompt)
    source = 'gemini'
    if text is None:
        log.info('Gemini unavailable, trying Ollama fallback')
        text = _call_ollama(prompt)
        source = 'ollama'
    if text is None:
        return None

    log.info('LLM[%s] %s vs %s: %s', source, player_a, player_b, text[:150])

    verdict = 'BET'
    confidence = 50
    reason = text

    m = re.search(r'VERDICT:\s*(BET|SKIP|REDUCE)', text, re.IGNORECASE)
    if m:
        verdict = m.group(1).upper()
    m = re.search(r'CONFIDENCE:\s*(\d+)', text)
    if m:
        confidence = min(100, max(0, int(m.group(1))))
    m = re.search(r'REASON:\s*(.+)', text, re.IGNORECASE)
    if m:
        reason = m.group(1).strip()

    return {
        'verdict': verdict,
        'confidence': confidence,
        'reason': reason,
        'raw': text,
        'player': player_a,
        'opponent': player_b,
        'source': source,
    }


def filter_picks_with_llm(picks, tennis_elo, surface='hard', tourney=''):
    """
    Filter a list of betting picks through the LLM.
    Returns filtered list (only BET verdicts pass, REDUCE passes with flag).
    """
    if not picks:
        return picks

    filtered = []
    for p in picks:
        match = p.get('match', '')
        _mt = p.get('market_type', '')
        # Set/total markets use Poisson model directly, bypass LLM veto
        if _mt in ('tennis_exact_sets', 'tennis_winner_and_total', 'tennis_team_win_set'):
            filtered.append(p)
            continue
        if ' vs ' not in match:
            filtered.append(p)
            continue

        parts = match.split(' vs ')
        player_a = parts[0].strip()
        player_b = parts[1].strip()
        label = p.get('label', '')
        if 'Winner:' in label:
            pick_player = label.replace('Winner:', '').strip()
        else:
            pick_player = player_a

        if pick_player in player_a or player_a in pick_player:
            fav, dog = player_a, player_b
        else:
            fav, dog = player_b, player_a

        prob = p.get('model_prob', 0.5)
        odds = p.get('price', 1.0)
        elo_fav = tennis_elo.overall.get(fav, 1500)
        elo_dog = tennis_elo.overall.get(dog, 1500)
        form_fav = tennis_elo.get_form(fav) or 0.5
        form_dog = tennis_elo.get_form(dog) or 0.5
        h2h = tennis_elo.get_h2h(fav, dog)
        matches_fav = tennis_elo._match_count.get(fav, 0)
        matches_dog = tennis_elo._match_count.get(dog, 0)

        result = analyze_match(
            fav, dog, elo_fav, elo_dog, prob, odds,
            form_fav, form_dog, h2h[0], h2h[1],
            surface, tourney, matches_fav, matches_dog,
        )

        if result is None:
            log.info('  [LLM OFF] Passing through: %s', match)
            filtered.append(p)
            continue

        verdict = result['verdict']
        confidence = result['confidence']
        reason = result['reason']
        src = result.get('source', '?')

        if verdict == 'BET':
            log.info('  [LLM/%s BET %d%%] %s: %s', src, confidence, match, reason)
            filtered.append(p)
        elif verdict == 'REDUCE':
            log.info('  [LLM/%s REDUCE %d%%] %s: %s', src, confidence, match, reason)
            p['_llm_reduce'] = True
            filtered.append(p)
        else:
            log.info('  [LLM/%s SKIP %d%%] %s: %s', src, confidence, match, reason)

    log.info('LLM filter: %d/%d picks passed', len(filtered), len(picks))
    # Safety net: if LLM vetoed everything, pass top 2 by edge anyway
    if picks and len(filtered) == 0 and len(picks) >= 2:
        top2 = sorted(picks, key=lambda x: x.get('edge', 0), reverse=True)[:2]
        log.warning('LLM vetoed ALL %d picks — passing top 2 by edge as safety net', len(picks))
        return top2
    return filtered


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
def is_ollama_available():
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def is_gemini_available():
    return bool(_get_gemini_key())
