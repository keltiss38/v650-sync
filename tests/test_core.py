"""Tests sans compteur ni internet: format MAPINFO, géométrie des zones, contrôle d'une carte, écriture en plusieurs trames,
et écriture sécurisée d'une carte (avec un faux compteur en mémoire).  Lancer:  python -m unittest discover -s tests -v"""
import hashlib
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from v650sync import mapgen, mapinfo, mapservice, pftp, service, uploads  # noqa: E402

H = lambda s: bytes.fromhex(s.replace(" ", ""))
# états de MAPINFO.BPB au format relevé sur le compteur (carte installée, zone demandée), avec des coordonnées fictives
MAPINFO_AVANT = H("0a 12 09 66 66 66 66 66 e6 46 40 11 cd cc cc cc cc cc 18 40 12 16 0a 07 08 ea 0f 10 07 18 0c 12 09 08 0a 10 28 18 24 20 d8 04 18 01 18 01")
MAPINFO_DEMANDE = H("0a 12 09 cd cc cc cc cc ec 46 40 11 9a 99 99 99 99 99 18 40 12 16 0a 07 08 ea 0f 10 07 18 0c 12 09 08 0a 10 28 18 24 20 d8 04 18 01 18 00")


def fake_map(payload=b"x" * 40000, bbox=(45.0, 5.0, 46.0, 6.0)):
    """Fichier Mapsforge v3 minimal mais valide (en-tête cohérent, un seul sous-fichier)."""
    proj = b"Mercator"
    body = struct.pack(">I", 3) + b"\0" * 8 + struct.pack(">q", 0) + struct.pack(">iiii", *[int(v * 1e6) for v in bbox]) + struct.pack(">H", 256)
    body += bytes([len(proj)]) + proj + b"\x00" + struct.pack(">HH", 0, 0) + b"\x01"
    hs = len(body) + 19
    total = 24 + hs + len(payload)
    body = struct.pack(">I", 3) + struct.pack(">q", total) + struct.pack(">q", 0) + struct.pack(">iiii", *[int(v * 1e6) for v in bbox]) + struct.pack(">H", 256)
    body += bytes([len(proj)]) + proj + b"\x00" + struct.pack(">HH", 0, 0) + b"\x01" + bytes([14, 10, 21]) + struct.pack(">qq", 24 + hs, len(payload))
    return b"mapsforge binary OSM" + struct.pack(">I", len(body)) + body + payload


class MapInfoTests(unittest.TestCase):
    def test_roundtrip_octet_pour_octet(self):
        for raw in (MAPINFO_AVANT, MAPINFO_DEMANDE):
            i = mapinfo.decode(raw)
            self.assertEqual(mapinfo.encode(i["lat"], i["lon"], i["installed"], i["pending"]), raw)

    def test_drapeau(self):
        self.assertFalse(mapinfo.decode(MAPINFO_AVANT)["pending"])
        self.assertTrue(mapinfo.decode(MAPINFO_DEMANDE)["pending"])

    def test_grille_de_polar(self):  # emprises relevées dans l'en-tête des cartes de Polar (points fictifs dans les mêmes cellules)
        self.assertEqual(mapinfo.polar_pack_bbox(46.5, 5.0), (44.55, 2.04, 48.60, 8.16))
        self.assertEqual(mapinfo.polar_pack_bbox(45.2, 1.0), (43.20, -1.99, 47.25, 3.98))
        self.assertEqual(mapinfo.polar_pack_bbox(45.3, 5.0), (43.20, 1.99, 47.25, 7.96))


class GeometryTests(unittest.TestCase):
    ROUTE = [(45.0 + i * 0.01, 5.0 + i * 0.01) for i in range(60)]

    def test_bande_couvre_le_trace(self):
        rings = mapgen.corridor_rings(self.ROUTE, 10)
        c = mapgen.coverage(self.ROUTE, rings)
        self.assertEqual(c["km_outside"], 0)
        self.assertGreater(mapgen.area_km2(rings), 1500)  # tracé d'environ 80 km, bande de 20 km de large

    def test_trace_hors_zone(self):
        rings = mapgen.corridor_rings(self.ROUTE, 10)
        ailleurs = [(46.5, 8.0), (46.6, 8.1)]
        self.assertGreater(mapgen.coverage(ailleurs, rings)["km_outside"], 5)

    def test_carre(self):
        r = [mapgen.rect_ring(45.0, 5.0, 25)]
        self.assertTrue(mapgen.inside(r, 45.0, 5.0))
        self.assertFalse(mapgen.inside(r, 45.5, 5.0))
        self.assertAlmostEqual(mapgen.area_km2(r), 2500, delta=300)

    def test_simplify_garde_les_extremites(self):
        s = mapgen.simplify(self.ROUTE, 1.0)
        self.assertEqual((s[0], s[-1]), (self.ROUTE[0], self.ROUTE[-1]))
        self.assertLess(len(s), len(self.ROUTE))


class InspectMapTests(unittest.TestCase):
    def test_carte_valide_et_tronquee(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.map")
            open(p, "wb").write(fake_map())
            self.assertEqual(mapgen.inspect_map(p)["version"], 3)
            open(p, "wb").write(fake_map()[:-10])  # fichier incomplet
            with self.assertRaises(mapgen.MapgenError):
                mapgen.inspect_map(p)
            open(p, "wb").write(b"autre chose")
            with self.assertRaises(mapgen.MapgenError):
                mapgen.inspect_map(p)


# ------------------------------------------------------------------------------------------------ écriture PFTP
TOKEN = b"\x00\x02\x00\x00"


def _recvn(c, n):
    b = b""
    while len(b) < n:
        x = c.recv(n - len(b))
        if not x:
            raise EOFError
        b += x
    return b


def _fake_device(srv, store):
    c, _ = srv.accept()
    srv.close()
    c.sendall(b"Polar FTP server version 2.0.0 at TEST.\n")
    try:
        while True:
            L, fl = divmod(struct.unpack("<H", _recvn(c, 2))[0], 4)[0], 0
            body = _recvn(c, L)
            n = struct.unpack_from("<H", body)[0]
            op = body[2:2 + n]
            path = op[4:].decode()
            data = bytearray(body[2 + n:])
            if L == pftp.MAX_FRAME:
                c.sendall(TOKEN)
                while True:
                    h = struct.unpack("<H", _recvn(c, 2))[0]
                    assert h & 3 == 1, "trame de suite attendue avec le drapeau 1"
                    data += _recvn(c, h >> 2)
                    if h >> 2 < pftp.MAX_FRAME:
                        break
                    c.sendall(TOKEN)
            store[path] = bytes(data)
            c.sendall(b"\x00\x00\x00\x00")
    except EOFError:
        pass
    finally:
        c.close()


class MultiFrameWriteTests(unittest.TestCase):
    def _client(self, store):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        threading.Thread(target=_fake_device, args=(srv, store), daemon=True).start()
        return pftp.PftpClient.from_socket(socket.create_connection(srv.getsockname()), timeout=5)

    def test_petite_et_grosse_ecriture(self):
        store = {}
        c = self._client(store)
        big = os.urandom(100_000)
        seen = []
        c.put("/U/0/MAP/0/MAPDATA.MAP", big, progress=lambda a, b: seen.append(a))
        c.put("/U/0/PETIT.BIN", b"abc")  # le flux doit rester synchronisé après une écriture en plusieurs trames
        c.close()
        self.assertEqual(store["/U/0/MAP/0/MAPDATA.MAP"], big)
        self.assertEqual(store["/U/0/PETIT.BIN"], b"abc")
        self.assertEqual(seen[-1], len(big))

    def test_multiple_exact_refuse(self):
        c = self._client({})
        op = len(b"\x08\x01\x12" + bytes([len("/U/0/X.BIN")]) + b"/U/0/X.BIN")
        with self.assertRaises(pftp.PftpError):
            c.put("/U/0/X.BIN", bytes(16383 * 3 - 2 - op))
        c.close()


# ------------------------------------------------------------------------- écriture sécurisée avec un faux compteur
class FakeClient:
    """Compteur en mémoire: la carte n'est acceptée que si le drapeau de MAPINFO est à 0 (comportement observé)."""

    def __init__(self, files):
        self.files = dict(files)
        self.log = []

    def list_dir(self, path):
        return [(k, len(v)) for k, v in self.files.items() if k.startswith(path) and "/" not in k[len(path):]]

    def get_file(self, path, progress=None):
        if progress:
            progress(len(self.files[path]))
        return self.files[path]

    def put(self, path, data=b"", progress=None):
        self.log.append(path)
        if path == mapservice.MAP and not mapinfo.decode(self.files[mapinfo.PATH])["pending"]:
            raise pftp.PftpError("refusé: code 106")
        self.files[path] = bytes(data)
        if progress:
            progress(len(data), len(data))


class SafeWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dirs = service.DataDirs(self.tmp.name)
        self.old_dev, self.old_status = service.device, service._status_from
        service._status_from = lambda dev, c: {"battery": 90, "free_gb": 2.0}
        installed = mapinfo.encode(45.0, 5.0, datetime(2026, 1, 1, tzinfo=timezone.utc), pending=False)
        self.client = FakeClient({mapinfo.PATH: installed, mapservice.MAP: b"ANCIENNE CARTE" * 100})

        @contextmanager
        def fake_device():
            yield None, self.client
        service.device = fake_device
        blob = fake_map()
        d = os.path.join(mapservice.maps_dir(self.dirs), "essai")
        os.makedirs(d)
        with open(os.path.join(d, "MAPDATA.MAP"), "wb") as f:
            f.write(blob)
        self.blob = blob
        mapservice.save(self.dirs, {"maps": [{"id": "essai", "name": "Essai", "path": os.path.join(d, "MAPDATA.MAP"), "size": len(blob),
                                              "sha256": hashlib.sha256(blob).hexdigest(), "bbox": [45.0, 5.0, 46.0, 6.0], "rings": [[[45, 5], [45, 6], [46, 6], [46, 5]]],
                                              "center": [45.5, 5.5], "kind": "fabriquee"}], "installed": None, "pending_write": None})

    def tearDown(self):
        service.device, service._status_from = self.old_dev, self.old_status
        self.tmp.cleanup()

    def test_ecriture_complete(self):
        r = mapservice.write_map(self.dirs, "essai", lambda m: None, lambda a, b: None)
        self.assertTrue(r["verified"])
        self.assertEqual(self.client.files[mapservice.MAP], self.blob)
        info = mapinfo.decode(self.client.files[mapinfo.PATH])
        self.assertFalse(info["pending"])  # drapeau remis à 1 à la fin
        self.assertAlmostEqual(info["lat"], 45.5)
        data = mapservice.load(self.dirs)
        self.assertIsNone(data["pending_write"])
        self.assertEqual(data["installed"]["id"], "essai")
        # la carte précédente du compteur a été copiée sur le PC AVANT d'être remplacée
        sauv = [m for m in data["maps"] if m["kind"] == "sauvegarde"]
        self.assertEqual(len(sauv), 1)
        with open(sauv[0]["path"], "rb") as f:
            self.assertEqual(f.read(), b"ANCIENNE CARTE" * 100)
        self.assertEqual(self.client.log[0], mapinfo.PATH)  # le drapeau est levé avant la carte

    def test_fichier_modifie_refuse(self):
        data = mapservice.load(self.dirs)
        with open(data["maps"][0]["path"], "ab") as f:
            f.write(b"x")
        with self.assertRaises(ValueError):
            mapservice.write_map(self.dirs, "essai", lambda m: None, lambda a, b: None)
        self.assertEqual(self.client.files[mapservice.MAP], b"ANCIENNE CARTE" * 100)  # rien n'a été écrit

    def test_batterie_faible(self):
        service._status_from = lambda dev, c: {"battery": 10, "free_gb": 2.0}
        with self.assertRaises(ValueError):
            mapservice.write_map(self.dirs, "essai", lambda m: None, lambda a, b: None)
        self.assertEqual(self.client.log, [])

    def test_ecriture_interrompue_reconnue(self):
        def boom(path, data=b"", progress=None):
            self.client.log.append(path)
            if path == mapservice.MAP:
                raise pftp.PftpError("connexion fermée")
            self.client.files[path] = bytes(data)
        self.client.put = boom
        with self.assertRaises(pftp.PftpError):
            mapservice.write_map(self.dirs, "essai", lambda m: None, lambda a, b: None)
        rec = mapservice.load(self.dirs)
        self.assertIsNotNone(rec["pending_write"])  # marqueur gardé: le programme saura que l'écriture n'est pas finie
        self.assertTrue(mapservice.interrupted_write(self.dirs, {"pending": True}))
        self.assertFalse(mapservice.interrupted_write(self.dirs, {"pending": False}))


class RouteLibraryTests(unittest.TestCase):
    """Fusion compteur + Komoot + GPX: un parcours = une ligne, statut « synchronisé » décidé sur la trace."""

    def _gpx(self, path, pts):
        body = "".join(f'<trkpt lat="{a:.6f}" lon="{b:.6f}"><ele>{100 + i % 20}</ele></trkpt>' for i, (a, b) in enumerate(pts))
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0"?><gpx><trk><name>x</name><trkseg>{body}</trkseg></trk></gpx>')

    def _put_device_route(self, slot, name, pts, source_id=None):
        from v650sync import route as R
        lat0, lon0 = pts[0]
        ky, kx = R._scales(lat0)
        points = [(round((lo - lon0) * kx), round((la - lat0) * ky), i * 1000, 0) for i, (la, lo) in enumerate(pts)]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        raw = R.encode({"route_id": 1000 + slot, "name": name, "length": 9000.0, "lat": lat0, "lon": lon0, "alt": 100.0, "points": points,
                        "created": now, "modified": now, "source_id": source_id})
        d = os.path.join(self.dirs.backup, "SERIAL", "U", "0", "ROUTES", str(slot))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "PROUTE.BPB"), "wb") as f:
            f.write(raw)

    def setUp(self):
        from v650sync import routelib
        self.routelib = routelib
        self.tmp = tempfile.TemporaryDirectory()
        self.dirs = service.DataDirs(self.tmp.name)
        os.makedirs(self.dirs.routes_in)
        self.line = [(45.0 + i * 0.001, 5.0 + i * 0.0008) for i in range(120)]   # ~13 km

    def tearDown(self):
        self.tmp.cleanup()

    def test_synchronise_puis_modifie_dans_komoot(self):
        self._put_device_route(0, "Ma boucle", self.line, source_id="555")
        self._gpx(os.path.join(self.dirs.routes_in, "2025-01-01_555_Ma boucle - komoot.gpx"), self.line)
        self.routelib.save_komoot_cache(self.dirs, [{"id": 555, "name": "Ma boucle", "km": 13, "date": "2025-01-01", "up": 100}])
        items = self.routelib.build(self.dirs)
        self.assertEqual(len(items), 1)                      # une seule ligne pour le compteur ET Komoot
        self.assertTrue(items[0]["on_device"])
        self.assertEqual(items[0]["status"], "sync")
        # même nom, mais la trace a été modifiée dans Komoot: décalage d'environ 300 m au milieu
        moved = [(a + (0.003 if 40 < i < 80 else 0), b) for i, (a, b) in enumerate(self.line)]
        self._gpx(os.path.join(self.dirs.routes_in, "2025-01-01_555_Ma boucle - komoot.gpx"), moved)
        self.assertEqual(self.routelib.build(self.dirs)[0]["status"], "diff")

    def test_komoot_seul_gpx_seul_et_rapprochement_par_nom(self):
        self._put_device_route(1, "Sans identifiant", self.line)          # pas d'identifiant Komoot dans le fichier du compteur
        self.routelib.save_komoot_cache(self.dirs, [{"id": 7, "name": "Sans identifiant", "km": 13, "date": "", "up": 0},
                                                    {"id": 8, "name": "Seulement Komoot", "km": 20, "date": "", "up": 0}])
        self._gpx(os.path.join(self.dirs.routes_in, "perso.gpx"), self.line[:60])
        items = {i["name"]: i for i in self.routelib.build(self.dirs)}
        self.assertEqual(set(items), {"Sans identifiant", "Seulement Komoot", "x"})
        self.assertTrue(items["Sans identifiant"]["on_device"] and items["Sans identifiant"]["komoot_id"] == "7")
        self.assertFalse(items["Seulement Komoot"]["on_device"])
        self.assertEqual(items["x"]["source"], "gpx")

    def test_non_verifie_sans_gpx(self):
        self._put_device_route(2, "Parcours", self.line, source_id="9")
        self.routelib.save_komoot_cache(self.dirs, [{"id": 9, "name": "Parcours", "km": 13, "date": "", "up": 0}])
        it = self.routelib.build(self.dirs)[0]
        self.assertIsNone(it["status"])                      # GPX Komoot non téléchargé: la trace n'a pas été comparée
        self.assertIsNone(it["file"])


class MakePlanTests(unittest.TestCase):
    """Zone de carte: formes dessinées (surface, trait avec marge), niveaux de détail (sans réseau: l'index Geofabrik est simulé)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dirs = service.DataDirs(self.tmp.name)
        self.old = mapgen.load_index
        mapgen.load_index = lambda *a, **k: []   # aucune région: pas de téléchargement

    def tearDown(self):
        mapgen.load_index = self.old
        self.tmp.cleanup()

    def test_formes(self):
        carre = {"type": "polygon", "pts": [[45.0, 5.0], [45.0, 5.2], [45.2, 5.2], [45.2, 5.0]]}
        trait = {"type": "path", "pts": [[46.0 + i * 0.01, 5.0 + i * 0.01] for i in range(40)]}
        p1 = mapservice.make_plan(self.dirs, [], 10, shapes=[carre])
        p2 = mapservice.make_plan(self.dirs, [], 10, shapes=[trait])
        self.assertAlmostEqual(p1["area_km2"], 22 * 15, delta=120)          # ~ 22 km x 15 km
        self.assertGreater(p2["area_km2"], 600)                              # bande de 20 km autour d'un trait de ~ 60 km
        both = mapservice.make_plan(self.dirs, [], 10, shapes=[carre, trait])
        self.assertGreater(both["area_km2"], p1["area_km2"] + p2["area_km2"] - 200)  # zones disjointes: la surface s'additionne (à la précision de la grille de 2 km)

    def test_niveaux_de_detail(self):
        carre = {"type": "polygon", "pts": [[45.0, 5.0], [45.0, 5.5], [45.5, 5.5], [45.5, 5.0]]}
        velo = mapservice.make_plan(self.dirs, [], 10, shapes=[carre], profile="velo")["estimate_mb"]
        det = mapservice.make_plan(self.dirs, [], 10, shapes=[carre], profile="detaille")["estimate_mb"]
        self.assertGreater(det[1], velo[1] * 1.5)                            # les bâtiments alourdissent nettement la carte
        with self.assertRaises(ValueError):
            mapservice.make_plan(self.dirs, [], 10, shapes=[carre], profile="inconnu")

    def test_rien_a_couvrir(self):
        with self.assertRaises(ValueError):
            mapservice.make_plan(self.dirs, [], 10)

    def test_courbes_de_niveau(self):
        carre = {"type": "polygon", "pts": [[45.0, 5.0], [45.0, 5.5], [45.5, 5.5], [45.5, 5.0]]}
        sans = mapservice.make_plan(self.dirs, [], 10, shapes=[carre])
        self.assertIsNone(sans["dem"])
        avec = mapservice.make_plan(self.dirs, [], 10, shapes=[carre], contours="full")
        self.assertTrue({"N45E005"} <= {t["name"] for t in avec["dem"]["tiles"]})
        self.assertGreater(avec["download_mb"], sans["download_mb"])             # les altitudes s'ajoutent au téléchargement
        self.assertGreater(avec["estimate_mb"][1], sans["estimate_mb"][1])       # courbes: carte un peu plus lourde
        self.assertIn("SRTM", avec["dem"]["credit"])
        with self.assertRaises(ValueError):
            mapservice.make_plan(self.dirs, [], 10, shapes=[carre], contours="inconnu")


class KomootUploadTests(unittest.TestCase):
    """Envoi d'une séance vers Komoot (serveur simulé): un seul appel (visibilité du compte respectée), doublon, jeton refusé, enregistrement."""

    def setUp(self):
        import http.server
        from unittest import mock

        self.calls = []
        self.mode = {"dup": False, "patch_fails": False, "token_ok": True, "status": "friends"}
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self._read()
                test.calls.append(("POST", self.path, body, self.headers.get("Authorization"), self.headers.get("Content-Type")))
                if not test.mode["token_ok"]:
                    return self._reply(401, {"error": "unauthorized"})
                self._reply(202 if test.mode["dup"] else 201, {"id": 4242, "status": test.mode["status"]})

            def do_PATCH(self):
                body = self._read()
                test.calls.append(("PATCH", self.path, body, self.headers.get("Authorization"), self.headers.get("Content-Type")))
                self._reply(500 if test.mode["patch_fails"] else 200, {"id": 4242})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.old = os.environ.get("V650SYNC_KOMOOT_URL")
        os.environ["V650SYNC_KOMOOT_URL"] = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.addCleanup(lambda: os.environ.pop("V650SYNC_KOMOOT_URL", None) if self.old is None else os.environ.__setitem__("V650SYNC_KOMOOT_URL", self.old))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = service.DataDirs(self.tmp.name)
        for target, repl in (("session_file", lambda d, sid, fmt: (f"{sid}.fit", b"FITDATA")),
                             ("list_sessions", lambda d: [{"id": "2026-11-08_08-40-00Z", "start_local": "2026-11-08 09:40", "gpx": False}])):
            p = mock.patch.object(service, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def test_upload_posts_the_file_and_leaves_visibility_alone(self):
        from v650sync import komoot

        r = komoot.upload_activity("u", "tok", b"FITDATA", "Ma sortie", "fit", "racebike")
        self.assertEqual((r["tour_id"], r["created"]), (4242, True))
        self.assertEqual(len(self.calls), 1)                                   # un seul appel: la visibilité du compte est respectée (comme Polar Flow)
        post = self.calls[0]
        self.assertEqual(post[0], "POST")
        self.assertIn("data_type=fit", post[1])
        self.assertNotIn("status=", post[1])
        self.assertEqual(post[2], b"FITDATA")
        self.assertEqual(post[4], "application/octet-stream")
        self.assertTrue(post[3].startswith("Basic "))

    def test_duplicate_is_left_alone(self):
        from v650sync import komoot

        self.mode["dup"] = True
        r = komoot.upload_activity("u", "tok", b"FITDATA", "x")
        self.assertEqual((r["tour_id"], r["created"]), (4242, False))
        self.assertEqual([c[0] for c in self.calls], ["POST"])                # séance déjà connue: rien n'est modifié

    def test_refused_token(self):
        from v650sync import komoot

        self.mode["token_ok"] = False
        with self.assertRaises(komoot.KomootError) as cm:
            komoot.upload_activity("u", "bad", b"FITDATA", "x")
        self.assertIn("Reconnectez-vous", str(cm.exception))
        self.assertNotIn("bad", str(cm.exception))                            # le jeton n'apparaît jamais dans un message

    def test_service_records_and_needs_connection(self):
        with self.assertRaises(uploads.UploadError):
            service.upload_session(self.dirs, "2026-11-08_08-40-00Z", "komoot")   # pas connecté
        cfg = service.config(self.dirs)
        cfg.data["komoot"] = {"username": "u", "token": "plain:tok"}
        cfg.save()
        res = service.upload_session(self.dirs, "2026-11-08_08-40-00Z", "komoot")
        self.assertEqual((res["tour_id"], res["created"]), (4242, True))
        self.assertIn("Polar+V650+2026-11-08+09%3A40", self.calls[0][1])        # nom de la séance
        self.assertEqual(service.config(self.dirs).data["uploaded"]["2026-11-08_08-40-00Z"]["komoot"]["tour_id"], 4242)
        self.assertEqual(service.list_sessions(self.dirs)[0]["id"], "2026-11-08_08-40-00Z")  # (liste simulée)


class PolarZoomTests(unittest.TestCase):
    """Niveaux d'apparition relevés dans la carte de Polar: sans eux, des routes manquent aux petits zooms."""

    def test_table(self):
        z = mapgen._polar_zoom()
        self.assertTrue(all("=" in k for k in z))                      # pas de clé de commentaire
        self.assertEqual([z["highway=primary"], z["highway=secondary"], z["highway=tertiary"], z["railway=rail"]], [10, 10, 10, 10])
        self.assertEqual([z["highway=residential"], z["highway=service"], z["highway=track"], z["highway=unclassified"]], [12, 12, 12, 12])
        self.assertEqual([z["highway=footway"], z["highway=path"], z["highway=steps"]], [13, 13, 13])
        self.assertGreater(len(z), 80)


class DemFetchTests(unittest.TestCase):
    """Téléchargement des altitudes (serveur local simulé): cache, mer (403), fichier corrompu."""

    def setUp(self):
        import gzip
        import http.server

        n = 121
        data = gzip.compress(struct.pack(f">{n * n}h", *[300 + (i % n) * 3 for i in range(n * n)]))  # pente d'ouest en est: 300 m à 660 m
        self.hits = []
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                test.hits.append(self.path)
                if self.path.endswith("N45E006.hgt.gz"):
                    body, code = data, 200
                elif self.path.endswith("N45E007.hgt.gz"):
                    body, code = b"pas du gzip", 200
                else:
                    body, code = b"<Error>AccessDenied</Error>", 403  # S3: clé absente
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.environ.get("V650SYNC_DEM_URL")
        os.environ["V650SYNC_DEM_URL"] = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.addCleanup(lambda: os.environ.pop("V650SYNC_DEM_URL", None) if self.old is None else os.environ.__setitem__("V650SYNC_DEM_URL", self.old))

    def _ring(self, lat, lon):
        return [(lat, lon), (lat, lon + 0.2), (lat + 0.2, lon + 0.2), (lat + 0.2, lon)]

    def test_download_then_cache(self):
        ring = self._ring(45.3, 6.3)
        dem = mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None)
        self.assertIsNotNone(dem)
        self.assertEqual(dem.n, 121)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "N45E006.hgt.gz")))
        n_hits = len(self.hits)
        mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None)           # 2e fois: tout est en cache
        self.assertEqual(len(self.hits), n_hits)
        tiles = mapgen.dem_tiles(self.tmp.name, [ring])
        self.assertTrue(all(t["cached"] for t in tiles))

    def test_sea_tile_is_remembered(self):
        ring = self._ring(60.3, -20.3)                                         # le serveur répond 403: pas de tuile
        self.assertIsNone(mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None))
        n_hits = len(self.hits)
        self.assertIsNone(mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None))
        self.assertEqual(len(self.hits), n_hits)                               # marqueur « .none »: pas de nouvelle requête

    def test_corrupt_download_is_refused(self):
        ring = self._ring(45.3, 7.3)
        with self.assertRaises(mapgen.MapgenError):
            mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None)
        self.assertFalse(os.path.isfile(os.path.join(self.tmp.name, "N45E007.hgt.gz")))   # le fichier abîmé n'est pas gardé

    def test_make_contours_writes_osm(self):
        ring = self._ring(45.3, 6.2)
        dem = mapgen.fetch_dem(self.tmp.name, [ring], log=lambda m: None)
        out = os.path.join(self.tmp.name, "c.osm")
        n = mapgen.make_contours_from(dem, [ring], "main", out, log=lambda m: None)
        self.assertGreater(n, 0)                                               # la pente coupe les niveaux 400 m et 500 m dans la zone
        self.assertIn('k="contour_ext"', open(out, encoding="utf-8").read())


class JobsTests(unittest.TestCase):
    def test_progress_steps(self):
        from v650sync import jobs

        j = jobs.new_job()
        p = jobs.Progress(j)
        p.plan([("a", "Première"), ("b", "Deuxième"), ("c", "Troisième")])
        p.begin("a")
        p(10, 40)
        self.assertEqual((j["steps"][0]["state"], j["steps"][0]["done"], j["steps"][0]["total"]), ("running", 10, 40))
        p.begin("b")  # une étape qui commence referme la précédente
        self.assertEqual([s["state"] for s in j["steps"]], ["done", "running", "pending"])
        self.assertEqual(j["progress"], [0, 0])
        p(150, 300)
        p.finish(False)  # échec: l'étape en cours passe en erreur, la suivante reste en attente
        self.assertEqual([s["state"] for s in j["steps"]], ["done", "error", "pending"])
        q = jobs.Progress(j)
        q.finish(True)
        self.assertTrue(all(s["state"] == "done" for s in j["steps"]))
        n = jobs.steps_of(lambda d, t: None)  # simple fonction (tests, ligne de commande): ne fait rien, ne plante pas
        n.plan([("x", "y")]); n.begin("x"); n.end()

    def test_build_and_main_jobs_run_in_parallel(self):
        from v650sync import webui

        gate, started = threading.Event(), threading.Event()

        def slow(log, progress):
            started.set()
            gate.wait(10)
            return {"ok": 1}

        for slot in webui.JOBS.values():
            slot["running"] = False
        try:
            self.assertTrue(webui._start_job("map-build", slow))
            self.assertTrue(started.wait(5))
            self.assertFalse(webui._start_job("map-build", slow))  # une seule fabrication à la fois
            done = threading.Event()
            self.assertTrue(webui._start_job("connect", lambda log, progress: done.set()))  # la lecture du compteur n'est pas bloquée par la fabrication
            self.assertTrue(done.wait(5))
            self.assertEqual(webui.JOBS["build"]["kind"], "map-build")
            self.assertTrue(webui.JOBS["build"]["running"])
        finally:
            gate.set()
            for _ in range(50):
                if not webui.any_job_running():
                    break
                time.sleep(0.1)
        self.assertEqual(webui.JOBS["build"]["result"], {"ok": 1})

    def test_build_plans_only_needed_steps(self):
        from unittest import mock

        from v650sync import jobs, mapgen, mapservice

        class Rec(jobs.NullSteps):
            def __init__(self):
                self.plan_, self.began = [], []

            def plan(self, steps):
                self.plan_ = [k for k, _ in steps]

            def begin(self, key, label=None):
                self.began.append(key)

            def __call__(self, d, t):
                pass

        rec = Rec()
        info = {"size": 10, "bbox": (0, 0, 1, 1)}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(mapgen, "tools_status", return_value={"installed": True, "items": []}), \
                mock.patch.object(mapgen, "needs_download", return_value=False), mock.patch.object(mapgen, "fetch_regions", return_value=["x.pbf"]), \
                mock.patch.object(mapgen, "build_map", return_value=info), mock.patch.object(mapgen, "sha256_file", return_value="h"):
            plan = {"regions": [{"name": "Région d'essai"}], "rings": [[(0, 0), (0, 1), (1, 1), (1, 0)]], "center": [0.5, 0.5], "profile": "velo"}
            mapservice.build(service.DataDirs(d), plan, "t", lambda m: None, rec)
        self.assertEqual(rec.plan_, ["build", "check"])  # outils déjà là, données déjà téléchargées: seulement 2 étapes
        self.assertEqual(rec.began, ["build", "check"])


    def test_build_steps_list_each_tool_and_region(self):
        from unittest import mock

        from v650sync import jobs, mapgen, mapservice

        plans = []

        class Rec(jobs.NullSteps):
            def plan(self, steps):
                plans.append(steps)

        items = [{"id": "jre", "label": "Java 17", "mb": 43.9, "installed": False}, {"id": "osmosis", "label": "Osmosis", "mb": 15.2, "installed": True}]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(mapgen, "tools_status", return_value={"installed": False, "items": items}), \
                mock.patch.object(mapgen, "needs_download", return_value=True), mock.patch.object(mapgen, "install_tools"), \
                mock.patch.object(mapgen, "fetch_regions", return_value=["x.pbf"]), mock.patch.object(mapgen, "build_map", return_value={"size": 10, "bbox": (0, 0, 1, 1)}), \
                mock.patch.object(mapgen, "sha256_file", return_value="h"):
            plan = {"regions": [{"name": "Alsace", "mb": 120}, {"name": "Bourgogne", "mb": 200}], "rings": [[(0, 0), (0, 1), (1, 1), (1, 0)]], "center": [0.5, 0.5]}
            mapservice.build(service.DataDirs(d), plan, "t", lambda m: None, Rec())
        keys = [k for k, _ in plans[0]]
        self.assertEqual(keys, ["tool_jre", "reg_Alsace", "reg_Bourgogne", "build", "check"])
        self.assertIn("Java 17", plans[0][0][1])
        self.assertIn("120 Mo", plans[0][1][1])

    def test_pbf_timestamp_reads_header(self):
        import zlib

        from v650sync import mapgen

        def varint(n):
            out = bytearray()
            while True:
                b, n = n & 0x7F, n >> 7
                out.append(b | (0x80 if n else 0))
                if not n:
                    return bytes(out)

        ts = 1_762_560_000  # 2025-11-08
        hdr = varint(32 << 3) + varint(ts)
        z = zlib.compress(hdr)
        blob = varint(2 << 3) + varint(len(hdr)) + varint(3 << 3 | 2) + varint(len(z)) + z
        bh = varint(1 << 3 | 2) + varint(9) + b"OSMHeader" + varint(3 << 3) + varint(len(blob))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.osm.pbf")
            with open(p, "wb") as f:
                f.write(struct.pack(">I", len(bh)) + bh + blob)
            self.assertEqual(mapgen.pbf_timestamp(p), ts)
            self.assertEqual(mapgen.date_fr(ts), "2025-11-08")
            with open(p, "wb") as f:
                f.write(b"pas un pbf")
            self.assertIsNotNone(mapgen.pbf_timestamp(p))  # illisible: on retombe sur la date du fichier


class CorridorTests(unittest.TestCase):
    def test_small_closed_loop_still_gives_a_zone(self):
        import math

        from v650sync import mapgen

        loop = [(45.0 + 0.02 * math.sin(a / 10), 5.0 + 0.03 * math.cos(a / 10)) for a in range(0, 63)] + [(45.0, 5.03)]  # boucle d'environ 3 km, départ = arrivée
        rings = mapgen.corridor_rings(loop, 15)
        self.assertTrue(rings)
        self.assertGreater(mapgen.area_km2(rings), 500)

    def test_straight_route_has_round_ends(self):
        from v650sync import mapgen

        line = [(45.0, 5.0 + 0.01 * i) for i in range(40)]
        rings = mapgen.corridor_rings(line, 10)
        self.assertEqual(len(rings), 3)  # un rectangle + un disque à chaque bout
        self.assertEqual(sorted(len(r) for r in rings), [4, 24, 24])


class StrictClipTests(unittest.TestCase):
    def test_osmosis_arguments_clip_strictly_by_default(self):
        import inspect

        from v650sync import mapgen

        sig = inspect.signature(mapgen.build_map)
        self.assertFalse(sig.parameters["complete_ways"].default)
        self.assertFalse(sig.parameters["complete_relations"].default)

    def test_tile_coverage_rejects_non_map(self):
        from v650sync import mapgen

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.map")
            with open(p, "wb") as f:
                f.write(b"pas une carte")
            with self.assertRaises(mapgen.MapgenError):
                mapgen.tile_coverage(p)


class ServerEndpointsTests(unittest.TestCase):
    """Le vrai serveur local répond (un « import time » local avait cassé /api/jobs sans qu'aucun test ne le voie)."""

    def test_get_endpoints_answer(self):
        import json
        import urllib.request
        from http.server import ThreadingHTTPServer

        from v650sync import webui

        with tempfile.TemporaryDirectory() as d:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
            port = srv.server_address[1]
            srv.RequestHandlerClass = webui.make_handler(service.DataDirs(d), "tok", port)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                for path in ("/api/ping", "/api/job", "/api/jobs", "/api/map/status", "/api/journal", "/api/services", "/api/library"):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                        self.assertEqual(r.status, 200, path)
                        data = json.loads(r.read())
                    if path == "/api/jobs":
                        self.assertTrue({"main", "build", "now"} <= set(data))
                        self.assertIn("steps", data["build"])
            finally:
                srv.shutdown()
                srv.server_close()


class IntervalsCheckTests(unittest.TestCase):
    def _server(self, ok_ids):
        import json
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                aid = self.path.rstrip("/").rsplit("/", 1)[-1]
                code, body = (200, {"name": "Test"}) if aid in ok_ids else (403, {"error": "interdit"})
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):  # comme Cloudflare devant intervals.icu: 403 « error code: 1010 »
                    code, body = 403, {"error": "error code: 1010"}
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_wrong_athlete_id_falls_back_to_zero(self):
        from v650sync import secretstore, uploads

        srv = self._server({"0"})
        old = uploads.ENDPOINTS["intervals"]
        uploads.ENDPOINTS["intervals"] = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = uploads.Config(os.path.join(d, "c.json"))
                cfg.data["intervals"] = {"athlete_id": "i123456", "api_key": secretstore.protect("cle")}
                name, note = uploads.intervals_check_any(cfg)
                self.assertEqual((name, cfg.data["intervals"]["athlete_id"]), ("Test", "0"))
                self.assertIn("i123456", note)
            srv.ok = set()
        finally:
            uploads.ENDPOINTS["intervals"] = old
            srv.shutdown()
            srv.server_close()

    def test_bad_key_gives_clear_message(self):
        from v650sync import secretstore, uploads

        srv = self._server(set())
        old = uploads.ENDPOINTS["intervals"]
        uploads.ENDPOINTS["intervals"] = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = uploads.Config(os.path.join(d, "c.json"))
                cfg.data["intervals"] = {"athlete_id": "0", "api_key": secretstore.protect("mauvaise")}
                with self.assertRaises(uploads.UploadError) as cm:
                    uploads.intervals_check_any(cfg)
                self.assertIn("clé d'API", str(cm.exception))
                self.assertNotIn("mauvaise", str(cm.exception))
        finally:
            uploads.ENDPOINTS["intervals"] = old
            srv.shutdown()
            srv.server_close()


class AutoSendTests(unittest.TestCase):
    """Envoi automatique: seules les séances plus récentes que la référence sont traitées, retentées en cas d'échec, jamais envoyées deux fois."""

    def setUp(self):
        from unittest import mock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = service.DataDirs(self.tmp.name)
        self.sessions = [{"id": "2026-11-01_08-00-00Z", "start_local": "2026-11-01 09:00", "gpx": False},
                         {"id": "2026-11-05_08-00-00Z", "start_local": "2026-11-05 09:00", "gpx": False}]
        self.uploaded, self.fail = [], [False]

        def fake_upload(dirs, sid, target):
            if self.fail[0]:
                raise RuntimeError("pas d'internet")
            self.uploaded.append(sid)
            cfg = service.config(dirs)
            cfg.data.setdefault("uploaded", {}).setdefault(sid, {})[target] = {"at": "x"}
            cfg.save()

        for target, repl in (("list_sessions", lambda d: [dict(s) for s in self.sessions]), ("upload_session", fake_upload),
                             ("session_file", lambda d, sid, fmt: (f"{sid}.gpx", b"<gpx/>"))):
            p = mock.patch.object(service, target, repl)
            p.start()
            self.addCleanup(p.stop)
        cfg = service.config(self.dirs)
        cfg.data["intervals"] = {"api_key": "x", "athlete_id": "0"}
        cfg.save()

    def test_nothing_when_off(self):
        self.assertEqual(service.auto_process(self.dirs, lambda m: None), {"intervals": 0, "gpx": 0, "komoot": 0, "errors": 0})
        self.assertEqual(self.uploaded, [])

    def test_history_not_sent_then_new_session_sent_once(self):
        a = service.set_auto(self.dirs, True, True)
        self.assertEqual(a["baseline"], "2026-11-05_08-00-00Z")  # l'historique est considéré comme connu
        self.assertEqual(service.auto_process(self.dirs, lambda m: None)["intervals"], 0)
        self.sessions.append({"id": "2026-11-10_08-00-00Z", "start_local": "2026-11-10 09:00", "gpx": False})
        r = service.auto_process(self.dirs, lambda m: None)
        self.assertEqual((r["intervals"], r["gpx"], r["errors"]), (1, 1, 0))
        self.assertEqual(self.uploaded, ["2026-11-10_08-00-00Z"])
        self.assertTrue(os.path.isfile(os.path.join(self.dirs.export, "2026-11-10_08-00-00Z.gpx")))
        self.sessions[-1]["gpx"] = True
        service.auto_process(self.dirs, lambda m: None)  # 2e lecture: rien de plus
        self.assertEqual(self.uploaded, ["2026-11-10_08-00-00Z"])

    def test_failure_is_retried(self):
        service.set_auto(self.dirs, True, False)
        self.sessions.append({"id": "2026-11-10_08-00-00Z", "start_local": "2026-11-10 09:00", "gpx": False})
        self.fail[0] = True
        r = service.auto_process(self.dirs, lambda m: None)
        self.assertEqual((r["intervals"], r["errors"]), (0, 1))
        self.fail[0] = False
        self.assertEqual(service.auto_process(self.dirs, lambda m: None)["intervals"], 1)

    def test_toggle_keeps_baseline_and_public_state(self):
        service.set_auto(self.dirs, True, False)
        self.sessions.append({"id": "2026-11-10_08-00-00Z", "start_local": "x", "gpx": False})
        a = service.set_auto(self.dirs, True, True)  # déjà actif: la référence ne bouge pas
        self.assertEqual(a["baseline"], "2026-11-05_08-00-00Z")
        self.assertEqual(service.config(self.dirs).public()["auto"], {"intervals": True, "gpx": True, "komoot": False})

    def test_auto_komoot_needs_connection_and_sends_once(self):
        service.set_auto(self.dirs, False, False, True)
        self.sessions.append({"id": "2026-11-10_08-00-00Z", "start_local": "2026-11-10 09:00", "gpx": False})
        r = service.auto_process(self.dirs, lambda m: None)                   # Komoot non connecté: ignoré, pas d'erreur
        self.assertEqual((r["komoot"], r["errors"], self.uploaded), (0, 0, []))
        cfg = service.config(self.dirs)
        cfg.data["komoot"] = {"username": "u", "token": "plain:t"}
        cfg.save()
        r = service.auto_process(self.dirs, lambda m: None)
        self.assertEqual((r["komoot"], r["errors"]), (1, 0))
        self.assertEqual(self.uploaded, ["2026-11-10_08-00-00Z"])
        service.auto_process(self.dirs, lambda m: None)                       # déjà envoyée: pas de doublon
        self.assertEqual(self.uploaded, ["2026-11-10_08-00-00Z"])


class ContourTests(unittest.TestCase):
    """Courbes de niveau sur un relief synthétique: un cône (altitude 1200 m au centre, -50 m par km) dans une tuile N45E006."""

    N = 121  # 1 échantillon tous les 30 secondes d'arc (~925 m): rapide à calculer

    def setUp(self):
        import gzip

        from v650sync import contours

        self.contours = contours
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        n = self.N
        vals = []
        self.c_lat, self.c_lon = 45.5, 6.5
        for r in range(n):
            lat = 46 - r / (n - 1)
            for c in range(n):
                lon = 6 + c / (n - 1)
                km = math.hypot((lat - self.c_lat) * 111.2, (lon - self.c_lon) * 111.32 * math.cos(math.radians(lat)))
                vals.append(int(round(1200 - 50 * km)))
        path = os.path.join(self.tmp.name, "N45E006.hgt.gz")
        with open(path, "wb") as f:
            f.write(gzip.compress(struct.pack(f">{n * n}h", *vals)))
        arr, n = contours.read_hgt(path)
        self.dem = contours.Dem({(45, 6): (arr, n)})
        self.ring = [(45.05, 6.05), (45.05, 6.95), (45.95, 6.95), (45.95, 6.05)]

    def _radius_km(self, pts):
        return [math.hypot((p[0] - self.c_lat) * 111.2, (p[1] - self.c_lon) * 111.32 * math.cos(math.radians(p[0]))) for p in pts]

    def test_circles_at_the_right_altitude(self):
        lines = self.contours.contour_lines(self.dem, [self.ring], equidistance=100, log=lambda m: None)
        levels = sorted(e for e, _ in lines)
        self.assertEqual(levels, [100 * k for k in range(1, 12)])  # 100 .. 1100, une courbe chacune
        for ele, pts in lines:
            self.assertEqual(pts[0], pts[-1])  # boucle fermée
            expected = (1200 - ele) / 50
            r = self._radius_km(pts)
            self.assertAlmostEqual(sum(r) / len(r), expected, delta=0.35, msg=f"courbe {ele} m")
            self.assertLess(max(r) - min(r), 0.7)

    def test_classes_and_fine_lines(self):
        c = self.contours
        self.assertEqual([c.classify(x) for x in (500, 1000, 100, 600, 25, 125)],
                         ["elevation_major", "elevation_major", "elevation_medium", "elevation_medium", "elevation_minor", "elevation_minor"])
        fine = c.contour_lines(self.dem, [self.ring], equidistance=25, log=lambda m: None)
        self.assertEqual(len(fine), 47)  # 25 m .. 1175 m: 47 niveaux (le sommet est à 1200 m)

    def test_zone_clips_lines(self):
        east = [(45.05, 6.5), (45.05, 6.95), (45.95, 6.95), (45.95, 6.5)]  # moitié est: les cercles deviennent des arcs
        lines = self.contours.contour_lines(self.dem, [east], equidistance=100, log=lambda m: None)
        self.assertTrue(lines)
        for ele, pts in lines:
            self.assertNotEqual(pts[0], pts[-1], f"la courbe {ele} m devrait être ouverte")
            self.assertGreater(min(p[1] for p in pts), 6.5 - 0.03)  # au plus une cellule (~1 km) au-delà de la limite

    def test_decimation_keeps_shape(self):
        lines = self.contours.contour_lines(self.dem, [self.ring], equidistance=100, decim=2, log=lambda m: None)
        self.assertEqual(len(lines), 11)

    def test_missing_tile_is_sea_level(self):
        far = [(46.05, 6.2), (46.05, 6.4), (46.2, 6.4), (46.2, 6.2)]  # tuile N46E006 absente
        self.assertEqual(self.contours.contour_lines(self.dem, [far], log=lambda m: None), [])

    def test_osm_file(self):
        import xml.etree.ElementTree as ET

        lines = self.contours.contour_lines(self.dem, [self.ring], equidistance=100, log=lambda m: None)
        out = os.path.join(self.tmp.name, "c.osm")
        nodes, ways = self.contours.write_osm(out, lines)
        root = ET.parse(out).getroot()
        self.assertEqual(len(root.findall("node")), nodes)
        ws = root.findall("way")
        self.assertEqual(len(ws), ways)
        w500 = [w for w in ws if {t.get("k"): t.get("v") for t in w.findall("tag")}.get("ele") == "500"][0]
        tags = {t.get("k"): t.get("v") for t in w500.findall("tag")}
        self.assertEqual(tags["contour_ext"], "elevation_major")
        refs = [nd.get("ref") for nd in w500.findall("nd")]
        self.assertEqual(refs[0], refs[-1])  # voie fermée
        self.assertTrue(all(int(i) >= self.contours.BASE_ID for i in refs))
        ids = [int(n.get("id")) for n in root.findall("node")]
        self.assertEqual(ids, sorted(ids))  # trié: prêt pour la fusion Osmosis


class ConfigSharedTests(unittest.TestCase):
    """La clé Intervals.icu ne doit pas disparaître parce qu'une autre partie du programme réécrit config.json avec une copie périmée."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = service.DataDirs(self.tmp.name)

    def test_same_object_and_no_stale_overwrite(self):
        a = service.config(self.dirs)
        b = service.config(self.dirs)
        self.assertIs(a, b)
        a.data["komoot"] = {"username": "u", "token": "t"}
        a.save()
        b.data["intervals"] = {"api_key": "k", "athlete_id": "0"}
        b.save()
        again = uploads.Config(os.path.join(self.dirs.root, "config.json"))  # ce qui est vraiment sur le disque
        self.assertEqual(again.data["intervals"]["api_key"], "k")
        self.assertEqual(again.data["komoot"]["token"], "t")

    def test_utf8_bom_is_accepted(self):
        path = os.path.join(self.dirs.root, "config.json")
        os.makedirs(self.dirs.root, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig") as f:   # le Bloc-notes de Windows ajoute un BOM
            json.dump({"intervals": {"api_key": "k", "athlete_id": "0"}}, f)
        self.assertEqual(uploads.Config(path).data["intervals"]["api_key"], "k")

    def test_external_edit_is_picked_up(self):
        cfg = service.config(self.dirs)
        cfg.save()
        path = os.path.join(self.dirs.root, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"intervals": {"api_key": "z", "athlete_id": "5"}}, f)
        os.utime(path, ns=(10**18, 10**18))  # date de modification différente quoi qu'il arrive
        self.assertEqual(service.config(self.dirs).data["intervals"]["athlete_id"], "5")


class JournalTests(unittest.TestCase):
    def _filtered(self, text):
        import logging

        from v650sync import journal

        rec = logging.LogRecord("x", logging.INFO, "", 0, text, (), None)
        journal._Redact().filter(rec)
        return rec.msg

    def test_masks_secrets_emails_and_home(self):
        home = os.path.expanduser("~")
        for raw, secret in (("login jean.dupont@example.com ok", "jean.dupont"), ("token=abcdef123456 reçu", "abcdef123456"),
                            ("password: S3cret!x", "S3cret"), ("Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==", "QWxhZGRpbjpvcGVuIHNlc2FtZQ"),
                            ("api_key=zzz999", "zzz999")):
            self.assertNotIn(secret, self._filtered(raw), raw)
        self.assertNotIn(home, self._filtered(f"fichier {home}\\Polar V650\\x.gpx"))
        self.assertIn("PUT /U/0/ROUTES/1/PROUTE.BPB", self._filtered("PUT /U/0/ROUTES/1/PROUTE.BPB (1200 octets)"))  # le reste est conservé

    def test_report_zip_is_masked(self):
        import zipfile

        from v650sync import journal

        old = os.environ.get("LOCALAPPDATA")
        with tempfile.TemporaryDirectory() as app, tempfile.TemporaryDirectory() as data:
            os.environ["LOCALAPPDATA"] = app
            try:
                os.makedirs(journal.paths.settings_dir())
                with open(journal.path(), "w", encoding="utf-8") as f:
                    f.write("2026-11-11 10:00:00.000 INFO [MainThread] v650sync.web: connexion marie@example.org token=SECRETVALUE1\n")
                dirs = service.DataDirs(data)
                rep = journal.make_report(dirs)
                with zipfile.ZipFile(rep) as z:
                    names = z.namelist()
                    allText = "\n".join(z.read(n).decode("utf-8") for n in names)
                self.assertIn("systeme.txt", names)
                self.assertIn("journal.txt", names)
                self.assertIn("connexion", allText)
                self.assertNotIn("marie@example.org", allText)
                self.assertNotIn("SECRETVALUE1", allText)
            finally:
                if old is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old

    def test_logstream_splits_lines(self):
        import logging

        from v650sync import journal

        got = []

        class H(logging.Handler):
            def emit(self, rec):
                got.append(rec.getMessage())

        h = H()
        lg = journal.get("console")
        lg.addHandler(h)
        lg.setLevel("INFO")
        try:
            s = journal.LogStream()
            s.write("ligne 1\nli")
            s.write("gne 2\n\n")
        finally:
            lg.removeHandler(h)
        self.assertEqual(got, ["ligne 1", "ligne 2"])


class OwnerTests(unittest.TestCase):
    """Titulaire du compteur lu dans USERID.BPB: prénom, nom, e-mail, identifiant; rien d'autre."""

    @staticmethod
    def _field(num, value):
        if isinstance(value, int):
            return bytes([num << 3, value]) if value < 128 else bytes([num << 3, (value & 127) | 128, value >> 7])
        data = value.encode()
        return bytes([(num << 3) | 2, len(data)]) + data

    def test_owner_fields(self):
        data = self._field(1, 300) + self._field(2, "camille@example.com") + self._field(3, "x" * 4) + self._field(5, "Camille") + self._field(6, "Demo")
        self.assertEqual(service.owner_from(data), {"first": "Camille", "last": "Demo", "email": "camille@example.com", "polar_id": 300})

    def test_owner_missing_fields(self):
        self.assertEqual(service.owner_from(self._field(5, "Camille")), {"first": "Camille", "last": "", "email": "", "polar_id": None})


class ProfileEditTests(unittest.TestCase):
    """Modification du profil du compteur: seuls les champs voulus changent, copie préalable, relecture, retour en arrière."""

    @staticmethod
    def _sample():
        from v650sync import profileedit as pe

        stamp = pe.encode([(1, 2, pe.encode([(1, 0, 2026), (2, 0, 7), (3, 0, 10)])), (2, 2, pe.encode([(1, 0, 17), (2, 0, 25), (3, 0, 55), (4, 0, 0)])), (3, 0, 0)])

        def sub(*vals, extra=()):
            return pe.encode([(1, *vals), (2, 2, stamp), *extra])

        phys = pe.encode([
            (3, 2, sub(5, struct.pack("<f", 87.0), extra=[(3, 0, 2)])), (4, 2, sub(5, struct.pack("<f", 172.0))),
            (5, 2, sub(0, 182, extra=[(3, 0, 2)])), (6, 2, sub(0, 50)), (10, 2, sub(0, 41)), (15, 2, sub(0, 230)), (100, 2, stamp)])
        user = pe.encode([(1, 0, 123456), (2, 2, b"camille@example.com"), (3, 2, b"\x01" * 20), (5, 2, b"Camille"), (6, 2, b"Demo"), (100, 2, stamp)])
        return phys, user

    def _fake_device(self, files, fail_on=None, ignore_writes=False):
        from unittest import mock

        test = self

        class Client:
            def __init__(self):
                self.puts = []

            def get_file(self, path):
                return files[path]

            def put(self, path, data=b"", progress=None):
                self.puts.append(path)
                if path == fail_on:
                    raise service.PftpError("refusé par le compteur (test)")
                if not ignore_writes:
                    files[path] = data

        client = Client()

        @contextmanager
        def dev():
            yield None, client

        p = mock.patch.object(service, "device", dev)
        p.start()
        self.addCleanup(p.stop)
        patch2 = mock.patch.object(service.time, "sleep", lambda s: None)
        patch2.start()
        self.addCleanup(patch2.stop)
        return client

    def setUp(self):
        self.phys, self.user = self._sample()
        self.files = {service.PHYS_PATH: self.phys, service.USER_PATH: self.user}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = service.DataDirs(self.tmp.name)

    def test_only_requested_fields_change(self):
        from v650sync import profileedit as pe

        new_phys, new_user, changed = pe.apply(self.phys, self.user, {"weight_kg": "80,5", "ftp": 250, "first": "Alex"})
        self.assertEqual(sorted(changed), ["first", "ftp", "weight_kg"])
        vals = pe.read_values(new_phys, new_user)
        self.assertEqual((vals["weight_kg"], vals["ftp"], vals["first"], vals["last"], vals["hr_max"]), (80.5, 250, "Alex", "Demo", 182))
        old, new = pe.decode(self.phys), pe.decode(new_phys)
        self.assertEqual([f for f in old if f[0] not in (3, 15)], [f for f in new if f[0] not in (3, 15)])  # le reste est identique
        self.assertEqual(len(pe.decode(self.user)), len(pe.decode(new_user)))
        self.assertEqual(pe.decode(new_user)[1], pe.decode(self.user)[1])  # l'e-mail n'est pas touché

    def test_validation(self):
        from v650sync import profileedit as pe

        for bad in ({"weight_kg": 5}, {"weight_kg": "abc"}, {"hr_max": 500}, {"first": ""}, {"last": "x" * 31}, {"email": "a@b.c"}, {"first": "A\x00B"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                pe.clean(bad)

    def test_edit_writes_backup_and_verifies(self):
        client = self._fake_device(self.files)
        res = service.edit_profile(self.dirs, {"weight_kg": 80, "last": "Martin"})
        self.assertEqual(sorted(res["changed"]), ["last", "weight_kg"])
        self.assertTrue(res["verified"])
        self.assertEqual(sorted(client.puts), sorted([service.PHYS_PATH, service.USER_PATH]))
        b = os.path.join(self.dirs.root, "sauvegarde-profil", res["backup"])
        with open(os.path.join(b, "PHYSDATA.BPB"), "rb") as f:
            self.assertEqual(f.read(), self.phys)
        with open(os.path.join(b, "USERID.BPB"), "rb") as f:
            self.assertEqual(f.read(), self.user)
        self.assertEqual(service.profile_backups(self.dirs)[0]["stamp"], res["backup"])

    def test_nothing_to_change_writes_nothing(self):
        client = self._fake_device(self.files)
        res = service.edit_profile(self.dirs, {"weight_kg": 87, "first": "Camille"})
        self.assertEqual(res["changed"], [])
        self.assertEqual(client.puts, [])
        self.assertFalse(os.path.exists(os.path.join(self.dirs.root, "sauvegarde-profil")))

    def test_second_write_refused_puts_the_first_back(self):
        self._fake_device(self.files, fail_on=service.USER_PATH)
        with self.assertRaises(service.PftpError):
            service.edit_profile(self.dirs, {"weight_kg": 80, "first": "Alex"})
        self.assertEqual(self.files[service.PHYS_PATH], self.phys)
        self.assertEqual(self.files[service.USER_PATH], self.user)

    def test_readback_mismatch_is_an_error(self):
        self._fake_device(self.files, ignore_writes=True)
        with self.assertRaises(service.PftpError):
            service.edit_profile(self.dirs, {"hr_max": 190})

    def test_restore_from_backup(self):
        self._fake_device(self.files)
        res = service.edit_profile(self.dirs, {"weight_kg": 70, "first": "Alex"})
        self.assertNotEqual(self.files[service.PHYS_PATH], self.phys)
        back = service.restore_profile(self.dirs)
        self.assertEqual(back["restored"], res["backup"])
        self.assertEqual(self.files[service.PHYS_PATH], self.phys)
        self.assertEqual(self.files[service.USER_PATH], self.user)
        with self.assertRaises(service.PftpError):
            service.restore_profile(self.dirs, "../../etc")


class UpdateCheckTests(unittest.TestCase):
    """Vérification des mises à jour: comparaison de versions, URL sûre, une requête par jour, désactivation, échec réseau (GitHub simulé)."""

    def setUp(self):
        import http.server
        from unittest import mock
        from v650sync import updates

        self.updates = updates
        self.hits = []
        self.reply = {"code": 200, "body": {"tag_name": "v9.9.9", "html_url": updates.RELEASES_URL + "/tag/v9.9.9"}}
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                test.hits.append((self.path, self.headers.get("User-Agent"), self.headers.get("Authorization")))
                body = json.dumps(test.reply["body"]).encode()
                self.send_response(test.reply["code"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name}),
                      mock.patch.object(updates, "API_URL", f"http://127.0.0.1:{self.srv.server_address[1]}/latest")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_versions(self):
        u = self.updates
        self.assertEqual(u.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(u.parse_version("0.2"), (0, 2))
        self.assertEqual(u.parse_version("1.0.0-beta"), (1, 0, 0))
        self.assertIsNone(u.parse_version("latest"))
        self.assertTrue(u.is_newer("v0.1.1", "0.1.0"))
        self.assertTrue(u.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(u.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(u.is_newer("0.1", "0.1.0"))
        self.assertFalse(u.is_newer("0.0.9", "0.1.0"))
        self.assertFalse(u.is_newer("n'importe quoi", "0.1.0"))

    def test_url_is_restricted_to_the_repo(self):
        u = self.updates
        self.assertEqual(u.safe_url("https://evil.example/x"), u.RELEASES_URL)
        self.assertEqual(u.safe_url(None), u.RELEASES_URL)
        self.assertEqual(u.safe_url(u.RELEASES_URL + "/tag/v1"), u.RELEASES_URL + "/tag/v1")

    def test_check_once_a_day_and_anonymous(self):
        st = self.updates.check()
        self.assertTrue(st["available"])
        self.assertEqual(st["latest"], "v9.9.9")
        self.assertEqual(len(self.hits), 1)
        self.assertTrue(self.hits[0][1].startswith("V650Sync/"))
        self.assertIsNone(self.hits[0][2])
        self.updates.check()  # moins d'un jour: pas de nouvelle requête
        self.assertEqual(len(self.hits), 1)
        self.updates.check(force=True)
        self.assertEqual(len(self.hits), 2)
        self.assertTrue(self.updates.state()["available"])  # état relu sans réseau

    def test_disabled_makes_no_request(self):
        self.updates.set_enabled(False)
        self.assertFalse(self.updates.check()["enabled"])
        self.assertEqual(self.hits, [])

    def test_no_release_and_network_failure(self):
        self.reply.update(code=404, body={"message": "Not Found"})
        st = self.updates.check()
        self.assertFalse(st["available"])
        self.assertEqual(st["latest"], "")
        self.srv.shutdown()
        self.srv.server_close()
        st = self.updates.check(force=True)  # serveur arrêté: aucune exception, état conservé
        self.assertTrue(st.get("error"))
        self.assertFalse(st["available"])

    def test_foreign_release_url_is_replaced(self):
        self.reply["body"] = {"tag_name": "v9.0.0", "html_url": "https://evil.example/download.exe"}
        self.assertEqual(self.updates.check()["url"], self.updates.RELEASES_URL)


class TranslationFileTests(unittest.TestCase):
    """Le dictionnaire français → anglais doit rester cohérent (variables {0}, {1}… identiques des deux côtés)."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "v650sync", "i18n_en.json")
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    def test_structure(self):
        self.assertGreater(len(self.data["ex"]), 100)
        self.assertGreater(len(self.data["pat"]), 50)
        for k, v in self.data["ex"].items():
            self.assertTrue(k.strip() and v.strip(), k)

    def test_placeholders_match(self):
        import re
        for k, v in self.data["pat"]:
            self.assertEqual(sorted(re.findall(r"\{\d+\}", k)), sorted(re.findall(r"\{\d+\}", v)), k)

    def test_page_serves_dictionary(self):
        from v650sync import webui
        self.assertIn("pat", json.loads(webui.I18N_JSON))


if __name__ == "__main__":
    unittest.main()
