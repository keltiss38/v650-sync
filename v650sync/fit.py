"""Écriture d'un fichier FIT (activité vélo) à partir d'une séance du V650.

Messages produits: file_id, event (démarrage/arrêt du chrono), record (1 par échantillon: position, altitude, FC, cadence,
distance, vitesse), lap, session, activity. Les numéros de champ sont ceux du profil FIT public du SDK Garmin.
"""
import struct

EPOCH_OFFSET = 631065600  # 1989-12-31T00:00:00Z en secondes Unix

ENUM, SINT8, UINT8, SINT16, UINT16, SINT32, UINT32 = 0x00, 0x01, 0x02, 0x83, 0x84, 0x85, 0x86
INVALID = {ENUM: 0xFF, UINT8: 0xFF, UINT16: 0xFFFF, SINT32: 0x7FFFFFFF, UINT32: 0xFFFFFFFF, SINT16: 0x7FFF}
SIZE = {ENUM: 1, SINT8: 1, UINT8: 1, SINT16: 2, UINT16: 2, SINT32: 4, UINT32: 4}
FMT = {ENUM: "<B", SINT8: "<b", UINT8: "<B", SINT16: "<h", UINT16: "<H", SINT32: "<i", UINT32: "<I"}

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc16(data, crc=0):
    for b in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[b & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc


class _Writer:
    def __init__(self):
        self.body = bytearray()
        self.defined = {}

    def define(self, local, mesg_num, fields):
        """fields: [(numéro de champ, type de base)]"""
        self.body += bytes([0x40 | local, 0, 0]) + struct.pack("<HB", mesg_num, len(fields))
        for num, bt in fields:
            self.body += bytes([num, SIZE[bt], bt])
        self.defined[local] = fields

    def data(self, local, values):
        self.body.append(local)
        for (num, bt), v in zip(self.defined[local], values):
            if v is None:
                v = INVALID[bt]
            self.body += struct.pack(FMT[bt], int(round(v)))

    def finish(self):
        header = struct.pack("<BBHI4s", 14, 0x20, 2140, len(self.body), b".FIT")
        header += struct.pack("<H", crc16(header))
        out = header + bytes(self.body)
        return out + struct.pack("<H", crc16(out))


def _fit_time(unix_s):
    return int(unix_s // 1) - EPOCH_OFFSET  # Polar tronque à la seconde (vérifié sur 3 séances)


def _semicircles(deg):
    return None if deg is None else int(round(deg * (2 ** 31) / 180.0))


def encode_activity(sess):
    """sess: dict avec start_unix (float), records[], events[], summary{}.

    records: dicts t_unix, lat, lon, alt_m, hr, cadence, distance_m, speed_ms (None si absent).
    events: [(t_unix, 'start'|'stop')]. summary: duration_s, timer_s, distance_m, avg_speed_ms, max_speed_ms, avg_hr, max_hr,
    ascent_m, descent_m, calories, avg_cadence, max_cadence.
    """
    w = _Writer()
    start = sess["start_unix"]
    end = sess["records"][-1]["t_unix"] if sess["records"] else start
    s = sess["summary"]

    # file_id: type 4 = activity, fabricant 123 = Polar Electro
    w.define(0, 0, [(0, ENUM), (1, UINT16), (2, UINT16), (3, UINT32), (4, UINT32)])
    w.data(0, [4, 123, 0xFFFF, 1, _fit_time(start)])

    w.define(1, 21, [(253, UINT32), (0, ENUM), (1, ENUM), (4, UINT8)])  # event
    for t, kind in sess["events"]:
        w.data(1, [_fit_time(t), 0, 0 if kind == "start" else 4, 0])

    w.define(2, 20, [(253, UINT32), (0, SINT32), (1, SINT32), (2, UINT16), (3, UINT8), (4, UINT8), (5, UINT32), (6, UINT16)])  # record
    for r in sess["records"]:
        w.data(2, [_fit_time(r["t_unix"]), _semicircles(r.get("lat")), _semicircles(r.get("lon")),
                   None if r.get("alt_m") is None else (r["alt_m"] + 500) * 5,
                   r.get("hr") or None, r.get("cadence"),
                   None if r.get("distance_m") is None else r["distance_m"] * 100,
                   None if r.get("speed_ms") is None else r["speed_ms"] * 1000])

    common = [s["duration_s"] * 1000, s["timer_s"] * 1000, s["distance_m"] * 100]
    lap_fields = [(253, UINT32), (0, ENUM), (1, ENUM), (2, UINT32), (7, UINT32), (8, UINT32), (9, UINT32), (13, UINT16), (14, UINT16),
                  (15, UINT8), (16, UINT8), (21, UINT16), (22, UINT16), (25, ENUM), (254, UINT16)]
    w.define(3, 19, lap_fields)
    w.data(3, [_fit_time(end), 9, 1, _fit_time(start), *common, s["avg_speed_ms"] * 1000, s["max_speed_ms"] * 1000,
               s.get("avg_hr"), s.get("max_hr"), s.get("ascent_m"), s.get("descent_m"), 2, 0])

    sess_fields = [(253, UINT32), (0, ENUM), (1, ENUM), (2, UINT32), (5, ENUM), (6, ENUM), (7, UINT32), (8, UINT32), (9, UINT32),
                   (11, UINT16), (14, UINT16), (15, UINT16), (16, UINT8), (17, UINT8), (18, UINT8), (19, UINT8), (22, UINT16),
                   (23, UINT16), (25, UINT16), (26, UINT16), (254, UINT16)]
    w.define(4, 18, sess_fields)
    w.data(4, [_fit_time(end), 8, 1, _fit_time(start), 2, 7, *common, s.get("calories"), s["avg_speed_ms"] * 1000,
               s["max_speed_ms"] * 1000, s.get("avg_hr"), s.get("max_hr"), s.get("avg_cadence"), s.get("max_cadence"),
               s.get("ascent_m"), s.get("descent_m"), 0, 1, 0])

    w.define(5, 34, [(253, UINT32), (0, UINT32), (1, UINT16), (2, ENUM), (3, ENUM), (4, ENUM), (5, UINT32)])  # activity
    w.data(5, [_fit_time(end), s["timer_s"] * 1000, 1, 0, 26, 1, _fit_time(end)])
    return w.finish()
