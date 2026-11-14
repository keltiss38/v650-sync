"""Courbes de niveau à partir de tuiles d'altitude SRTM (.hgt, 1° x 1°), en Python standard (sans numpy).

La carte Polar contient des courbes: des lignes marquées `contour_ext=elevation_major|medium|minor` (+ `ele=<altitude>` pour les deux premières
classes). Mesuré sur une carte de Polar: principales tous les 500 m, moyennes tous les 100 m, fines tous les 25 m environ. On produit les mêmes
lignes, au format OSM XML, pour les fusionner avec l'extrait OpenStreetMap avant la fabrication (Osmosis, voir mapgen.build_map).

Méthode: échantillonnage de la zone (grille d'altitudes, éventuellement 1 point sur 2 ou 3 pour les grandes zones), lignes de niveau par
« marching squares » cellule par cellule, raccordement des segments en lignes, simplification (Douglas-Peucker), écriture OSM.
Seules les cellules dans la zone (anneaux de la zone) sont parcourues: le coût suit la surface de la zone, pas son rectangle englobant.
"""
import array
import gzip
import math
import os
import re
import sys

STAMP = "2026-01-01T00:00:00Z"  # Osmosis exige un horodatage sur chaque élément
BASE_ID = 10_000_000_000_000  # identifiants OSM des lignes produites: très au-dessus de ceux d'OpenStreetMap (< 20 milliards)
PLEIN = 25      # équidistance des courbes fines (m)
PRINCIPALES = 100  # « principales » = moyennes (100 m) et majeures (500 m): pas de courbes fines
MAX_CELLS = 4_000_000  # au-delà, on prend un point sur 2 (puis sur 3...) pour garder un calcul de quelques dizaines de secondes


class ContourError(Exception):
    pass


# --------------------------------------------------------------------------------------------------------------- tuiles

def tile_name(lat, lon):
    return f"{'N' if lat >= 0 else 'S'}{abs(int(math.floor(lat))):02d}{'E' if lon >= 0 else 'W'}{abs(int(math.floor(lon))):03d}"


def parse_tile_name(name):
    m = re.match(r"([NS])(\d{2})([EW])(\d{3})", os.path.basename(name).upper())
    if not m:
        raise ContourError(f"nom de tuile inconnu: {name}")
    return (1 if m.group(1) == "N" else -1) * int(m.group(2)), (1 if m.group(3) == "E" else -1) * int(m.group(4))


def read_hgt(path):
    """Tuile .hgt ou .hgt.gz -> (array('h') de n*n altitudes en m, n). Ligne 0 = bord nord, colonne 0 = bord ouest."""
    with open(path, "rb") as f:
        raw = f.read()
    if path.lower().endswith(".gz"):
        raw = gzip.decompress(raw)
    n = int(round(math.sqrt(len(raw) / 2)))
    if n * n * 2 != len(raw) or n < 3:
        raise ContourError(f"taille de tuile inattendue ({len(raw)} octets)")
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder == "little":
        a.byteswap()  # les .hgt sont en gros-boutiste
    return a, n


def tiles_for(rings):
    """Tuiles d'1° qui recouvrent la zone: [(lat, lon, nom)]."""
    lats = [p[0] for r in rings for p in r]
    lons = [p[1] for r in rings for p in r]
    out = []
    for la in range(int(math.floor(min(lats) - 0.01)), int(math.floor(max(lats) + 0.01)) + 1):  # marge: la grille déborde un peu de la zone
        for lo in range(int(math.floor(min(lons) - 0.01)), int(math.floor(max(lons) + 0.01)) + 1):
            out.append((la, lo, tile_name(la, lo)))
    return out


class Dem:
    """Mosaïque de tuiles de même définition. Réseau d'échantillons: 1 échantillon tous les 1/(n-1) degré, ancré sur les degrés entiers."""

    def __init__(self, tiles):
        """tiles: {(lat, lon): (array, n)}"""
        if not tiles:
            raise ContourError("aucune tuile d'altitude")
        ns = {n for _, n in tiles.values()}
        if len(ns) != 1:
            raise ContourError("tuiles de définitions différentes")
        self.tiles = tiles
        self.n = ns.pop()
        self.d = 1.0 / (self.n - 1)

    def _row_values(self, iy, ix0, count, decim):
        """Altitudes de la ligne d'échantillons `iy` pour les colonnes ix0, ix0+decim, ... (count valeurs)."""
        n1 = self.n - 1
        tlat = (iy - 1) // n1  # tuile dont la ligne iy est la plus au sud possible (bord entier: tuile du sud)
        r = (tlat + 1) * n1 - iy
        out = array.array("h")
        ix, end = ix0, ix0 + count * decim
        while ix < end:
            tlon = ix // n1
            t = self.tiles.get((tlat, tlon))
            c = ix - tlon * n1
            last = min(end, (tlon + 1) * n1)  # colonnes de cette tuile: ix .. last-1
            cnt = (last - ix + decim - 1) // decim
            if t is None:
                out.extend([0] * cnt)  # pas de tuile (la mer n'en a pas): altitude 0, aucune courbe n'y sera tracée
            else:
                base = r * self.n
                out.extend(t[0][base + c: base + c + cnt * decim: decim])
            ix += cnt * decim
        return out


# --------------------------------------------------------------------------------------------------------------- zone

def _row_intervals(rings, lat):
    """Intervalles de longitude [(a, b)] de l'union des anneaux à la latitude `lat`."""
    out = []
    for ring in rings:
        xs = []
        k = len(ring)
        for i in range(k):
            (la1, lo1), (la2, lo2) = ring[i], ring[(i + 1) % k]
            if (la1 <= lat < la2) or (la2 <= lat < la1):
                xs.append(lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            out.append((xs[i], xs[i + 1]))
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


# ---------------------------------------------------------------------------------------------------- lignes de niveau

# arêtes coupées par case (bits: 1 = coin NO au-dessus, 2 = NE, 4 = SE, 8 = SO): T haut, R droite, B bas, L gauche
_SEG = {1: [("L", "T")], 2: [("T", "R")], 3: [("L", "R")], 4: [("R", "B")], 6: [("T", "B")], 7: [("L", "B")], 8: [("L", "B")], 9: [("T", "B")],
        11: [("R", "B")], 12: [("L", "R")], 13: [("T", "R")], 14: [("L", "T")]}
_SADDLE5 = ([("T", "R"), ("L", "B")], [("L", "T"), ("R", "B")])   # (centre au-dessus, centre en dessous)
_SADDLE10 = ([("L", "T"), ("R", "B")], [("T", "R"), ("L", "B")])


def contour_lines(dem, rings, equidistance=PLEIN, decim=None, progress=None, log=print, tol_km=0.006, min_points=4):
    """Lignes de niveau de la zone: liste de (altitude, [(lat, lon), ...]). `equidistance` en m (25 = toutes les courbes, 100 = principales)."""
    d = dem.d
    lats = [p[0] for r in rings for p in r]
    lons = [p[1] for r in rings for p in r]
    iy_top = int(math.ceil(max(lats) / d)) + 1
    iy_bot = int(math.floor(min(lats) / d)) - 1
    ix_left = int(math.floor(min(lons) / d)) - 1
    ix_right = int(math.ceil(max(lons) / d)) + 1
    if decim is None:  # on ne compte que les cellules de la zone (une bande autour d'un parcours est bien plus petite que son rectangle)
        pas = max(1, (iy_top - iy_bot) // 200)
        total = 0.0
        for iy in range(iy_bot, iy_top, pas):
            total += sum((b - a) / d for a, b in _row_intervals(rings, iy * d)) * pas
        decim = max(1, int(math.ceil(math.sqrt(total / MAX_CELLS))))
    nrows = (iy_top - iy_bot) // decim + 1
    ncols = (ix_right - ix_left) // decim + 1
    dd = d * decim  # pas de la grille en degrés
    lat_top = iy_top * d
    lon_left = ix_left * d
    log(f"Courbes de niveau: grille {ncols} x {nrows}, 1 point sur {decim} (pas ≈ {dd * 111200:.0f} m), équidistance {equidistance} m")
    rows = []
    for i in range(nrows):
        rows.append(dem._row_values(iy_top - i * decim, ix_left, ncols, decim))
        # trous (-32768): on reprend la valeur voisine
    for r in rows:
        for j, v in enumerate(r):
            if v < -1000:
                r[j] = r[j - 1] if j else 0
    STEP = int(equidistance)
    n1, n2 = {}, {}  # voisins de chaque point d'intersection (au plus 2)

    def link(a, b):
        if a in n1:
            n2[a] = b
        else:
            n1[a] = b
        if b in n1:
            n2[b] = a
        else:
            n1[b] = a

    R0 = list(rows[0])
    I0 = [v // STEP for v in R0]
    segs = 0
    for r in range(nrows - 1):
        R1 = list(rows[r + 1])
        I1 = [v // STEP for v in R1]
        lat_mid = lat_top - (r + 0.5) * dd
        spans = _row_intervals(rings, lat_mid)
        for lo_a, lo_b in spans:
            c0 = max(int(math.floor((lo_a - lon_left) / dd)) - 1, 0)
            c1 = min(int(math.ceil((lo_b - lon_left) / dd)) + 1, ncols - 1)
            for c in range(c0, c1):
                ia, ib, id_, ie = I0[c], I0[c + 1], I1[c], I1[c + 1]
                if ia == ib == id_ == ie:
                    continue
                kmin = min(ia, ib, id_, ie) + 1
                kmax = max(ia, ib, id_, ie)
                a, b, dv, e = R0[c], R0[c + 1], R1[c], R1[c + 1]
                base = (r * ncols + c) * 2
                for k in range(kmin, kmax + 1):
                    if k <= 0:
                        continue  # pas de courbe au niveau de la mer
                    L = k * STEP
                    case = (a >= L) | ((b >= L) << 1) | ((e >= L) << 2) | ((dv >= L) << 3)
                    if case == 5 or case == 10:
                        centre = (a + b + dv + e) >= 4 * L
                        pairs = (_SADDLE5 if case == 5 else _SADDLE10)[0 if centre else 1]
                    else:
                        pairs = _SEG.get(case)
                        if not pairs:
                            continue
                    for p, q in pairs:
                        # clé = (numéro d'arête << 9) | niveau ; H(r, c) = arête horizontale, V(r, c) = verticale
                        kp = ((base if p == "T" else base + 2 * ncols if p == "B" else base + 1 if p == "L" else base + 3) << 9) | (k & 511)
                        kq = ((base if q == "T" else base + 2 * ncols if q == "B" else base + 1 if q == "L" else base + 3) << 9) | (k & 511)
                        link(kp, kq)
                        segs += 1
        R0, I0 = R1, I1
        if progress and r % 200 == 0:
            progress(r, nrows)
    log(f"Courbes de niveau: {segs} segments, raccordement…")

    def point(key):
        k = key & 511
        e = key >> 9
        L = k * STEP
        horiz = (e & 1) == 0
        cell = e >> 1
        r, c = divmod(cell, ncols)
        if horiz:
            p, q = rows[r][c], rows[r][c + 1]
            x, y = c + (L - p) / (q - p), r
        else:
            p, q = rows[r][c], rows[r + 1][c]
            x, y = c, r + (L - p) / (q - p)
        return lat_top - y * dd, lon_left + x * dd

    from .mapgen import simplify

    out = []

    def emit(chain):
        if len(chain) < min_points:
            return
        pts = simplify([point(k) for k in chain], tol_km)
        if len(pts) >= 3 or (len(pts) == 2 and chain[0] != chain[-1]):
            out.append(((chain[0] & 511) * STEP, pts))

    seen = set()
    # lignes ouvertes d'abord (extrémités de degré 1), puis boucles
    for start in list(n1):
        if start in seen or start in n2:
            continue
        chain = [start]
        seen.add(start)
        cur, prev = n1[start], start
        while True:
            chain.append(cur)
            if cur in seen:
                break
            seen.add(cur)
            nxt = n1[cur] if n1[cur] != prev else n2.get(cur)
            if nxt is None:
                break
            prev, cur = cur, nxt
        emit(chain)
    for start in list(n1):
        if start in seen:
            continue
        chain = [start]
        seen.add(start)
        cur, prev = n1[start], start
        while cur not in seen:
            chain.append(cur)
            seen.add(cur)
            nxt = n1[cur] if n1[cur] != prev else n2.get(cur)
            if nxt is None:
                break
            prev, cur = cur, nxt
        chain.append(start)  # boucle fermée
        emit(chain)
    log(f"Courbes de niveau: {len(out)} lignes")
    return out


def classify(ele):
    """Classe Polar d'une altitude: majeure (multiple de 500 m), moyenne (100 m), fine (le reste)."""
    return "elevation_major" if ele % 500 == 0 else "elevation_medium" if ele % 100 == 0 else "elevation_minor"


def write_osm(path, lines):
    """Écrit les lignes dans un fichier OSM XML (nœuds puis voies, triés: prêt pour `osmosis --read-xml` + `--merge`). Renvoie (nœuds, voies)."""
    nid, wid = BASE_ID, BASE_ID
    nodes = []
    ways = []
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="V650 Sync">\n')
        refs = []
        for ele, pts in lines:
            ids = []
            closed = len(pts) > 2 and pts[0] == pts[-1]
            for lat, lon in (pts[:-1] if closed else pts):
                nid += 1
                f.write(f'<node id="{nid}" version="1" timestamp="{STAMP}" lat="{lat:.6f}" lon="{lon:.6f}"/>\n')
                ids.append(nid)
            if closed:
                ids.append(ids[0])
            refs.append((ele, ids))
        for ele, ids in refs:
            wid += 1
            cls = classify(ele)
            f.write(f'<way id="{wid}" version="1" timestamp="{STAMP}">')
            f.write("".join(f'<nd ref="{i}"/>' for i in ids))
            f.write(f'<tag k="contour_ext" v="{cls}"/>')
            if cls != "elevation_minor":
                f.write(f'<tag k="ele" v="{ele}"/>')
            f.write("</way>\n")
        f.write("</osm>\n")
    return nid - BASE_ID, wid - BASE_ID
