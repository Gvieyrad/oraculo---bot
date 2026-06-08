"""
ganagol_standings_csv.py
CSV-based standings for ARG, RUS, JPN (football-data.co.uk data).
Called from ganagol.py after the main API standings load.
"""
import json, os
from pathlib import Path
from datetime import datetime as _dt, timedelta as _td

_CSV_DIR = Path(__file__).parent / ".oraculo_cache" / "csv"

_CSV_LEAGUES = {
    "ARG": "new_ARG.json",
    "RUS": "new_RUS.json",
    "JPN": "new_JPN.json",
}


def _norm(name):
    n = name.lower()
    for tok in (" fc", " sc", " cf", " ac", " as ", " rc ", " cd ", " ud ", " sd "):
        n = n.replace(tok, " ")
    return " ".join(n.strip().split())


def _motivation(pos, total, pts, pts_list):
    pts_2nd  = pts_list[1] if len(pts_list) > 1 else pts
    pts_safe = pts_list[total - 4] if total >= 4 else 0
    bottom3  = pos > (total - 3)
    if bottom3:
        return "REL"
    if pos == 1 and (pts - pts_2nd) >= 12:
        return "CAMP"
    if pos <= 3:
        return "TITLE"
    if pos <= 6:
        return "EUR"
    if (pts - pts_safe) >= 15:
        return "SAFE"
    return "MID"


def load_csv_standings():
    """
    Returns dict: norm_name -> {pos, total, mot, league, name, pts}
    Covers ARG, RUS, JPN using last 365 days of match results.
    """
    result = {}
    for code, fname in _CSV_LEAGUES.items():
        fpath = _CSV_DIR / fname
        if not fpath.exists():
            continue
        try:
            matches = json.load(open(fpath))
        except Exception:
            continue

        valid = []
        for m in matches:
            d = m.get("date", "")
            if not d:
                continue
            try:
                dt = _dt.strptime(d[:10], "%Y-%m-%d")
            except Exception:
                continue
            hg = m.get("home_goals")
            ag = m.get("away_goals")
            if hg is None or ag is None:
                continue
            valid.append((dt, m["home"], m["away"], int(hg), int(ag)))

        if not valid:
            continue

        max_dt = max(v[0] for v in valid)
        cutoff = max_dt - _td(days=365)
        season = [(dt, h, a, hg, ag) for dt, h, a, hg, ag in valid if dt >= cutoff]

        stats = {}
        for _, home, away, hg, ag in season:
            for t in (home, away):
                if t not in stats:
                    stats[t] = {"pts": 0, "gf": 0, "ga": 0}
            if hg > ag:
                stats[home]["pts"] += 3
            elif hg == ag:
                stats[home]["pts"] += 1
                stats[away]["pts"] += 1
            else:
                stats[away]["pts"] += 3
            stats[home]["gf"] += hg; stats[home]["ga"] += ag
            stats[away]["gf"] += ag; stats[away]["ga"] += hg

        ranked = sorted(stats.items(),
                        key=lambda x: (x[1]["pts"], x[1]["gf"] - x[1]["ga"], x[1]["gf"]),
                        reverse=True)

        total = len(ranked)
        pts_list = [v["pts"] for _, v in ranked]

        for pos, (name, s) in enumerate(ranked, 1):
            mot  = _motivation(pos, total, s["pts"], pts_list)
            norm = _norm(name)
            result[norm] = {
                "pos":    pos,
                "total":  total,
                "mot":    mot,
                "league": code,
                "name":   name,
                "pts":    s["pts"],
            }

    return result
