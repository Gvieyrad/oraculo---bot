#!/usr/bin/env python3
"""
oraculo_health.py - Health check for the Oráculo prediction system.

Validates configuration, API connectivity, model freshness, and cache state.
Returns JSON status suitable for monitoring.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger('oraculo.health')


def check_health():
    """Run all health checks. Returns dict with overall status."""
    checks = {}

    # 1. Python version
    checks['python'] = {
        'ok': sys.version_info >= (3, 10),
        'version': '%d.%d.%d' % sys.version_info[:3],
    }

    # 2. Dependencies
    checks['dependencies'] = _check_dependencies()

    # 3. API keys configured
    checks['api_keys'] = _check_api_keys()

    # 4. Model exists and is recent
    checks['model'] = _check_model()

    # 5. Cache state
    checks['cache'] = _check_cache()

    # 6. Config files
    checks['configs'] = _check_configs()

    # Overall
    all_ok = all(c.get('ok', False) for c in checks.values())
    return {
        'status': 'healthy' if all_ok else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        'checks': checks,
    }


def _check_dependencies():
    """Check required Python packages."""
    required = ['numpy', 'sklearn', 'joblib', 'requests']
    optional = ['xgboost', 'lightgbm', 'betfairlightweight']
    missing = []
    found = []

    for pkg in required:
        try:
            __import__(pkg)
            found.append(pkg)
        except ImportError:
            missing.append(pkg)

    opt_status = {}
    for pkg in optional:
        try:
            __import__(pkg)
            opt_status[pkg] = True
        except ImportError:
            opt_status[pkg] = False

    return {
        'ok': len(missing) == 0,
        'found': found,
        'missing': missing,
        'optional': opt_status,
    }


def _check_api_keys():
    """Check that API keys are configured (not placeholders)."""
    results = {}

    # Football Data
    football_key = os.environ.get('FOOTBALL_DATA_ORG_KEY', '')
    if not football_key:
        cfg_path = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                football_key = cfg.get('football_data_org_key', '')
            except Exception:
                pass
    results['football_data'] = bool(football_key) and not football_key.startswith('TU_')

    # Odds API
    odds_key = os.environ.get('ODDS_API_KEY', '')
    if not odds_key:
        cfg_path = os.path.join(SCRIPT_DIR, 'oraculo_config.json')
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                odds_key = cfg.get('odds_api_key', '')
            except Exception:
                pass
    results['odds_api'] = bool(odds_key) and not odds_key.startswith('TU_')

    # Cloudbet
    cb_key = os.environ.get('CLOUDBET_API_KEY', '')
    if not cb_key:
        cb_cfg = os.path.join(SCRIPT_DIR, 'cloudbet_config.json')
        if os.path.exists(cb_cfg):
            try:
                with open(cb_cfg) as f:
                    cfg = json.load(f)
                cb_key = cfg.get('api_key', '')
            except Exception:
                pass
    results['cloudbet'] = bool(cb_key) and not cb_key.startswith('TU_')

    # At minimum football_data should be configured
    return {
        'ok': results.get('football_data', False),
        'keys': results,
    }


def _check_model():
    """Check model files exist and are recent."""
    models_dir = os.path.join(SCRIPT_DIR, 'models')
    enhanced_dir = os.path.join(SCRIPT_DIR, 'models_enhanced')

    model_files = []
    for d in [models_dir, enhanced_dir]:
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith(('.pkl', '.joblib')):
                    fp = os.path.join(d, f)
                    age_days = (time.time() - os.path.getmtime(fp)) / 86400
                    model_files.append({
                        'name': f,
                        'size_kb': round(os.path.getsize(fp) / 1024, 1),
                        'age_days': round(age_days, 1),
                    })

    # Check manifest
    manifest_path = os.path.join(models_dir, 'model_manifest.json')
    has_manifest = os.path.exists(manifest_path)

    recent = any(m['age_days'] < 7 for m in model_files)

    return {
        'ok': len(model_files) > 0,
        'recent': recent,
        'has_manifest': has_manifest,
        'models': model_files,
    }


def _check_cache():
    """Check cache directory state."""
    cache_dir = os.path.join(SCRIPT_DIR, '.oraculo_cache')
    if not os.path.exists(cache_dir):
        return {'ok': True, 'namespaces': {}}

    namespaces = {}
    for ns in os.listdir(cache_dir):
        ns_path = os.path.join(cache_dir, ns)
        if os.path.isdir(ns_path):
            files = [f for f in os.listdir(ns_path) if f.endswith('.json')]
            total_size = sum(os.path.getsize(os.path.join(ns_path, f))
                          for f in files)
            namespaces[ns] = {
                'files': len(files),
                'size_kb': round(total_size / 1024, 1),
            }

    return {
        'ok': True,
        'writable': os.access(cache_dir, os.W_OK),
        'namespaces': namespaces,
    }


def _check_configs():
    """Check config files exist."""
    configs = {
        'oraculo_config.json': False,
        'cloudbet_config.json': False,
        'cloudbet_betting_config.json': False,
        'auto_betting_config.json': False,
    }
    for name in configs:
        configs[name] = os.path.exists(os.path.join(SCRIPT_DIR, name))

    return {
        'ok': True,  # Configs are optional
        'files': configs,
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    result = check_health()

    if '--json' in sys.argv:
        print(json.dumps(result, indent=2))
    else:
        status = result['status'].upper()
        icon = 'OK' if status == 'HEALTHY' else 'FAIL'
        print('=' * 60)
        print('  ORACULO HEALTH CHECK: [%s] %s' % (icon, status))
        print('=' * 60)

        for name, check in result['checks'].items():
            ok = 'OK' if check.get('ok') else 'FAIL'
            print('\n  [%s] %s' % (ok, name))
            for k, v in check.items():
                if k != 'ok':
                    if isinstance(v, dict):
                        for k2, v2 in v.items():
                            print('      %-20s %s' % (k2, v2))
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict):
                                print('      %s' % item.get('name', item))
                            else:
                                print('      %s' % item)
                    else:
                        print('      %-20s %s' % (k, v))

        print('\n' + '=' * 60)
