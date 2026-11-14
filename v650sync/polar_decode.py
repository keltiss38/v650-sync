"""Décodage des fichiers de séance Polar (protobuf) sans dépendance externe.

Structures (définitions du projet cmaion/polar, vérifiées sur des fichiers du V650):
- TSESS.BPB   PbTrainingSession  (champ 1 = start: PbLocalDateTime)
- SAMPLES.GZB PbExerciseSamples  (gzip)
- ROUTE.GZB   PbExerciseRouteSamples (gzip)
- RR.GZB      PbExerciseRRIntervals (gzip)
"""
import gzip
import struct
from datetime import datetime, timedelta, timezone


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def parse(b):
    """Message protobuf -> {numéro de champ: [valeurs]} ; varint -> int, longueur -> bytes, 32/64 bits -> bytes."""
    out, i = {}, 0
    while i < len(b):
        tag, i = _varint(b, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"type de fil protobuf inconnu: {wt}")
        out.setdefault(f, []).append(v)
    return out


def _packed_varints(values):
    """Champ répété uint32: soit paquets (bytes), soit valeurs individuelles (int)."""
    out = []
    for v in values:
        if isinstance(v, int):
            out.append(v)
        else:
            i = 0
            while i < len(v):
                n, i = _varint(v, i)
                out.append(n)
    return out


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def _packed_floats(values):
    out = []
    for v in values:
        out.extend(struct.unpack(f"<{len(v) // 4}f", v))
    return out


def _packed_doubles(values):
    out = []
    for v in values:
        out.extend(struct.unpack(f"<{len(v) // 8}d", v))
    return out


def _duration_ms(b):
    d = parse(b)
    g = lambda k: d.get(k, [0])[0]  # noqa: E731
    return ((g(1) * 60 + g(2)) * 60 + g(3)) * 1000 + g(4)


def _datetime(b):
    """PbLocalDateTime / PbSystemDateTime -> (datetime naïf, décalage fuseau en minutes ou None)."""
    d = parse(b)
    date, time = parse(d[1][0]), parse(d[2][0])
    dt = datetime(date[1][0], date[2][0], date[3][0], time[1][0], time[2][0], time[3][0], time.get(4, [0])[0] * 1000)
    tz = d.get(4)
    return dt, (_zigzag_int32(tz[0]) if tz else None)


def _zigzag_int32(n):
    """int32 protobuf (non zigzag): varint 64 bits en complément à deux si négatif."""
    return n - (1 << 64) if n >= (1 << 63) else n


def read_gz(path):
    with open(path, "rb") as f:
        return gzip.decompress(f.read())


def decode_route(raw):
    m = parse(raw)
    out = {
        "duration_ms": _packed_varints(m.get(1, [])),
        "lat": _packed_doubles(m.get(2, [])),
        "lon": _packed_doubles(m.get(3, [])),
        "alt": [_zigzag(n) for n in _packed_varints(m.get(4, []))],
        "satellites": _packed_varints(m.get(5, [])),
    }
    if 9 in m:
        out["first_location_time"], _ = _datetime(m[9][0])
    return out


def decode_samples(raw):
    m = parse(raw)
    return {
        "interval_ms": _duration_ms(m[1][0]) if 1 in m else 1000,
        "heart_rate": _packed_varints(m.get(2, [])),
        "cadence": _packed_varints(m.get(4, [])),
        "altitude": _packed_floats(m.get(6, [])),
        "temperature": _packed_floats(m.get(8, [])),
        "speed": _packed_floats(m.get(9, [])),
        "distance": _packed_floats(m.get(11, [])),
    }


def decode_rr(raw):
    m = parse(raw)
    return {"rr_ms": _packed_varints(m.get(1, []))}


def decode_session_start(raw):
    """TSESS.BPB -> (début local naïf, décalage UTC en minutes)."""
    return _datetime(parse(raw)[1][0])


def _f32(b):
    return struct.unpack("<f", b)[0]


def decode_base(raw):
    """BASE.BPB (PbExerciseBase): durée, distance, calories, montée/descente, sport."""
    m = parse(raw)
    return {
        "duration_ms": _duration_ms(m[2][0]) if 2 in m else 0,
        "distance_m": _f32(m[4][0]) if 4 in m else None,
        "calories": m.get(5, [None])[0],
        "ascent_m": _f32(m[10][0]) if 10 in m else None,
        "descent_m": _f32(m[11][0]) if 11 in m else None,
        "sport_id": parse(m[3][0])[1][0] if 3 in m else None,
    }


def decode_stats(raw):
    """STATS.BPB (PbExerciseStatistics): FC min/moy/max, vitesse moy/max (km/h), cadence, altitude."""
    m = parse(raw)
    out = {}
    if 1 in m:
        h = parse(m[1][0])
        out.update(hr_min=h.get(1, [None])[0], hr_avg=h.get(2, [None])[0], hr_max=h.get(3, [None])[0])
    if 2 in m:
        s = parse(m[2][0])
        out.update(speed_avg_kmh=_f32(s[1][0]) if 1 in s else None, speed_max_kmh=_f32(s[2][0]) if 2 in s else None)
    if 3 in m:
        c = parse(m[3][0])
        out.update(cad_avg=c.get(1, [None])[0], cad_max=c.get(2, [None])[0])
    if 4 in m:
        a = parse(m[4][0])
        out.update(alt_min=_f32(a[1][0]) if 1 in a else None, alt_max=_f32(a[3][0]) if 3 in a else None)
    return out


def start_utc(local_dt, tz_minutes):
    return (local_dt - timedelta(minutes=tz_minutes or 0)).replace(tzinfo=timezone.utc)
