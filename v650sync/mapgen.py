"""Fabrication de cartes hors ligne (Mapsforge version 3) pour le V650, à partir des données OpenStreetMap.

Ce module ne parle jamais au compteur (voir mapservice.py). Il sait:
  - installer les outils (Java 17, Osmosis 0.48.3, plugin mapsforge-map-writer 0.5.1) dans un dossier du programme,
    chaque fichier étant vérifié par son empreinte SHA-256;
  - décrire une zone (carré autour d'un point, ou bande autour d'un parcours) par une liste de rectangles;
  - choisir les extraits régionaux Geofabrik qui couvrent la zone, les télécharger (mis en cache);
  - lancer Osmosis et vérifier le fichier produit.

Le compteur lit la version 3 du format (écrite par le plugin 0.5.1, qui exige Osmosis ≤ 0.48). Le réglage des balises est calqué sur les
cartes de Polar (voir data/mapdata/tags_polar.json), sans bâtiments ni numéros de rue, avec des commerces et services utiles à vélo.
Données © contributeurs OpenStreetMap (ODbL), extraits Geofabrik.
"""
import ctypes
import glob
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib

from . import contours, jobs, journal, polar_decode as pd

LOG = journal.get("mapgen")
UA = "V650Sync-opensource"
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"

TOOLS = {
    "jre": {"label": "Java 17 (Eclipse Temurin, JRE)", "source": "github.com/adoptium (Temurin 17.0.21+9)",
            "url": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jre_x64_windows_hotspot_17.0.21_9.zip",
            "sha256": "fe4656bd78fb09812dc38c49da777f79cc33e7ee435a254be82f9e7f40d9d117", "size": 43860024, "file": "jre17.zip"},
    "osmosis": {"label": "Osmosis 0.48.3", "source": "github.com/openstreetmap/osmosis",
                "url": "https://github.com/openstreetmap/osmosis/releases/download/0.48.3/osmosis-0.48.3.zip",
                "sha256": "b4e9776e99f5446191cf572df8d34f177d0a0d53e27d3cf306f9f923595731a6", "size": 15180729, "file": "osmosis-0.48.3.zip"},
    "plugin": {"label": "Plugin mapsforge-map-writer 0.5.1", "source": "repo1.maven.org (Maven Central)",
               "url": "https://repo1.maven.org/maven2/org/mapsforge/mapsforge-map-writer/0.5.1/mapsforge-map-writer-0.5.1-jar-with-dependencies.jar",
               "sha256": "92e9b0ba4a4384213b80916c407b2309403724574726a2d28374718f0d497b6c", "size": 5893487,
               "file": "mapsforge-map-writer-0.5.1-jar-with-dependencies.jar"},
}
DENSITY_MB_PER_100KM2 = (0.30, 0.60)  # taille estimée des cartes (mesurée à 0,55–0,62 en zone dense)
PROFILES = {"leger": (False, False), "velo": (False, True), "detaille": (True, True)}  # profil: (avec bâtiments, avec commerces et services)
PROFILE_SIZE = {"leger": 1.0, "velo": 1.0, "detaille": 1.85}  # facteur de taille mesuré sur 25 km: 3,85 / 3,88 / 7,15 Mo
ZOOM_CONF = "5,0,7,9,8,9,14,10,21"  # mêmes niveaux de zoom que les cartes de Polar


class MapgenError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- outils

def tools_status(base):
    """État des outils: {'installed': bool, 'missing': [...], 'download_mb': n}."""
    missing = [k for k in TOOLS if not _tool_ready(base, k)]
    return {"installed": not missing, "missing": missing,
            "items": [{"id": k, "label": TOOLS[k]["label"], "source": TOOLS[k]["source"], "mb": round(TOOLS[k]["size"] / 1e6, 1),
                       "installed": k not in missing} for k in TOOLS],
            "download_mb": round(sum(TOOLS[k]["size"] for k in missing) / 1e6, 1)}


def _tool_ready(base, key):
    if key == "jre":
        return bool(glob.glob(os.path.join(base, "jre", "jdk-*", "bin", "java.exe")))
    if key == "osmosis":
        return os.path.isfile(os.path.join(base, "osmosis", "lib", "default", "plexus-classworlds-2.5.2.jar"))
    return os.path.isfile(os.path.join(base, "osmosis", "lib", "default", TOOLS["plugin"]["file"]))


def download(url, dest, sha256=None, size=None, log=print, progress=None):
    """Télécharge `url` vers `dest` (via un fichier .part), vérifie l'empreinte SHA-256 si donnée."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    LOG.info("Téléchargement %s", url)
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or size or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    LOG.info("Téléchargé: %s, %d octets", os.path.basename(dest), done)
    if sha256 and h.hexdigest() != sha256:
        LOG.error("Empreinte inattendue pour %s: %s", os.path.basename(dest), h.hexdigest())
        os.remove(part)
        raise MapgenError(f"empreinte inattendue pour {os.path.basename(dest)} (fichier corrompu ou modifié): téléchargement refusé")
    os.replace(part, dest)


def install_tools(base, log=print, progress=None):
    """Télécharge et installe ce qui manque. Chaque fichier doit avoir l'empreinte attendue."""
    dl = os.path.join(base, "telechargements")
    st = jobs.steps_of(progress)
    for key in ("jre", "osmosis", "plugin"):
        if _tool_ready(base, key):
            continue
        t = TOOLS[key]
        st.begin("tool_" + key)
        log(f"Téléchargement: {t['label']} ({t['size'] / 1e6:.1f} Mo) depuis {t['source']}")
        dest = os.path.join(dl, t["file"])
        download(t["url"], dest, t["sha256"], t["size"], log, progress)
        if key == "jre":
            shutil.rmtree(os.path.join(base, "jre"), ignore_errors=True)
            _unzip(dest, os.path.join(base, "jre"))
        elif key == "osmosis":
            shutil.rmtree(os.path.join(base, "osmosis"), ignore_errors=True)
            _unzip(dest, os.path.join(base, "osmosis"))
        else:
            os.makedirs(os.path.join(base, "osmosis", "lib", "default"), exist_ok=True)
            shutil.copy(dest, os.path.join(base, "osmosis", "lib", "default", t["file"]))
        log(f"{t['label']}: installé")
    if not tools_status(base)["installed"]:
        raise MapgenError("installation des outils incomplète (Osmosis 0.48.3 attendu avec lib/default)")


def _unzip(zip_path, dest):
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.namelist():  # refuse les chemins qui sortent du dossier
            if not os.path.realpath(os.path.join(root, m)).startswith(root):
                raise MapgenError("archive suspecte")
        z.extractall(root)


# ------------------------------------------------------------------------------------------------- réglage des balises

_CONTOUR_ZOOM = {"contour_ext=elevation_major": "10", "contour_ext=elevation_medium": "10", "contour_ext=elevation_minor": "11"}  # relevé dans une carte de Polar
_PLACE_ZOOM = {"country": "0", "island": "12", "neighbourhood": "14", "quarter": "14", "suburb": "13", "locality": "15",
               "farm": "15", "isolated_dwelling": "15"}
VELO_POI = {  # lieux dessinés comme des points (valeur = niveau d'apparition: 16 aux échelles de 100 à 200 m, 17 en gros plan)
    "shop": {"bakery": 16, "supermarket": 16, "convenience": 16, "bicycle": 16, "butcher": 16, "mall": 16, "organic": 17, "greengrocer": 16,
             "deli": 17, "pastry": 16, "cheese": 17, "seafood": 17, "farm": 17, "chocolate": 17, "ice_cream": 17, "beverages": 17, "wine": 17,
             "sports": 16, "outdoor": 16, "department_store": 16, "general": 17, "variety_store": 17},
    "amenity": {"drinking_water": 16, "fountain": 17, "toilets": 16, "cafe": 16, "restaurant": 17, "fast_food": 16, "pharmacy": 16, "fuel": 16,
                "hospital": 15, "clinic": 16, "doctors": 17, "fire_station": 16, "police": 16, "townhall": 16, "bicycle_repair_station": 16,
                "bicycle_rental": 17, "compressed_air": 16, "marketplace": 16, "ferry_terminal": 15, "atm": 17, "post_office": 17, "pub": 17,
                "bar": 17, "bus_station": 15, "ice_cream": 17},
    "leisure": {"stadium": 15, "sports_centre": 16, "sports_hall": 16, "marina": 15},
    "aeroway": {"aerodrome": 13, "terminal": 15},
    "man_made": {"water_tap": 17},
    "harbour": {"yes": 15},
}
VELO_WAY = {  # lieux souvent dessinés comme des surfaces (mairies, gymnases, stades, hôpitaux...): contour + nom
    "shop": {"supermarket": 16, "mall": 16},
    "amenity": {"fuel": 16, "toilets": 16, "townhall": 16, "police": 16, "fire_station": 16, "hospital": 15, "clinic": 16, "marketplace": 16,
                "ferry_terminal": 15},
    "leisure": {"sports_hall": 16, "marina": 15},
    "aeroway": {"aerodrome": 13, "terminal": 15},
    "harbour": {"yes": 15},
}
_STRIP_ADDR = """<?xml version="1.0" encoding="utf-8" ?>
<translations>
  <translation>
    <name>Retire les adresses (numéros de rue)</name>
    <description>Supprime toutes les balises addr:* pour ne pas les afficher sur le compteur.</description>
    <match mode="or">
      <tag k="addr:.*" v=".*" />
    </match>
    <output>
      <copy-unmatched />
    </output>
  </translation>
</translations>
"""


_POLAR_ZOOM = {}


def _polar_zoom():
    """{balise: zoom d'apparition} relevé dans une carte de Polar (mapdata/zoom_polar.json)."""
    if not _POLAR_ZOOM:
        with open(os.path.join(os.path.dirname(__file__), "mapdata", "zoom_polar.json"), encoding="utf-8") as f:
            _POLAR_ZOOM.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    return _POLAR_ZOOM


def write_mapping(base):
    """Écrit mapping/velo.xml (balises calquées sur Polar, sans bâtiments, + commerces et services) et mapping/strip-addr.xml."""
    from xml.sax.saxutils import quoteattr
    import xml.etree.ElementTree as ET

    jar = os.path.join(base, "osmosis", "lib", "default", TOOLS["plugin"]["file"])
    with zipfile.ZipFile(jar) as z:
        default = ET.fromstring(z.read("tag-mapping.xml"))
    ns = "{http://mapsforge.org/tag-mapping}"
    dpoi, dway = {}, {}
    for sec in default:
        for e in sec:
            if e.tag.endswith("osm-tag"):
                (dpoi if sec.tag.replace(ns, "") == "pois" else dway)[f"{e.get('key')}={e.get('value')}"] = dict(e.attrib)
    ref = json.load(open(os.path.join(os.path.dirname(__file__), "mapdata", "tags_polar.json"), encoding="utf-8"))

    def entry(tag, table):
        k, v = tag.split("=", 1)
        a = dict(table.get(tag, {"key": k, "value": v}))
        if tag not in table and k == "place" and v in _PLACE_ZOOM:
            a["zoom-appear"] = _PLACE_ZOOM[v]
        parts = " ".join(f"{n}={quoteattr(a[n])}" for n in ("key", "value", "zoom-appear", "renderable", "force-polygon-line", "enabled", "equivalent-values") if n in a)
        return f"\t\t<osm-tag {parts} />"

    d = os.path.join(base, "mapping")
    os.makedirs(d, exist_ok=True)
    for profile, (buildings, extras) in PROFILES.items():
        dp, dw = dict(dpoi), dict(dway)
        for tag, z in _polar_zoom().items():  # niveaux d'apparition relevés dans une carte de Polar (routes, chemins, eau, bois…): même densité aux petits zooms
            k, v = tag.split("=", 1)
            dw[tag] = {**dw.get(tag, {"key": k, "value": v}), "zoom-appear": str(z)}
        for tag, z in _CONTOUR_ZOOM.items():  # courbes de niveau: mêmes niveaux d'apparition que dans les cartes de Polar (sinon 16 par défaut)
            k, v = tag.split("=")
            dw[tag] = {"key": k, "value": v, "zoom-appear": z, "force-polygon-line": "true"}  # une courbe fermée reste une ligne (sinon: quadrillage aux bords des tuiles)
        for tag in ref["way"]:
            if tag.startswith("ele="):
                dw[tag] = {"key": "ele", "value": tag[4:], "zoom-appear": "10", "force-polygon-line": "true"}
        pois = sorted(ref["poi"])
        ways = [t for t in sorted(ref["way"]) if buildings or not t.startswith("building=")]  # les bâtiments pèsent la moitié de la carte
        if extras:
            for table, section, dflt in ((VELO_POI, pois, dp), (VELO_WAY, ways, dw)):
                for k, vals in table.items():
                    for v, z in vals.items():
                        if f"{k}={v}" not in section:
                            section.append(f"{k}={v}")
                        dflt.setdefault(f"{k}={v}", {"key": k, "value": v})["zoom-appear"] = str(z)  # nos niveaux l'emportent sur ceux du réglage par défaut
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<tag-mapping xmlns="http://mapsforge.org/tag-mapping" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
                 '\txsi:schemaLocation="http://mapsforge.org/tag-mapping ../resources/tag-mapping.xsd" default-zoom-appear="16"', f'\tprofile-name="{profile}">',
                 "\t<pois>"] + [entry(t, dp) for t in pois] + ["\t</pois>", "\t<ways>"] + [entry(t, dw) for t in ways] + ["\t</ways>", "</tag-mapping>", ""]
        with open(os.path.join(d, profile + ".xml"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    with open(os.path.join(d, "strip-addr.xml"), "w", encoding="utf-8") as f:
        f.write(_STRIP_ADDR)
    return d


# ----------------------------------------------------------------------------------------------------------- géométrie
# Une zone = liste d'anneaux [(lat, lon), ...]: des rectangles orientés qui se recouvrent. Osmosis en fait l'union.

def _km(lat):
    return 111.2, 111.32 * math.cos(math.radians(lat))


def rect_ring(lat, lon, half_km):
    """Carré de 2 x half_km autour d'un point."""
    ky, kx = _km(lat)
    dy, dx = half_km / ky, half_km / kx
    return [(lat - dy, lon - dx), (lat - dy, lon + dx), (lat + dy, lon + dx), (lat + dy, lon - dx)]


def simplify(points, tol_km):
    """Douglas-Peucker sur le tracé (coordonnées locales en km). Renvoie une liste de (lat, lon)."""
    if len(points) < 3:
        return list(points)
    lat0 = sum(p[0] for p in points) / len(points)
    ky, kx = _km(lat0)
    xy = [(p[1] * kx, p[0] * ky) for p in points]
    keep = [False] * len(xy)
    keep[0] = keep[-1] = True
    stack = [(0, len(xy) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, a
        for i in range(a + 1, b):
            px, py = xy[i]
            d = math.hypot(px - ax, py - ay) if norm == 0 else abs(dx * (ay - py) - (ax - px) * dy) / norm
            if d > best:
                best, idx = d, i
        if best > tol_km:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [points[i] for i, k in enumerate(keep) if k]


def disc_ring(lat, lon, r_km, n=24):
    """Disque (polygone à n côtés circonscrit au cercle de rayon r_km): bouts arrondis et coudes lissés d'une bande."""
    ky, kx = _km(lat)
    r = r_km / math.cos(math.pi / n)
    return [(lat + r * math.sin(2 * math.pi * i / n) / ky, lon + r * math.cos(2 * math.pi * i / n) / kx) for i in range(n)]


def corridor_rings(points, margin_km):
    """Bande de demi-largeur `margin_km` autour du tracé, à contour arrondi: un rectangle orienté par tronçon, plus un disque aux deux
    extrémités et à chaque coude marqué (sinon les rectangles laissent des crans, ou des angles vifs, aux virages)."""
    tol = max(1.0, margin_km / 4)
    pts = simplify(points, tol)
    if len(pts) <= 2 and len(points) > 2:  # petite boucle (départ = arrivée, plus petite que la tolérance): on garde sa vraie forme
        pts = simplify(points, 0.3)
    if len(pts) == 1:
        return [disc_ring(pts[0][0], pts[0][1], margin_km)]
    rings, dirs = [], []
    for a, b in zip(pts, pts[1:]):
        ky, kx = _km((a[0] + b[0]) / 2)
        dx, dy = (b[1] - a[1]) * kx, (b[0] - a[0]) * ky
        n = math.hypot(dx, dy)
        if n < 1e-6:
            dirs.append(None)
            continue
        ux, uy = dx / n, dy / n
        nx, ny = -uy, ux
        dirs.append((ux, uy))

        def sh(p, along, across):
            return (p[0] + (along * uy + across * ny) / ky, p[1] + (along * ux + across * nx) / kx)

        rings.append([sh(a, 0, -margin_km), sh(b, 0, -margin_km), sh(b, 0, margin_km), sh(a, 0, margin_km)])
    valid = [d for d in dirs if d]
    if not valid:  # tracé réduit à un point (ou deux points confondus): un disque
        return [disc_ring(pts[0][0], pts[0][1], margin_km)]
    for i, p in enumerate(pts):
        prev = dirs[i - 1] if i > 0 else None
        nxt = dirs[i] if i < len(dirs) else None
        if prev and nxt:  # coude: disque seulement si le virage dépasse ~8° (en dessous, les deux rectangles se recouvrent déjà)
            if prev[0] * nxt[0] + prev[1] * nxt[1] > math.cos(math.radians(8)):
                continue
        elif not valid:
            continue
        rings.append(disc_ring(p[0], p[1], margin_km))
    return rings


def _in_ring(lat, lon, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        (yi, xi), (yj, xj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def inside(rings, lat, lon):
    return any(_in_ring(lat, lon, r) for r in rings)


def _prep(rings):
    """Anneaux avec leur boîte englobante: évite de tester les points loin d'un anneau (bien plus rapide quand il y en a beaucoup)."""
    return [(min(p[0] for p in r), max(p[0] for p in r), min(p[1] for p in r), max(p[1] for p in r), r) for r in rings]


def _inside_p(prep, lat, lon):
    return any(a <= lat <= b and c <= lon <= d and _in_ring(lat, lon, r) for a, b, c, d, r in prep)


def bbox(rings):
    la = [p[0] for r in rings for p in r]
    lo = [p[1] for r in rings for p in r]
    return min(la), min(lo), max(la), max(lo)


def raster_cells(rings, step_km=2.0):
    """Centres de cellules (lat, lon) de `step_km` qui tombent dans la zone: sert à mesurer la surface et à choisir les régions."""
    s, w, n, e = bbox(rings)
    lat0 = (s + n) / 2
    ky, kx = _km(lat0)
    out = []
    prep = _prep(rings)
    la = s
    while la <= n:
        _, kxl = _km(la)
        lo = w
        while lo <= e:
            if _inside_p(prep, la, lo):
                out.append((la, lo))
            lo += step_km / kxl
        la += step_km / ky
    return out


def area_km2(rings, step_km=2.0):
    return len(raster_cells(rings, step_km)) * step_km * step_km


def estimate_mb(area, profile="velo", contour_mode="none"):
    f = PROFILE_SIZE.get(profile, 1.0) * CONTOUR_SIZE.get(contour_mode, 1.0)
    return (round(area / 100 * DENSITY_MB_PER_100KM2[0] * f), round(area / 100 * DENSITY_MB_PER_100KM2[1] * f))


def coverage(points, rings, step_km=1.0):
    """Part du tracé couverte par la zone: {'fraction', 'km_total', 'km_outside'}."""
    total = out = 0.0
    prev = None
    prep = _prep(rings)
    for p in points:
        if prev is not None:
            ky, kx = _km(p[0])
            d = math.hypot((p[0] - prev[0]) * ky, (p[1] - prev[1]) * kx)
            total += d
            if not (_inside_p(prep, *p) and _inside_p(prep, *prev)):
                out += d
        prev = p
    return {"km_total": round(total, 1), "km_outside": round(out, 1), "fraction": 1.0 if total == 0 else round(1 - out / total, 3)}


def write_poly(path, rings):
    """Fichier polygone d'Osmosis: une section par anneau (longitude, latitude)."""
    with open(path, "w", encoding="ascii") as f:
        f.write("zone\n")
        for i, r in enumerate(rings, 1):
            f.write(f"{i}\n")
            for la, lo in r:
                f.write(f"   {lo:.6f}   {la:.6f}\n")
            f.write("END\n")
        f.write("END\n")


# ------------------------------------------------------------------------------------------------ extraits Geofabrik

def load_index(cache, log=print, max_age_days=30):
    """Index des régions Geofabrik (mis en cache)."""
    if not os.path.isfile(cache) or time.time() - os.path.getmtime(cache) > max_age_days * 86400:
        log("Téléchargement de la liste des régions Geofabrik (3,8 Mo)")
        download(GEOFABRIK_INDEX, cache)
    return json.load(open(cache, encoding="utf-8"))["features"]


def _poly_rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [[(lat, lon) for lon, lat in poly[0]] for poly in polys]  # anneau extérieur de chaque polygone


def pick_regions(features, rings):
    """Régions Geofabrik de plus bas niveau dont le territoire croise la zone. Renvoie [{'id','name','url'}]."""
    parents = {f["properties"].get("parent") for f in features}
    cells = raster_cells(rings, 5.0) or [((bbox(rings)[0] + bbox(rings)[2]) / 2, (bbox(rings)[1] + bbox(rings)[3]) / 2)]
    cand = []  # (surface du territoire, région, cellules couvertes)
    for f in features:
        p = f["properties"]
        url = (p.get("urls") or {}).get("pbf")
        if p["id"] in parents or not url or not f.get("geometry"):
            continue
        covered, area = set(), 0.0
        for r in _poly_rings(f["geometry"]):
            la = [q[0] for q in r]
            lo = [q[1] for q in r]
            mn, mx = (min(la), min(lo)), (max(la), max(lo))
            area += (mx[0] - mn[0]) * (mx[1] - mn[1])
            for i, c in enumerate(cells):
                if mn[0] <= c[0] <= mx[0] and mn[1] <= c[1] <= mx[1] and _in_ring(c[0], c[1], r):
                    covered.add(i)
        if covered:
            cand.append((area, {"id": p["id"], "name": p.get("name", p["id"]), "url": url}, covered))
    # plus petits territoires d'abord (évite les gros extraits multi-pays qui en chevauchent d'autres), puis on ne garde que les utiles
    cand.sort(key=lambda x: x[0])
    done, chosen = set(), []
    for _, reg, cov in cand:
        if cov - done:
            chosen.append(reg)
            done |= cov
    return chosen


_SIZES = {}  # url -> (heure, octets): la taille d'un extrait ne change pas en une heure; sans ça chaque recalcul de zone refait une requête réseau par région (plusieurs secondes)


def remote_size(url):
    hit = _SIZES.get(url)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        n = int(r.headers.get("Content-Length") or 0)
    _SIZES[url] = (time.time(), n)
    return n


def cached_pbf(osm_dir, region):
    return os.path.join(osm_dir, region["id"].replace("/", "_") + "-latest.osm.pbf")


def fetch_regions(osm_dir, regions, log=print, progress=None, max_age_days=30):
    """Télécharge les extraits manquants ou trop anciens (un téléchargement par région, gardé en cache). Renvoie les chemins."""
    paths = []
    st = jobs.steps_of(progress)
    for r in regions:
        dest = cached_pbf(osm_dir, r)
        if needs_download(osm_dir, r, max_age_days):
            st.begin("reg_" + r["name"])
            log(f"Téléchargement de {r['name']} (Geofabrik)")
            download(r["url"], dest, log=log, progress=progress)
        paths.append(dest)
    return paths


# ---------------------------------------------------------------------------------------- altitudes (courbes de niveau)

DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"  # tuiles SRTM d'1°, jeu public « Terrain Tiles » d'AWS, sans compte
DEM_MB = 17  # taille moyenne d'une tuile (N45E006: 16,6 Mo)
DEM_CREDIT = "Altitudes : SRTM (NASA / USGS, domaine public), via les « Terrain Tiles » d'AWS."
CONTOUR_STEP = {"none": None, "main": 100, "full": 25}  # équidistance des courbes (m) par option
CONTOUR_SIZE = {"none": 1.0, "main": 1.04, "full": 1.16}  # surcoût en taille de carte (mesuré: 4,26 Mo contre 3,69 Mo avec toutes les courbes)


def dem_url(name):
    base = os.environ.get("V650SYNC_DEM_URL")  # tests: serveur local
    return (base + "/{name}.hgt.gz" if base else DEM_URL).format(ns=name[:3], name=name)


def dem_path(dem_dir, name):
    return os.path.join(dem_dir, name + ".hgt.gz")


def dem_tiles(dem_dir, rings):
    """Tuiles d'altitude de la zone: [{'name', 'cached', 'sea', 'mb'}]. `sea`: le serveur n'a pas cette tuile (mer), déjà constaté."""
    out = []
    for la, lo, name in contours.tiles_for(rings):
        cached = os.path.isfile(dem_path(dem_dir, name))
        sea = os.path.isfile(os.path.join(dem_dir, name + ".none"))
        out.append({"name": name, "lat": la, "lon": lo, "cached": cached or sea, "sea": sea,
                    "mb": round(os.path.getsize(dem_path(dem_dir, name)) / 1e6, 1) if cached else (0 if sea else DEM_MB)})
    return out


def fetch_dem(dem_dir, rings, log=print, progress=None, cancel=None):
    """Télécharge les tuiles d'altitude manquantes (une étape par tuile) et renvoie la mosaïque (contours.Dem), ou None s'il n'y a aucune terre."""
    st = jobs.steps_of(progress)
    tiles = {}
    for t in dem_tiles(dem_dir, rings):
        if cancel is not None and cancel.is_set():
            raise MapgenError("fabrication annulée")
        dest = dem_path(dem_dir, t["name"])
        if not t["cached"]:
            st.begin("alt_" + t["name"])
            log(f"Téléchargement des altitudes {t['name']} (SRTM, environ {DEM_MB} Mo)")
            try:
                download(dem_url(t["name"]), dest, log=log, progress=progress)
                contours.read_hgt(dest)  # contrôle: archive lisible, taille de grille cohérente
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):  # pas de tuile: la mer (S3 répond 403 pour une clé absente)
                    open(os.path.join(dem_dir, t["name"] + ".none"), "w").close()
                    log(f"Altitudes {t['name']}: pas de tuile (mer), ignorée")
                    continue
                raise MapgenError(f"téléchargement des altitudes impossible ({t['name']}: HTTP {e.code})") from None
            except (OSError, contours.ContourError, EOFError, zlib.error) as e:
                if os.path.isfile(dest):
                    os.remove(dest)
                raise MapgenError(f"altitudes {t['name']} illisibles ou incomplètes ({e}): réessayez") from None
        if os.path.isfile(dest):
            tiles[(t["lat"], t["lon"])] = contours.read_hgt(dest)
    return contours.Dem(tiles) if tiles else None


def make_contours_from(dem, rings, mode, out_osm, log=print, progress=None):
    """Courbes de niveau de la zone dans `out_osm` (OSM XML). Renvoie le nombre de lignes. mode: 'main' (100 m) ou 'full' (25 m)."""
    try:
        lines = contours.contour_lines(dem, rings, equidistance=CONTOUR_STEP[mode], progress=progress, log=log)
        contours.write_osm(out_osm, lines)
    except contours.ContourError as e:
        raise MapgenError(f"courbes de niveau impossibles: {e}") from None
    return len(lines)


def pbf_timestamp(path):
    """Date des données OpenStreetMap d'un extrait (époque UTC, lue dans l'en-tête du fichier .pbf), ou la date du fichier à défaut."""
    try:
        with open(path, "rb") as f:
            n = struct.unpack(">I", f.read(4))[0]
            bh = pd.parse(f.read(n))
            blob = pd.parse(f.read(bh[3][0]))
        raw = zlib.decompress(blob[3][0]) if 3 in blob else blob[1][0]
        hdr = pd.parse(raw)
        if 32 in hdr:
            return int(hdr[32][0])
    except (OSError, ValueError, KeyError, IndexError, struct.error, zlib.error):
        pass
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


def date_fr(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else None


def needs_download(osm_dir, r, max_age_days=30):
    dest = cached_pbf(osm_dir, r)
    stale = os.path.isfile(dest) and time.time() - os.path.getmtime(dest) > max_age_days * 86400
    return not os.path.isfile(dest) or stale


# --------------------------------------------------------------------------------------------------- fabrication

def _ram_bytes():
    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("ae", ctypes.c_ulonglong)]
    s = MS()
    s.l = ctypes.sizeof(MS)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return int(s.tp)
    except (AttributeError, OSError):
        return 8 << 30


def fmt_min(s):
    s = int(s)
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _proc_mem_gb(p):
    """Mémoire (Go) utilisée par un processus enfant, ou None (Windows: GetProcessMemoryInfo)."""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pm = PMC()
        pm.cb = ctypes.sizeof(PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(int(p._handle)), ctypes.byref(pm), pm.cb)
        return pm.WorkingSetSize / 2**30 if ok else None
    except Exception:  # noqa: BLE001 - confort d'affichage
        return None


def build_map(base, rings, pbf_paths, out_map, work_dir, log=print, progress=None, cancel=None, profile="velo", complete_relations=False, complete_ways=False,
              contours_osm=None):
    """Fabrique `out_map` (Mapsforge v3) pour la zone `rings` à partir des extraits `pbf_paths`. Renvoie inspect_map(out_map).

    Découpage STRICT de la zone (completeWays=no, completeRelations=no): la carte suit exactement le contour demandé. Avec « complete »,
    Osmosis ajoutait les routes entières qui traversent la zone et tous les membres des relations (itinéraires, frontières…): essai réel
    sur une bande de 284 km², 50 % des tuiles de données tombaient hors de la zone (la carte remplissait tout le rectangle englobant) et la
    fabrication prenait 230 s au lieu de 20 s. Seule contrepartie: au bord de la zone, une forêt ou un lac coupé peut perdre son remplissage.

    Osmosis renvoie un code d'erreur à la fin (le plugin 0.5.1 n'a pas la méthode close() d'Osmosis 0.48) alors que le fichier est
    complet: on se fie donc au contrôle du fichier, pas au code de sortie.
    """
    java = glob.glob(os.path.join(base, "jre", "jdk-*", "bin", "java.exe"))
    if not java or not tools_status(base)["installed"]:
        raise MapgenError("outils de carte non installés")
    mapping = os.path.join(base, "mapping")
    if profile not in PROFILES:
        raise MapgenError("niveau de détail inconnu")
    write_mapping(base)  # toujours: les fichiers de balises suivent les réglages de cette version (rapide)
    os.makedirs(work_dir, exist_ok=True)
    poly = os.path.join(work_dir, "zone.poly")
    write_poly(poly, rings)
    s, w, n, e = bbox(rings)
    area = area_km2(rings, 4.0)
    maptype = "ram" if area <= 20000 else "hd"
    heap_mb = int(min(max(4096, _ram_bytes() * 0.55 / 1048576), 14000))
    home = os.path.join(base, "osmosis")
    args = []
    for i, pbf in enumerate(pbf_paths):
        args += ["--read-pbf", f"file={pbf}", "--bounding-polygon", f"file={poly}", f"completeWays={'yes' if complete_ways else 'no'}",
                 f"completeRelations={'yes' if complete_relations else 'no'}"]
        if i:
            args += ["--merge"]
    if contours_osm:  # courbes de niveau (voir contours.py): fichier OSM trié, déjà limité à la zone, fusionné avant l'écriture
        # PAS de --bounding-polygon ici: avec les identifiants très élevés des courbes, il supprimait des lignes entières (des rectangles de carte
        # sans courbes: 4 982 voies gardées sur 6 948 mesurées); sans lui, toutes sont là. Le calcul s'arrête déjà aux bords de la zone.
        args += ["--read-xml", f"file={contours_osm}"]
        if pbf_paths:
            args += ["--merge"]
    args += ["--tag-transform", f"file={os.path.join(mapping, 'strip-addr.xml')}", "--mapfile-writer", f"file={out_map}",
             f"bbox={s:.5f},{w:.5f},{n:.5f},{e:.5f}", f"zoom-interval-conf={ZOOM_CONF}", f"type={maptype}",
             f"tag-conf-file={os.path.join(mapping, profile + '.xml')}"]
    cmd = [java[0], f"-Xmx{heap_mb}m", "-cp", os.path.join(home, "lib", "default", "plexus-classworlds-2.5.2.jar"),
           f"-Dapp.home={home}", f"-Dclassworlds.conf={os.path.join(home, 'config', 'plexus.conf')}",
           "org.codehaus.classworlds.Launcher"] + args
    if os.path.exists(out_map):
        os.remove(out_map)
    log(f"Fabrication de la carte ({area:,.0f} km², mode {maptype}, mémoire {heap_mb // 1024} Go)")
    # sortie complète d'Osmosis dans un fichier à côté de la carte (jointe aux rapports de bug); les lignes de progression
    # « written N% » ne sont gardées que de 10 en 10
    osm_log = open(os.path.join(os.path.dirname(os.path.normpath(work_dir)), "osmosis.log"), "w", encoding="utf-8", buffering=1)  # ligne par ligne: lisible pendant le travail
    osm_log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} zone {area:,.0f} km², profil {profile}, type {maptype}, bbox {s:.4f},{w:.4f},{n:.4f},{e:.4f}\n"
                  f"{len(pbf_paths)} extrait(s): {', '.join(os.path.basename(x) for x in pbf_paths)}\ncommande: {' '.join(cmd)}\n\n")
    LOG.info("Osmosis: %s km², profil %s, type %s, mémoire %d Mo, %d extrait(s)", f"{area:,.0f}", profile, maptype, heap_mb, len(pbf_paths))
    p = subprocess.Popen(cmd, cwd=home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail = []
    stop = threading.Event()
    stp, t_start, beat = jobs.steps_of(progress), time.time(), [0.0]
    finished = [0.0]  # instant où Osmosis a annoncé la fin de l'écriture

    def watch():
        while not stop.is_set():
            if cancel is not None and cancel.is_set():
                p.terminate()
                return
            if finished[0] and time.time() - finished[0] > 20:  # fichier écrit mais Osmosis ne se ferme pas (fil resté ouvert): on l'arrête
                LOG.info("Osmosis a fini d'écrire la carte mais ne se ferme pas: arrêt du processus")
                p.terminate()
                return
            if time.time() - beat[0] >= 20:  # signe de vie: la lecture des données est longue et sans pourcentage avant l'écriture
                beat[0] = time.time()
                mem = _proc_mem_gb(p)
                txt = f"Osmosis travaille depuis {fmt_min(time.time() - t_start)}" + (f" · mémoire utilisée {mem:.1f} Go" if mem else "") + \
                      (f" · dernier message : {tail[-1][:90]}" if tail else " · aucun message pour l'instant (lecture des données)")
                stp.note(txt)
                if int(time.time() - t_start) // 60 != int(beat[1] if len(beat) > 1 else -1):  # une ligne par minute dans le journal
                    LOG.info(txt)
                    beat[:] = [beat[0], int(time.time() - t_start) // 60]
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    done_levels = 0
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        tail = (tail + [line])[-15:]
        m = re.search(r"written (\d+)% of sub file for zoom interval index (\d)", line)
        if not m or int(m.group(1)) % 10 == 0:
            osm_log.write(line + "\n")
        if m:
            if progress:
                progress(int(m.group(2)) * 100 + int(m.group(1)), 300)
            if m.group(1) == "100":
                done_levels += 1
        elif "OutOfMemoryError" in line:
            raise MapgenError("mémoire insuffisante pour cette zone: choisissez une zone plus petite")
        elif "Finished writing file" in line or line.endswith("finished..."):
            finished[0] = time.time()
    p.wait()
    stop.set()
    osm_log.write(f"\ncode de sortie d'Osmosis: {p.returncode} (non significatif: voir le contrôle du fichier)\n")
    osm_log.close()
    LOG.info("Osmosis terminé (code %s)", p.returncode)
    if cancel is not None and cancel.is_set():
        raise MapgenError("fabrication annulée")
    try:
        info = inspect_map(out_map)
    except (OSError, MapgenError) as e:
        LOG.error("Contrôle de la carte: %s", e)
        raise MapgenError(f"la carte n'a pas été produite ({e}). Fin du journal d'Osmosis: " + " | ".join(tail[-4:]))
    return info  # inspect_map a déjà vérifié que le fichier est complet (taille déclarée = taille réelle, sous-fichiers cohérents)


# ----------------------------------------------------------------------------------------------- contrôle d'un fichier

def _vbeu(b, o):
    r = s = 0
    while True:
        c = b[o]
        o += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, o


def _str(b, o):
    n, o = _vbeu(b, o)
    return b[o:o + n].decode("utf8", "replace"), o + n


def inspect_map(path):
    """Lit l'en-tête d'un fichier Mapsforge et vérifie sa cohérence. Lève MapgenError si ce n'est pas une carte v3 complète."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        d = f.read(65536)
    if d[:20] != b"mapsforge binary OSM":
        raise MapgenError("ce n'est pas un fichier Mapsforge")
    hs, ver, fsize, cdate = struct.unpack_from(">IIqq", d, 20)
    if ver != 3:
        raise MapgenError(f"version Mapsforge {ver}: le compteur lit la version 3")
    if fsize != size:
        raise MapgenError(f"taille déclarée {fsize} différente de la taille réelle {size} (fichier incomplet)")
    s, w, n, e = [v / 1e6 for v in struct.unpack_from(">iiii", d, 44)]
    tile = struct.unpack_from(">H", d, 60)[0]
    o = 62
    proj, o = _str(d, o)
    fl = d[o]
    o += 1
    if fl & 0x40:
        o += 8
    if fl & 0x20:
        o += 1
    for bit in (0x10, 0x08, 0x04):
        if fl & bit:
            _, o = _str(d, o)
    counts = []
    for _ in range(2):
        k = struct.unpack_from(">H", d, o)[0]
        o += 2
        for _ in range(k):
            _, o = _str(d, o)
        counts.append(k)
    nz = d[o]
    o += 1
    zi = []
    for _ in range(nz):
        zi.append((d[o], d[o + 1], d[o + 2], *struct.unpack_from(">qq", d, o + 3)))
        o += 19
    if o != 24 + hs:
        raise MapgenError("en-tête illisible")
    if not zi or zi[-1][3] + zi[-1][4] != size:
        raise MapgenError("sous-fichiers incohérents (fichier incomplet)")
    return {"version": ver, "size": size, "bbox": [s, w, n, e], "tile": tile, "projection": proj, "poi_tags": counts[0], "way_tags": counts[1],
            "zooms": [(a, b, c) for a, b, c, _, _ in zi], "sha256": None}


def tile_coverage(path):
    """Tuiles non vides du dernier niveau de zoom (le plus fin) d'un fichier Mapsforge: [(lat, lon, octets)] au centre de chaque tuile non vide.

    Sert à vérifier que la carte suit bien la zone demandée (et non tout le rectangle englobant)."""
    info = inspect_map(path)
    with open(path, "rb") as f:
        d = f.read(65536)
    # reprise de la lecture de l'en-tête pour retrouver les sous-fichiers
    o = 62
    _, o = _str(d, o)
    fl = d[o]
    o += 1
    o += 8 if fl & 0x40 else 0
    o += 1 if fl & 0x20 else 0
    for bit in (0x10, 0x08, 0x04):
        if fl & bit:
            _, o = _str(d, o)
    for _ in range(2):
        k = struct.unpack_from(">H", d, o)[0]
        o += 2
        for _ in range(k):
            _, o = _str(d, o)
    nz = d[o]
    o += 1
    zi = [(d[o + 19 * i], *struct.unpack_from(">qq", d, o + 19 * i + 3)) for i in range(nz)]
    base_zoom, start, size = zi[-1]
    s, w, n, e = info["bbox"]

    def tx(lon):
        return int((lon + 180.0) / 360.0 * (1 << base_zoom))

    def ty(lat):
        r = math.radians(lat)
        return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (1 << base_zoom))

    x0, x1, y0, y1 = tx(w), tx(e), ty(n), ty(s)
    ntiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    with open(path, "rb") as f:
        f.seek(start)
        sub = f.read(size)
    idx_off = 16 if sub[:16].startswith(b"+++IndexStart+++") else 0
    offs = [int.from_bytes(sub[idx_off + 5 * i: idx_off + 5 * i + 5], "big") & 0x7FFFFFFFFF for i in range(ntiles)] + [size]
    out = []
    for i in range(ntiles):
        if offs[i + 1] - offs[i] > 0:
            ix, iy = x0 + i % (x1 - x0 + 1), y0 + i // (x1 - x0 + 1)
            lon = (ix + 0.5) / (1 << base_zoom) * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (iy + 0.5) / (1 << base_zoom)))))
            out.append((lat, lon, offs[i + 1] - offs[i]))
    return {"base_zoom": base_zoom, "tiles_total": ntiles, "tiles": out}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
