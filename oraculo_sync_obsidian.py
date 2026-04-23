#!/usr/bin/env python3
"""
oraculo_sync_obsidian.py - Sync Oráculo predictions to Obsidian vault.

Reads prediction JSONs and model stats, writes markdown daily notes
to the configured Obsidian vault directory.
"""

import os
import sys
import json
import glob
import logging
from datetime import datetime

log = logging.getLogger('oraculo.obsidian')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Default vault path - override via env var or CLI arg
DEFAULT_VAULT = os.path.join(SCRIPT_DIR, 'Samael')


def sync_predictions(vault_path=None):
    """Sync latest predictions to Obsidian vault."""
    vault = vault_path or os.environ.get('OBSIDIAN_VAULT', DEFAULT_VAULT)
    if not os.path.exists(vault):
        log.warning('Obsidian vault not found: %s', vault)
        return False

    today = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Find latest prediction file
    pattern = os.path.join(SCRIPT_DIR, 'predicciones_hoy_*.json')
    pred_files = sorted(glob.glob(pattern), reverse=True)

    predictions = []
    pred_date = today
    if pred_files:
        try:
            with open(pred_files[0], 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                predictions = data.get('predictions', [])
                pred_date = data.get('date', today)
            elif isinstance(data, list):
                predictions = data
        except Exception as e:
            log.warning('Failed to read predictions: %s', e)

    # Load model stats from manifest
    model_info = _load_model_info()

    # Load performance log
    perf = _load_performance_log()

    # Build markdown
    content = _build_markdown(predictions, pred_date, model_info, perf, timestamp)

    # Write to vault
    note_path = os.path.join(vault, f'Oraculo - {today}.md')
    with open(note_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log.info('Synced to %s (%d predictions)', note_path, len(predictions))

    # Also update the status file
    status_path = os.path.join(vault, 'Oraculo - Estado Actual.md')
    with open(status_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return True


def _load_model_info():
    """Load model manifest info."""
    manifest_path = os.path.join(SCRIPT_DIR, 'models', 'model_manifest.json')
    if not os.path.exists(manifest_path):
        return {}
    try:
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        models = manifest.get('models', [])
        if models:
            latest = max(models, key=lambda m: m.get('saved_at', ''))
            return latest
        return {}
    except Exception:
        return {}


def _load_performance_log():
    """Load latest performance metrics."""
    log_path = os.path.join(SCRIPT_DIR, 'models', 'performance_log.json')
    if not os.path.exists(log_path):
        return {}
    try:
        with open(log_path, 'r') as f:
            history = json.load(f)
        if history:
            return history[-1]
        return {}
    except Exception:
        return {}


def _build_markdown(predictions, pred_date, model_info, perf, timestamp):
    """Build Obsidian markdown content."""
    lines = []
    lines.append(f'# Oraculo - Predicciones {pred_date}')
    lines.append('')
    lines.append(f'> **Actualizado**: {timestamp}')
    lines.append('')

    # Model info
    if model_info:
        lines.append('## Modelo')
        lines.append(f'- **Nombre**: {model_info.get("model_name", "N/A")}')
        lines.append(f'- **Samples**: {model_info.get("n_samples", 0)}')
        lines.append(f'- **Features**: {model_info.get("n_features", 0)}')
        lines.append(f'- **Guardado**: {model_info.get("saved_at", "N/A")[:19]}')
        lines.append('')

    # Performance
    if perf:
        acc = perf.get('accuracy', 0)
        brier = perf.get('brier_score', -1)
        lines.append('## Rendimiento')
        lines.append(f'- **Accuracy**: {acc*100:.1f}%')
        if brier >= 0:
            lines.append(f'- **Brier Score**: {brier:.4f}')
        lines.append('')

    # Predictions table
    lines.append('## Predicciones')
    lines.append('')

    if predictions:
        high_conf = [p for p in predictions if p.get('confidence', 0) >= 0.70]
        lines.append(f'**Total**: {len(predictions)} partidos | '
                     f'**Alta confianza**: {len(high_conf)}')
        lines.append('')
        lines.append('| # | Partido | Pred | Conf | H% | D% | A% |')
        lines.append('|---|---------|------|------|----|----|-----|')

        for i, p in enumerate(predictions, 1):
            home = p.get('home_team', 'N/A')
            away = p.get('away_team', 'N/A')
            pred = p.get('predicted', 'N/A').upper()
            conf = p.get('confidence', 0) * 100
            hp = p.get('home_prob', 0) * 100
            dp = p.get('draw_prob', 0) * 100
            ap = p.get('away_prob', 0) * 100
            icon = '**' if conf >= 70 else ''
            lines.append(f'| {i} | {icon}{home} vs {away}{icon} | '
                        f'{pred} | {conf:.0f}% | {hp:.0f} | {dp:.0f} | {ap:.0f} |')
    else:
        lines.append('*No hay predicciones disponibles*')

    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('**Sincronizado automaticamente por oraculo_sync_obsidian.py**')
    return '\n'.join(lines)


def sync_bet_results(vault_path=None):
    """Sync bet engine results to Obsidian."""
    vault = vault_path or os.environ.get('OBSIDIAN_VAULT', DEFAULT_VAULT)
    if not os.path.exists(vault):
        return False

    state_file = os.path.join(SCRIPT_DIR, 'picks', 'engine_state.json')
    if not os.path.exists(state_file):
        return False

    with open(state_file, 'r') as f:
        state = json.load(f)

    today = datetime.now().strftime('%Y-%m-%d')
    bankroll = state.get('bankroll', 0)
    active = state.get('active_bets', [])
    settled = state.get('settled_bets', [])

    won = [b for b in settled if b.get('status') == 'WON']
    lost = [b for b in settled if b.get('status') == 'LOST']
    total_profit = sum(b.get('profit', 0) for b in settled)

    lines = []
    lines.append(f'# Oraculo Betting Log - {today}')
    lines.append('')
    lines.append(f'> Actualizado: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('')
    lines.append('## Bankroll')
    lines.append(f'- **Balance**: ${bankroll:.2f} USDC')
    lines.append(f'- **Profit total**: ${total_profit:.2f}')
    lines.append(f'- **Apuestas activas**: {len(active)}')
    lines.append(f'- **Ganadas**: {len(won)}')
    lines.append(f'- **Perdidas**: {len(lost)}')
    if won or lost:
        wr = len(won) / (len(won) + len(lost)) * 100
        lines.append(f'- **Win rate**: {wr:.1f}%')
    lines.append('')

    if active:
        lines.append('## Apuestas Activas')
        lines.append('')
        lines.append('| Partido | Mercado | Odds | Stake |')
        lines.append('|---------|---------|------|-------|')
        for b in active:
            lines.append(f'| {b.get("match","")} | {b.get("market","")} | '
                        f'{b.get("odds",0):.2f} | ${b.get("stake",0):.2f} |')
        lines.append('')

    if settled:
        lines.append('## Historial')
        lines.append('')
        lines.append('| Fecha | Partido | Mercado | Resultado | P&L |')
        lines.append('|-------|---------|---------|-----------|-----|')
        for b in settled[-20:]:
            date = b.get('settled_at', '')[:10]
            result = b.get('status', '')
            profit = b.get('profit', 0)
            icon = '+' if profit > 0 else ''
            lines.append(f'| {date} | {b.get("match","")} | {b.get("market","")} | '
                        f'{result} | {icon}${profit:.2f} |')

    lines.append('')
    lines.append('---')
    lines.append('**Auto-sync por oraculo_sync_obsidian.py**')

    note_path = os.path.join(vault, f'Oraculo Betting - {today}.md')
    with open(note_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    vault = sys.argv[1] if len(sys.argv) > 1 else None
    sync_predictions(vault)
    sync_bet_results(vault)
    print('Sync complete')
