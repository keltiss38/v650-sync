"""Itinéraires du V650: /U/0/ROUTES/N/PROUTE.BPB (PbPlannedRoute, protobuf).

Format observé sur 13 fichiers écrits par FlowSync (voir README):
  1 route_id{1: uint64}   2 name{1: string}   3 length (float, m)   4 start_location{1: lat, 2: lon (double)}
  5 start_altitude (float, m)
  6 point* {1: x sint32 (m, est, depuis le départ)  2: y sint32 (m, nord)  3: time_offset uint32 (ms)  4: z sint32 (m, depuis start_altitude)}
  9 varint (=2)   10 created / 11 last_modified: PbSystemDateTime   12 identifiant externe (texte, ex. n° de tour Komoot)
Les décalages x/y sont absolus (par rapport à start_location), en mètres. Projection (voir _scales): 110 937 m par degré de
latitude, et pour la longitude la longueur du degré sur l'ellipsoïde WGS84 à la latitude de départ; ajustée sur les
itinéraires de FlowSync, écart résiduel < 1 m.
"""
import math
import os
import random
import re
import struct
from datetime import datetime, timezone

from . import polar_decode as pd

KY = 110937.0  # mètres par degré de latitude, tel qu'utilisé par FlowSync et le compteur (mesuré, voir _scales)
_WGS84_A, _WGS84_E2 = 6378137.0, 0.00669437999014


def _scales(lat0):
    """(mètres par degré de latitude, mètres par degré de longitude) de la projection des itinéraires.

    Mesurés en comparant les itinéraires écrits par FlowSync au tracé GPX d'origine (4 itinéraires, de 15 à 215 km):
    la longitude suit l'ellipsoïde WGS84 à la latitude de DÉPART (constante sur tout le parcours), la latitude vaut
    110 937 m/° (et non 111 195 m/°, qui décalait les tracés de plusieurs dizaines de mètres). Écart résiduel < 1 m.
    """
    s = math.sin(math.radians(lat0)) ** 2
    kx = math.radians(1) * _WGS84_A * math.cos(math.radians(lat0)) / math.sqrt(1 - _WGS84_E2 * s)
    return KY, kx


MAX_POINTS = 450  # les fichiers de FlowSync en ont de 340 à 474
DEFAULT_SPEED_MS = 20 / 3.6  # estimation du temps de parcours quand le GPX n'a pas d'horodatage


# --- encodage protobuf -----------------------------------------------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _zz(n):
    return (n << 1) ^ (n >> 63)


def _ld(field, payload):
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _vi(field, n):
    return _varint(field << 3) + _varint(n)


def _system_datetime(dt):
    date = _vi(1, dt.year) + _vi(2, dt.month) + _vi(3, dt.day)
    time = _vi(1, dt.hour) + _vi(2, dt.minute) + _vi(3, dt.second) + _vi(4, dt.microsecond // 1000)
    return _ld(1, date) + _ld(2, time) + _vi(3, 1)


def encode(route):
    """route: dict(route_id, name, length, lat, lon, alt, points[(x, y, t_ms, z)], created, modified, source_id|None)."""
    out = bytearray()
    out += _ld(1, _vi(1, route["route_id"]))
    out += _ld(2, _ld(1, route["name"].encode("utf-8")))
    out += _varint(3 << 3 | 5) + struct.pack("<f", route["length"])
    out += _ld(4, _varint(1 << 3 | 1) + struct.pack("<d", route["lat"]) + _varint(2 << 3 | 1) + struct.pack("<d", route["lon"]))
    out += _varint(5 << 3 | 5) + struct.pack("<f", route["alt"])
    for x, y, t, z in route["points"]:
        out += _ld(6, _vi(1, _zz(x)) + _vi(2, _zz(y)) + _vi(3, t) + _vi(4, _zz(z)))
    out += _vi(9, 2)
    out += _ld(10, _system_datetime(route["created"]))
    out += _ld(11, _system_datetime(route["modified"]))
    if route.get("source_id"):
        out += _ld(12, str(route["source_id"]).encode("ascii"))
    return bytes(out)


# --- décodage --------------------------------------------------------------------------------------

def _dt(b):
    d = pd.parse(b)
    date, time = pd.parse(d[1][0]), pd.parse(d[2][0])
    return datetime(date[1][0], date[2][0], date[3][0], time[1][0], time[2][0], time[3][0], time.get(4, [0])[0] * 1000, tzinfo=timezone.utc)


def decode(raw):
    m = pd.parse(raw)
    loc = pd.parse(m[4][0])
    pts = []
    for v in m.get(6, []):
        p = pd.parse(v)
        pts.append((pd._zigzag(p[1][0]), pd._zigzag(p[2][0]), p.get(3, [0])[0], pd._zigzag(p[4][0]) if 4 in p else 0))
    return {
        "route_id": pd.parse(m[1][0])[1][0],
        "name": pd.parse(m[2][0])[1][0].decode("utf-8"),
        "length": struct.unpack("<f", m[3][0])[0],
        "lat": struct.unpack("<d", loc[1][0])[0],
        "lon": struct.unpack("<d", loc[2][0])[0],
        "alt": struct.unpack("<f", m[5][0])[0],
        "points": pts,
        "created": _dt(m[10][0]), "modified": _dt(m[11][0]),
        "source_id": m[12][0].decode("ascii") if 12 in m else None,
    }


# --- favoris: /U/0/FAV/NN/TST.BPB et ID.BPB ---------------------------------------------------------
# FlowSync écrit pour chaque itinéraire un « favori »: TST.BPB (PbTrainingSessionTarget: nom + cible d'exercice de type 3
# qui référence l'itinéraire par son route_id) et ID.BPB (identifiant de favori + dates). L'ordre des favoris sur le compteur
# (celui de l'écran « Ordre sur dispositif » de Polar Flow) est le numéro de dossier FAV/NN.

SPORT_CYCLING = 38  # sport_id observé dans les 13 favoris du compteur


def encode_fav_tst(name, route_id, sport_id=SPORT_CYCLING, phases=True):
    target = _vi(1, 3)
    if sport_id is not None:
        target += _ld(2, _vi(1, sport_id))
    if phases:
        target += _ld(4, b"")
    target += _ld(5, _vi(1, route_id))
    return _ld(2, _ld(1, name.encode("utf-8"))) + _ld(6, target)


def decode_fav_tst(raw):
    m = pd.parse(raw)
    t = pd.parse(m[6][0])
    return {"name": pd.parse(m[2][0])[1][0].decode("utf-8"), "route_id": pd.parse(t[5][0])[1][0],
            "sport_id": pd.parse(t[2][0])[1][0] if 2 in t else None, "phases": 4 in t}


def _fav_datetime(dt):
    date = _vi(1, dt.year) + _vi(2, dt.month) + _vi(3, dt.day)
    time = _vi(1, dt.hour) + _vi(2, dt.minute) + _vi(3, dt.second) + _vi(4, dt.microsecond // 1000)
    return _ld(1, date) + _ld(2, time) + _vi(3, 0)


def encode_fav_id(fav_id, modified, created=None):
    return _vi(1, fav_id) + _ld(2, _fav_datetime(modified)) + _ld(3, _fav_datetime(created or modified))


def decode_fav_id(raw):
    m = pd.parse(raw)
    return {"fav_id": m[1][0], "modified": _dt(m[2][0]), "created": _dt(m[3][0])}


# --- GPX -> itinéraire -----------------------------------------------------------------------------

def _hav(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


NAME_MAX = 30  # le compteur n'affiche qu'une ligne courte


def safe_name(text, fallback="Itineraire"):
    """Nom sans accents ni caractères spéciaux (lettres, chiffres, espace, tiret, souligné), 30 caractères au plus."""
    import unicodedata

    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9 _-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s[:NAME_MAX].rstrip(" -_") or fallback


def name_from_filename(path):
    """Nom lisible tiré du nom de fichier (retire la date, l'identifiant et le suffixe « - komoot » ajoutés à l'export)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}_\d+_", "", stem)
    return re.sub(r"\s*-\s*komoot$", "", stem, flags=re.I)


def gpx_stats(pts):
    """Chiffres clés d'un tracé [(lat, lon, ele)]: distance, montée, descente (seuil de 3 m contre le bruit), altitudes, profil."""
    cum, asc, desc, ref = [0.0], 0.0, 0.0, pts[0][2]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + _hav(pts[i - 1][:2], pts[i][:2]))
        d = pts[i][2] - ref
        if d >= 3:
            asc, ref = asc + d, pts[i][2]
        elif d <= -3:
            desc, ref = desc - d, pts[i][2]
    eles = [p[2] for p in pts]
    has_ele = max(eles) - min(eles) > 0
    step = max(1, len(pts) // 160)
    return {"km": round(cum[-1] / 1000, 1), "ascent": round(asc), "descent": round(desc), "has_ele": has_ele,
            "alt_min": round(min(eles)) if has_ele else None, "alt_max": round(max(eles)) if has_ele else None,
            "profile": [{"km": round(cum[i] / 1000, 2), "alt": round(pts[i][2], 1)} for i in range(0, len(pts), step)] if has_ele else []}


def read_gpx(path):
    """Points (lat, lon, ele) de la première trace (ou route) d'un GPX, et son nom."""
    import html

    with open(path, encoding="utf-8") as f:
        xml = f.read()
    pts = []
    for m in re.finditer(r"<(?:trkpt|rtept)\b([^>]*)>(.*?)</(?:trkpt|rtept)>", xml, re.S):
        attrs = m.group(1)
        lat = float(re.search(r'lat="([-\d.]+)"', attrs).group(1))
        lon = float(re.search(r'lon="([-\d.]+)"', attrs).group(1))
        ele = re.search(r"<ele>([-\d.]+)</ele>", m.group(2))
        pts.append((lat, lon, float(ele.group(1)) if ele else 0.0))
    name = re.search(r"<trk>.*?<name>([^<]*)</name>", xml, re.S) or re.search(r"<name>([^<]*)</name>", xml)
    return pts, (html.unescape(name.group(1)).strip() if name else None)


def _rdp(xy, tol):
    """Ramer-Douglas-Peucker itératif sur des points (x, y) en mètres; renvoie les indices conservés."""
    keep = [False] * len(xy)
    keep[0] = keep[-1] = True
    stack = [(0, len(xy) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, a
        for i in range(a + 1, b):
            px, py = xy[i]
            d = math.hypot(px - ax, py - ay) if norm == 0 else abs(dx * (ay - py) - (ax - px) * dy) / norm
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [i for i, k in enumerate(keep) if k]


def gpx_to_route(path, name=None, source_id=None, max_points=MAX_POINTS, speed_ms=DEFAULT_SPEED_MS, now=None, route_id=None):
    pts, gpx_name = read_gpx(path)
    if len(pts) < 2:
        raise ValueError("le GPX contient moins de 2 points")
    lat0, lon0, ele0 = pts[0]
    ky, kx = _scales(lat0)
    xy = [((lon - lon0) * kx, (lat - lat0) * ky) for lat, lon, _ in pts]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + _hav(pts[i - 1][:2], pts[i][:2]))
    tol = 1.0
    while True:
        idx = _rdp(xy, tol)
        if len(idx) <= max_points:
            break
        tol *= 1.3
    points = [(round(xy[i][0]), round(xy[i][1]), round(cum[i] / speed_ms * 1000), round(pts[i][2] - ele0)) for i in idx]
    now = now or datetime.now(timezone.utc)
    return {
        "route_id": route_id or random.randint(19_000_000, 19_999_999),
        "name": safe_name(name or gpx_name),
        "length": cum[-1], "lat": lat0, "lon": lon0, "alt": ele0,
        "points": points, "created": now, "modified": now, "source_id": source_id,
    }, tol


HD_MAX_BYTES = 15000  # un PUT tient dans une seule trame (16 383 octets, en-tête compris); les écritures plus grosses ne sont pas gérées


def hd_route(path, **kwargs):
    """Version « haute définition »: le plus de points possible tout en tenant dans une seule trame d'écriture."""
    for mp in (2000, 1600, 1300, 1100, 1000, 900, 800, 700, 600, 500, 450):
        r, tol = gpx_to_route(path, max_points=mp, **kwargs)
        if len(encode(r)) <= HD_MAX_BYTES:
            return r, tol
    return r, tol


def route_to_latlon(route):
    ky, kx = _scales(route["lat"])
    return [(route["lat"] + y / ky, route["lon"] + x / kx) for x, y, _, _ in route["points"]]
