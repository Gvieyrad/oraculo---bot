"""
Calibrated F5 totals model for MLB.
Replaces formula-based approach with LogisticRegression per line,
trained on historical linescore data from MLB Stats API.
"""
import os, json, sqlite3, time, logging, pickle, requests
from datetime import datetime, timedelta

log = logging.getLogger("oraculo")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MLB_API    = "https://statsapi.mlb.com/api/v1"
DB_PATH    = os.path.join(SCRIPT_DIR, ".oraculo_cache", "mlb_f5.db")
MODEL_PATH = os.path.join(SCRIPT_DIR, "models", "mlb_f5_model.pkl")
PITCHER_CACHE = os.path.join(SCRIPT_DIR, ".oraculo_cache", "mlb_pitcher_logs.json")

TRAIN_DAYS = 180
LINES      = [3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
UNDER45_THRESHOLD = 0.58
FIP_CONST  = 3.10

PARK_FACTORS = {
    "Colorado Rockies":1.38,"Cincinnati Reds":1.12,"Boston Red Sox":1.10,
    "Philadelphia Phillies":1.09,"Texas Rangers":1.08,"Chicago Cubs":1.07,
    "Atlanta Braves":1.06,"Milwaukee Brewers":1.05,"Toronto Blue Jays":1.04,
    "Minnesota Twins":1.04,"Houston Astros":1.03,"Baltimore Orioles":1.02,
    "New York Yankees":1.01,"Kansas City Royals":1.01,"Washington Nationals":1.00,
    "Chicago White Sox":1.00,"Detroit Tigers":0.99,"Arizona Diamondbacks":0.98,
    "Los Angeles Angels":0.98,"Cleveland Guardians":0.97,"Pittsburgh Pirates":0.97,
    "Tampa Bay Rays":0.97,"St. Louis Cardinals":0.96,"New York Mets":0.96,
    "Seattle Mariners":0.95,"Los Angeles Dodgers":0.95,"Miami Marlins":0.94,
    "San Diego Padres":0.93,"San Francisco Giants":0.92,"Oakland Athletics":0.91,
}
DOME_TEAMS = {"Arizona Diamondbacks","Houston Astros","Miami Marlins",
              "Minnesota Twins","Tampa Bay Rays","Toronto Blue Jays","Texas Rangers"}

FEATURE_NAMES = [
    "hp_fip","ap_fip","avg_fip","hp_k9","ap_k9","hp_ip","ap_ip",
    "home_f5_off","away_f5_off","home_f5_def","away_f5_def",
    "combined_off","combined_def","park_factor","is_dome","weather_adj",
]


def _fip(hr, bb, hbp, k, ip):
    if not ip or ip < 0.1:
        return 4.50
    return round((13*hr + 3*(bb+hbp) - 2*k) / ip + FIP_CONST, 3)


def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS f5_games (
            game_pk   INTEGER PRIMARY KEY,
            date      TEXT,
            home      TEXT,
            away      TEXT,
            hp_id     INTEGER,
            ap_id     INTEGER,
            f5_home   INTEGER,
            f5_away   INTEGER,
            f5_total  INTEGER,
            status    TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON f5_games(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_home ON f5_games(home)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_away ON f5_games(away)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hp   ON f5_games(hp_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ap   ON f5_games(ap_id)")
    conn.commit()
    return conn


def fetch_f5_history(days_back=TRAIN_DAYS, force_refresh=False):
    """Fetch MLB schedule with linescore data, extract F5 runs, store in SQLite."""
    conn = _init_db()
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    # Check what's already cached
    if not force_refresh:
        cursor = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM f5_games WHERE status='Final'")
        row = cursor.fetchone()
        if row and row[2] and row[2] > 100:
            cached_start = datetime.strptime(row[0], "%Y-%m-%d").date() if row[0] else None
            cached_end   = datetime.strptime(row[1], "%Y-%m-%d").date() if row[1] else None
            if cached_start and cached_end:
                log.info(f"F5 DB has {row[2]} games ({row[0]} -> {row[1]})")
                # Only fetch missing tail
                if cached_end >= end_date - timedelta(days=2):
                    return conn
                start_date = cached_end - timedelta(days=1)

    log.info(f"Fetching F5 history {start_date} -> {end_date}")
    batch_size = 7
    cursor_date = start_date

    while cursor_date <= end_date:
        batch_end = min(cursor_date + timedelta(days=batch_size - 1), end_date)
        url = (f"{MLB_API}/schedule"
               f"?sportId=1"
               f"&startDate={cursor_date}&endDate={batch_end}"
               f"&hydrate=probablePitcher,linescore,team"
               f"&gameType=R")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"F5 schedule fetch error {cursor_date}: {e}")
            cursor_date += timedelta(days=batch_size)
            time.sleep(1)
            continue

        for date_entry in data.get("dates", []):
            game_date = date_entry.get("date", "")
            for game in date_entry.get("games", []):
                _process_game(conn, game, game_date)

        conn.commit()
        cursor_date += timedelta(days=batch_size)
        time.sleep(0.3)

    return conn


def _process_game(conn, game, game_date):
    """Extract F5 data from a single game object and upsert into DB."""
    pk     = game.get("gamePk")
    status = game.get("status", {}).get("abstractGameState", "")
    teams  = game.get("teams", {})
    home_t = teams.get("home", {})
    away_t = teams.get("away", {})

    home_name = home_t.get("team", {}).get("name", "")
    away_name = away_t.get("team", {}).get("name", "")

    hp_info = home_t.get("probablePitcher", {})
    ap_info = away_t.get("probablePitcher", {})
    hp_id   = hp_info.get("id") if hp_info else None
    ap_id   = ap_info.get("id") if ap_info else None

    f5_home = f5_away = f5_total = None
    if status == "Final":
        linescore = game.get("linescore", {})
        innings   = linescore.get("innings", [])
        home_runs = 0
        away_runs = 0
        for inn in innings[:5]:
            home_runs += inn.get("home", {}).get("runs", 0) or 0
            away_runs += inn.get("away", {}).get("runs", 0) or 0
        f5_home  = home_runs
        f5_away  = away_runs
        f5_total = home_runs + away_runs

    conn.execute("""
        INSERT OR REPLACE INTO f5_games
        (game_pk, date, home, away, hp_id, ap_id, f5_home, f5_away, f5_total, status)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (pk, game_date, home_name, away_name, hp_id, ap_id,
          f5_home, f5_away, f5_total, status))


# ── Pitcher cache ─────────────────────────────────────────────────────────────

def _load_pitcher_cache():
    if os.path.exists(PITCHER_CACHE):
        try:
            with open(PITCHER_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_pitcher_cache(cache):
    os.makedirs(os.path.dirname(PITCHER_CACHE), exist_ok=True)
    with open(PITCHER_CACHE, "w") as f:
        json.dump(cache, f)


def fetch_pitcher_gamelogs(pitcher_ids, season=None):
    """
    Fetch game-by-game pitching stats from MLB Stats API for a list of pitcher IDs.
    Returns dict: { pitcher_id: [ {date, ip, k, bb, hbp, hr, era, ...}, ... ] }
    Caches results for 12 hours.
    """
    if season is None:
        season = datetime.utcnow().year

    cache     = _load_pitcher_cache()
    now_ts    = time.time()
    ttl       = 12 * 3600
    results   = {}
    to_fetch  = []

    for pid in pitcher_ids:
        key = f"{pid}_{season}"
        if key in cache:
            entry = cache[key]
            if now_ts - entry.get("ts", 0) < ttl:
                results[pid] = entry["games"]
                continue
        to_fetch.append(pid)

    for pid in to_fetch:
        url = (f"{MLB_API}/people/{pid}/stats"
               f"?stats=gameLog&group=pitching&season={season}"
               f"&hydrate=team")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Pitcher gamelog fetch error pid={pid}: {e}")
            results[pid] = []
            continue

        games = []
        for split_group in data.get("stats", []):
            for split in split_group.get("splits", []):
                s = split.get("stat", {})
                games.append({
                    "date": split.get("date", ""),
                    "ip":   _parse_ip(s.get("inningsPitched", "0")),
                    "k":    s.get("strikeOuts", 0),
                    "bb":   s.get("baseOnBalls", 0),
                    "hbp":  s.get("hitByPitch", 0),
                    "hr":   s.get("homeRuns", 0),
                    "er":   s.get("earnedRuns", 0),
                    "h":    s.get("hits", 0),
                })

        games.sort(key=lambda g: g["date"])
        results[pid] = games
        cache[f"{pid}_{season}"] = {"ts": now_ts, "games": games}
        time.sleep(0.2)

    _save_pitcher_cache(cache)
    return results


def _parse_ip(ip_str):
    """Convert MLB inningsPitched string like '6.1' -> float 6.333"""
    try:
        ip_str = str(ip_str)
        if "." in ip_str:
            whole, frac = ip_str.split(".", 1)
            return int(whole) + int(frac) / 3.0
        return float(ip_str)
    except Exception:
        return 0.0


# ── Recent stats helpers ───────────────────────────────────────────────────────

def _pitcher_recent(games, as_of_date, n=3):
    """
    Compute FIP, K/9, avg IP for the last n starts before as_of_date.
    Returns dict with fip, k9, avg_ip.
    """
    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.strftime("%Y-%m-%d")
    past   = [g for g in games if g["date"] < cutoff]
    if not past:
        return {"fip": 4.50, "k9": 7.0, "avg_ip": 5.0}

    recent = past[-n:]
    tot_ip  = sum(g["ip"]  for g in recent)
    tot_k   = sum(g["k"]   for g in recent)
    tot_bb  = sum(g["bb"]  for g in recent)
    tot_hbp = sum(g["hbp"] for g in recent)
    tot_hr  = sum(g["hr"]  for g in recent)

    fip    = _fip(tot_hr, tot_bb, tot_hbp, tot_k, tot_ip)
    k9     = round(9 * tot_k / tot_ip, 2) if tot_ip > 0 else 7.0
    avg_ip = round(tot_ip / len(recent), 2)
    return {"fip": fip, "k9": k9, "avg_ip": avg_ip}


def _team_f5_recent(conn, team, as_of_date, n=10):
    """
    Compute average F5 runs scored and allowed by a team in last n games before as_of_date.
    Returns (avg_scored, avg_allowed).
    """
    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT f5_home, f5_away
        FROM f5_games
        WHERE home = ? AND date < ? AND status='Final' AND f5_total IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (team, cutoff, n)).fetchall()
    home_scored  = [r[0] for r in rows if r[0] is not None]
    home_allowed = [r[1] for r in rows if r[1] is not None]

    rows2 = conn.execute("""
        SELECT f5_home, f5_away
        FROM f5_games
        WHERE away = ? AND date < ? AND status='Final' AND f5_total IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (team, cutoff, n)).fetchall()
    away_scored  = [r[1] for r in rows2 if r[1] is not None]
    away_allowed = [r[0] for r in rows2 if r[0] is not None]

    all_scored  = home_scored  + away_scored
    all_allowed = home_allowed + away_allowed

    avg_scored  = round(sum(all_scored)  / len(all_scored),  3) if all_scored  else 2.3
    avg_allowed = round(sum(all_allowed) / len(all_allowed), 3) if all_allowed else 2.3
    return avg_scored, avg_allowed


# ── Feature builder ────────────────────────────────────────────────────────────

def build_features(home, away, hp_stats, ap_stats,
                   home_f5, away_f5,
                   park_factor=1.0, is_dome=False, weather_adj=0.0):
    """
    Build a 16-element feature vector for the F5 totals model.

    Parameters
    ----------
    home, away       : team name strings
    hp_stats         : dict with fip, k9, avg_ip  (home pitcher)
    ap_stats         : dict with fip, k9, avg_ip  (away pitcher)
    home_f5          : (avg_scored, avg_allowed) tuple for home team
    away_f5          : (avg_scored, avg_allowed) tuple for away team
    park_factor      : float (default 1.0)
    is_dome          : bool
    weather_adj      : float, positive = hitter-friendly wind/temp
    """
    hp_fip = hp_stats.get("fip", 4.50)
    ap_fip = ap_stats.get("fip", 4.50)
    hp_k9  = hp_stats.get("k9",  7.0)
    ap_k9  = ap_stats.get("k9",  7.0)
    hp_ip  = hp_stats.get("avg_ip", 5.0)
    ap_ip  = ap_stats.get("avg_ip", 5.0)

    home_off, home_def = home_f5
    away_off, away_def = away_f5

    combined_off = (home_off + away_off) / 2.0
    combined_def = (home_def + away_def) / 2.0

    return [
        hp_fip, ap_fip, (hp_fip + ap_fip) / 2.0,
        hp_k9, ap_k9,
        hp_ip, ap_ip,
        home_off, away_off,
        home_def, away_def,
        combined_off, combined_def,
        park_factor,
        1.0 if is_dome else 0.0,
        weather_adj,
    ]


# ── Model class ────────────────────────────────────────────────────────────────

class MLBF5Model:
    """
    One LogisticRegression classifier per F5 line.
    Predicts P(F5 total > line).
    """

    def __init__(self):
        self.models    = {}   # line -> sklearn LogisticRegression
        self.scalers   = {}   # line -> sklearn StandardScaler
        self.thresholds = {}  # line -> optimal decision threshold
        self.train_date = None
        self.n_samples  = 0

    def fit(self, X, y_dict):
        """
        X      : list of feature vectors (len = N)
        y_dict : { line: [0/1, ...] } binary labels per line
        """
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.model_selection import StratifiedKFold
            from sklearn.metrics import precision_recall_curve
            import numpy as np
        except ImportError:
            log.error("scikit-learn not installed; cannot train F5 model")
            return self

        Xarr = np.array(X, dtype=float)
        self.n_samples = len(Xarr)
        log.info(f"Training F5 model on {self.n_samples} samples")

        for line in LINES:
            y = np.array(y_dict.get(line, []), dtype=int)
            if len(y) != len(Xarr) or y.sum() < 10:
                log.warning(f"Skipping line {line}: insufficient positives ({y.sum()})")
                continue

            scaler = StandardScaler()
            Xs     = scaler.fit_transform(Xarr)

            clf = LogisticRegression(
                C=1.0, max_iter=500, class_weight="balanced",
                solver="lbfgs", random_state=42
            )
            clf.fit(Xs, y)

            # Find threshold that maximises F1 on training data
            proba    = clf.predict_proba(Xs)[:, 1]
            prec, rec, thresholds = precision_recall_curve(y, proba)
            f1       = 2 * prec * rec / (prec + rec + 1e-9)
            best_idx = int(f1.argmax())
            best_thr = float(thresholds[best_idx]) if best_idx < len(thresholds) else 0.5

            self.models[line]     = clf
            self.scalers[line]    = scaler
            self.thresholds[line] = round(best_thr, 4)
            log.info(f"  line={line}: threshold={best_thr:.3f}, "
                     f"positives={y.sum()}/{len(y)}")

        self.train_date = datetime.utcnow().isoformat()
        return self

    def predict(self, features, line):
        """
        Returns dict with keys: prob_over, prob_under, edge, pick, confidence.
        prob_over is P(F5 total > line).
        """
        clf    = self.models.get(line)
        scaler = self.scalers.get(line)
        thr    = self.thresholds.get(line, 0.50)

        if clf is None or scaler is None:
            # Fallback: naive formula
            avg_fip  = (features[0] + features[1]) / 2.0
            est_runs = max(0.5, (4.50 - avg_fip) * 0.6 + 4.5) * features[13]
            prob_over = min(0.85, max(0.15, 0.5 + (est_runs - line) * 0.08))
            return {
                "prob_over":  round(prob_over, 4),
                "prob_under": round(1 - prob_over, 4),
                "edge":       round(abs(prob_over - 0.5) * 2, 4),
                "pick":       "OVER" if prob_over > 0.5 else "UNDER",
                "confidence": "low",
                "model":      "fallback",
            }

        import numpy as np
        Xs   = scaler.transform(np.array(features, dtype=float).reshape(1, -1))
        prob = float(clf.predict_proba(Xs)[0, 1])

        prob_over  = round(prob, 4)
        prob_under = round(1 - prob, 4)
        edge       = round(abs(prob - 0.5) * 2, 4)

        if prob_under > thr + 0.05 and prob_under >= UNDER45_THRESHOLD:
            pick = "UNDER"
        elif prob_over > thr + 0.05:
            pick = "OVER"
        else:
            pick = "PASS"

        confidence = "high" if edge > 0.30 else "medium" if edge > 0.15 else "low"

        return {
            "prob_over":  prob_over,
            "prob_under": prob_under,
            "edge":       edge,
            "pick":       pick,
            "confidence": confidence,
            "threshold":  round(thr, 4),
            "model":      "logistic",
        }

    def save(self):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        data = {
            "models":     self.models,
            "scalers":    self.scalers,
            "thresholds": self.thresholds,
            "train_date": self.train_date,
            "n_samples":  self.n_samples,
        }
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(data, f)
        log.info("F5 model saved -> %s", MODEL_PATH)

    @classmethod
    def load(cls):
        if not os.path.exists(MODEL_PATH):
            return None
        try:
            with open(MODEL_PATH, "rb") as f:
                data = pickle.load(f)
            # Support both old (class instance) and new (dict) format
            if isinstance(data, dict):
                obj = cls()
                obj.models     = data["models"]
                obj.scalers    = data["scalers"]
                obj.thresholds = data["thresholds"]
                obj.train_date = data.get("train_date", "")
                obj.n_samples  = data.get("n_samples", 0)
            else:
                obj = data
            log.info("F5 model loaded (%d samples, %s)", obj.n_samples, obj.train_date)
            return obj
        except Exception as e:
            log.warning("F5 model load error: %s", e)
            return None


# ── Training pipeline ──────────────────────────────────────────────────────────

def train_f5_model(days_back=TRAIN_DAYS, force=False):
    """
    Full training pipeline:
    1. Fetch F5 history from MLB Stats API -> SQLite
    2. Fetch pitcher gamelogs for all pitchers in history
    3. Build feature vectors for each game
    4. Train MLBF5Model
    5. Save to disk
    Returns trained MLBF5Model.
    """
    log.info("=== F5 training pipeline start ===")
    conn = fetch_f5_history(days_back=days_back, force_refresh=force)

    season = datetime.utcnow().year
    rows   = conn.execute("""
        SELECT game_pk, date, home, away, hp_id, ap_id, f5_total
        FROM f5_games
        WHERE status='Final' AND f5_total IS NOT NULL AND hp_id IS NOT NULL AND ap_id IS NOT NULL
        ORDER BY date ASC
    """).fetchall()

    log.info(f"Training games: {len(rows)}")
    if len(rows) < 50:
        log.warning("Insufficient training data, aborting")
        return None

    pitcher_ids = set()
    for row in rows:
        if row[4]: pitcher_ids.add(row[4])
        if row[5]: pitcher_ids.add(row[5])

    log.info(f"Fetching gamelogs for {len(pitcher_ids)} pitchers")
    pitcher_logs = fetch_pitcher_gamelogs(list(pitcher_ids), season=season)

    X      = []
    y_dict = {line: [] for line in LINES}
    skipped = 0

    for (pk, gdate, home, away, hp_id, ap_id, f5_total) in rows:
        hp_games = pitcher_logs.get(hp_id, [])
        ap_games = pitcher_logs.get(ap_id, [])

        hp_stats = _pitcher_recent(hp_games, gdate, n=3)
        ap_stats = _pitcher_recent(ap_games, gdate, n=3)

        home_f5 = _team_f5_recent(conn, home, gdate, n=10)
        away_f5 = _team_f5_recent(conn, away, gdate, n=10)

        pf      = PARK_FACTORS.get(home, 1.00)
        is_dome = home in DOME_TEAMS

        features = build_features(home, away, hp_stats, ap_stats,
                                  home_f5, away_f5, pf, is_dome, 0.0)
        X.append(features)

        for line in LINES:
            y_dict[line].append(1 if f5_total > line else 0)

    log.info(f"Built {len(X)} feature vectors (skipped {skipped})")

    model = MLBF5Model()
    model.fit(X, y_dict)
    model.save()
    log.info("=== F5 training pipeline complete ===")
    return model


# ── Cached model getter ────────────────────────────────────────────────────────

_cached_model = None
_model_mtime  = None

def get_model():
    """
    Return a cached MLBF5Model instance.
    Reloads from disk if the pickle file has been updated.
    """
    global _cached_model, _model_mtime
    if os.path.exists(MODEL_PATH):
        mtime = os.path.getmtime(MODEL_PATH)
        if _cached_model is None or mtime != _model_mtime:
            _cached_model = MLBF5Model.load()
            _model_mtime  = mtime
    if _cached_model is None:
        # Auto-train if no model exists yet
        log.info("No F5 model found, triggering auto-train")
        _cached_model = train_f5_model()
        if os.path.exists(MODEL_PATH):
            _model_mtime = os.path.getmtime(MODEL_PATH)
    return _cached_model


# ── Live prediction ────────────────────────────────────────────────────────────

def predict_live(home, away, hp_id, ap_id, line=4.5,
                 weather_adj=0.0, season=None):
    """
    Generate a live F5 totals prediction.

    Parameters
    ----------
    home, away   : team name strings
    hp_id, ap_id : MLB pitcher IDs (integers)
    line         : F5 total line (e.g. 4.5)
    weather_adj  : float, >0 = hitter-friendly conditions
    season       : MLB season year (defaults to current year)

    Returns
    -------
    dict with keys: home, away, line, prob_over, prob_under, edge, pick,
                    confidence, hp_stats, ap_stats, features, model, park_factor
    """
    if season is None:
        season = datetime.utcnow().year

    today    = datetime.utcnow().strftime("%Y-%m-%d")
    _valid_ids   = [p for p in [hp_id, ap_id] if p is not None]
    pitcher_logs = fetch_pitcher_gamelogs(_valid_ids, season=season) if _valid_ids else {}

    hp_games = pitcher_logs.get(hp_id, [])
    ap_games = pitcher_logs.get(ap_id, [])

    hp_stats = _pitcher_recent(hp_games, today, n=3)
    ap_stats = _pitcher_recent(ap_games, today, n=3)

    conn     = _init_db()
    home_f5  = _team_f5_recent(conn, home, today, n=10)
    away_f5  = _team_f5_recent(conn, away, today, n=10)
    conn.close()

    pf      = PARK_FACTORS.get(home, 1.00)
    is_dome = home in DOME_TEAMS

    features = build_features(home, away, hp_stats, ap_stats,
                               home_f5, away_f5, pf, is_dome, weather_adj)

    model  = get_model()
    result = model.predict(features, line) if model else {
        "prob_over": 0.5, "prob_under": 0.5, "edge": 0.0,
        "pick": "PASS", "confidence": "low", "model": "none"
    }

    result.update({
        "home":         home,
        "away":         away,
        "line":         line,
        "hp_stats":     hp_stats,
        "ap_stats":     ap_stats,
        "features":     dict(zip(FEATURE_NAMES, features)),
        "park_factor":  pf,
        "is_dome":      is_dome,
        "home_f5_off":  home_f5[0],
        "home_f5_def":  home_f5[1],
        "away_f5_off":  away_f5[0],
        "away_f5_def":  away_f5[1],
    })
    return result


# ── Feature importances ────────────────────────────────────────────────────────

def get_feature_importances():
    """
    Log and return top feature importances (coefficients) per line.
    Returns dict: { line: [ (feature_name, coef), ... ] }
    """
    model = get_model()
    if model is None:
        log.warning("No F5 model available for importances")
        return {}

    import numpy as np
    out = {}
    for line, clf in model.models.items():
        coefs = clf.coef_[0]
        pairs = sorted(zip(FEATURE_NAMES, coefs),
                       key=lambda x: abs(x[1]), reverse=True)
        out[line] = [(name, round(float(c), 4)) for name, c in pairs]
        top3 = ", ".join(f"{n}={c:+.3f}" for n, c in pairs[:3])
        log.info(f"F5 line {line} top features: {top3}")
    return out


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="MLB F5 Totals Model")
    ap.add_argument("--train",   action="store_true", help="Train/retrain model")
    ap.add_argument("--days",    type=int, default=TRAIN_DAYS)
    ap.add_argument("--force",   action="store_true", help="Force re-fetch history")
    ap.add_argument("--predict", action="store_true", help="Live prediction example")
    ap.add_argument("--home",    default="New York Yankees")
    ap.add_argument("--away",    default="Boston Red Sox")
    ap.add_argument("--hp-id",   type=int, default=543037, help="Home pitcher MLB ID")
    ap.add_argument("--ap-id",   type=int, default=605483, help="Away pitcher MLB ID")
    ap.add_argument("--line",    type=float, default=4.5)
    ap.add_argument("--importances", action="store_true")
    args = ap.parse_args()

    if args.train:
        train_f5_model(days_back=args.days, force=args.force)

    if args.predict:
        res = predict_live(args.home, args.away, args.hp_id, args.ap_id,
                           line=args.line)
        import pprint
        pprint.pprint(res)

    if args.importances:
        imps = get_feature_importances()
        for line, pairs in sorted(imps.items()):
            print(f"\nLine {line}:")
            for name, c in pairs:
                print(f"  {name:25s} {c:+.4f}")
