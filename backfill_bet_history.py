
import json, sqlite3, os

SCRIPT_DIR = "/home/noc/oraculo_v2"
LOG_FILE = SCRIPT_DIR + "/predictions_log.jsonl"
DB_FILE = SCRIPT_DIR + "/oraculo.db"

conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

cur.execute("SELECT bet_id FROM bet_history")
existing = {r[0] for r in cur.fetchall()}

rows = [json.loads(l) for l in open(LOG_FILE)]
settled = [r for r in rows if r.get("result") in ("WIN", "LOSS")]

inserted = skipped = errors = 0
for r in settled:
    bid = r.get("bet_id", "")
    if bid and bid in existing:
        skipped += 1
        continue
    try:
        cur.execute("INSERT OR IGNORE INTO bet_history (bet_id, match, league, sport, label, market_type, placed_at, settled_at, stake, price, model_prob, edge, result, pnl, currency) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (bid, r.get("match",""), r.get("league",""), r.get("sport",""), r.get("label",""), r.get("market_type",""), r.get("ts",""), r.get("settled_ts",""), float(r.get("stake") or 0), float(r.get("odds") or 0), float(r.get("model_prob") or 0), float(r.get("edge") or 0), r.get("result",""), float(r.get("win_loss") or 0), r.get("currency","USDT")))
        inserted += 1
    except Exception as e:
        errors += 1

conn.commit()
conn.close()
print(f"Done: inserted={inserted} skipped={skipped} errors={errors}")

conn2 = sqlite3.connect(DB_FILE)
total = conn2.execute("SELECT COUNT(*) FROM bet_history").fetchone()[0]
wins = conn2.execute("SELECT COUNT(*) FROM bet_history WHERE result=?" , ("WIN",)).fetchone()[0]
pnl = conn2.execute("SELECT COALESCE(SUM(pnl),0) FROM bet_history").fetchone()[0]
conn2.close()
print(f"bet_history: {total} rows | {wins}W/{total-wins}L | PnL={pnl:.2f}")
