"""/U/0/MAP/0/MAPINFO.BPB: état de la carte hors ligne du compteur (PbMapInfo, déduit de captures de FlowSync).

  1 centre {1: latitude (double), 2: longitude (double)}   la zone demandée sur le compteur (Réglages > Cartes)
  2 date de la dernière installation (PbSystemDateTime, UTC)
  3 drapeau: 1 = carte installée, 0 = nouvelle zone demandée (le compteur change le centre et met 0, FlowSync télécharge)

Trois états comparés: sauvegarde avant déplacement du carré (drapeau 1), lu par FlowSync après déplacement (nouveau centre,
ancienne date, drapeau 0), écrit par FlowSync après installation (même centre, date du jour, drapeau 1).
"""
import struct
from datetime import datetime, timezone

from . import polar_decode as pd
from . import route as R

PATH = "/U/0/MAP/0/MAPINFO.BPB"


def decode(raw):
    m = pd.parse(raw)
    c = pd.parse(m[1][0])
    flag = m.get(3, [1])[0]
    return {
        "lat": struct.unpack("<d", c[1][0])[0],
        "lon": struct.unpack("<d", c[2][0])[0],
        "installed": R._dt(m[2][0]),
        "pending": flag == 0,
    }


def encode(lat, lon, installed=None, pending=False):
    """Octets de MAPINFO.BPB à écrire après l'installation d'une carte (drapeau 1) ou pour la demander (drapeau 0)."""
    when = installed or datetime.now(timezone.utc)
    center = R._varint(1 << 3 | 1) + struct.pack("<d", lat) + R._varint(2 << 3 | 1) + struct.pack("<d", lon)
    return R._ld(1, center) + R._ld(2, R._system_datetime(when)) + R._vi(3, 0 if pending else 1)


def polar_pack_bbox(lat, lon):
    """Emprise (lat_min, lon_min, lat_max, lon_max) du bloc de carte que Polar fournit pour une zone demandée (modèle déduit
    de trois blocs réels; à valider sur d'autres zones).

    Grille de cellules de 150 km: pas en latitude 1,35° ; la cellule contenant la zone demandée et ses 8 voisines forment le
    bloc (450 km). Le pas en longitude vaut 150 km / (111,195 km × cos(latitude du bord nord du bloc)), arrondi au centième de degré.
    """
    import math

    row = math.floor(lat / 1.35)
    top = (row + 2) * 1.35
    step = round(150.0 / (111.195 * math.cos(math.radians(top))), 2)
    col = math.floor(lon / step)
    return (round((row - 1) * 1.35, 2), round((col - 1) * step, 2), round(top, 2), round((col + 2) * step, 2))


def as_dict(info):
    """Version JSON de decode() pour la page."""
    return {"lat": round(info["lat"], 6), "lon": round(info["lon"], 6),
            "installed_utc": info["installed"].strftime("%Y-%m-%d %H:%M:%S"), "pending": info["pending"]}
