"""Export des séances du compteur (copiées par `backup`) en GPX 1.1 avec fréquence cardiaque et cadence."""
import glob
import os
import struct
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from . import polar_decode as pd

NS = ('xmlns="http://www.topografix.com/GPX/1/1" '
      'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')


def find_sessions(backup_dir):
    """Dossiers de séances (.../U/0/AAAAMMJJ/E/HHMMSS) contenant TSESS.BPB et SAMPLES/ROUTE."""
    pattern = os.path.join(backup_dir, "U", "0", "*", "E", "*", "TSESS.BPB")
    return sorted(os.path.dirname(p) for p in glob.glob(pattern))


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _altitude_offset(samples_raw):
    """altitude_calibration (champ 7, opération SUM): Polar Flow applique la dernière valeur à toute la séance."""
    m = pd.parse(samples_raw)
    offset = 0.0
    for v in m.get(7, []):
        c = pd.parse(v)
        if c.get(3, [2])[0] == 2 and 2 in c:
            offset = struct.unpack("<f", c[2][0])[0]
    return offset


def _pauses(samples_raw):
    """Pauses: (début en temps enregistré, durée en horloge réelle), en ms."""
    out = []
    for v in pd.parse(samples_raw).get(30, []):
        c = pd.parse(v)
        out.append((pd._duration_ms(c[1][0]), pd._duration_ms(c[2][0])))
    return sorted(out)


def load_session(session_dir):
    """Toutes les données utiles d'une séance, prêtes pour le FIT, la page web et les graphiques."""
    local, tz = pd.decode_session_start(_read(os.path.join(session_dir, "TSESS.BPB")))
    start = pd.start_utc(local, tz)
    raw = pd.read_gz(os.path.join(session_dir, "00", "SAMPLES.GZB"))
    s = pd.decode_samples(raw)
    route = pd.decode_route(pd.read_gz(os.path.join(session_dir, "00", "ROUTE.GZB")))
    base = pd.decode_base(_read(os.path.join(session_dir, "00", "BASE.BPB")))
    stats = pd.decode_stats(_read(os.path.join(session_dir, "00", "STATS.BPB")))
    pauses = _pauses(raw)
    alt_off = _altitude_offset(raw)
    interval = s["interval_ms"] or 1000
    n = max(len(s["heart_rate"]), len(s["speed"]), len(s["altitude"]), len(s["distance"]))
    start_unix = start.timestamp()

    def real_s(ms):
        return start_unix + (ms + sum(d for p, d in pauses if p < ms)) / 1000.0

    # Règle vérifiée sur les FIT de Polar Flow: l'échantillon i correspond à l'instant (i+1) × intervalle, et la position du
    # point de trace dont la durée est D va dans l'échantillon round(D / intervalle) - 1.
    pos = {}
    for k, ms in enumerate(route["duration_ms"]):
        pos[min(max(int(round(ms / interval)) - 1, 0), n - 1)] = k
    records = []
    for i in range(n):
        k = pos.get(i)
        records.append({
            "t_unix": real_s((i + 1) * interval),
            "lat": route["lat"][k] if k is not None else None, "lon": route["lon"][k] if k is not None else None,
            "alt_m": s["altitude"][i] + alt_off if i < len(s["altitude"]) else None,
            "hr": s["heart_rate"][i] if i < len(s["heart_rate"]) else None,
            "cadence": s["cadence"][i] if i < len(s["cadence"]) else None,
            "distance_m": s["distance"][i] if i < len(s["distance"]) else None,
            "speed_ms": s["speed"][i] / 3.6 if i < len(s["speed"]) else None,
        })
    events, acc = [(real_s(interval), "start")], 0
    for p, d in pauses:
        stop = start_unix + (p + acc) / 1000.0
        events += [(stop, "stop"), (stop + d / 1000.0, "start")]
        acc += d
    timer_s = base["duration_ms"] / 1000.0
    end_t = records[-1]["t_unix"] if records else start_unix
    summary = {
        "duration_s": max(end_t - records[0]["t_unix"], timer_s) if records else timer_s, "timer_s": timer_s,
        "distance_m": base["distance_m"] or 0, "calories": base["calories"], "ascent_m": base["ascent_m"], "descent_m": base["descent_m"],
        "avg_speed_ms": (stats.get("speed_avg_kmh") or 0) / 3.6, "max_speed_ms": (stats.get("speed_max_kmh") or 0) / 3.6,
        "avg_hr": stats.get("hr_avg"), "max_hr": stats.get("hr_max"), "min_hr": stats.get("hr_min"),
        "avg_cadence": stats.get("cad_avg"), "max_cadence": stats.get("cad_max"), "alt_max": stats.get("alt_max"),
    }
    events.append((end_t, "stop"))
    return {"local": local, "tz_minutes": tz, "start_unix": start_unix, "records": records, "events": events,
            "summary": summary, "route": route, "pauses": pauses}


def session_to_fit(session_dir):
    from . import fit

    return fit.encode_activity(load_session(session_dir))


def session_to_gpx(session_dir, name=None):
    local, tz = pd.decode_session_start(_read(os.path.join(session_dir, "TSESS.BPB")))
    start = pd.start_utc(local, tz)
    raw = pd.read_gz(os.path.join(session_dir, "00", "SAMPLES.GZB"))
    samples = pd.decode_samples(raw)
    route = pd.decode_route(pd.read_gz(os.path.join(session_dir, "00", "ROUTE.GZB")))
    alt_off = _altitude_offset(raw)
    pauses = _pauses(raw)
    interval = samples["interval_ms"] or 1000
    n_samples = len(samples["heart_rate"])

    pts = []
    for k, ms in enumerate(route["duration_ms"]):
        real_ms = ms + sum(d for s, d in pauses if s < ms)  # un point à l'instant exact d'une pause est avant elle
        t = start + timedelta(milliseconds=real_ms)
        idx = min(int(round(ms / interval)), max(n_samples - 1, 0))
        hr = samples["heart_rate"][idx] if idx < len(samples["heart_rate"]) else 0
        cad = samples["cadence"][idx] if idx < len(samples["cadence"]) else None
        # altitude GPS de la trace: c'est celle que Polar Flow met dans son GPX (vérifié sur 8 séances).
        alt = float(route["alt"][k]) if k < len(route["alt"]) else None
        pts.append((route["lat"][k], route["lon"][k], alt, t, hr, cad))

    title = name or f"Polar V650 {local:%Y-%m-%d %H:%M}"
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<gpx version="1.1" creator="v650sync" {NS}>',
             f"<metadata><name>{escape(title)}</name><time>{start:%Y-%m-%dT%H:%M:%S}Z</time></metadata>",
             f"<trk><name>{escape(title)}</name><trkseg>"]
    for lat, lon, alt, t, hr, cad in pts:
        ext = []
        if hr:
            ext.append(f"<gpxtpx:hr>{hr}</gpxtpx:hr>")
        if cad is not None:
            ext.append(f"<gpxtpx:cad>{cad}</gpxtpx:cad>")
        ele = f"<ele>{alt:.1f}</ele>" if alt is not None else ""
        e = f"<extensions><gpxtpx:TrackPointExtension>{''.join(ext)}</gpxtpx:TrackPointExtension></extensions>" if ext else ""
        lines.append(f'<trkpt lat="{lat:.8f}" lon="{lon:.8f}">{ele}<time>{t:%Y-%m-%dT%H:%M:%S.%f}'[:-3] + f"Z</time>{e}</trkpt>")
    lines += ["</trkseg></trk>", "</gpx>"]
    return "\n".join(lines) + "\n", start


def export_all(backup_dir, out_dir, log=print):
    os.makedirs(out_dir, exist_ok=True)
    done = 0
    for sess in find_sessions(backup_dir):
        try:
            gpx, start = session_to_gpx(sess)
        except (OSError, KeyError, ValueError) as e:
            log(f"  ignoré {os.path.relpath(sess, backup_dir)}: {type(e).__name__} {e}")
            continue
        path = os.path.join(out_dir, f"{start:%Y-%m-%d_%H-%M-%S}Z.gpx")
        with open(path, "w", encoding="utf-8") as f:
            f.write(gpx)
        done += 1
    log(f"{done} séances exportées dans {out_dir}")
    return done
