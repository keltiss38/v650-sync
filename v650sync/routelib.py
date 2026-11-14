"""Bibliothèque de parcours: ceux du compteur, ceux de Komoot et les fichiers GPX réunis en UNE liste (un parcours = une ligne).

Rapprochement d'un parcours du compteur et d'un parcours Komoot: d'abord par l'identifiant Komoot que FlowSync (et ce programme) inscrit
dans le fichier du compteur, sinon par le nom. Le statut « synchronisé » se décide sur la TRACE, pas sur le nom: on compare la trace du
compteur à celle du GPX Komoot (écart moyen et longueur), car on peut modifier une trace dans Komoot sans en changer le nom.
"""
import glob
import json
import math
import os
import re
import unicodedata

from . import route

SYNC_BASE_M, SYNC_PER_KM_M = 12.0, 0.06   # écart moyen toléré (m) = 12 + 0,06 × longueur (km)
SYNC_MAX_KM_PCT = 3.0                     # écart de longueur toléré
_MEM = {}                # résultats mémorisés: (chemin, date de modification) -> données


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _thumb_track(pts, n=120):
    step = max(1, len(pts) // n)
    out = [[round(p[0], 5), round(p[1], 5)] for p in pts[::step]]
    last = [round(pts[-1][0], 5), round(pts[-1][1], 5)]
    return out if out[-1] == last else out + [last]


def tracks_match(a, b):
    """(écart moyen en m, écart de longueur en %, vrai si les deux traces sont les mêmes à la tolérance près)."""
    if len(a) < 2 or len(b) < 2:
        return None, None, False
    key = (len(a), a[0], a[len(a) // 2], a[-1], len(b), b[0], b[len(b) // 2], b[-1])  # le calcul coûte ~0,1 s par paire: mémorisé (la liste est reconstruite à chaque clic)
    if key in _MATCH:
        return _MATCH[key]
    res = _tracks_match(a, b)
    _MATCH[key] = res
    return res


_MATCH = {}


def _tracks_match(a, b):
    def length(p):
        return sum(math.hypot((q[0] - r[0]) * 111.2, (q[1] - r[1]) * 111.32 * math.cos(math.radians(r[0]))) for r, q in zip(p, p[1:]))

    def mean_to(src, ref):
        lat0 = sum(p[0] for p in ref) / len(ref)
        ky, kx = 111200.0, 111320.0 * math.cos(math.radians(lat0))
        dense = []
        for p, q in zip(ref, ref[1:]):
            n = max(1, int(math.hypot((q[0] - p[0]) * ky, (q[1] - p[1]) * kx) / 100))
            dense += [(p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n) for i in range(n)]
        dense.append(ref[-1])
        sample = src[::max(1, len(src) // 150)]
        return sum(min(math.hypot((p[0] - d[0]) * ky, (p[1] - d[1]) * kx) for d in dense) for p in sample) / len(sample)

    mean = (mean_to(a, b) + mean_to(b, a)) / 2
    la, lb = length(a), length(b)
    pct = abs(la - lb) / (lb or 1) * 100
    # tolérance de l'écart moyen proportionnelle à la longueur: la simplification de FlowSync (~450 points) écarte d'autant plus
    # que le parcours est long (observé: 1,7 m pour 15 km, 10 m pour 49 km, 14,8 m pour 215 km); une trace décalée de 20 à 30 m est refusée
    tol = SYNC_BASE_M + SYNC_PER_KM_M * lb
    return round(mean, 1), round(pct, 2), mean < tol and pct < SYNC_MAX_KM_PCT


def _ascent(vals):
    asc, ref = 0.0, vals[0]
    for v in vals:
        d = v - ref
        if d >= 3:
            asc, ref = asc + d, v
        elif d <= -3:
            ref = v
    return round(asc)


# ---------------------------------------------------------------------------------------------------- sources

def _iso(x):
    """Date/heure -> « AAAA-MM-JJ HH:MM:SS » (triable comme du texte). Accepte datetime, époque, ou texte ISO (Komoot: 2026-11-09T18:20:11.123Z)."""
    import time as _t

    if x is None or x == "":
        return ""
    if isinstance(x, (int, float)):
        return _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(x))
    if hasattr(x, "strftime"):
        return x.strftime("%Y-%m-%d %H:%M:%S")
    return str(x)[:19].replace("T", " ")


def device_routes(dirs, live=None):
    """Parcours du compteur. `live`: liste déjà lue sur le compteur (service.list_routes); sinon la copie locale de la sauvegarde."""
    if live is not None:
        return [{"slot": x["slot"], "name": x["name"], "km": x["km"], "points": x["points"], "dplus": (x.get("stats") or {}).get("ascent"),
                 "pts": [tuple(p) for p in x.get("track") or []], "source_id": x.get("source_id")} for x in live if x.get("points")]
    out = []
    for f in sorted(glob.glob(os.path.join(dirs.backup, "*", "U", "0", "ROUTES", "*", "PROUTE.BPB"))):
        key = (f, os.path.getmtime(f))
        if key not in _MEM:
            try:
                with open(f, "rb") as fh:
                    r = route.decode(fh.read())
                ll = route.route_to_latlon(r)
                _MEM[key] = {"slot": int(os.path.basename(os.path.dirname(f))), "name": r["name"], "km": round(r["length"] / 1000, 1),
                             "points": len(r["points"]), "dplus": _ascent([p[3] for p in r["points"]]), "pts": ll, "source_id": r["source_id"],
                             "modified": _iso(r.get("modified") or r.get("created")), "created": _iso(r.get("created"))}
            except Exception:  # noqa: BLE001 - un fichier illisible n'empêche pas les autres
                _MEM[key] = None
        if _MEM[key]:
            out.append(dict(_MEM[key]))
    return sorted(out, key=lambda x: x["slot"])


def gpx_files(dirs):
    """GPX du dossier des parcours: {nom de fichier: {name, km, dplus, pts, komoot_id}}."""
    out = {}
    for f in sorted(glob.glob(os.path.join(dirs.routes_in, "*.gpx"))):
        key = (f, os.path.getmtime(f))
        if key not in _MEM:
            try:
                pts, name = route.read_gpx(f)
                st = route.gpx_stats(pts)
                m = re.match(r"\d{4}-\d{2}-\d{2}_(\d+)_", os.path.basename(f))
                _MEM[key] = {"name": name or route.name_from_filename(f), "km": st["km"], "dplus": st["ascent"] if st["has_ele"] else None,
                             "pts": [(a, b) for a, b, _ in pts], "komoot_id": m.group(1) if m else None, "points": len(pts),
                             "modified": _iso(os.path.getmtime(f))}
            except Exception:  # noqa: BLE001
                _MEM[key] = None
        if _MEM[key]:
            out[os.path.basename(f)] = _MEM[key]
    return out


def _cache_path(dirs):
    return os.path.join(dirs.root, "parcours", "komoot.json")


def komoot_cache(dirs):
    try:
        with open(_cache_path(dirs), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tours": [], "fetched": None}


def save_komoot_cache(dirs, tours):
    import time

    os.makedirs(os.path.dirname(_cache_path(dirs)), exist_ok=True)
    with open(_cache_path(dirs), "w", encoding="utf-8") as f:
        json.dump({"tours": tours, "fetched": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------------- fusion

def build(dirs, live_device=None):
    """Liste unique. Un parcours présent à la fois sur le compteur et dans Komoot n'apparaît qu'une fois."""
    dev = device_routes(dirs, live_device)
    files = gpx_files(dirs)
    tours = komoot_cache(dirs)["tours"]
    by_id = {str(t["id"]): t for t in tours}
    used_files, used_tours, items = set(), set(), []

    def file_for(kid, name):
        if kid:
            for fn, g in files.items():
                if g["komoot_id"] == str(kid) and fn not in used_files:
                    return fn
        n = norm(name)
        for fn, g in files.items():
            if fn not in used_files and (norm(g["name"]) == n or norm(route.name_from_filename(fn)) == n):
                return fn
        return None

    for d in dev:
        kid = d.get("source_id") if d.get("source_id") in by_id else None
        n = norm(d["name"])
        if not kid:
            for t in tours:
                tn = norm(t["name"])
                if str(t["id"]) not in used_tours and (tn == n or norm(route.safe_name(t["name"])) == n or (len(n) >= 20 and tn.startswith(n[:20]))):
                    kid = str(t["id"])
                    break
        if not kid and d.get("source_id"):
            kid = d["source_id"]  # identifiant Komoot connu même si Komoot n'est pas connecté
        fn = file_for(kid, d["name"])
        t = by_id.get(str(kid)) if kid else None
        status, mean, pct = None, None, None
        if fn:
            mean, pct, same = tracks_match(d["pts"], files[fn]["pts"]) if d["pts"] else (None, None, False)
            status = "sync" if same else ("diff" if mean is not None else None)
            used_files.add(fn)
        if kid:
            used_tours.add(str(kid))
        items.append({"id": f"d{d['slot']}", "name": d["name"], "km": d["km"], "dplus": d["dplus"], "points": d["points"], "slot": d["slot"], "on_device": True,
                      "komoot_id": kid, "file": fn, "source": "komoot" if (t or kid) else "compteur", "status": status, "mean_m": mean, "km_diff_pct": pct,
                      "track": _thumb_track(d["pts"]) if d["pts"] else [], "date": (t or {}).get("date"),
                      "created": _iso((t or {}).get("date")) or d.get("created", ""), "changed": _iso((t or {}).get("changed")),
                      # tri « Date »: comme Komoot (dernière modification sur Komoot) ; sans Komoot, la date du parcours dans le compteur
                      "modified": _iso((t or {}).get("changed") or (t or {}).get("date")) if t else _iso(d.get("modified"))})
    for t in tours:
        if str(t["id"]) in used_tours:
            continue
        fn = file_for(t["id"], t["name"])
        g = files.get(fn) if fn else None
        if fn:
            used_files.add(fn)
        items.append({"id": f"k{t['id']}", "name": t["name"], "km": g["km"] if g else t["km"], "dplus": g["dplus"] if g else t.get("up"), "points": g["points"] if g else None,
                      "slot": None, "on_device": False, "komoot_id": str(t["id"]), "file": fn, "source": "komoot", "status": None, "mean_m": None,
                      "km_diff_pct": None, "track": _thumb_track(g["pts"]) if g else [], "date": t.get("date"),
                      "created": _iso(t.get("date")), "changed": _iso(t.get("changed")),
                      "modified": _iso(t.get("changed") or t.get("date"))})
    for fn, g in files.items():
        if fn in used_files:
            continue
        items.append({"id": f"f{fn}", "name": g["name"], "km": g["km"], "dplus": g["dplus"], "points": g["points"], "slot": None, "on_device": False,
                      "komoot_id": g["komoot_id"], "file": fn, "source": "gpx", "status": None, "mean_m": None, "km_diff_pct": None,
                      "track": _thumb_track(g["pts"]), "date": None, "created": g.get("modified", ""), "changed": "",
                      "modified": g.get("modified", "")})
    return items


def detail(dirs, item_id):
    """Détail d'un élément pour la page: chiffres, tracé, profil, et la trace Komoot à comparer si elle diffère de celle du compteur."""
    from . import service

    item = next((i for i in build(dirs) if i["id"] == item_id), None)
    if not item:
        return {"error": "parcours introuvable"}
    out = {"item": item}
    gpx = os.path.join(dirs.routes_in, item["file"]) if item.get("file") else None
    if item["on_device"]:
        f = glob.glob(os.path.join(dirs.backup, "*", "U", "0", "ROUTES", str(item["slot"]), "PROUTE.BPB"))
        if not f:
            return {"error": "copie locale du parcours absente: lisez le compteur d'abord", "item": item}
        with open(f[0], "rb") as fh:
            r = route.decode(fh.read())
        ll = route.route_to_latlon(r)
        out["stats"] = route.gpx_stats([(la, lo, r["alt"] + p[3]) for (la, lo), p in zip(ll, r["points"])])
        step = max(1, len(ll) // 400)
        out["track"] = [[round(a, 5), round(b, 5)] for a, b in ll[::step]] + [[round(ll[-1][0], 5), round(ll[-1][1], 5)]]
        out["device_points"] = len(r["points"])
        if gpx and os.path.isfile(gpx) and item["status"] == "diff":
            pts = gpx_files(dirs)[item["file"]]["pts"]
            s2 = max(1, len(pts) // 400)
            out["other_track"] = [[round(a, 5), round(b, 5)] for a, b in pts[::s2]]
    elif gpx and os.path.isfile(gpx):
        out["preview"] = service.preview_route(gpx)
        out["preview"]["file"] = item["file"]
    else:
        out["need_download"] = True
    return out


def points_of_item(dirs, item_id, live_device=None):
    """Points (lat, lon) complets d'un élément de la bibliothèque, pour la couverture et la carte."""
    if item_id.startswith("d"):
        for d in device_routes(dirs, None):
            if f"d{d['slot']}" == item_id:
                return d["pts"]
    fn = item_id[1:] if item_id.startswith("f") else None
    files = gpx_files(dirs)
    if item_id.startswith("k"):
        kid = item_id[1:]
        fn = next((n for n, g in files.items() if g["komoot_id"] == kid), None)
    return files[fn]["pts"] if fn in files else []
