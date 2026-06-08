#!/bin/bash
# retrain_weekly.sh — Retrain Ganagol DC + GBC models
# Cron: 0 3 * * 6 /home/noc/oraculo_v2/retrain_weekly.sh >> /home/noc/oraculo_v2/retrain.log 2>&1

set -e
cd /home/noc/oraculo_v2
echo "=== $(date) === Ganagol weekly retrain ==="

echo "[1/2] DC retrain..."
python3 ganagol_retrain_v2.py
echo "[1/2] DC retrain done"

echo "[2/2] GBC retrain..."
python3 _train_ganagol_gbc.py
echo "[2/2] GBC retrain done"

echo "=== $(date) === Retrain complete ==="
