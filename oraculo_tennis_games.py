"""
oraculo_tennis_games.py — Modelo de games totales (mercado tennis.total_games).

Reusa el modelo serve/return de oraculo_tennis (_p_win_game: hold prob desde SPW)
y simula el partido game-by-game para obtener la DISTRIBUCION de games totales.
Rinde P(over linea) para comparar contra la linea de Cloudbet (tennis.total_games).

Research (Tier 1): los books auto-generan totales con formulas que mis-calibran
para matchups especificos (grandes servidores -> mas holds -> mas games/tiebreaks).
Confiamos en el moneyline y buscamos el error en el total derivado.

USO (cantera/shadow): prob_over(line, spw_a, spw_b, best_of=3) -> P(games totales > line)
"""
import random
try:
    from oraculo_tennis import _p_win_game
except Exception:
    def _p_win_game(p):
        if p <= 0: return 0.0
        if p >= 1: return 1.0
        q = 1.0 - p
        pre = p**4 + 4*(p**4)*q + 10*(p**4)*(q**2)
        pdr = 20*(p**3)*(q**3); pwd = (p**2)/(p**2+q**2)
        return pre + pdr*pwd


def _play_set(hold_a, hold_b, server, rng):
    """Simula un set. server: 0=A saca primero. Devuelve (games_a, games_b)."""
    ga = gb = 0
    s = server
    while True:
        # quien saca este game
        hold = hold_a if s == 0 else hold_b
        winner_server = rng.random() < hold
        if (s == 0 and winner_server) or (s == 1 and not winner_server):
            ga += 1
        else:
            gb += 1
        s = 1 - s
        # fin de set
        if ga >= 6 and ga - gb >= 2: return ga, gb
        if gb >= 6 and gb - ga >= 2: return ga, gb
        if ga == 7 and gb == 5: return ga, gb
        if gb == 7 and ga == 5: return ga, gb
        if ga == 6 and gb == 6:
            # tiebreak: ventaja leve al mejor hold; cuenta como 1 game (7-6)
            p_tb = max(0.05, min(0.95, 0.5 + (hold_a - hold_b) * 0.5))
            if rng.random() < p_tb: return 7, 6
            return 6, 7


def simulate_total_games(spw_a, spw_b, best_of=3, n_sim=4000, seed=12345):
    """Devuelve lista de games totales simulados."""
    rng = random.Random(seed)
    hold_a = _p_win_game(float(spw_a))
    hold_b = _p_win_game(float(spw_b))
    need = 2 if best_of == 3 else 3
    out = []
    for _ in range(n_sim):
        sa = sb = total = 0
        server = 0
        while sa < need and sb < need:
            ga, gb = _play_set(hold_a, hold_b, server, rng)
            total += ga + gb
            if ga > gb: sa += 1
            else: sb += 1
            server = 1 - server  # alterna saque inicial entre sets (aprox)
        out.append(total)
    return out


def prob_over(line, spw_a, spw_b, best_of=3, n_sim=4000):
    """P(games totales > line)."""
    sims = simulate_total_games(spw_a, spw_b, best_of, n_sim)
    over = sum(1 for g in sims if g > line)
    return over / len(sims)


def expected_games(spw_a, spw_b, best_of=3, n_sim=4000):
    sims = simulate_total_games(spw_a, spw_b, best_of, n_sim)
    return sum(sims) / len(sims)


if __name__ == '__main__':
    print('=== test: games esperados por matchup (best-of-3) ===')
    cases = [
        ('2 servebots parejos (SPW .70/.70)', .70, .70),
        ('parejo medio (SPW .62/.62)', .62, .62),
        ('blowout (SPW .70/.55)', .70, .55),
        ('clay parejo bajo saque (.58/.58)', .58, .58),
    ]
    for name, a, b in cases:
        eg = expected_games(a, b)
        po225 = prob_over(22.5, a, b)
        print('  %-38s E[games]=%.1f  P(over 22.5)=%.0f%%' % (name, eg, po225*100))
