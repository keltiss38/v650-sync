"""Envoi des séances (fichier FIT) vers Intervals.icu avec une clé d'API personnelle, bibliothèque standard uniquement.

Optionnel: sans clé enregistrée, rien n'est envoyé nulle part. La clé est saisie par l'utilisateur dans la page web et
stockée localement dans config.json (dossier de données); elle n'est jamais renvoyée à la page ni écrite dans les journaux.
"""
import base64
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request

from . import __version__, journal

LOG = journal.get("intervals")
INTERVALS_BASE = "https://intervals.icu/api/v1"
ENDPOINTS = {"intervals": INTERVALS_BASE}  # surchargeable dans les tests
# Intervals.icu est derrière Cloudflare, qui refuse l'identifiant par défaut de Python (« Python-urllib »: HTTP 403, « error code: 1010 »):
# on se présente avec le nom du programme.
USER_AGENT = f"V650Sync/{__version__} (usage personnel)"


class UploadError(Exception):
    pass


class Config:
    """Réglages (config.json). Un seul objet par fichier dans le programme (voir service.config): plusieurs copies lues
    puis réécrites à des moments différents pouvaient s'écraser entre elles (une clé enregistrée disparaissait)."""
    LOCK = threading.RLock()

    def __init__(self, path):
        self.path = path
        self.stamp = None
        self.load()

    def _mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def load(self):
        self.data = {"intervals": {}, "komoot": {}, "uploaded": {}}
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8-sig") as f:  # tolère le BOM que le Bloc-notes de Windows ajoute
                    self.data.update(json.load(f))
            except (OSError, ValueError):
                LOG.warning("config.json illisible: réglages ignorés")
        self.stamp = self._mtime()

    def refresh(self):
        """Relit le fichier s'il a été modifié ailleurs (autre instance, retouche à la main)."""
        with self.LOCK:
            if self._mtime() != self.stamp:
                self.load()

    def save(self):
        with self.LOCK:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            self.stamp = self._mtime()

    def public(self):
        """État sans aucun secret."""
        i, k = self.data.get("intervals", {}), self.data.get("komoot", {})
        a = self.data.get("auto", {})
        return {"intervals": {"configured": bool(i.get("api_key")), "athlete_id": i.get("athlete_id", "0")},
                "auto": {"intervals": bool(a.get("intervals")), "gpx": bool(a.get("gpx")), "komoot": bool(a.get("komoot"))},
                "komoot": {"connected": bool(k.get("token")), "username": k.get("username")}}


def _multipart(fields, file_field, filename, content):
    boundary = "----v650" + secrets.token_hex(8)
    out = bytearray()
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _request(req, timeout=60):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        try:
            detail = json.loads(raw or b"{}")
        except ValueError:
            detail = {}
        text = (detail.get("message") or detail.get("error") or detail) if isinstance(detail, dict) else detail
        if not text:
            text = raw[:120].decode("utf-8", "replace").strip() or "erreur"  # ex.: « error code: 1010 » (blocage Cloudflare)
        LOG.warning("Réponse HTTP %s de %s: %s", e.code, e.headers.get("Server", "?"), str(text)[:160])
        raise UploadError(f"HTTP {e.code}: {text}") from None
    except urllib.error.URLError as e:
        raise UploadError(f"réseau: {e.reason}") from None


def _headers(api_key, extra=None):
    return {"Authorization": _auth(api_key), "User-Agent": USER_AGENT, **(extra or {})}


def _auth(api_key):
    from .secretstore import unprotect

    return "Basic " + base64.b64encode(f"API_KEY:{unprotect(api_key)}".encode()).decode()  # clé chiffrée par Windows (ou ancien format en clair)


def intervals_check(cfg):
    i = cfg.data["intervals"]
    req = urllib.request.Request(f"{ENDPOINTS['intervals']}/athlete/{i.get('athlete_id', '0')}", headers=_headers(i["api_key"]))
    _, info = _request(req)
    return info.get("name") or "connecté"


def intervals_check_any(cfg):
    """Teste la clé. Si l'identifiant d'athlète saisi est refusé (403/404) mais que la clé est bonne, on utilise « 0 » (l'athlète de la clé)
    et on le dit. Renvoie (nom, remarque ou None). Ne journalise jamais la clé."""
    i = cfg.data["intervals"]
    given = str(i.get("athlete_id") or "0").strip() or "0"
    try:
        LOG.info("Test Intervals.icu avec l'athlète « %s »", given)
        return intervals_check(cfg), None
    except UploadError as e:
        LOG.warning("Intervals.icu a refusé le test (athlète « %s »): %s", given, e)
        if given == "0" or not re.search(r"HTTP (403|404)", str(e)):
            raise _explain(e) from None
        i["athlete_id"] = "0"  # l'identifiant saisi n'est pas celui de cette clé: on retente avec l'athlète de la clé
        try:
            name = intervals_check(cfg)
        except UploadError as e2:
            LOG.warning("Intervals.icu a aussi refusé l'athlète « 0 »: %s", e2)
            i["athlete_id"] = given
            raise _explain(e2) from None
        LOG.info("Intervals.icu accepte la clé avec l'athlète « 0 » (l'identifiant saisi était refusé)")
        return name, f"L'identifiant d'athlète « {given} » a été refusé par Intervals.icu ; la clé est bonne, l'identifiant « 0 » (l'athlète de la clé) est utilisé."


def _explain(e):
    m = re.search(r"HTTP (\d+)", str(e))
    if "1010" in str(e):
        return UploadError("Intervals.icu (via Cloudflare) a bloqué la requête du programme (code 1010), ce n'est pas un problème de clé. "
                           "Réessayez; si cela continue, créez un rapport de bug depuis l'onglet Compteur.")
    if m and m.group(1) in ("401", "403"):
        return UploadError(f"Intervals.icu a refusé la clé d'API (HTTP {m.group(1)}). Vérifiez qu'il s'agit bien de la clé générée dans Réglages > Développeur sur intervals.icu "
                           "(pas de l'identifiant client ni du secret), copiée en entier sans espace. Laissez l'identifiant d'athlète à 0.")
    return e


def intervals_upload(cfg, path, name=None):
    i = cfg.data["intervals"]
    if not i.get("api_key"):
        raise UploadError("Intervals.icu n'est pas configuré.")
    fields = {"name": name} if name else {}
    body, ctype = _multipart(fields, "file", os.path.basename(path), open(path, "rb").read())
    req = urllib.request.Request(f"{ENDPOINTS['intervals']}/athlete/{i.get('athlete_id', '0')}/activities", data=body, method="POST",
                                 headers=_headers(i["api_key"], {"Content-Type": ctype}))
    status, res = _request(req, timeout=120)
    acts = res if isinstance(res, list) else res.get("activities", [res])
    return {"status": status, "created": status == 201, "ids": [a.get("id") for a in acts if isinstance(a, dict)]}
