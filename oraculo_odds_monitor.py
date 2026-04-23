"""Odds monitoring — tracks line movements and detects steam moves."""
import os, json, time, logging, sqlite3
from datetime import datetime, timedelta

log = logging.getLogger('oraculo')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ODDS_DB = os.path.join(SCRIPT_DIR, '.oraculo_cache', 'odds_history.db')


def _init_db():
    os.makedirs(os.path.dirname(ODDS_DB), exist_ok=True)
    conn = sqlite3.connect(ODDS_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT, market_url TEXT, outcome TEXT,
        price REAL, timestamp TEXT,
        UNIQUE(event_id, market_url, outcome, timestamp)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_event ON odds_snapshots(event_id)""")
    conn.commit()
    return conn


def record_odds(api, events_data):
    """Record current odds snapshot for all active events."""
    conn = _init_db()
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M')
    recorded = 0
    for ev in events_data:
        eid = str(ev.get('id', ''))
        if not eid:
            continue
        markets = ev.get('markets', {})
        for mk_name, mk_data in markets.items():
            for sv in mk_data.get('submarkets', {}).values():
                for sel in sv.get('selections', []):
                    price = sel.get('price', 0)
                    murl = sel.get('marketUrl', '')
                    outcome = sel.get('outcome', '')
                    if price and murl:
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO odds_snapshots VALUES (NULL,?,?,?,?,?)",
                                (eid, murl, outcome, float(price), ts))
                            recorded += 1
                        except Exception:
                            pass
    conn.commit()
    conn.close()
    return recorded


def detect_steam_moves(event_id, market_url, threshold=0.08, window_hours=6):
    """Detect significant odds movement (steam move).
    Returns: (direction, magnitude, minutes_ago) or None.
    direction: 'DROP' (odds falling = sharp money on this side) or 'RISE'
    """
    conn = _init_db()
    cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).strftime('%Y-%m-%dT%H:%M')
    rows = conn.execute(
        "SELECT price, timestamp FROM odds_snapshots WHERE event_id=? AND market_url=? AND timestamp>=? ORDER BY timestamp",
        (event_id, market_url, cutoff)
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        return None

    first_price = rows[0][0]
    last_price = rows[-1][0]
    if first_price <= 0:
        return None

    change = (last_price - first_price) / first_price
    if abs(change) < threshold:
        return None

    direction = 'DROP' if change < 0 else 'RISE'
    # Parse time
    try:
        first_t = datetime.strptime(rows[0][1], '%Y-%m-%dT%H:%M')
        last_t = datetime.strptime(rows[-1][1], '%Y-%m-%dT%H:%M')
        minutes = (last_t - first_t).total_seconds() / 60
    except Exception:
        minutes = 0

    return {
        'direction': direction,
        'magnitude': abs(change),
        'from_price': first_price,
        'to_price': last_price,
        'minutes': minutes,
    }


def get_closing_odds(event_id, market_url):
    """Get the last recorded odds for an event+market (closing line)."""
    conn = _init_db()
    row = conn.execute(
        "SELECT price FROM odds_snapshots WHERE event_id=? AND market_url=? ORDER BY timestamp DESC LIMIT 1",
        (event_id, market_url)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def cleanup_old(days=7):
    """Remove odds snapshots older than N days."""
    conn = _init_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M')
    conn.execute("DELETE FROM odds_snapshots WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()
