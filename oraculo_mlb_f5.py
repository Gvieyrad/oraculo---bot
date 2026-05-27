"""
Calibrated F5 totals model for MLB -- v2 rebuild 2026-05-27.

Changes vs v1:
  - TRAIN_DAYS 180 -> 365
  - LINES extended to include 6.5 and 7.0
  - Umpire feature: HP umpire run-rate deviation from league avg (last 20 games)
  - Platt calibration (sigmoid) on top of LogisticRegression per line
"""
import os, json, sqlite3, time, logging, pickle, requests
from datetime import datetime, timedelta

log = logging.getLogger("oraculo")
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
MLB_API       = "https://statsapi.mlb.com/api/v1"
DB_PATH       = os.path.join(SCRIPT_DIR, ".oraculo_cache", "mlb_f5.db")
MODEL_PATH    = os.path.join(SCRIPT_DIR, "models", "mlb_f5_model.pkl")
PITCHER_CACHE = os.path.join(SCRIPT_DIR, ".oraculo_cache", "mlb_pitcher_logs.json")
UMPIRE_CACHE  = os.path.join(SCRIPT_DIR, ".oraculo_cache", "mlb_umpire_logs.json")

TRAIN_DAYS = 365
LINES      = [3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]
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

# 17-element feature vector (v1 had 16; umpire_run_dev is new at index 16)
FEATURE_NAMES = [
    "hp_fip","ap_fip","avg_fip","hp_k9","ap_k9","hp_ip","ap_ip",
    "home_f5_off","away_f5_off","home_f5_def","away_f5_def",
    "combined_off","combined_def","park_factor","is_dome","weather_adj",
    "umpire_run_dev",
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
            umpire_id INTEGER,
            f5_home   INTEGER,
            f5_away   INTEGER,
            f5_total  INTEGER,
            status    TEXT
        )""")
    try:
        conn.execute("ALTER TABLE f5_games ADD COLUMN umpire_id INTEGER")
        conn.commit()
    except Exception:
        pass
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_date  ON f5_games(date)",
        "CREATE INDEX IF NOT EXISTS idx_home  ON f5_games(home)",
        "CREATE INDEX IF NOT EXISTS idx_away  ON f5_games(away)",
        "CREATE INDEX IF NOT EXISTS idx_hp    ON f5_games(hp_id)",
        "CREATE INDEX IF NOT EXISTS idx_ap    ON f5_games(ap_id)",
        "CREATE INDEX IF NOT EXISTS idx_ump   ON f5_games(umpire_id)",
    ]:
        conn.execute(idx_sql)
    conn.commit()
    return conn


def fetch_f5_history(days_back=TRAIN_DAYS, force_refresh=False):
    """Fetch MLB schedule with linescore + umpire officials. Incremental."""
    conn = _init_db()
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    if not force_refresh:
        cursor = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM f5_games WHERE status='Final'")
        row = cursor.fetchone()
        if row and row[2] and row[2] > 100:
            cached_end = datetime.strptime(row[1], "%Y-%m-%d").date() if row[1] else None
            if cached_end and cached_end >= end_date - timedelta(days=2):
                log.info("F5 DB has %d games (%s -> %s)", row[2], row[0], row[1])
                return conn
            if cached_end:
                start_date = cached_end - timedelta(days=1)

    log.info("Fetching F5 history %s -> %s", start_date, end_date)
    batch_size  = 7
    cursor_date = start_date

    while cursor_date <= end_date:
        batch_end = min(cursor_date + timedelta(days=batch_size - 1), end_date)
        url = (f"{MLB_API}/schedule"
               f"?sportId=1"
               f"&startDate={cursor_date}&endDate={batch_end}"
               f"&hydrate=probablePitcher,linescore,officials,team"
               f"&gameType=R")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("F5 schedule fetch error %s: %s", cursor_date, e)
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

    umpire_id = None
    for official in game.get("officials", []):
        otype = official.get("officialType", "") or official.get("type", {}).get("description", "")
        if "Home Plate" in otype or otype.lower() in ("hp", "home plate"):
            umpire_id = official.get("official", {}).get("id")
            break

    f5_home = f5_away = f5_total = None
    if status == "Final":
        innings = game.get("linescore", {}).get("innings", [])
        hr = sum(inn.get("home", {}).get("runs", 0) or 0 for inn in innings[:5])
        ar = sum(inn.get("away", {}).get("runs", 0) or 0 for inn in innings[:5])
        f5_home, f5_away, f5_total = hr, ar, hr + ar

    conn.execute("""
        INSERT OR REPLACE INTO f5_games
        (game_pk, date, home, away, hp_id, ap_id, umpire_id,
         f5_home, f5_away, f5_total, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (pk, game_date, home_name, away_name, hp_id, ap_id, umpire_id,
          f5_home, f5_away, f5_total, status))


# ── Pitcher cache ──────────────────────────────────────────────────────────────

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
    if season is None:
        season = datetime.utcnow().year

    cache    = _load_pitcher_cache()
    now_ts   = time.time()
    ttl      = 12 * 3600
    results  = {}
    to_fetch = []

    for pid in pitcher_ids:
        key = f"{pid}_{season}"
        if key in cache and now_ts - cache[key].get("ts", 0) < ttl:
            results[pid] = cache[key]["games"]
        else:
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
            log.warning("Pitcher gamelog error pid=%s: %s", pid, e)
            results[pid] = []
            continue

        games = []
        for sg in data.get("stats", []):
            for split in sg.get("splits", []):
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
    try:
        ip_str = str(ip_str)
        if "." in ip_str:
            w, f = ip_str.split(".", 1)
            return int(w) + int(f) / 3.0
        return float(ip_str)
    except Exception:
        return 0.0


# ── Umpire stats ───────────────────────────────────────────────────────────────

def get_umpire_run_dev(conn, umpire_id, as_of_date, n=20):
    """
    Deviation of umpire's avg F5 total from league avg (4.35).
    Negative = pitcher-friendly zone. Returns 0.0 when unknown.
    """
    if umpire_id is None:
        return 0.0

    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT f5_total FROM f5_games WHERE umpire_id=? AND date<? "
        "AND status='Final' AND f5_total IS NOT NULL ORDER BY date DESC LIMIT ?",
        (umpire_id, cutoff, n)
    ).fetchall()

    vals = [r[0] for r in rows]
    if len(vals) < 5:
        return 0.0
    league_avg = 4.35
    return round(sum(vals) / len(vals) - league_avg, 3)


# ── Recent stats helpers ───────────────────────────────────────────────────────

def _pitcher_recent(games, as_of_date, n=3):
    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.strftime("%Y-%m-%d")
    past   = [g for g in games if g["date"] < cutoff]
    if not past:
        return {"fip": 4.50, "k9": 7.0, "avg_ip": 5.0}

    recent  = past[-n:]
    tot_ip  = sum(g["ip"] for g in recent)
    tot_k   = sum(g["k"]  for g in recent)
    tot_bb  = sum(g["bb"] for g in recent)
    tot_hbp = sum(g["hbp"] for g in recent)
    tot_hr  = sum(g["hr"] for g in recent)

    fip    = _fip(tot_hr, tot_bb, tot_hbp, tot_k, tot_ip)
    k9     = round(9 * tot_k / tot_ip, 2) if tot_ip > 0 else 7.0
    avg_ip = round(tot_ip / len(recent), 2)
    return {"fip": fip, "k9": k9, "avg_ip": avg_ip}


def _team_f5_recent(conn, team, as_of_date, n=10):
    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT f5_home, f5_away FROM f5_games WHERE home=? AND date<? "
        "AND status='Final' AND f5_total IS NOT NULL ORDER BY date DESC LIMIT ?",
        (team, cutoff, n)
    ).fetchall()
    h_sc = [r[0] for r in rows if r[0] is not None]
    h_al = [r[1] for r in rows if r[1] is not None]

    rows2 = conn.execute(
        "SELECT f5_home, f5_away FROM f5_games WHERE away=? AND date<? "
        "AND status='Final' AND f5_total IS NOT NULL ORDER BY date DESC LIMIT ?",
        (team, cutoff, n)
    ).fetchall()
    a_sc = [r[1] for r in rows2 if r[1] is not None]
    a_al = [r[0] for r in rows2 if r[0] is not None]

    all_sc = h_sc + a_sc
    all_al = h_al + a_al
    avg_sc = round(sum(all_sc) / len(all_sc), 3) if all_sc else 2.3
    avg_al = round(sum(all_al) / len(all_al), 3) if all_al else 2.3
    return avg_sc, avg_al


# ── Feature builder ────────────────────────────────────────────────────────────

def build_features(home, away, hp_stats, ap_stats,
                   home_f5, away_f5,
                   park_factor=1.0, is_dome=False, weather_adj=0.0,
                   umpire_run_dev=0.0):
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
        hp_k9, ap_k9, hp_ip, ap_ip,
        home_off, away_off, home_def, away_def,
        combined_off, combined_def,
        park_factor,
        1.0 if is_dome else 0.0,
        weather_adj,
        umpire_run_dev,
    ]


# ── Model class ────────────────────────────────────────────────────────────────

class MLBF5Model:
    """LogisticRegression + Platt calibration per F5 line. v2."""

    def __init__(self):
        self.models     = {}
        self.scalers    = {}
        self.thresholds = {}
        self.train_date = None
        self.n_samples  = 0

    def fit(self, X, y_dict):
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.metrics import precision_recall_curve
            import numpy as np
        except ImportError:
            log.error("scikit-learn not installed; cannot train F5 model")
            return self

        Xarr = np.array(X, dtype=float)
        self.n_samples = len(Xarr)
        log.info("Training F5 model v2 on %d samples", self.n_samples)

        for line in LINES:
            y = np.array(y_dict.get(line, []), dtype=int)
            if len(y) != len(Xarr) or y.sum() < 10:
                log.warning("Skipping line %s: insufficient positives (%d)", line, y.sum())
                continue

            scaler = StandardScaler()
            Xs     = scaler.fit_transform(Xarr)

            base = LogisticRegression(
                C=1.0, max_iter=500, class_weight="balanced",
                solver="lbfgs", random_state=42
            )

            # Platt calibration when we have enough samples
            if len(y) >= 500:
                clf = CalibratedClassifierCV(base, method="sigmoid", cv=5)
            else:
                clf = base
            clf.fit(Xs, y)
            proba = clf.predict_proba(Xs)[:, 1]

            prec, rec, thresholds = precision_recall_curve(y, proba)
            f1       = 2 * prec * rec / (prec + rec + 1e-9)
            best_idx = int(f1.argmax())
            best_thr = float(thresholds[best_idx]) if best_idx < len(thresholds) else 0.5

            self.models[line]     = clf
            self.scalers[line]    = scaler
            self.thresholds[line] = round(best_thr, 4)
            log.info("  line=%s: threshold=%.3f, positives=%d/%d",
                     line, best_thr, y.sum(), len(y))

        self.train_date = datetime.utcnow().isoformat()
        return self

    def predict(self, features, line):
        clf    = self.models.get(line)
        scaler = self.scalers.get(line)
        thr    = self.thresholds.get(line, 0.50)

        if clf is None or scaler is None:
            avg_fip   = (features[0] + features[1]) / 2.0
            est_runs  = max(0.5, (4.50 - avg_fip) * 0.6 + 4.5) * features[13]
            prob_over = min(0.85, max(0.15, 0.5 + (est_runs - line) * 0.08))
            return {
                "prob_over": round(prob_over, 4),
                "prob_under": round(1 - prob_over, 4),
                "edge": round(abs(prob_over - 0.5) * 2, 4),
                "pick": "OVER" if prob_over > 0.5 else "UNDER",
                "confidence": "low", "model": "fallback",
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
            "prob_over": prob_over, "prob_under": prob_under,
            "edge": edge, "pick": pick, "confidence": confidence,
            "threshold": round(thr, 4), "model": "logistic_platt_v2",
        }

    def save(self):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        data = {
            "models": self.models, "scalers": self.scalers,
            "thresholds": self.thresholds, "train_date": self.train_date,
            "n_samples": self.n_samples, "version": 2,
        }
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(data, f)
        log.info("F5 model v2 saved -> %s", MODEL_PATH)

    @classmethod
    def load(cls):
        if not os.path.exists(MODEL_PATH):
            return None
        try:
            with open(MODEL_PATH, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict):
                obj = cls()
                obj.models     = data["models"]
                obj.scalers    = data["scalers"]
                obj.thresholds = data["thresholds"]
                obj.train_date = data.get("train_date", "")
                obj.n_samples  = data.get("n_samples", 0)
            else:
                obj = data
            log.info("F5 model loaded v2 (%d samples, %s)", obj.n_samples, obj.train_date)
            return obj
        except Exception as e:
            log.warning("F5 model load error: %s", e)
            return None


# ── Training pipeline ──────────────────────────────────────────────────────────

def train_f5_model(days_back=TRAIN_DAYS, force=False):
    """Fetch 365d, build 17-feature vectors with umpire, train+calibrate, save."""
    log.info("=== F5 training pipeline v2 start (TRAIN_DAYS=%d) ===", days_back)
    conn = fetch_f5_history(days_back=days_back, force_refresh=force)

    season = datetime.utcnow().year
    rows   = conn.execute("""
        SELECT game_pk, date, home, away, hp_id, ap_id, umpire_id, f5_total
        FROM f5_games
        WHERE status='Final' AND f5_total IS NOT NULL
          AND hp_id IS NOT NULL AND ap_id IS NOT NULL
        ORDER BY date ASC
    """).fetchall()

    log.info("Training games: %d", len(rows))
    if len(rows) < 50:
        log.warning("Insufficient training data, aborting")
        return None

    pitcher_ids = set()
    for row in rows:
        if row[4]: pitcher_ids.add(row[4])
        if row[5]: pitcher_ids.add(row[5])

    log.info("Fetching gamelogs for %d pitchers", len(pitcher_ids))
    pitcher_logs = fetch_pitcher_gamelogs(list(pitcher_ids), season=season)

    X      = []
    y_dict = {line: [] for line in LINES}

    for (pk, gdate, home, away, hp_id, ap_id, umpire_id, f5_total) in rows:
        hp_stats = _pitcher_recent(pitcher_logs.get(hp_id, []), gdate, n=3)
        ap_stats = _pitcher_recent(pitcher_logs.get(ap_id, []), gdate, n=3)
        home_f5  = _team_f5_recent(conn, home, gdate, n=10)
        away_f5  = _team_f5_recent(conn, away, gdate, n=10)
        pf       = PARK_FACTORS.get(home, 1.00)
        is_dome  = home in DOME_TEAMS
        ump_dev  = get_umpire_run_dev(conn, umpire_id, gdate, n=20)

        features = build_features(home, away, hp_stats, ap_stats,
                                  home_f5, away_f5, pf, is_dome, 0.0, ump_dev)
        X.append(features)
        for line in LINES:
            y_dict[line].append(1 if f5_total > line else 0)

    log.info("Built %d feature vectors", len(X))
    model = MLBF5Model()
    model.fit(X, y_dict)
    model.save()
    conn.close()
    log.info("=== F5 training pipeline v2 complete ===")
    return model


# ── Cached model getter ────────────────────────────────────────────────────────

_cached_model = None
_model_mtime  = None

def get_model():
    global _cached_model, _model_mtime
    if os.path.exists(MODEL_PATH):
        mtime = os.path.getmtime(MODEL_PATH)
        if _cached_model is None or mtime != _model_mtime:
            _cached_model = MLBF5Model.load()
            _model_mtime  = mtime
    if _cached_model is None:
        log.info("No F5 model found, triggering auto-train")
        _cached_model = train_f5_model()
        if os.path.exists(MODEL_PATH):
            _model_mtime = os.path.getmtime(MODEL_PATH)
    return _cached_model


# ── Live prediction ────────────────────────────────────────────────────────────

def predict_live(home, away, hp_id, ap_id, line=4.5,
                 weather_adj=0.0, season=None, umpire_id=None):
    if season is None:
        season = datetime.utcnow().year

    today = datetime.utcnow().strftime("%Y-%m-%d")
    valid = [p for p in [hp_id, ap_id] if p is not None]
    pitcher_logs = fetch_pitcher_gamelogs(valid, season=season) if valid else {}

    hp_stats = _pitcher_recent(pitcher_logs.get(hp_id, []), today, n=3)
    ap_stats = _pitcher_recent(pitcher_logs.get(ap_id, []), today, n=3)

    conn    = _init_db()
    home_f5 = _team_f5_recent(conn, home, today, n=10)
    away_f5 = _team_f5_recent(conn, away, today, n=10)
    ump_dev = get_umpire_run_dev(conn, umpire_id, today, n=20)
    conn.close()

    pf      = PARK_FACTORS.get(home, 1.00)
    is_dome = home in DOME_TEAMS

    features = build_features(home, away, hp_stats, ap_stats,
                               home_f5, away_f5, pf, is_dome, weather_adj, ump_dev)

    model = get_model()
    result = model.predict(features, line) if model else {
        "prob_over": 0.5, "prob_under": 0.5, "edge": 0.0,
        "pick": "PASS", "confidence": "low", "model": "none"
    }
    result.update({
        "home": home, "away": away, "line": line,
        "hp_stats": hp_stats, "ap_stats": ap_stats,
        "features": dict(zip(FEATURE_NAMES, features)),
        "park_factor": pf, "is_dome": is_dome,
        "home_f5_off": home_f5[0], "home_f5_def": home_f5[1],
        "away_f5_off": away_f5[0], "away_f5_def": away_f5[1],
        "umpire_run_dev": ump_dev,
    })
    return result


# ── Feature importances ────────────────────────────────────────────────────────

def get_feature_importances():
    model = get_model()
    if model is None:
        return {}
    import numpy as np
    out = {}
    for line, clf in model.models.items():
        try:
            coefs = clf.calibrated_classifiers_[0].estimator.coef_[0]
        except AttributeError:
            try:
                coefs = clf.coef_[0]
            except Exception:
                continue
        pairs = sorted(zip(FEATURE_NAMES, coefs), key=lambda x: abs(x[1]), reverse=True)
        out[line] = [(n, round(float(c), 4)) for n, c in pairs]
        top3 = ", ".join(f"{n}={c:+.3f}" for n, c in pairs[:3])
        log.info("F5 line %s top features: %s", line, top3)
    return out


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="MLB F5 Totals Model v2")
    ap.add_argument("--train",       action="store_true")
    ap.add_argument("--days",        type=int, default=TRAIN_DAYS)
    ap.add_argument("--force",       action="store_true")
    ap.add_argument("--predict",     action="store_true")
    ap.add_argument("--home",        default="New York Yankees")
    ap.add_argument("--away",        default="Boston Red Sox")
    ap.add_argument("--hp-id",       type=int, default=543037)
    ap.add_argument("--ap-id",       type=int, default=605483)
    ap.add_argument("--line",        type=float, default=4.5)
    ap.add_argument("--importances", action="store_true")
    args = ap.parse_args()

    if args.train:
        train_f5_model(days_back=args.days, force=args.force)
    if args.predict:
        res = predict_live(args.home, args.away, args.hp_id, args.ap_id, line=args.line)
        import pprint
        pprint.pprint(res)
    if args.importances:
        imps = get_feature_importances()
        for line, pairs in sorted(imps.items()):
            print(f"\nLine {line}:")
            for name, c in pairs:
                print(f"  {name:25s} {c:+.4f}")
