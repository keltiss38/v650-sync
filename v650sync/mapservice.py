"""Cartes hors ligne côté programme: zones, fabrication, copie de sécurité, écriture sur le compteur, reprise.

Règles de sécurité:
  - rien n'est écrit sur le compteur sans une copie locale de la carte qu'il contient (lue d'abord si elle manque);
  - un marqueur « écriture en cours » est enregistré avant d'écrire: si l'écriture est interrompue, le drapeau du compteur reste à 0 et
    le programme le reconnaît au branchement suivant (au lieu de croire que l'utilisateur a demandé une zone);
  - après l'écriture, la carte est relue en entier et son empreinte comparée.
Ce que le compteur exige (observé): drapeau de MAPINFO.BPB à 0 pour accepter la carte (le programme le lève lui-même), écriture en
plusieurs trames pour MAPDATA.MAP, puis MAPINFO.BPB (centre, date, drapeau 1). Il faut éteindre puis rallumer le compteur ensuite.
"""
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

from . import jobs, mapgen, mapinfo, paths, service
from .pftp import PftpError

MAP = "/U/0/MAP/0/MAPDATA.MAP"
MIN_BATTERY = 30


def tools_dir():
    return os.path.join(paths.settings_dir(), "outils-carte")


def maps_dir(dirs):
    return os.path.join(dirs.root, "cartes")


def _records_file(dirs):
    return os.path.join(maps_dir(dirs), "cartes.json")


def load(dirs):
    try:
        with open(_records_file(dirs), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("maps", [])
    data.setdefault("installed", None)
    data.setdefault("pending_write", None)
    return data


def save(dirs, data):
    os.makedirs(maps_dir(dirs), exist_ok=True)
    tmp = _records_file(dirs) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _records_file(dirs))


def safe_id(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:30] or "carte"


# --------------------------------------------------------------------------------------------- état et couverture

def polar_rings(info):
    """Zone d'une carte de Polar déduite du centre demandé (modèle de grille de 150 km, vérifié sur 3 blocs)."""
    s, w, n, e = mapinfo.polar_pack_bbox(info["lat"], info["lon"])
    return [[(s, w), (s, e), (n, e), (n, w)]]


def installed_coverage(dirs, map_info):
    """Zone couverte par la carte du compteur d'après `map_info` (dict de la lecture de MAPINFO) et nos enregistrements.

    Renvoie {'origin': 'ours'|'polar'|'inconnue', 'rings': [...], 'name': ...} ou None s'il n'y a pas de carte."""
    if not map_info:
        return None
    rec = load(dirs)["installed"]
    if rec and rec.get("rings"):
        c = rec.get("center") or [None, None]
        same_date = rec.get("mapinfo_utc") == map_info["installed_utc"]
        # le compteur peut réécrire la date au redémarrage: on reconnaît aussi notre carte à son centre (écrit par nous dans MAPINFO)
        same_center = c[0] is not None and abs(c[0] - map_info["lat"]) < 0.002 and abs(c[1] - map_info["lon"]) < 0.002
        if same_date or same_center:
            return {"origin": "ours", "rings": rec["rings"], "name": rec.get("name")}
    return {"origin": "polar", "rings": polar_rings(map_info), "name": "carte de Polar (zone déduite du centre)"}


def interrupted_write(dirs, map_info):
    """Vrai si le compteur attend une zone (drapeau 0) alors que NOUS avons commencé une écriture qui n'est pas finie."""
    rec = load(dirs)["pending_write"]
    return bool(map_info and map_info.get("pending") and rec)


def points_of(dirs, gpx_name_or_points):
    """Points (lat, lon) d'un tracé: soit une liste déjà prête, soit le nom d'un GPX du dossier des parcours."""
    if isinstance(gpx_name_or_points, (list, tuple)):
        return [(float(a), float(b)) for a, b in gpx_name_or_points]
    pts, _ = service.route.read_gpx(os.path.join(dirs.routes_in, os.path.basename(gpx_name_or_points)))
    return [(la, lo) for la, lo, _ in pts]


def device_routes_points(dirs):
    """Tracés des parcours du compteur, d'après la copie locale (aucun accès au compteur). {nom: [(lat, lon), ...]}"""
    import glob

    out = {}
    for f in sorted(glob.glob(os.path.join(dirs.backup, "*", "U", "0", "ROUTES", "*", "PROUTE.BPB"))):
        try:
            r = service.route.decode(open(f, "rb").read())
            out[r["name"]] = service.route.route_to_latlon(r)
        except Exception:  # noqa: BLE001 - un fichier illisible n'empêche pas les autres
            continue
    return out


# ------------------------------------------------------------------------------------------------------- plan

def dem_dir(dirs):
    return os.path.join(maps_dir(dirs), "altitudes")


def make_plan(dirs, routes, margin_km=15.0, home=None, home_half_km=0.0, shapes=None, profile="velo", contours="none"):
    """Zone à fabriquer: bande autour de chaque parcours (+ éventuellement un carré autour de `home`=(lat, lon)).

    `routes`: liste de listes de points. Renvoie la zone, la surface, la taille estimée, les extraits à télécharger et le temps estimé."""
    rings = []
    for pts in routes:
        if len(pts) >= 2:
            rings += mapgen.corridor_rings(pts, margin_km)
    if home and home_half_km > 0:
        rings.append(mapgen.rect_ring(home[0], home[1], home_half_km))
    for sh in shapes or []:  # formes dessinées sur la carte: surface (polygone) ou trait (bande autour du trait, avec la marge)
        pts = [(float(a), float(b)) for a, b in sh.get("pts", [])]
        if sh.get("type") == "path" and len(pts) >= 2:
            rings += mapgen.corridor_rings(pts, margin_km)
        elif sh.get("type") == "polygon" and len(pts) >= 3:
            rings.append(pts)
    if profile not in mapgen.PROFILES:
        raise ValueError("niveau de détail inconnu")
    if contours not in mapgen.CONTOUR_STEP:
        raise ValueError("option de courbes de niveau inconnue")
    if not rings:
        raise ValueError("aucune zone à couvrir")
    area = mapgen.area_km2(rings)
    base = tools_dir()
    feats = mapgen.load_index(os.path.join(base, "geofabrik-index.json"))
    regions = mapgen.pick_regions(feats, rings)
    osm = os.path.join(maps_dir(dirs), "osm")
    stamps = []
    for r in regions:
        r["cached"] = os.path.isfile(mapgen.cached_pbf(osm, r))
        r["osm_date"] = None
        try:  # taille de l'extrait (réseau, mémorisée une heure); en cache local: la taille du fichier déjà là
            r["mb"] = round(os.path.getsize(mapgen.cached_pbf(osm, r)) / 1e6) if r["cached"] else round(mapgen.remote_size(r["url"]) / 1e6)
        except OSError:
            r["mb"] = None
        if r["cached"]:
            ts = mapgen.pbf_timestamp(mapgen.cached_pbf(osm, r))
            r["osm_date"] = mapgen.date_fr(ts)
            stamps.append(ts)
    lo, hi = mapgen.estimate_mb(area, profile, contours)
    dl = sum(r["mb"] or 0 for r in regions if not r["cached"])
    dem = None
    if contours != "none":  # courbes de niveau: tuiles d'altitude SRTM à télécharger (une fois, gardées dans le dossier de données)
        tiles = mapgen.dem_tiles(dem_dir(dirs), rings)
        dem = {"tiles": tiles, "download_mb": round(sum(t["mb"] for t in tiles if not t["cached"])), "credit": mapgen.DEM_CREDIT}
        dl += dem["download_mb"]
    all_mb = sum(r["mb"] or 0 for r in regions)
    warn = None
    if area > 8000 or len(regions) > 3:  # le rapport d'un essai réel: 22 656 km² et 5 régions (1,5 Go de données) ont tourné plus de 20 minutes sans fin
        sp = lambda n: f"{n:,}".replace(",", " ")  # noqa: E731 - séparateur de milliers
        warn = (f"Zone très étendue ({sp(round(area))} km², {len(regions)} régions, {sp(all_mb)} Mo de données) : le téléchargement et la "
                "fabrication peuvent durer de nombreuses minutes et demander beaucoup de mémoire. Pour aller plus vite, décochez les parcours très longs "
                "ou lointains, ou dessinez une zone plus restreinte.")
    south, west, north, east = mapgen.bbox(rings)
    return {"rings": rings, "bbox": [south, west, north, east], "area_km2": round(area), "estimate_mb": [lo, hi], "regions": regions,
            "osm_date": mapgen.date_fr(min((t for t in stamps if t), default=None)), "all_mb": all_mb, "warning": warn,
            "download_mb": dl, "tools": mapgen.tools_status(base), "margin_km": margin_km, "profile": profile, "contours": contours, "dem": dem,
            "minutes": max(3, round(area / 2500 + sum(r["mb"] or 0 for r in regions) / 150)) + (1 if dem else 0),
            "center": [round((south + north) / 2, 4), round((west + east) / 2, 4)]}


def route_coverage(dirs, map_info, routes):
    """Pour chaque tracé: part couverte par la carte du compteur. `routes`: {nom: points}."""
    cov = installed_coverage(dirs, map_info)
    if not cov:
        return None
    out = {}
    for name, pts in routes.items():
        c = mapgen.coverage(pts, cov["rings"])
        out[name] = c
    return {"origin": cov["origin"], "name": cov["name"], "routes": out, "rings": [[list(p) for p in r] for r in cov["rings"]],
            "outside": any(v["km_outside"] > 0.5 for v in out.values())}


# ------------------------------------------------------------------------------------------------ fabrication

def build(dirs, plan, name, log, progress, cancel=None, refresh=False):
    """Installe les outils si besoin, télécharge les extraits manquants (tous si `refresh`: données plus récentes), fabrique la carte
    et l'enregistre. Renvoie l'enregistrement. Chaque outil et chaque région est une étape avec sa propre barre."""
    base = tools_dir()
    st = jobs.steps_of(progress)
    osm = os.path.join(maps_dir(dirs), "osm")
    max_age = 0 if refresh else 30
    status = mapgen.tools_status(base)
    todo = [r for r in plan["regions"] if mapgen.needs_download(osm, r, max_age)]
    mode = plan.get("contours") or "none"
    rings = [[tuple(p) for p in r] for r in plan["rings"]]
    alt = [t for t in mapgen.dem_tiles(dem_dir(dirs), rings) if not t["cached"]] if mode != "none" else []
    st.plan([("tool_" + i["id"], f"Télécharger et installer {i['label']} ({i['mb']:.0f} Mo)") for i in status["items"] if not i["installed"]]
            + [("reg_" + r["name"], f"Télécharger les données OpenStreetMap : {r['name']}" + (f" ({r['mb']} Mo)" if r.get("mb") else "")) for r in todo]
            + [("alt_" + t["name"], f"Télécharger les altitudes SRTM : {t['name']} (environ {t['mb']:.0f} Mo)") for t in alt]
            + ([("contours", "Calculer les courbes de niveau")] if mode != "none" else [])
            + [("build", "Fabriquer la carte"), ("check", "Contrôler et enregistrer la carte")])
    if not status["installed"]:
        mapgen.install_tools(base, log, progress)
    pbfs = mapgen.fetch_regions(osm, plan["regions"], log, progress, max_age)
    stamps = [mapgen.pbf_timestamp(p) for p in pbfs]
    osm_date = mapgen.date_fr(min((t for t in stamps if t), default=None))
    mid = f"{safe_id(name)}-{time.strftime('%Y%m%d-%H%M%S')}"
    mdir = os.path.join(maps_dir(dirs), mid)
    out = os.path.join(mdir, "MAPDATA.MAP")
    os.makedirs(mdir, exist_ok=True)
    work = os.path.join(mdir, "tmp")
    os.makedirs(work, exist_ok=True)
    cont_osm = None
    if mode != "none":
        cont_osm = os.path.join(work, "courbes.osm")
        # les étapes « altitudes » sont commencées par fetch_dem; « contours » ne commence qu'ensuite, pour le calcul
        dem = mapgen.fetch_dem(dem_dir(dirs), rings, log, progress, cancel)
        st.begin("contours")
        n_lines = mapgen.make_contours_from(dem, rings, mode, cont_osm, log, progress) if dem is not None else 0
        if not n_lines:
            cont_osm = None
    st.begin("build")
    info = mapgen.build_map(base, rings, pbfs, out, work, log, progress, cancel, plan.get("profile", "velo"), contours_osm=cont_osm)
    st.begin("check")
    shutil.rmtree(os.path.join(mdir, "tmp"), ignore_errors=True)
    info["sha256"] = mapgen.sha256_file(out)
    rec = {"id": mid, "name": name, "path": out, "size": info["size"], "sha256": info["sha256"], "bbox": info["bbox"], "rings": rings,
           "center": plan["center"], "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "margin_km": plan.get("margin_km"),
           "regions": [r["name"] for r in plan["regions"]], "kind": "fabriquee", "profile": plan.get("profile", "velo"), "osm_date": osm_date,
           "contours": mode if cont_osm else "none"}
    data = load(dirs)
    data["maps"].append(rec)
    save(dirs, data)
    log(f"Carte « {name} » prête: {info['size'] / 1e6:.0f} Mo (données OpenStreetMap du {osm_date or 'date inconnue'})")
    return {k: rec[k] for k in ("id", "name", "size", "created", "center", "regions", "profile", "osm_date", "contours")}


def list_maps(dirs):
    data = load(dirs)
    inst = data["installed"]
    return {"maps": [{k: m.get(k) for k in ("id", "name", "size", "created", "center", "kind", "regions", "profile", "osm_date", "contours")} | {"exists": os.path.isfile(m["path"])}
                     for m in data["maps"]],
            "installed": {k: inst.get(k) for k in ("id", "name", "written_utc", "center", "size", "osm_date", "created")} if inst else None,
            "pending_write": data["pending_write"]}


def delete_map(dirs, map_id):
    data = load(dirs)
    keep = []
    for m in data["maps"]:
        if m["id"] == map_id:
            if data["installed"] and data["installed"].get("id") == map_id:
                raise ValueError("cette carte est installée sur le compteur: elle sert de copie")
            shutil.rmtree(os.path.dirname(m["path"]), ignore_errors=True)
        else:
            keep.append(m)
    data["maps"] = keep
    save(dirs, data)


# ----------------------------------------------------------------------------------------- écriture sur le compteur

def _device_map_size(c):
    return dict(c.list_dir("/U/0/MAP/0/")).get(MAP, 0)


def ensure_local_backup(dirs, c, log, progress):
    """Garantit qu'une copie locale de la carte actuelle du compteur existe. Renvoie son enregistrement (ou None s'il n'y a pas de carte)."""
    size = _device_map_size(c)
    if not size:
        log("Le compteur n'a pas de carte: rien à sauvegarder")
        return None
    data = load(dirs)
    for m in data["maps"]:
        if m["size"] == size and os.path.isfile(m["path"]) and (data["installed"] or {}).get("id") == m["id"]:
            log("Copie locale de la carte actuelle déjà présente")
            return m
    for m in data["maps"]:  # un enregistrement de même taille déjà sauvegardé plus tôt
        if m.get("kind") == "sauvegarde" and m["size"] == size and os.path.isfile(m["path"]):
            log("Copie locale de la carte actuelle déjà présente")
            return m
    log(f"Lecture de la carte actuelle du compteur ({size / 1e6:.0f} Mo) pour en garder une copie")
    raw = c.get_file(MAP, progress=lambda n: progress(n, size))
    mid = "sauvegarde-" + time.strftime("%Y%m%d-%H%M%S")
    mdir = os.path.join(maps_dir(dirs), mid)
    os.makedirs(mdir, exist_ok=True)
    out = os.path.join(mdir, "MAPDATA.MAP")
    with open(out, "wb") as f:
        f.write(raw)
    try:
        info = mapgen.inspect_map(out)
        bbox = info["bbox"]
    except mapgen.MapgenError:
        bbox = None
    rec = {"id": mid, "name": "Carte du compteur avant remplacement", "path": out, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
           "bbox": bbox, "rings": [[(bbox[0], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]), (bbox[2], bbox[1])]] if bbox else None,
           "center": None, "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "kind": "sauvegarde", "regions": []}
    data = load(dirs)
    data["maps"].append(rec)
    save(dirs, data)
    log("Copie locale enregistrée")
    return rec


def write_map(dirs, map_id, log, progress):
    """Écrit la carte `map_id` (fabriquée ou sauvegardée) sur le compteur, avec toutes les vérifications. ÉCRIT SUR LE COMPTEUR."""
    data = load(dirs)
    rec = next((m for m in data["maps"] if m["id"] == map_id), None)
    if not rec or not os.path.isfile(rec["path"]):
        raise ValueError("carte introuvable")
    with open(rec["path"], "rb") as f:
        blob = f.read()
    want = hashlib.sha256(blob).hexdigest()
    if want != rec["sha256"]:
        raise ValueError("le fichier de la carte a changé depuis sa fabrication: écriture refusée")
    mapgen.inspect_map(rec["path"])
    op = len(b"\x08\x01\x12" + bytes([len(MAP)]) + MAP.encode())
    if (2 + op + len(blob)) % 16383 == 0:
        raise ValueError("taille de carte impossible à écrire (multiple exact de 16 383): changez légèrement la marge et refabriquez")
    stp = jobs.steps_of(progress)
    stp.plan([("check", "Vérifier le compteur (batterie, place disponible)"), ("backup", "Sauvegarder la carte actuelle du compteur sur ce PC"),
              ("ask", "Demander la zone au compteur"), ("send", f"Envoyer la carte ({len(blob) / 1e6:.0f} Mo)"),
              ("verify", "Relire la carte pour la vérifier")])
    stp.begin("check")
    with service.device() as (dev, c):
        st = service._status_from(dev, c)
        if (st.get("battery") or 0) < MIN_BATTERY:
            raise ValueError(f"batterie du compteur trop basse ({st.get('battery')} %): branchez-le et chargez-le d'abord")
        cur = _device_map_size(c)
        free = (st.get("free_gb") or 0) * 1e9
        if free + cur < len(blob) + 100e6:
            raise ValueError("pas assez de place sur le compteur pour cette carte")
        orig_raw = c.get_file(mapinfo.PATH)
        orig = mapinfo.decode(orig_raw)
        stp.begin("backup")
        ensure_local_backup(dirs, c, log, progress)
        data = load(dirs)
        data["pending_write"] = {"map_id": map_id, "since": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        save(dirs, data)
        stp.begin("ask")
        if not orig["pending"]:
            log("Demande de zone (drapeau de MAPINFO à 0)")
            c.put(mapinfo.PATH, mapinfo.encode(orig["lat"], orig["lon"], orig["installed"], pending=True))
            time.sleep(0.5)
        log(f"Écriture de la carte ({len(blob) / 1e6:.0f} Mo)")
        stp.begin("send")
        t0 = time.time()
        try:
            c.put(MAP, blob, progress=progress)
        except PftpError:
            try:  # carte refusée: on remet MAPINFO comme avant, la carte du compteur n'a pas été modifiée par un refus
                c.put(mapinfo.PATH, orig_raw)
            except Exception:  # noqa: BLE001
                pass
            raise
        log(f"Carte envoyée en {time.time() - t0:.0f} s")
        lat, lon = rec["center"] or ((rec["bbox"][0] + rec["bbox"][2]) / 2, (rec["bbox"][1] + rec["bbox"][3]) / 2)
        now = datetime.now(timezone.utc)
        new = mapinfo.encode(lat, lon, now, pending=False)
        c.put(mapinfo.PATH, new)
        time.sleep(1.0)
        log("Relecture complète pour vérifier")
        stp.begin("verify")
        back = c.get_file(MAP, progress=lambda n: progress(n, len(blob)))
        if hashlib.sha256(back).hexdigest() != want:
            raise ValueError("la relecture de la carte ne correspond pas: ne pas utiliser le compteur avec cette carte, relancer l'écriture")
        mi = mapinfo.decode(c.get_file(mapinfo.PATH))
    data = load(dirs)
    data["installed"] = {"id": map_id, "name": rec["name"], "written_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "mapinfo_utc": mi["installed"].strftime("%Y-%m-%d %H:%M:%S"),
                         "center": [lat, lon], "size": len(blob), "rings": rec["rings"], "osm_date": rec.get("osm_date"), "created": rec.get("created")}
    data["pending_write"] = None
    save(dirs, data)
    log("Terminé. Éteignez puis rallumez le compteur pour qu'il prenne la carte en compte.")
    return {"ok": True, "verified": True, "name": rec["name"], "size": len(blob), "note": "Éteignez puis rallumez le compteur."}
