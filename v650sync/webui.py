"""Page web locale (127.0.0.1) pour piloter le V650: synchroniser, télécharger les séances en GPX, ajouter des itinéraires.

Sécurité: le serveur n'écoute que sur 127.0.0.1, vérifie l'en-tête Host, et exige un jeton aléatoire (en-tête X-Token) pour
toute action qui modifie quelque chose. Une page web quelconque ne peut donc pas écrire sur le compteur à votre insu.
"""
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, jobs, journal, komoot, mapgen, mapservice, paths, routelib, secretstore, service, updates, uploads

REPO_URL = "https://github.com/keltiss38/v650-sync"

LOG = journal.get("web")
QUIET_PATHS = ("/api/ping", "/api/job", "/api/jobs", "/favicon.png", "/api/closing")  # appelés toutes les secondes: pas dans le journal
PLANS = {}  # zone calculée par /api/map/plan, utilisée par /api/map/build
MAP_CANCEL = threading.Event()
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z$")
# Deux emplacements: « main » pour tout ce qui parle au compteur (une seule session à la fois), « build » pour la fabrication des cartes
# et l'installation des outils (rien à voir avec le compteur: peut tourner pendant qu'on le lit).
JOBS = {"main": jobs.new_job(), "build": jobs.new_job()}
JOB = JOBS["main"]  # le même dictionnaire est réutilisé pour toute la durée du programme
SLOT_OF = {"map-build": "build", "map-tools": "build"}
TITLES = {"connect": "Lecture du compteur", "sync": "Lecture du compteur", "map-build": "Fabrication de la carte", "map-tools": "Installation des outils de carte",
          "map-write": "Écriture de la carte sur le compteur", "komoot-gpx": "Téléchargement des GPX depuis Komoot"}
JOB_LOCK = threading.Lock()
_JOB_SEQ = [0]


def any_job_running():
    return any(j["running"] for j in JOBS.values())


def _start_job(kind, fn):
    job = JOBS[SLOT_OF.get(kind, "main")]
    with JOB_LOCK:
        if job["running"]:
            return False
        _JOB_SEQ[0] += 1
        job.update(id=_JOB_SEQ[0], running=True, kind=kind, title=TITLES.get(kind, kind), log=[], result=None, error=None, progress=[0, 0], info={},
                   steps=[], started=time.time(), ended=0.0, note="")
    prog = jobs.Progress(job)
    jlog = journal.get("tache")
    jlog.info("Tâche « %s » démarrée", kind)

    def say(m):
        job["log"].append(str(m))
        jlog.log(10 if str(m).startswith("  /") else 20, "[%s] %s", kind, m)  # une ligne par fichier lu sur le compteur: niveau détaillé seulement

    def run():
        t0 = time.time()
        try:
            job["result"] = fn(say, prog)
            prog.finish(True)
            jlog.info("Tâche « %s » terminée en %.1f s", kind, time.time() - t0)
        except Exception as e:  # noqa: BLE001 - on remonte le message à la page
            job["error"] = f"{type(e).__name__}: {e}"
            prog.finish(False)
            jlog.error("Tâche « %s » en échec après %.1f s: %s", kind, time.time() - t0, job["error"], exc_info=True)
        finally:
            job["ended"] = time.time()
            job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def _journal_info(dirs):
    p = journal.path()
    return {"path": p, "size": os.path.getsize(p) if os.path.isfile(p) else 0, "reports": os.path.join(dirs.root, "rapports"),
            "debug": bool(os.environ.get("V650SYNC_DEBUG")), "version": __version__, "repo": REPO_URL}


def _refresh_local(dirs):
    """Après une modification des parcours du compteur: met à jour la copie locale (sans jamais faire échouer l'opération déjà réussie)."""
    try:
        service.refresh_local_routes(dirs)
    except Exception:  # noqa: BLE001
        pass


def make_handler(dirs, token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "v650sync"

        def log_message(self, *a):  # silencieux: le journal est alimenté par log_request
            pass

        def handle_one_request(self):
            self._t0 = time.time()
            super().handle_one_request()

        def log_request(self, code="-", size="-"):  # une ligne par appel: méthode, adresse (sans paramètres), résultat, durée
            p = self.path.split("?")[0]
            if p in QUIET_PATHS and str(code) == "200":
                return
            lvl = journal.logging.WARNING if str(code)[:1] in "45" else journal.logging.INFO
            LOG.log(lvl, "%s %s -> %s (%.0f ms)", self.command, p, code, (time.time() - getattr(self, "_t0", time.time())) * 1000)

        # --- utilitaires ---
        def _ok_host(self):
            return self.headers.get("Host", "") in allowed_hosts

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            if n > 50 * 1024 * 1024:
                raise ValueError("fichier trop gros")
            return self.rfile.read(n)

        # --- GET ---
        def do_GET(self):
            if not self._ok_host():
                return self._json({"error": "hôte refusé"}, 403)
            path = self.path.split("?")[0]
            if path == "/":
                theme = paths.load_settings().get("theme", "auto")
                theme = theme if theme in ("auto", "light", "dark") else "auto"
                st = paths.load_settings()
                lang = st.get("lang", "auto")
                lang = lang if lang in ("auto", "fr", "en") else "auto"
                units = st.get("units", "auto")
                units = units if units in ("auto", "metric", "imperial") else "auto"
                dev_metric = {True: "1", False: "0"}.get(st.get("device_metric"), "")  # dernière préférence d'unités lue sur le compteur
                html = PAGE.replace("__TOKEN__", token).replace("__THEME__", theme).replace(
                    "__THEME_ATTR__", "" if theme == "auto" else f' data-theme="{theme}"').replace("__LANG__", lang).replace(
                    "__UNITS__", units).replace("__DEVMETRIC__", dev_metric).replace("__I18N__", I18N_JSON).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
            elif path == "/api/status":
                self._json(service.status())
            elif path == "/api/sessions":
                self._json(service.list_sessions(dirs))
            elif path == "/api/routes":
                try:
                    self._json({"routes": service.list_routes()})
                except Exception as e:  # noqa: BLE001
                    LOG.error("Erreur dans %s: %s", path, e, exc_info=True)
                    self._json({"error": str(e)}, 500)
            elif path == "/api/ping":
                LAST_PING["t"] = time.time()  # (pas d'« import time » local ici: il rendrait « time » local à toute la fonction et casserait /api/jobs)
                self._json({"ok": True})
            elif path == "/favicon.png":
                try:
                    with open(os.path.join(os.path.dirname(__file__), "icon.png"), "rb") as f:
                        data = f.read()
                except OSError:
                    return self._json({"error": "introuvable"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/komoot/tours":
                kind = "recorded" if "kind=recorded" in self.path else "planned"
                try:
                    self._json(service.komoot_tours(dirs, kind))
                except komoot.KomootError as e:
                    self._json({"error": str(e)}, 400)
            elif path == "/api/focus":  # 2e lancement du programme: ramène la fenêtre existante au premier plan
                ok, w = False, WINDOW["w"]
                if w is not None:
                    try:
                        w.restore()
                        w.show()
                        w.on_top = True
                        w.on_top = False
                        ok = True
                    except Exception:  # noqa: BLE001
                        pass
                self._json({"ok": ok})
            elif path == "/api/data-dir":
                self._json({"path": dirs.root, "first_run": paths.is_first_run(dirs.root)})
            elif path == "/api/services":
                self._json(service.config(dirs).public())
            elif path == "/api/profile":
                try:
                    self._json(service.read_profile())
                except Exception as e:  # noqa: BLE001
                    LOG.error("Erreur dans %s: %s", path, e, exc_info=True)
                    self._json({"error": str(e)}, 500)
            elif path == "/api/library":  # parcours du compteur + Komoot + GPX en une liste (copie locale du compteur, sans le contacter)
                try:
                    items = routelib.build(dirs)
                    k = service.config(dirs).public().get("komoot", {})
                    self._json({"items": items, "favorites": sum(1 for i in items if i["on_device"]), "komoot": k,
                                "komoot_fetched": routelib.komoot_cache(dirs)["fetched"]})
                except Exception as e:  # noqa: BLE001
                    LOG.error("Erreur dans %s: %s", path, e, exc_info=True)
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            elif path == "/api/map/status":
                try:
                    self._json({"tools": mapgen.tools_status(mapservice.tools_dir()), **mapservice.list_maps(dirs)})
                except Exception as e:  # noqa: BLE001
                    LOG.error("Erreur dans %s: %s", path, e, exc_info=True)
                    self._json({"error": str(e)}, 500)
            elif path == "/api/journal":
                self._json(_journal_info(dirs))
            elif path == "/api/update":  # état enregistré (sans réseau)
                self._json(updates.state())
            elif path == "/api/job":
                self._json(JOB)
            elif path == "/api/jobs":  # toutes les tâches (page entière: panneau des tâches, reprise après changement d'onglet)
                self._json({"main": JOBS["main"], "build": JOBS["build"], "now": time.time()})
            elif path.startswith("/api/session/"):
                sid = path[len("/api/session/"):]
                if not ID_RE.match(sid):
                    return self._json({"error": "identifiant invalide"}, 400)
                try:
                    self._json(service.session_detail(dirs, sid))
                except KeyError:
                    self._json({"error": "séance introuvable"}, 404)
            elif path.startswith("/download/") and path.rsplit(".", 1)[-1] in ("gpx", "fit"):
                sid, fmt = path[len("/download/"):].rsplit(".", 1)
                if not ID_RE.match(sid):
                    return self._json({"error": "introuvable"}, 404)
                try:
                    name, data = service.session_file(dirs, sid, fmt)
                except KeyError:
                    return self._json({"error": "introuvable"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "application/gpx+xml" if fmt == "gpx" else "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "introuvable"}, 404)

        # --- cartes hors ligne (voir mapservice.py): rien n'est écrit sur le compteur sans /api/map/write ---
        def _map_routes(self, body):
            """Tracés demandés par la page: parcours du compteur (copie locale) et/ou GPX du dossier des parcours."""
            routes = {}
            src = body.get("sources") or {}
            if src.get("device_routes"):
                routes.update(mapservice.device_routes_points(dirs))
            for g in src.get("gpx") or []:
                routes[os.path.basename(str(g))] = mapservice.points_of(dirs, g)
            for i in src.get("items") or []:  # éléments de la bibliothèque (clé = identifiant)
                pts = routelib.points_of_item(dirs, str(i))
                if pts:
                    routes[str(i)] = pts
            return routes

        def _map_api(self, path, body):
            if path == "/api/map/plan":
                routes = self._map_routes(body)
                home = body.get("home")
                plan = mapservice.make_plan(dirs, list(routes.values()), float(body.get("margin_km") or 15),
                                            (float(home["lat"]), float(home["lon"])) if home else None, float(home["half_km"]) if home else 0,
                                            body.get("shapes") or [], str(body.get("profile") or "velo"), str(body.get("contours") or "none"))
                pid = secrets.token_hex(6)
                PLANS.clear()  # un seul plan à la fois
                PLANS[pid] = plan
                return {"plan_id": pid, **{k: v for k, v in plan.items() if k != "rings"},
                        "rings": [[[round(a, 4), round(b, 4)] for a, b in r] for r in plan["rings"]]}
            if path == "/api/map/coverage":
                routes = self._map_routes(body)
                return mapservice.route_coverage(dirs, body.get("map"), routes) or {"routes": {}, "outside": False, "rings": []}
            if path == "/api/map/build":
                plan = PLANS.get(body.get("plan_id"))
                if not plan:
                    return {"error": "plan expiré: recalculez la zone"}
                MAP_CANCEL.clear()
                name = str(body.get("name") or "Ma carte")[:60]
                refresh, eta = bool(body.get("refresh")), plan.get("minutes", 0)

                def run(log, progress):
                    JOBS["build"]["info"]["eta_min"] = eta  # durée annoncée à la page (estimation au départ)
                    return mapservice.build(dirs, plan, name, log, progress, MAP_CANCEL, refresh)

                return {"started": _start_job("map-build", run)}
            if path == "/api/map/cancel":
                MAP_CANCEL.set()
                return {"ok": True}
            if path == "/api/map/install-tools":
                return {"started": _start_job("map-tools", lambda log, progress: mapgen.install_tools(mapservice.tools_dir(), log, progress))}
            if path == "/api/map/write":
                mid = str(body.get("map_id") or "")
                return {"started": _start_job("map-write", lambda log, progress: mapservice.write_map(dirs, mid, log, progress))}
            if path == "/api/map/delete":
                mapservice.delete_map(dirs, str(body.get("map_id") or ""))
                return {"ok": True}
            return {"error": "introuvable"}

        # --- POST ---
        def do_POST(self):
            if self.path == "/api/closing" and self._ok_host():  # sendBeacon n'envoie pas d'en-tête: le jeton est dans le corps
                try:
                    if json.loads(self._body() or b"{}").get("token") == token:
                        CLOSING["t"] = time.time()
                except ValueError:
                    pass
                self.send_response(204)
                self.end_headers()
                return
            if not self._ok_host() or self.headers.get("X-Token") != token:
                return self._json({"error": "jeton ou hôte invalide"}, 403)
            path = self.path.split("?")[0]
            try:
                if path == "/api/connect":  # branchement: état + heure + profil + lecture, en une seule session
                    started = _start_job("connect", lambda log, progress: service.connect_sync(
                        dirs, log, progress, lambda k, v: JOB["info"].__setitem__(k, v)))
                    return self._json({"started": started})
                if path == "/api/sync":
                    started = _start_job("sync", lambda log, progress: service.sync(dirs, log, progress))
                    return self._json({"started": started})
                if path == "/api/journal/open":  # dossier du journal (chemin fixe)
                    os.makedirs(paths.settings_dir(), exist_ok=True)
                    os.startfile(paths.settings_dir())
                    return self._json({"ok": True})
                if path == "/api/journal/report":  # rapport de bug: zip local, rien n'est envoyé nulle part
                    dest = journal.make_report(dirs)
                    try:
                        import subprocess

                        subprocess.Popen(["explorer", "/select,", dest])
                    except OSError:
                        pass
                    return self._json({"path": dest, "size": os.path.getsize(dest)})
                if path == "/api/open-folder":  # ouvre le dossier de données dans l'Explorateur (chemin fixe, rien venant de la page)
                    os.makedirs(dirs.root, exist_ok=True)
                    os.startfile(dirs.root)
                    return self._json({"ok": True})
                if path == "/api/komoot/login":
                    b = json.loads(self._body() or b"{}")
                    try:
                        return self._json(service.komoot_login(dirs, b.get("email"), b.get("password")))
                    except komoot.KomootError as e:
                        return self._json({"error": str(e)}, 400)
                if path == "/api/komoot/logout":
                    return self._json(service.komoot_logout(dirs))
                if path == "/api/komoot/fetch":
                    b = json.loads(self._body() or b"{}")
                    try:
                        return self._json(service.komoot_fetch(dirs, int(b["id"]), str(b.get("name") or ""), str(b.get("date") or "")))
                    except (komoot.KomootError, ValueError, KeyError) as e:
                        return self._json({"error": str(e) if isinstance(e, komoot.KomootError) else "tour invalide"}, 400)
                if path == "/api/update/check":  # auto (au plus une fois par jour, si activé) ou manuel (force)
                    force = bool(json.loads(self._body() or b"{}").get("force"))
                    return self._json(updates.check(force))
                if path == "/api/update/enable":
                    return self._json(updates.set_enabled(json.loads(self._body() or b"{}").get("enabled") is not False))
                if path == "/api/open-external":  # adresses fixes uniquement (rien ne vient de la page)
                    urls = {"strava_upload": "https://www.strava.com/upload/select", "intervals_settings": "https://intervals.icu/settings",
                            "komoot_site": "https://www.komoot.com", "repo": REPO_URL, "licence": REPO_URL + "/blob/main/LICENSE",
                            "update": updates.state()["url"]}
                    target = urls.get(json.loads(self._body() or b"{}").get("target"))
                    if not target:
                        return self._json({"error": "cible inconnue"}, 400)
                    webbrowser.open(target)
                    return self._json({"ok": True})
                if path == "/api/units":  # unités d'affichage: auto (suit le compteur), metric ou imperial
                    units = json.loads(self._body() or b"{}").get("units")
                    if units not in ("auto", "metric", "imperial"):
                        return self._json({"error": "unités inconnues"}, 400)
                    settings = paths.load_settings()
                    settings["units"] = units
                    paths.save_settings(settings)
                    return self._json({"ok": True})
                if path == "/api/units/seen":  # préférence d'unités du compteur, lue dans son profil: mémorisée pour le prochain lancement
                    metric = json.loads(self._body() or b"{}").get("metric")
                    if not isinstance(metric, bool):
                        return self._json({"error": "valeur invalide"}, 400)
                    settings = paths.load_settings()
                    if settings.get("device_metric") != metric:
                        settings["device_metric"] = metric
                        paths.save_settings(settings)
                    return self._json({"ok": True})
                if path == "/api/lang":  # langue de l'interface: auto (suit Windows), fr ou en
                    lang = json.loads(self._body() or b"{}").get("lang")
                    if lang not in ("auto", "fr", "en"):
                        return self._json({"error": "langue inconnue"}, 400)
                    settings = paths.load_settings()
                    settings["lang"] = lang
                    paths.save_settings(settings)
                    return self._json({"ok": True})
                if path == "/api/theme":
                    theme = json.loads(self._body() or b"{}").get("theme")
                    if theme not in ("auto", "light", "dark"):
                        return self._json({"error": "thème inconnu"}, 400)
                    settings = paths.load_settings()
                    settings["theme"] = theme
                    paths.save_settings(settings)
                    return self._json({"ok": True})
                if path == "/api/pick-folder":
                    return self._json({"path": paths.pick_folder(json.loads(self._body() or b"{}").get("start", dirs.root))})
                if path == "/api/data-dir":
                    if any_job_running():
                        return self._json({"error": "Une opération est en cours: réessayez dans un instant."}, 409)
                    new = paths.set_data_dir(json.loads(self._body() or b"{}").get("path", ""))
                    dirs.__init__(new)
                    return self._json({"ok": True, "path": new})
                if path == "/api/preview":
                    ct = self.headers.get("Content-Type", "")
                    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ct.encode() + b"\r\n\r\n" + self._body())
                    for part in msg.iter_parts():
                        fname = os.path.basename(part.get_filename() or "")
                        if fname.lower().endswith(".gpx"):
                            os.makedirs(dirs.routes_in, exist_ok=True)
                            dest = os.path.join(dirs.routes_in, fname)
                            with open(dest, "wb") as f:
                                f.write(part.get_payload(decode=True))
                            out = service.preview_route(dest)
                            out["file"] = fname
                            return self._json(out)
                    return self._json({"error": "aucun fichier GPX reçu"}, 400)
                body = json.loads(self._body() or b"{}")
                if path == "/api/quit":
                    self._json({"ok": True})
                    request_stop()
                    return
                if path == "/api/zip":
                    ids = [i for i in body.get("ids", []) if ID_RE.match(i)]
                    fmt = "fit" if body.get("format") == "fit" else "gpx"
                    data = service.zip_sessions(dirs, ids, fmt)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", f'attachment; filename="seances_{fmt}.zip"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/put-route":
                    fname = os.path.basename(body.get("file", ""))
                    gpx = os.path.join(dirs.routes_in, fname)
                    if not fname.lower().endswith(".gpx") or not os.path.isfile(gpx):
                        return self._json({"error": "GPX introuvable"}, 400)
                    res = service.put_route(gpx, name=body.get("name") or None, hd=bool(body.get("hd")))
                    _refresh_local(dirs)
                    return self._json(res)
                if path == "/api/intervals/config":
                    cfg = service.config(dirs)
                    previous = dict(cfg.data.get("intervals") or {})
                    cfg.data["intervals"].update(athlete_id=str(body.get("athlete_id") or "0").strip(), api_key=secretstore.protect(str(body["api_key"]).strip()))
                    cfg.save()
                    try:
                        name, note = uploads.intervals_check_any(cfg)
                        cfg.save()  # l'identifiant d'athlète a pu être ramené à 0
                        return self._json({"ok": True, "name": name, "note": note, "athlete_id": cfg.data["intervals"].get("athlete_id", "0")})
                    except uploads.UploadError as e:
                        cfg.data["intervals"] = previous  # la clé qui marchait déjà n'est pas perdue parce qu'un nouvel essai échoue
                        cfg.save()
                        return self._json({"error": str(e)}, 400)
                if path == "/api/clientlog":  # erreurs JavaScript de la page: dans le journal (rapports de bug)
                    b = json.loads(self._body() or b"{}")
                    LOG.error("Erreur dans la page: %s (%s:%s)", str(b.get("message", ""))[:300], str(b.get("source", ""))[-60:], b.get("line"))
                    return self._json({"ok": True})
                if path == "/api/auto":  # envoi automatique des nouvelles séances (désactivé par défaut)
                    a = service.set_auto(dirs, body.get("intervals"), body.get("gpx"), body.get("komoot"))
                    return self._json({"intervals": a["intervals"], "gpx": a["gpx"], "komoot": a["komoot"]})
                if path == "/api/disconnect":
                    cfg = service.config(dirs)
                    cfg.data["intervals"] = {}
                    cfg.save()
                    return self._json({"ok": True})
                if path == "/api/upload":
                    try:
                        return self._json(service.upload_session(dirs, body["session"], body["target"]))
                    except uploads.UploadError as e:
                        return self._json({"error": str(e)}, 400)
                if path == "/api/set-time":
                    return self._json(service.set_clock())
                if path == "/api/remove-route":
                    res = service.remove_route(int(body["slot"]))
                    _refresh_local(dirs)
                    return self._json(res)
                if path.startswith("/api/map/"):
                    return self._json(self._map_api(path, body))
                if path == "/api/library/komoot-refresh":
                    try:
                        return self._json(service.komoot_refresh(dirs, "recorded" if body.get("kind") == "recorded" else "planned"))
                    except komoot.KomootError as e:
                        return self._json({"error": str(e)}, 400)
                if path == "/api/library/komoot-gpx":
                    ids = [str(i) for i in body.get("ids", [])][:60]
                    return self._json({"started": _start_job("komoot-gpx", lambda log, progress: service.komoot_download(dirs, ids, log, progress))})
                if path == "/api/update-route":
                    fname = os.path.basename(body.get("file", ""))
                    gpx = os.path.join(dirs.routes_in, fname)
                    if not fname.lower().endswith(".gpx") or not os.path.isfile(gpx):
                        return self._json({"error": "GPX introuvable"}, 400)
                    res = service.update_route(dirs, int(body["slot"]), gpx, hd=bool(body.get("hd")))
                    _refresh_local(dirs)
                    return self._json(res)
                if path == "/api/library/detail":
                    return self._json(routelib.detail(dirs, str(body.get("id") or "")))
            except Exception as e:  # noqa: BLE001
                LOG.error("Erreur dans %s: %s", path, e, exc_info=True)
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"error": "introuvable"}, 404)

    return Handler


IDLE_EXIT_S = 180  # onglet de navigateur: sans signe de vie de la page pendant ce délai, on s'arrête (un onglet caché ralentit ses minuteurs à 1 par minute)
LAST_PING = {"t": 0.0}
CLOSING = {"t": 0.0}  # la page a signalé sa fermeture (pagehide); une page rechargée redonne signe de vie dans la seconde qui suit
CLOSE_GRACE_S = 5
SERVER = {"srv": None}
WINDOW = {"w": None, "done": False, "stopping": False}  # fenêtre native (pywebview), si elle est utilisée


def request_stop():
    """Arrêt gracieux depuis n'importe quel thread (bouton Quitter, croix de la fenêtre, signal, onglet fermé).

    Ordre: la lecture en cours s'arrête entre deux fichiers, la session du compteur est refermée, le serveur s'arrête,
    et seulement ensuite la fenêtre se ferme (pendant ce temps la page affiche « Fermeture en cours… »).
    """
    if WINDOW["stopping"]:
        return
    WINDOW["stopping"] = True
    LOG.info("Arrêt demandé")
    MAP_CANCEL.set()  # une fabrication de carte en cours: Osmosis (Java) est arrêté avec le programme, pas laissé en tâche de fond

    def run():
        print("Arrêt demandé: fin de la lecture et fermeture de la session du compteur...", flush=True)
        service.shutdown()
        print("Session du compteur refermée.", flush=True)
        srv = SERVER["srv"]
        if srv is not None:
            srv.shutdown()
        print("Serveur arrêté.", flush=True)
        w = WINDOW["w"]
        if w is not None:
            WINDOW["done"] = True
            try:
                w.destroy()
            except Exception as e:  # noqa: BLE001 - fenêtre déjà fermée
                print(f"Fermeture de la fenêtre: {type(e).__name__}: {e}", flush=True)
            print("Fenêtre fermée.", flush=True)
        LOG.info("Programme arrêté proprement")

    threading.Thread(target=run, daemon=True).start()


def _run_native(url):
    """Ouvre la page dans une vraie fenêtre (pywebview + WebView2). Bloque jusqu'à sa fermeture. Faux si indisponible."""
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        print(f"Fenêtre native indisponible ({type(e).__name__}): {e}")
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True  # export FIT/GPX/ZIP: boîte « Enregistrer sous »
    except Exception:  # noqa: BLE001
        pass
    icon = os.path.join(os.path.dirname(__file__), "icon.ico")
    try:
        window = webview.create_window("V650 Sync", url, width=1280, height=860, min_size=(940, 620), background_color="#0b0f14")
    except Exception as e:  # noqa: BLE001
        print(f"Fenêtre native impossible: {e}")
        return False
    WINDOW["w"] = window

    def on_closing():
        """Croix de la fenêtre: on affiche « Fermeture en cours… » et on arrête proprement; la fenêtre se ferme à la fin."""
        if WINDOW["done"]:
            return True

        def message():  # hors du fil de l'interface: evaluate_js y attendrait indéfiniment sa propre réponse
            try:
                window.evaluate_js("showClosing()")
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=message, daemon=True).start()
        request_stop()
        return False

    window.events.closing += on_closing
    try:
        webview.start(gui="edgechromium", icon=icon if os.path.isfile(icon) else None)
    except Exception as e:  # noqa: BLE001 - pas de WebView2, etc.: on retombe sur la fenêtre Edge/Chrome
        print(f"Fenêtre native non démarrée ({type(e).__name__}): {e}")
        WINDOW["w"] = None
        return False
    return True


def _install_stop_handlers():
    import signal

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *a: request_stop())
            except (ValueError, OSError):
                pass
    if os.name == "nt":  # fermeture de la fenêtre console, déconnexion, arrêt de Windows
        import ctypes

        handler_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def on_event(event):
            request_stop()
            service.shutdown(wait_s=4)  # Windows ne laisse que quelques secondes: on ferme la session du compteur ici
            return 1

        _install_stop_handlers.keep = handler_t(on_event)  # garde une référence (sinon le gestionnaire est détruit)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_install_stop_handlers.keep, 1)


def _first_instance():
    """Vrai si aucun autre V650 Sync ne tourne déjà (un seul programme à la fois parle au compteur)."""
    if os.name != "nt":
        return True
    import ctypes

    _first_instance.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\V650Sync")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _running_file():
    from .paths import settings_dir

    return os.path.join(settings_dir(), "running.json")


def serve(data_root, open_browser=True, port=0, keep_alive=False):
    import time

    from . import window

    # Lancé deux fois: on remet simplement la fenêtre du programme déjà ouvert au premier plan (port imposé = tests, pas de contrôle)
    if open_browser and not port and not _first_instance():
        try:
            with open(_running_file(), encoding="utf-8") as f:
                url = json.load(f)["url"]
            import urllib.request

            try:
                if json.loads(urllib.request.urlopen(url + "api/focus", timeout=3).read()).get("ok"):
                    return  # la fenêtre native existante est revenue au premier plan
            except (OSError, ValueError):
                pass
            if window.open_app_window(url) is None:
                webbrowser.open(url)
        except (OSError, ValueError, KeyError):
            pass
        return

    dirs = service.DataDirs(data_root)
    journal.setup(dirs.root)
    LOG.info("Démarrage du serveur local (port demandé %s, keep_alive=%s) · dossier de données: %s", port or "auto", keep_alive, dirs.root)
    if not paths.is_first_run(dirs.root):  # au premier lancement, rien n'est créé avant que l'utilisateur ait choisi le dossier
        os.makedirs(dirs.root, exist_ok=True)
    token = secrets.token_urlsafe(24)
    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):  # fenêtre fermée en pleine requête: sans importance, pas de trace dans le journal
            pass

    srv = QuietServer(("127.0.0.1", port), lambda *a: None)
    SERVER["srv"] = srv
    real_port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(dirs, token, real_port)
    url = f"http://127.0.0.1:{real_port}/"
    print(f"Page web: {url}\nDossier de données: {dirs.root}\nFermez la fenêtre, cliquez sur « Quitter » ou faites Ctrl+C pour arrêter.")
    _install_stop_handlers()
    if open_browser and not port:
        try:
            os.makedirs(os.path.dirname(_running_file()), exist_ok=True)
            with open(_running_file(), "w", encoding="utf-8") as f:
                json.dump({"url": url}, f)
        except OSError:
            pass

    win = {"proc": None, "managed": False}

    def watchdog():  # fenêtre non surveillée (ou onglet de navigateur): la page prévient à sa fermeture, ou cesse de donner signe de vie
        while True:
            time.sleep(1)
            if keep_alive or win["managed"]:
                continue
            idle = time.time() - LAST_PING["t"]
            closing = CLOSING["t"] and LAST_PING["t"] < CLOSING["t"] and time.time() - CLOSING["t"] > CLOSE_GRACE_S
            if closing or (LAST_PING["t"] and idle > IDLE_EXIT_S and not any_job_running()):
                print("Page fermée: arrêt du programme.")
                request_stop()
                return

    def watch_window(proc):
        t0 = time.time()
        proc.wait()
        print(f"Le navigateur de la fenêtre s'est terminé après {time.time() - t0:.1f} s (code {proc.returncode}).")
        if time.time() - t0 < 10:
            # Le lanceur a pu passer la main à un autre processus (la fenêtre existe alors quand même). On ne sait si la fenêtre est là
            # que si la page se manifeste: on attend son premier signe de vie avant de conclure (jamais de 2e fenêtre « au cas où »).
            win["managed"] = False
            for _ in range(40):
                if LAST_PING["t"] >= t0:
                    print("La fenêtre est ouverte (page active); sa fermeture sera détectée par la page.")
                    return
                time.sleep(0.5)
            print("Aucune fenêtre n'a répondu: ouverture dans le navigateur par défaut.")
            webbrowser.open(url)
            return
        if not keep_alive:
            print("Fenêtre fermée: arrêt du programme.")
            request_stop()

    threading.Thread(target=watchdog, daemon=True).start()
    server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    server_thread.start()
    if open_browser:
        win["managed"] = True  # le temps d'essayer la fenêtre native
        if _run_native(url):  # bloque jusqu'à la fermeture de la fenêtre
            request_stop()
        else:
            win["managed"] = False
            proc = window.open_app_window(url)
            if proc is not None:
                win.update(proc=proc, managed=True)
                threading.Thread(target=watch_window, args=(proc,), daemon=True).start()
            else:
                threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        request_stop()
    finally:
        service.shutdown()  # termine la lecture en cours et referme proprement la session du compteur
        srv.server_close()
        if win["proc"] is not None and win["proc"].poll() is None:
            win["proc"].terminate()  # bouton « Quitter »: la fenêtre se ferme avec le programme
        if open_browser and not port:
            try:
                os.remove(_running_file())
            except OSError:
                pass


PAGE = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()
try:  # dictionnaire français -> anglais de l'interface (voir le traducteur au début de index.html)
    with open(os.path.join(os.path.dirname(__file__), "i18n_en.json"), encoding="utf-8") as _f:
        I18N_JSON = json.dumps(json.load(_f), ensure_ascii=False).replace("</", "<\\/")
except (OSError, ValueError):
    I18N_JSON = '{"ex":{},"pat":[]}'
