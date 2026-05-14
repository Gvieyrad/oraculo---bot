"""
Oraculo LLM Module — Match quality filter using local Ollama.
Uses Qwen 2.5 3B as a second opinion before placing bets.
Also scrapes tennis news for injury/context awareness.
"""
import json
import logging
import os
import re
import time
from datetime import datetime

import requests

log = logging.getLogger('oraculo_llm')

OLLAMA_URL = 'http://localhost:11434/api/generate'
OLLAMA_MODEL = 'qwen2.5:3b'
OLLAMA_TIMEOUT = 120  # seconds
NEWS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '.oraculo_cache', 'tennis_news.json')

# ---------------------------------------------------------------------------
# News scraper
# ---------------------------------------------------------------------------
def fetch_tennis_news():
    """Scrape recent tennis news headlines for context."""
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
    news = []

    # Source 1: Google News RSS for tennis injuries/withdrawals
    sources = [
        ('https://news.google.com/rss/search?q=ATP+tennis+injury+withdrawal+2026&hl=en', 'google'),
        ('https://news.google.com/rss/search?q=tennis+Miami+Open+2026&hl=en', 'google'),
    ]

    for url, source in sources:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                # Simple XML parsing for RSS titles
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
                if not titles:
                    titles = re.findall(r'<title>(.*?)</title>', r.text)
                for title in titles[:10]:
                    if any(kw in title.lower() for kw in
                           ['tennis', 'atp', 'wta', 'injury', 'withdraw', 'retire',
                            'miami', 'upset', 'defeat', 'pulls out', 'out of']):
                        news.append({
                            'title': title,
                            'source': source,
                            'date': datetime.now().strftime('%Y-%m-%d'),
                        })
        except Exception as e:
            log.debug('News fetch failed from %s: %s', source, e)

    # Source 2: Tennis Abstract recent results
    try:
        r = requests.get('http://www.tennisabstract.com/cgi-bin/leaders.cgi',
                         headers=headers, timeout=10)
        if r.status_code == 200 and len(r.text) > 1000:
            # Extract any relevant info
            log.debug('Tennis Abstract accessible')
    except Exception:
        pass

    # Cache news
    try:
        os.makedirs(os.path.dirname(NEWS_CACHE_FILE), exist_ok=True)
        with open(NEWS_CACHE_FILE, 'w') as f:
            json.dump({'news': news, 'fetched': datetime.now().isoformat()}, f, indent=2)
    except Exception:
        pass

    return news


def get_cached_news():
    """Get cached news, refresh if older than 2 hours."""
    try:
        if os.path.exists(NEWS_CACHE_FILE):
            data = json.load(open(NEWS_CACHE_FILE))
            fetched = datetime.fromisoformat(data.get('fetched', '2000-01-01'))
            age_hours = (datetime.now() - fetched).total_seconds() / 3600
            if age_hours < 2:
                return data.get('news', [])
    except Exception:
        pass
    return fetch_tennis_news()


def get_news_context(player_a, player_b):
    """Get relevant news for two players."""
    news = get_cached_news()
    relevant = []
    for n in news:
        title = n.get('title', '').lower()
        # Check if either player mentioned
        for name in [player_a, player_b]:
            parts = name.lower().split()
            if any(p in title for p in parts if len(p) > 3):
                relevant.append(n['title'])
                break
    return relevant[:5]  # Max 5 relevant headlines


# ---------------------------------------------------------------------------
# LLM Match Analyzer
# ---------------------------------------------------------------------------
def analyze_match(player_a, player_b, elo_a, elo_b, prob_a, odds_a,
                  form_a=None, form_b=None, h2h_a=0, h2h_b=0,
                  surface='hard', tourney='', matches_a=0, matches_b=0):
    """
    Ask LLM to evaluate a betting opportunity.

    Returns:
        dict with keys: verdict (BET/SKIP/REDUCE), confidence (0-100), reason (str)
        or None if LLM unavailable
    """
    # Get news context
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

    try:
        r = requests.post(OLLAMA_URL, json={
            'model': OLLAMA_MODEL,
            'prompt': prompt,
            'stream': False,
            'options': {'num_predict': 100, 'temperature': 0.2}
        }, timeout=OLLAMA_TIMEOUT)

        if r.status_code != 200:
            log.warning('LLM unavailable: HTTP %d', r.status_code)
            return None

        text = r.json().get('response', '').strip()
        log.info('LLM analysis for %s vs %s: %s', player_a, player_b, text[:150])

        # Parse response
        verdict = 'BET'  # Default: trust the model, LLM only filters clear negatives
        confidence = 50
        reason = text

        verdict_match = re.search(r'VERDICT:\s*(BET|SKIP|REDUCE)', text, re.IGNORECASE)
        if verdict_match:
            verdict = verdict_match.group(1).upper()

        conf_match = re.search(r'CONFIDENCE:\s*(\d+)', text)
        if conf_match:
            confidence = min(100, max(0, int(conf_match.group(1))))

        reason_match = re.search(r'REASON:\s*(.+)', text, re.IGNORECASE)
        if reason_match:
            reason = reason_match.group(1).strip()

        return {
            'verdict': verdict,
            'confidence': confidence,
            'reason': reason,
            'raw': text,
            'player': player_a,
            'opponent': player_b,
        }

    except requests.exceptions.Timeout:
        log.warning('LLM timeout (%ds) for %s vs %s', OLLAMA_TIMEOUT, player_a, player_b)
        return None
    except Exception as e:
        log.debug('LLM error: %s', e)
        return None


def filter_picks_with_llm(picks, tennis_elo, surface='hard', tourney=''):
    """
    Filter a list of betting picks through the LLM.

    Args:
        picks: list of pick dicts from scan_tennis
        tennis_elo: TennisElo instance
        surface: court surface
        tourney: tournament name

    Returns:
        filtered list of picks (only BET verdicts pass)
    """
    if not picks:
        return picks

    filtered = []
    for p in picks:
        match = p.get('match', '')
        # Phase 2 set-market picks — LLM analyze_match() is designed for match
        # winner only; these markets (exact_sets, winner_and_total, team_win_set)
        # have their own Poisson-based probability model, bypass LLM veto.
        _mt = p.get('market_type', '')
        if _mt in ('tennis_exact_sets', 'tennis_winner_and_total', 'tennis_team_win_set'):
            filtered.append(p)
            continue

        if ' vs ' not in match:
            filtered.append(p)  # Can't analyze, pass through
            continue

        parts = match.split(' vs ')
        player_a = parts[0].strip()
        player_b = parts[1].strip()

        # Get the pick player (the one we're betting on)
        label = p.get('label', '')
        if 'Winner:' in label:
            pick_player = label.replace('Winner:', '').strip()
        else:
            pick_player = player_a

        # Determine which is our pick
        if pick_player in player_a or player_a in pick_player:
            fav, dog = player_a, player_b
            prob = p.get('model_prob', 0.5)
            odds = p.get('price', 1.0)
        else:
            fav, dog = player_b, player_a
            prob = p.get('model_prob', 0.5)
            odds = p.get('price', 1.0)

        # Get Elo data
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
            surface, tourney, matches_fav, matches_dog
        )

        if result is None:
            # LLM unavailable, pass through (don't block bets if Ollama is down)
            log.info('  [LLM OFF] Passing through: %s', match)
            filtered.append(p)
            continue

        verdict = result['verdict']
        confidence = result['confidence']
        reason = result['reason']

        if verdict == 'BET':
            log.info('  [LLM BET %d%%] %s: %s', confidence, match, reason)
            filtered.append(p)
        elif verdict == 'REDUCE':
            log.info('  [LLM REDUCE %d%%] %s: %s', confidence, match, reason)
            p['_llm_reduce'] = True  # Signal to reduce stake by 50%
            filtered.append(p)
        else:
            log.info('  [LLM SKIP %d%%] %s: %s', confidence, match, reason)
            # Don't add to filtered — bet is vetoed

    log.info('LLM filter: %d/%d picks passed', len(filtered), len(picks))
    # Safety net: if LLM vetoed >80% of picks, pass through top 2 by edge anyway
    if picks and len(filtered) == 0 and len(picks) >= 2:
        top2 = sorted(picks, key=lambda x: x.get('edge', 0), reverse=True)[:2]
        log.warning('LLM vetoed ALL %d picks — passing top 2 by edge as safety net', len(picks))
        return top2
    return filtered


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
def is_ollama_available():
    """Check if Ollama is running and responsive."""
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=5)
        return r.status_code == 200
    except Exception:
        return False
