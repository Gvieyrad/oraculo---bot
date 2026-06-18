import sys, json, math, random
sys.path.insert(0, "/home/noc/.local/lib/python3.12/site-packages")
sys.path.insert(0, "/home/noc/oraculo_v2")
random.seed(42)

from oraculo_wc_model import predict_match, get_player_adjusted_xg

groups = json.load(open("/home/noc/oraculo_v2/wc2026/wc2026_groups.json"))

def pp(lam, k):
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def poisson_probs(lam_h, lam_a, max_g=6):
    ph, pd, pa = 0.0, 0.0, 0.0
    for i in range(max_g+1):
        for j in range(max_g+1):
            p = pp(lam_h, i) * pp(lam_a, j)
            if i > j: ph += p
            elif i == j: pd += p
            else: pa += p
    return ph, pd, pa

# Overrides post-MD1 (Jun 18):
# Spain 0-0 Cape Verde -> severe atk penalty
# Portugal 1-1 DR Congo -> atk penalty
# Sweden 5-1 Tunisia -> confirmed overperform
# Colombia 3-1 Uzbekistan -> overperform
# Norway 4-1 Iraq -> slight bonus
MANUAL_ATK = {
    "Brazil": 0.78, "Japan": 0.92, "England": 0.88,
    "Spain": 0.68,
    "Portugal": 0.80,
    "Sweden": 1.20,
    "Colombia": 1.10,
    "Norway": 1.05,
}
MANUAL_DEF = {"Netherlands": 0.95}

def get_xg(h, a):
    try:
        tup = get_player_adjusted_xg(h, a, neutral=True)
        hxg, axg = tup[0], tup[1]
    except Exception:
        r = predict_match(h, a, neutral=True)
        hxg, axg = r.get("xg_home", 1.2), r.get("xg_away", 1.0)
    if h in MANUAL_ATK: hxg *= MANUAL_ATK[h]
    if a in MANUAL_ATK: axg *= MANUAL_ATK[a]
    if a in MANUAL_DEF: hxg /= MANUAL_DEF[a]
    if h in MANUAL_DEF: axg /= MANUAL_DEF[h]
    return round(hxg, 3), round(axg, 3)

def best_score(hxg, axg, result, max_g=6):
    best_p, best_hs, best_as_ = -1, 1, 0
    for i in range(max_g+1):
        for j in range(max_g+1):
            p = pp(hxg, i) * pp(axg, j)
            ok = ((result == "H" and i > j) or
                  (result == "A" and j > i) or
                  (result == "D" and i == j))
            if ok and p > best_p:
                best_p, best_hs, best_as_ = p, i, j
    return best_hs, best_as_

def predict_game(h, a):
    hxg, axg = get_xg(h, a)
    ph, pd, pa = poisson_probs(hxg, axg)
    if ph >= pa and ph >= pd: result = "H"
    elif pa > ph and pa > pd: result = "A"
    else: result = "D"
    hs, as_ = best_score(hxg, axg, result)
    return hs, as_, ph, pd, pa

# Resultados reales MD1 (24 partidos)
MD1 = {
    "A": [("Mexico", 2, "South Africa", 0), ("Republic of Korea", 2, "Czech Republic", 1)],
    "B": [("Canada", 1, "Bosnia & Herzegovina", 1), ("Qatar", 1, "Switzerland", 1)],
    "C": [("Brazil", 1, "Morocco", 1), ("Haiti", 0, "Scotland", 1)],
    "D": [("USA", 4, "Paraguay", 1), ("Australia", 2, "Turkey", 0)],
    "E": [("Germany", 7, "Curacao", 1), ("Ivory Coast", 1, "Ecuador", 0)],
    "F": [("Netherlands", 2, "Japan", 2), ("Sweden", 5, "Tunisia", 1)],
    "G": [("Belgium", 1, "Egypt", 1), ("Iran", 2, "New Zealand", 2)],
    "H": [("Spain", 0, "Cape Verde", 0), ("Saudi Arabia", 1, "Uruguay", 1)],
    "I": [("France", 3, "Senegal", 1), ("Iraq", 1, "Norway", 4)],
    "J": [("Argentina", 3, "Algeria", 0), ("Austria", 3, "Jordan", 1)],
    "K": [("Portugal", 1, "DR Congo", 1), ("Uzbekistan", 1, "Colombia", 3)],
    "L": [("England", 4, "Croatia", 2), ("Ghana", 1, "Panama", 0)],
}

print("=" * 70)
print("POLLA WC 2026 - ACTUALIZADO POST-MD1 (Jun 18)")
print("MD1: resultados REALES | MD2+MD3: predicciones modelo ajustado")
print("=" * 70)

group_standings = {}
group_pts_full = {}

for grp in sorted(groups.keys()):
    t0, t1, t2, t3 = groups[grp]
    standings = {t: {"pts":0,"gf":0,"ga":0} for t in [t0,t1,t2,t3]}

    print("\n=== GRUPO %s: %s | %s | %s | %s ===" % (grp, t0, t1, t2, t3))

    for h, hg, a, ag in MD1[grp]:
        hg, ag = int(hg), int(ag)
        if hg > ag: standings[h]["pts"] += 3
        elif ag > hg: standings[a]["pts"] += 3
        else: standings[h]["pts"] += 1; standings[a]["pts"] += 1
        standings[h]["gf"] += hg; standings[h]["ga"] += ag
        standings[a]["gf"] += ag; standings[a]["ga"] += hg
        print("  [REAL]  %-22s %d-%d %-22s" % (h, hg, ag, a))

    for h, a in [(t0, t2), (t1, t3)]:
        hs, as_, ph, pd, pa = predict_game(h, a)
        if hs > as_: standings[h]["pts"] += 3
        elif as_ > hs: standings[a]["pts"] += 3
        else: standings[h]["pts"] += 1; standings[a]["pts"] += 1
        standings[h]["gf"] += hs; standings[h]["ga"] += as_
        standings[a]["gf"] += as_; standings[a]["ga"] += hs
        print("  [MD2]   %-22s %d-%d %-22s  H:%2.0f%% D:%2.0f%% A:%2.0f%%" % (
            h, hs, as_, a, ph*100, pd*100, pa*100))

    for h, a in [(t0, t3), (t1, t2)]:
        hs, as_, ph, pd, pa = predict_game(h, a)
        if hs > as_: standings[h]["pts"] += 3
        elif as_ > hs: standings[a]["pts"] += 3
        else: standings[h]["pts"] += 1; standings[a]["pts"] += 1
        standings[h]["gf"] += hs; standings[h]["ga"] += as_
        standings[a]["gf"] += as_; standings[a]["ga"] += hs
        print("  [MD3]   %-22s %d-%d %-22s  H:%2.0f%% D:%2.0f%% A:%2.0f%%" % (
            h, hs, as_, a, ph*100, pd*100, pa*100))

    for t in standings:
        standings[t]["gd"] = standings[t]["gf"] - standings[t]["ga"]

    ranked = sorted(standings.items(),
                    key=lambda x: (x[1]["pts"], x[1]["gd"], x[1]["gf"]), reverse=True)
    print("\n  #  Equipo                  Pts  GD")
    for i, (t, s) in enumerate(ranked):
        adv = "<-- clasifica" if i < 2 else ""
        print("  %d  %-22s   %d  %+d  %s" % (i+1, t, s["pts"], s["gd"], adv))

    group_standings[grp] = [t for t, _ in ranked]
    group_pts_full[grp] = {t: s for t, s in ranked}

thirds = []
for grp in sorted(group_standings.keys()):
    t = group_standings[grp][2]
    s = group_pts_full[grp][t]
    thirds.append((t, s["pts"], s["gd"], s["gf"], grp))
thirds.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
best3 = [t[0] for t in thirds[:8]]

print("\n" + "=" * 70)
print("8 MEJORES TERCEROS:")
for i, (t, pts, gd, gf, grp) in enumerate(thirds[:8]):
    print("  %d. %-22s (Grp %s) Pts:%d GD:%+d GF:%d" % (i+1, t, grp, pts, gd, gf))

def predict_ko(h, a, label=""):
    hxg, axg = get_xg(h, a)
    ph, pd, pa = poisson_probs(hxg, axg)
    if ph >= pa:
        winner = h; result = "H"; pwin = ph + pd/2
    else:
        winner = a; result = "A"; pwin = pa + pd/2
    hs, as_ = best_score(hxg, axg, result)
    if label:
        print("  %-22s %d-%d %-22s  [%s %.0f%%]" % (h, hs, as_, a, winner, pwin*100))
    return winner, hs, as_

gs = group_standings
bracket = [
    (gs["A"][0], gs["B"][1]),
    (gs["C"][0], gs["D"][1]),
    (gs["E"][0], gs["F"][1]),
    (gs["G"][0], gs["H"][1]),
    (gs["I"][0], gs["J"][1]),
    (gs["K"][0], gs["L"][1]),
    (gs["B"][0], gs["A"][1]),
    (gs["D"][0], gs["C"][1]),
    (gs["F"][0], gs["E"][1]),
    (gs["H"][0], gs["G"][1]),
    (gs["J"][0], gs["I"][1]),
    (gs["L"][0], gs["K"][1]),
    (best3[0], best3[1]),
    (best3[2], best3[3]),
    (best3[4], best3[5]),
    (best3[6], best3[7]),
]

print("\n" + "=" * 70)
print("ELIMINATORIAS")
print("=" * 70)
print("\n--- OCTAVOS (R16) ---")
r16_winners = []
for h, a in bracket:
    w, hs, as_ = predict_ko(h, a, label=True)
    r16_winners.append(w)

print("\n--- CUARTOS DE FINAL ---")
qf_winners = []
for h, a in [(r16_winners[i], r16_winners[i+1]) for i in range(0, 16, 2)]:
    w, hs, as_ = predict_ko(h, a, label=True)
    qf_winners.append(w)

print("\n--- SEMIFINALES (4 equipos) ---")
sf_winners, sf_losers = [], []
for h, a in [(qf_winners[0], qf_winners[1]), (qf_winners[2], qf_winners[3]),
             (qf_winners[4], qf_winners[5]), (qf_winners[6], qf_winners[7])]:
    w, hs, as_ = predict_ko(h, a, label=True)
    sf_winners.append(w)
    sf_losers.append(a if w == h else h)

print("\n--- SEMIFINALES FINALES ---")
fin_teams, third_teams = [], []
for h, a in [(sf_winners[0], sf_winners[1]), (sf_winners[2], sf_winners[3])]:
    w, hs, as_ = predict_ko(h, a, label=True)
    fin_teams.append(w)
    third_teams.append(a if w == h else h)

print("\n--- TERCER PUESTO ---")
predict_ko(third_teams[0], third_teams[1], label=True)

print("\n--- FINAL ---")
f1, f2 = fin_teams[0], fin_teams[1]
hxg, axg = get_xg(f1, f2)
ph, pd, pa = poisson_probs(hxg, axg)
if ph >= pa:
    champion = f1; pwin = ph + pd/2; result = "H"
else:
    champion = f2; pwin = pa + pd/2; result = "A"
hs, as_ = best_score(hxg, axg, result)
print("  FINAL: %-22s %d-%d %-22s" % (f1, hs, as_, f2))
print()
print("  *** CAMPEON: %s (%.0f%%) ***" % (champion, pwin*100))
print()
print("CAMBIOS VS PREDICCION ORIGINAL (Jun 10):")
print("  Champion original: Espana")
print("  Champion nuevo:    %s" % champion)
