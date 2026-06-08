#!/usr/bin/env python3
"""
ganagol_intl.py — ELO-based 1X2 fallback para equipos fuera del modelo DC.

Cubre:
  - Selecciones nacionales masculinas (~150 equipos, ELO FIFA)
  - Selecciones nacionales femeninas (~30 equipos)
  - Clubes africanos: Botola Pro (Marruecos), Ligue 1 Algerie
  - Clubes USL Championship (USA)

Exporta:
  predict_intl(home_raw, away_raw, neutral=False)
    -> (ph, pd, pa, lam, mu)  o  None si algun equipo no esta en la base
"""
import unicodedata
from math import exp, factorial

# ── Poisson math ──────────────────────────────────────────────────────────────

def _pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return exp(-lam) * (lam ** k) / factorial(k)

def _tau(x, y, lam, mu, rho=-0.13):
    if x == 0 and y == 0: return 1 - lam * mu * rho
    if x == 0 and y == 1: return 1 + lam * rho
    if x == 1 and y == 0: return 1 + mu * rho
    if x == 1 and y == 1: return 1 - rho
    return 1.0

def _poisson_1x2(lam, mu, max_goals=8):
    ph = pd = pa = 0.0
    for i in range(max_goals):
        for j in range(max_goals):
            p = _pmf(i, lam) * _pmf(j, mu) * _tau(i, j, lam, mu)
            if   i > j: ph += p
            elif i == j: pd += p
            else:        pa += p
    t = ph + pd + pa
    return (ph / t, pd / t, pa / t) if t > 0 else (1/3, 1/3, 1/3)

def _elo_to_lambda(elo_h, elo_a, base_h=1.35, base_a=1.08, k=500):
    d = elo_h - elo_a
    lam = max(0.25, min(4.5, base_h * exp(d / k)))
    mu  = max(0.25, min(4.5, base_a * exp(-d / k)))
    return lam, mu

# ── Normalizacion ─────────────────────────────────────────────────────────────

def _norm(s):
    nfkd = unicodedata.normalize('NFKD', s.lower().strip())
    return ''.join(c for c in nfkd if not unicodedata.combining(c))

# ── ELO selecciones masculinas (junio 2026) ───────────────────────────────────

_MEN_ELO_RAW = {
    # Tier S
    'Brasil': 2090, 'Brazil': 2090,
    'Argentina': 2080,
    'Francia': 2065, 'France': 2065,
    'Portugal': 1975,
    'Inglaterra': 1970, 'England': 1970,
    'Espana': 1960, 'Spain': 1960,
    # Tier A
    'Alemania': 1950, 'Germany': 1950,
    'Italia': 1940, 'Italy': 1940,
    'Paises Bajos': 1935, 'Netherlands': 1935, 'Holanda': 1935,
    'Belgica': 1905, 'Belgium': 1905,
    'Croacia': 1845, 'Croatia': 1845,
    'Dinamarca': 1825, 'Denmark': 1825,
    'Suiza': 1845, 'Switzerland': 1845,
    'Serbia': 1830,
    'Ucrania': 1825, 'Ukraine': 1825,
    'Mexico': 1875,
    'Estados Unidos': 1855, 'USA': 1855, 'United States': 1855,
    'Uruguay': 1860,
    'Colombia': 1845,
    'Marruecos': 1825, 'Morocco': 1825,
    'Senegal': 1800,
    'Japon': 1845, 'Japan': 1845,
    'Corea del Sur': 1795, 'South Korea': 1795,
    'Canada': 1815,
    'Rusia': 1790, 'Russia': 1790,
    'Austria': 1755,
    'Escocia': 1755, 'Scotland': 1755,
    'Turquia': 1805, 'Turkey': 1805,
    'Noruega': 1775, 'Norway': 1775,
    # Tier B
    'Suecia': 1775, 'Sweden': 1775,
    'Polonia': 1755, 'Poland': 1755,
    'Chile': 1770,
    'Ecuador': 1745,
    'Hungria': 1765, 'Hungary': 1765,
    'Eslovaquia': 1745, 'Slovakia': 1745,
    'Republica Checa': 1745, 'Czech Republic': 1745, 'Czechia': 1745,
    'Georgia': 1725,
    'Rumania': 1685, 'Romania': 1685,
    'Eslovenia': 1725, 'Slovenia': 1725,
    'Albania': 1645,
    'Peru': 1715,
    'Costa de Marfil': 1745, 'Ivory Coast': 1745,
    'Nigeria': 1725,
    'Argelia': 1735, 'Algeria': 1735,
    'Tunez': 1725, 'Tunisia': 1725,
    'Iran': 1725,
    'Arabia Saudita': 1725, 'Saudi Arabia': 1725,
    'Australia': 1715,
    'Islandia': 1745, 'Iceland': 1745,
    'Gales': 1725, 'Wales': 1725,
    'Paraguay': 1705,
    'Venezuela': 1625,
    'Bolivia': 1605,
    'Costa Rica': 1695,
    'Panama': 1665,
    'Honduras': 1645,
    'Jamaica': 1625,
    'Mali': 1705,
    'Camerun': 1705, 'Cameroon': 1705,
    'Egipto': 1695, 'Egypt': 1695,
    'Ghana': 1675,
    # Tier C
    'Grecia': 1655, 'Greece': 1655,
    'Bosnia y Herzegovina': 1695, 'Bosnia': 1695, 'Bosnia & Herzegovina': 1695,
    'Irlanda': 1685, 'Ireland': 1685,
    'Macedonia del Norte': 1605, 'North Macedonia': 1605, 'FYR Macedonia': 1605,
    'Montenegro': 1595,
    'Armenia': 1625,
    'Azerbaiyan': 1590, 'Azerbaijan': 1590,
    'Irlanda del Norte': 1620, 'Northern Ireland': 1620,
    'Finlandia': 1635, 'Finland': 1635,
    'Guinea': 1625,
    'Burkina Faso': 1650,
    'Uganda': 1590,
    'Tanzania': 1540,
    'Bielorrusia': 1620, 'Belarus': 1620,
    'Siria': 1540, 'Syria': 1540,
    'Zambia': 1555,
    'Kenia': 1525, 'Kenya': 1525,
    'Kazajistan': 1510, 'Kazakhstan': 1510,
    # Tier D
    'Malta': 1355,
    'Andorra': 1375,
    'Luxemburgo': 1495, 'Luxembourg': 1495,
    'San Marino': 1285,
    'Gibraltar': 1325,
    'Liechtenstein': 1345,
}

# ── ELO selecciones femeninas (junio 2026) ────────────────────────────────────

_WOMEN_ELO_RAW = {
    'Espana F': 2055, 'Spain W': 2055, 'Spain Women': 2055,
    'Inglaterra F': 1960, 'England W': 1960, 'England Women': 1960,
    'Suecia F': 1950, 'Sweden W': 1950, 'Sweden Women': 1950,
    'Alemania F': 1860, 'Germany W': 1860,
    'Francia F': 1905, 'France W': 1905,
    'Paises Bajos F': 1845, 'Netherlands W': 1845,
    'Japon F': 1885, 'Japan W': 1885,
    'Australia F': 1855, 'Australia W': 1855,
    'Canada F': 1825, 'Canada W': 1825,
    'Brasil F': 1805, 'Brazil W': 1805,
    'Noruega F': 1785, 'Norway W': 1785,
    'Dinamarca F': 1725, 'Denmark W': 1725, 'Denmark Women': 1725,
    'Italia F': 1725, 'Italy W': 1725,
    'China F': 1755, 'China W': 1755,
    'Corea del Sur F': 1705, 'South Korea W': 1705,
    'Argentina F': 1685, 'Argentina W': 1685,
    'Belgica F': 1705, 'Belgium W': 1705,
    'Suiza F': 1685, 'Switzerland W': 1685,
    'Portugal F': 1665, 'Portugal W': 1665,
    'Colombia F': 1725, 'Colombia W': 1725,
    'Nigeria F': 1665, 'Nigeria W': 1665,
    'Jamaica F': 1605, 'Jamaica W': 1605,
    'Marruecos F': 1585, 'Morocco W': 1585,
    'Nueva Zelanda F': 1665, 'New Zealand W': 1665,
    'Irlanda F': 1605, 'Ireland W': 1605,
    'Escocia F': 1625, 'Scotland W': 1625,
    'Austria F': 1625, 'Austria W': 1625,
    'Polonia F': 1605, 'Poland W': 1605,
    'Islandia F': 1665, 'Iceland W': 1665,
    'Ucrania F': 1615, 'Ukraine W': 1615,
    'Hungria F': 1575, 'Hungary W': 1575,
    'Zambia F': 1595, 'Zambia W': 1595,
}

# ── Clubes africanos y USL (junio 2026) ───────────────────────────────────────

_CLUB_ELO_RAW = {
    # Botola Pro Marruecos
    'Raja Casablanca': 1785, 'Raja': 1785,
    'Wydad Casablanca': 1775, 'Wydad': 1775,
    'FAR Rabat': 1685, 'AS FAR': 1685,
    'Renaissance Berkane': 1665,
    'MO Oujda': 1655, 'Mouloudia Oujda': 1655,
    'FUS Rabat': 1560,
    'HUSA Agadir': 1530, 'Agadir': 1530, 'Hassania Agadir': 1530,
    'CODM Meknes': 1585,
    'Olympic Safi': 1630,
    'Difaa El Jadida': 1630,
    'Olympic Khouribga': 1600,
    # Ligue 1 Algerie Mobilis
    'CR Belouizdad': 1720, 'Belouizdad': 1720, 'CRB': 1720,
    'ES Setif': 1700, 'Setif': 1700,
    'MC Alger': 1695, 'MCA': 1695,
    'USM Alger': 1685, 'USMA': 1685,
    'NA Hussein Dey': 1665,
    'CS Constantine': 1655,
    'JS Kabylie': 1645, 'Kabylie': 1645, 'JSK': 1645,
    'USM Blida': 1570,
    'MC Oran': 1610,
    'ASO Chlef': 1590,
    'RC Relizane': 1555,
    'US Biskra': 1545,
    'MC El Bayadh': 1440, 'El Bayadh': 1440,
    'ES Mostaganem': 1420, 'Mostaganem': 1420,
    # USL Championship 2026
    'Louisville City FC': 1680, 'Louisville City': 1680, 'Louisville': 1680,
    'Birmingham Legion': 1600, 'Birmingham Legion FC': 1600,
    'San Antonio FC': 1650, 'San Antonio': 1650,
    'Sacramento Republic': 1645,
    'Orange County SC': 1625,
    'Phoenix Rising': 1685,
    'Colorado Springs Switchbacks': 1645,
    'Indy Eleven': 1590,
    'Tampa Bay Rowdies': 1635,
    'New Mexico United': 1605,
    'FC Tulsa': 1585,
    'Charleston Battery': 1605,
    'Detroit City FC': 1620,
    'Oakland Roots': 1595,
    'El Paso Locomotive': 1610,
    'Pittsburgh Riverhounds': 1580,
}

# ── Indice normalizado ────────────────────────────────────────────────────────

_INDEX = {}  # norm_name -> (elo, pool)

def _build_index():
    for name, elo in _MEN_ELO_RAW.items():
        _INDEX[_norm(name)] = (float(elo), 'men')
    for name, elo in _WOMEN_ELO_RAW.items():
        _INDEX[_norm(name)] = (float(elo), 'women')
    for name, elo in _CLUB_ELO_RAW.items():
        _INDEX[_norm(name)] = (float(elo), 'club')

_build_index()


def _lookup(name):
    return _INDEX.get(_norm(name))


# ── API publica ───────────────────────────────────────────────────────────────

def predict_intl(home_raw, away_raw, neutral=False, friendly=False,
                 home_elim=False, away_elim=False):
    """
    Genera prediccion 1X2 basada en ELO para equipos fuera del modelo DC.

    friendly=True  : amistoso → reduce goals → mas empates (~35% vs ~27%)
    home_elim=True : local eliminado/clasificado → -15% motivacion
    away_elim=True : visitante eliminado/clasificado → -15% motivacion

    Returns (ph, pd, pa, lam, mu) o None si algun equipo no esta en la base.
    """
    h = _lookup(home_raw)
    a = _lookup(away_raw)
    if h is None or a is None:
        return None

    elo_h, pool_h = h
    elo_a, pool_a = a

    pool = pool_h if pool_h == pool_a else 'men'
    if pool == 'women':
        base_h, base_a = (1.60, 1.22) if not neutral else (1.40, 1.40)
    elif pool == 'club':
        base_h, base_a = (1.45, 1.12) if not neutral else (1.28, 1.28)
    else:
        base_h, base_a = (1.35, 1.08) if not neutral else (1.22, 1.22)

    lam, mu = _elo_to_lambda(elo_h, elo_a, base_h, base_a)

    if friendly:           # amistoso: menos intensidad → menos goles
        lam *= 0.82
        mu  *= 0.82
    if home_elim:          # local eliminado: pierde ventaja local
        lam *= 0.85
    if away_elim:          # visitante eliminado: menos presion
        mu  *= 0.85
    ph, pd, pa = _poisson_1x2(lam, mu)
    return ph, pd, pa, lam, mu


def get_elo(name):
    r = _lookup(name)
    return r[0] if r else None


if __name__ == '__main__':
    tests = [
        ('Suecia', 'Grecia', False),
        ('Hungria', 'Finlandia', False),
        ('Eslovaquia', 'Montenegro', False),
        ('Rusia', 'Burkina Faso', False),
        ('Irlanda del Norte', 'Guinea', False),
        ('Azerbaiyan', 'Malta', False),
        ('Bielorrusia', 'Siria', False),
        ('Tanzania', 'Uganda', False),
        ('Agadir', 'FUS Rabat', False),
        ('JS Kabylie', 'Belouizdad', False),
        ('Mostaganem', 'El Bayadh', False),
        ('Espana F', 'Inglaterra F', False),
        ('Dinamarca F', 'Suecia F', False),
        ('Birmingham Legion', 'Louisville', False),
    ]
    print(f"{'Partido':<38} {'L%':>6} {'E%':>6} {'V%':>6}  Pick")
    print('-' * 64)
    for h, a, n in tests:
        r = predict_intl(h, a, n)
        if r:
            ph, pd, pa, lam, mu = r
            pk = 'L' if ph == max(ph, pd, pa) else ('E' if pd == max(ph, pd, pa) else 'V')
            print(f"{h+' vs '+a:<38} {ph:>6.1%} {pd:>6.1%} {pa:>6.1%}  [{pk}]")
        else:
            print(f"{h+' vs '+a:<38}  SIN DATOS")
