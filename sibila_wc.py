#!/usr/bin/env python3
"""
sibila_wc.py -- WC 2026 Sibila integration
Auto-resolucion de picks shadow + calibracion xG + reporte diario

Fuente de resultados: Cloudbet market settlement detection
Fallback: entrada manual via --manual

Uso:
  python3 sibila_wc.py               # run all: resolve + report
  python3 sibila_wc.py --report      # solo reporte
  python3 sibila_wc.py --resolve     # solo resolver picks automatico
  python3 sibila_wc.py --calibrate   # solo calibracion JSON
  python3 sibila_wc.py --manual "Mexico vs South Africa" 2 0 1 0
  #                     ^match_name ft_home ft_away ht_home ht_away
"""

import argparse
import json
import logging
import re
import sqlite3
import os
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

SCRIPT_DIR = "/home/noc/oraculo_v2"
DB_PATH = os.path.join(SCRIPT_DIR, "sibila.db")
CFG_PATH = os.path.join(SCRIPT_DIR, "cloudbet_config.json")
WC_LEAGUE_KEY = "soccer-international-world-cup"
CB_BASE = "https://sports-api.cloudbet.com"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sibila_wc")


# ---------------------------------------------------------------------------
# Cloudbet helpers
# ---------------------------------------------------------------------------

def _cb_headers():
    cfg = json.load(open(CFG_PATH))
    return {"X-API-Key": cfg.get("api_key", ""), "accept": "application/json"}


def _team_name(raw):
    if isinstance(raw, dict):
        return raw.get("name", "")
    return str(raw or "")


def _sim(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def fetch_wc_events():
    r = requests.get(
        "{}/pub/v2/odds/competitions/{}".format(CB_BASE, WC_LEAGUE_KEY),
        headers=_cb_headers(),
        timeout=20,
    )
    r.raise_for_status()
    events = r.json().get("events", [])
    return [e for e in events if e.get("home") and e.get("away")]


# ---------------------------------------------------------------------------
# Market settlement detection
# ---------------------------------------------------------------------------

def detect_result_from_markets(event):
    """
    After a WC match finishes, Cloudbet settles markets.
    Winning selections drop to price ~1.0 or get status=SELECTION_WON.
    Returns result dict or None if match not yet settled.
    """
    mkts = event.get("markets", {})
    status = event.get("status", "")

    if status not in ("RESULTED", "SETTLED", "COMPLETED"):
        # Heuristic: all match_odds prices = 0 means post-match suspension
        mo = mkts.get("soccer.match_odds", {})
        subs = list(mo.get("submarkets", {}).values())
        if not subs:
            return None
        sels = subs[0].get("selections", [])
        prices = [s.get("price", 1) for s in sels]
        if not all(p == 0 for p in prices):
            return None

    result = {
        "home": _team_name(event.get("home")),
        "away": _team_name(event.get("away")),
        "cutoff": event.get("cutoffTime", ""),
        "ft_goals_2h": None,
        "match_winner": None,
    }

    # --- 2H total goals ---
    g2h = mkts.get("soccer.total_goals_period_second_half", {})
    best_under = {}
    for sk, sv in g2h.get("submarkets", {}).items():
        m = re.search(r"total=([0-9.]+)", sk)
        if not m:
            continue
        line = float(m.group(1))
        for sel in sv.get("selections", []):
            outcome = sel.get("outcome", "")
            price = sel.get("price", 1.0)
            sel_status = sel.get("status", "")
            won = sel_status == "SELECTION_WON" or (0.99 <= price <= 1.02)
            if outcome == "under" and won:
                best_under[line] = "under_won"
            elif outcome == "over" and won:
                best_under[line] = "over_won"

    if best_under:
        won_unders = sorted([l for l, v in best_under.items() if v == "under_won"])
        lost_unders = sorted([l for l, v in best_under.items() if v == "over_won"])
        if won_unders:
            min_won = won_unders[0]
            if min_won == 0.5:
                result["ft_goals_2h"] = 0
            elif min_won == 1.5:
                result["ft_goals_2h"] = 1
            elif min_won == 2.5:
                result["ft_goals_2h"] = 2 if (lost_unders and max(lost_unders) <= 1.5) else None
        elif lost_unders:
            result["ft_goals_2h"] = int(max(lost_unders) + 0.5)

    # --- Match winner ---
    mo = mkts.get("soccer.match_odds", {})
    for sk, sv in mo.get("submarkets", {}).items():
        for sel in sv.get("selections", []):
            sel_status = sel.get("status", "")
            price = sel.get("price", 0)
            if sel_status == "SELECTION_WON" or (0.99 <= price <= 1.02):
                result["match_winner"] = sel.get("outcome")
                break

    if result["ft_goals_2h"] is not None or result["match_winner"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Pick resolution
# ---------------------------------------------------------------------------

def _match_pick_to_result(pick_match, results_map):
    best_score, best_result = 0, None
    for key, res in results_map.items():
        s = _sim(pick_match, key)
        if s > best_score:
            best_score = s
            best_result = res
    return best_result if best_score > 0.60 else None


def _resolve_pick(pick, result):
    side = str(pick.get("side", "") or "").lower()
    market = str(pick.get("market", "") or "").lower()
    odds = float(pick.get("odds") or 1.0)
    stake = float(pick.get("shadow_stake") or 1.0)

    # Goals 2H Under/Over
    if "goals 2h" in side and result.get("ft_goals_2h") is not None:
        g = result["ft_goals_2h"]
        m = re.search(r"(under|over)\s*([0-9.]+)", side)
        if m:
            direction, line = m.group(1), float(m.group(2))
            if abs(g - line) < 0.01:
                return "VOID", 0.0
            won = (direction == "under" and g < line) or (direction == "over" and g > line)
            pnl = round(stake * (odds - 1), 4) if won else round(-stake, 4)
            return ("WIN" if won else "LOSS"), pnl

    # 1X2
    if "result_1x2" in market and result.get("match_winner"):
        m = re.search(r"(home|draw|away)", side)
        if m:
            won = m.group(1) == result["match_winner"]
            pnl = round(stake * (odds - 1), 4) if won else round(-stake, 4)
            return ("WIN" if won else "LOSS"), pnl

    return None, 0.0


def resolve_wc_picks(manual_results=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, match, market, side, odds, shadow_stake "
        "FROM sibila_picks WHERE league = ? AND result IS NULL",
        (WC_LEAGUE_KEY,),
    )
    pending = cur.fetchall()
    log.info("WC pending picks: %d", len(pending))

    if not pending:
        conn.close()
        return 0

    results_map = {}
    if manual_results:
        for r in manual_results:
            key = "{} vs {}".format(r["home"], r["away"])
            results_map[key] = r
    else:
        try:
            events = fetch_wc_events()
            for e in events:
                res = detect_result_from_markets(e)
                if res:
                    key = "{} vs {}".format(res["home"], res["away"])
                    results_map[key] = res
            log.info("Detected %d settled results from Cloudbet", len(results_map))
        except Exception as ex:
            log.warning("Cloudbet detection failed: %s", ex)

    if not results_map:
        log.info("No settled results found yet.")
        conn.close()
        return 0

    resolved = 0
    for pick in pending:
        result = _match_pick_to_result(pick["match"], results_map)
        if not result:
            continue
        outcome, pnl = _resolve_pick(dict(pick), result)
        if outcome is None:
            continue
        cur.execute(
            "UPDATE sibila_picks SET result=?, pnl=?, resolved_ts=? WHERE id=?",
            (outcome, pnl, datetime.now(timezone.utc).isoformat(), pick["id"]),
        )
        log.info("RESOLVED: %s | %s | %s | pnl=%.4f", pick["match"], pick["side"], outcome, pnl)
        resolved += 1

    conn.commit()
    conn.close()
    log.info("Resolved %d/%d WC picks", resolved, len(pending))
    return resolved


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def analyze_wc_calibration():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT side, prob_model, odds, result, pnl FROM sibila_picks "
        "WHERE league = ? AND result IN ('WIN','LOSS','VOID')",
        (WC_LEAGUE_KEY,),
    )
    picks = cur.fetchall()
    conn.close()

    if not picks:
        return {"n": 0, "message": "Sin datos resueltos aun"}

    buckets = {}
    for p in picks:
        side = str(p["side"] or "").lower()
        m = re.search(r"xg ([0-9.]+)", side)
        xg = float(m.group(1)) if m else -1
        bucket = "xG={:.1f}".format(xg) if xg >= 0 else "other"
        if bucket not in buckets:
            buckets[bucket] = {"n": 0, "wins": 0, "pnl": 0.0, "probs": []}
        buckets[bucket]["n"] += 1
        buckets[bucket]["pnl"] += p["pnl"] or 0
        if p["result"] == "WIN":
            buckets[bucket]["wins"] += 1
        buckets[bucket]["probs"].append(p["prob_model"] or 0)

    total = len(picks)
    wins = sum(1 for p in picks if p["result"] == "WIN")
    total_pnl = sum(p["pnl"] or 0 for p in picks)
    brier = sum(
        (float(p["prob_model"] or 0) - (1 if p["result"] == "WIN" else 0)) ** 2
        for p in picks if p["result"] in ("WIN", "LOSS")
    ) / total

    return {
        "n": total,
        "wins": wins,
        "wr_pct": round(100 * wins / total, 1),
        "pnl": round(total_pnl, 2),
        "brier_score": round(brier, 4),
        "xg_buckets": {
            k: {
                "n": v["n"],
                "wr_pct": round(100 * v["wins"] / v["n"], 1) if v["n"] else 0,
                "pnl": round(v["pnl"], 2),
                "avg_prob": round(sum(v["probs"]) / len(v["probs"]), 3),
            }
            for k, v in sorted(buckets.items())
        },
    }


def recommend_gate(calib):
    if calib["n"] < 10:
        return "Insuficientes datos (N<10). Mantener gate actual xG<=0.4."
    recs = []
    for bucket, stats in calib.get("xg_buckets", {}).items():
        if stats["n"] < 3:
            continue
        wr = stats["wr_pct"]
        if wr < 60:
            recs.append("BLOQUEAR {}: WR={}% (N={})".format(bucket, wr, stats["n"]))
        elif wr > 85:
            recs.append("MANTENER {}: WR={}% (N={}) OK".format(bucket, wr, stats["n"]))
    return "\n".join(recs) if recs else "Gate actual parece correcto con datos disponibles."


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

def generate_wc_report():
    calib = analyze_wc_calibration()
    gate_rec = recommend_gate(calib)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = ["=== SIBILA WC 2026 REPORT -- {} ===".format(now), ""]

    if calib["n"] == 0:
        lines.append("Sin picks resueltos aun. Esperando primeros resultados del torneo.")
    else:
        lines += [
            "Picks resueltos: {} | Wins: {} | WR: {}%".format(
                calib["n"], calib["wins"], calib["wr_pct"]),
            "Shadow PnL: ${:+.2f} | Brier: {:.4f} (0=perfecto, 0.25=random)".format(
                calib["pnl"], calib["brier_score"]),
            "",
            "xG buckets:",
        ]
        for bucket, stats in calib["xg_buckets"].items():
            lines.append("  {:12s} N={:3d} WR={:5.1f}% PnL=${:+7.2f}".format(
                bucket, stats["n"], stats["wr_pct"], stats["pnl"]))
        lines += ["", "Recomendaciones gate:", gate_rec]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sibila_picks WHERE league=? AND result IS NULL",
                (WC_LEAGUE_KEY,))
    pending = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sibila_picks WHERE league=?", (WC_LEAGUE_KEY,))
    total_wc = cur.fetchone()[0]
    conn.close()

    lines += [
        "",
        "Picks pendientes: {} / {} WC total".format(pending, total_wc),
        "",
        "Uso manual: python3 sibila_wc.py --manual \"Mexico vs South Africa\" 2 0 1 0",
        "           (ft_home ft_away ht_home ht_away)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sibila WC 2026")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument(
        "--manual", nargs=5,
        metavar=("MATCH", "FT_H", "FT_A", "HT_H", "HT_A"),
        help='Ej: "Mexico vs South Africa" 2 0 1 0',
    )
    args = parser.parse_args()

    manual_results = None
    if args.manual:
        match_name, ft_h, ft_a, ht_h, ht_a = args.manual
        ft_h, ft_a, ht_h, ht_a = int(ft_h), int(ft_a), int(ht_h), int(ht_a)
        goals_2h = (ft_h - ht_h) + (ft_a - ht_a)
        winner = "home" if ft_h > ft_a else ("away" if ft_a > ft_h else "draw")
        teams = match_name.split(" vs ")
        manual_results = [{
            "home": teams[0].strip() if len(teams) > 0 else match_name,
            "away": teams[1].strip() if len(teams) > 1 else "",
            "ft_goals_2h": goals_2h,
            "match_winner": winner,
        }]
        log.info("Manual: %s | 2H goals=%d | winner=%s", match_name, goals_2h, winner)
        resolve_wc_picks(manual_results)
        print(generate_wc_report())
        return

    if args.calibrate:
        calib = analyze_wc_calibration()
        print(json.dumps(calib, indent=2))
        print(recommend_gate(calib))
    elif args.resolve:
        resolve_wc_picks()
        print(generate_wc_report())
    else:
        # default: resolve + report
        resolve_wc_picks()
        print(generate_wc_report())


if __name__ == "__main__":
    main()
