"""Parcours depuis Komoot, et envoi de séances vers Komoot (bibliothèque standard uniquement).

La logique (adresses, authentification, pagination, export GPX) est reprise de la bibliothèque Python « kompy »
(https://github.com/Tsadoq/kompy, licence MIT, Copyright (c) 2023-2026 Matteo Villosio), réécrite sans dépendance.
Elle utilise l'API interne de Komoot (api.komoot.de), non documentée et non officielle: elle peut changer sans préavis.
Usage prévu: votre propre compte uniquement, à un rythme raisonnable, dans le respect des conditions d'utilisation de Komoot.
Ce programme n'est ni affilié à Komoot ni approuvé par Komoot.

Le mot de passe n'est utilisé que pour la connexion puis oublié: seul le jeton renvoyé par Komoot (équivalent d'un mot de passe)
est conservé, chiffré par Windows. Rien de ce module n'écrit le mot de passe ou le jeton dans un journal ou un message d'erreur.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, journal

LOG = journal.get("komoot")
API_BASE = "https://api.komoot.de"
MAX_GPX_BYTES = 25 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 10  # 1000 tours au plus
USER_AGENT = f"V650Sync/{__version__} (+non officiel; compte personnel)"


class KomootError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # jamais de redirection: l'en-tête d'authentification ne doit pas suivre vers un autre site
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _base():
    """Adresse de l'API. Seul un serveur local (http://127.0.0.1...) peut la remplacer (tests)."""
    import os

    override = os.environ.get("V650SYNC_KOMOOT_URL", "")
    return override.rstrip("/") if override.startswith("http://127.0.0.1") else API_BASE


def _request(path, auth, accept, query=None, retries=2, limit=2_000_000):
    url = _base() + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    shown = re.sub(r"/email/[^/]+/", "/email/***/", path)  # jamais d'e-mail, de mot de passe ni de jeton dans le journal
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            with _opener.open(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = r.read(limit + 1)
                LOG.info("GET %s -> %s, %d octets (%.1f s)", shown, r.status, len(data), time.time() - t0)
                if len(data) > limit:
                    raise KomootError("réponse de Komoot trop volumineuse")
                return data
        except urllib.error.HTTPError as e:
            LOG.warning("GET %s -> HTTP %s (essai %d/%d)", shown, e.code, attempt + 1, retries + 1)
            if e.code in (401, 403):
                raise KomootError("Komoot a refusé la connexion (identifiants ou jeton invalides).") from None
            if e.code in (429, 502, 503, 504) and attempt < retries:
                try:
                    wait = min(float(e.headers.get("Retry-After", 2)), 8)
                except ValueError:
                    wait = 2
                time.sleep(wait)
                continue
            if e.code == 429:
                raise KomootError("Komoot limite le nombre de requêtes: réessayez dans quelques minutes.") from None
            if e.code == 404:
                raise KomootError("Élément introuvable sur Komoot.") from None
            raise KomootError(f"Komoot a répondu par une erreur (HTTP {e.code}).") from None
        except urllib.error.URLError as e:
            LOG.warning("GET %s -> injoignable: %s (essai %d/%d)", shown, e.reason, attempt + 1, retries + 1)
            if attempt < retries:
                time.sleep(1)
                continue
            raise KomootError(f"Komoot est injoignable ({e.reason}). Vérifiez votre connexion internet.") from None
    raise KomootError("Komoot n'a pas répondu.")


def login(email, password):
    """Connexion avec l'e-mail et le mot de passe. Retourne {"username", "token"} (le mot de passe n'est pas conservé)."""
    email = (email or "").strip()
    if not email or not password or "@" not in email:
        raise KomootError("Saisissez l'adresse e-mail et le mot de passe de votre compte Komoot.")
    raw = _request(f"/v006/account/email/{urllib.parse.quote(email, safe='')}/", (email, password), "application/hal+json, application/json", retries=1)
    try:
        payload = json.loads(raw)
        username, token = str(payload["username"]), str(payload["password"])  # chez Komoot, le jeton s'appelle « password »
    except (ValueError, KeyError, TypeError):
        raise KomootError("Réponse inattendue de Komoot: l'API a peut-être changé.") from None
    return {"username": username, "token": token}


def list_tours(username, token, kind="tour_planned"):
    """Tours enregistrés du compte: [{id, name, sport, km, date, up, down, duration_s}], les plus récents d'abord."""
    if kind not in ("tour_planned", "tour_recorded"):
        raise KomootError("type de tour inconnu")
    out = []
    for page in range(MAX_PAGES):
        raw = _request(f"/v007/users/{urllib.parse.quote(str(username), safe='')}/tours/", (username, token), "application/hal+json, application/json",
                       {"type": kind, "limit": PAGE_SIZE, "page": page, "sort_field": "date", "sort_direction": "desc"})
        try:
            payload = json.loads(raw)
            tours = (payload.get("_embedded") or {}).get("tours") or []
        except (ValueError, AttributeError):
            raise KomootError("Réponse inattendue de Komoot: l'API a peut-être changé.") from None
        for t in tours:
            try:
                out.append({"id": int(t["id"]), "name": str(t.get("name") or f"Tour {t['id']}"), "sport": t.get("sport"),
                            "km": round(float(t.get("distance") or 0) / 1000, 1), "date": str(t.get("date") or "")[:10],
                            "up": round(float(t.get("elevation_up") or 0)), "down": round(float(t.get("elevation_down") or 0)),
                            "duration_s": int(t.get("duration") or 0), "changed": str(t.get("changed_at") or "")})
            except (KeyError, ValueError, TypeError):
                continue  # tour illisible: ignoré (comme kompy)
        info = payload.get("page") or {}
        has_next = (info.get("number", page) + 1 < info.get("totalPages", 0)) if info else bool((payload.get("_links") or {}).get("next"))
        if not tours or not has_next:
            break
    return out


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _send(method, path, auth, body=None, query=None, content_type=None, timeout=120):
    """Requête qui MODIFIE le compte (envoi d'une séance): jamais rejouée automatiquement. Retourne (code HTTP, octets)."""
    url = _base() + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Accept": "application/hal+json, application/json", "User-Agent": USER_AGENT,
               "Authorization": "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        t0 = time.time()
        with _opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=timeout) as r:
            data = r.read(2_000_000)
            LOG.info("%s %s -> %s, %d octets envoyés, %d reçus (%.1f s)", method, path, r.status, len(body or b""), len(data), time.time() - t0)
            return r.status, data
    except urllib.error.HTTPError as e:
        snippet = (e.read(300) or b"").decode("utf-8", "replace").replace("\n", " ")
        LOG.warning("%s %s -> HTTP %s %s", method, path, e.code, snippet[:160])
        if e.code in (401, 403):
            raise KomootError("Komoot a refusé la connexion (jeton invalide ou expiré). Reconnectez-vous dans l'onglet Connexions.") from None
        if e.code == 429:
            raise KomootError("Komoot limite le nombre de requêtes: réessayez dans quelques minutes.") from None
        if e.code in (400, 415, 422):
            raise KomootError(f"Komoot a refusé ce fichier (HTTP {e.code}). Vous pouvez l'importer à la main depuis komoot.com.") from None
        raise KomootError(f"Komoot a répondu par une erreur (HTTP {e.code}).") from None
    except urllib.error.URLError as e:
        LOG.warning("%s %s -> injoignable: %s", method, path, e.reason)
        raise KomootError(f"Komoot est injoignable ({e.reason}). Vérifiez votre connexion internet.") from None


def upload_activity(username, token, data, name, data_type="fit", sport="racebike"):
    """Envoie une séance enregistrée (FIT, GPX ou TCX) sur le compte Komoot, comme `kompy.upload_tour`.

    Komoot donne à la séance la visibilité par défaut de VOTRE compte (réglage de Komoot, comme avec Polar Flow): l'application n'y touche pas.
    Si Komoot connaissait déjà ce fichier (réponse 202), rien n'est créé ni modifié. Retourne {"tour_id", "created"}."""
    if data_type not in ("fit", "gpx", "tcx"):
        raise KomootError("format de fichier inconnu")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise KomootError("fichier vide ou trop volumineux pour Komoot")
    code, raw = _send("POST", "/v007/tours/", (username, token), body=data, content_type="application/octet-stream",
                      query={"data_type": data_type, "sport": sport, "name": str(name)[:100]})
    try:
        tour_id = int(json.loads(raw)["id"])
    except (ValueError, KeyError, TypeError):
        raise KomootError("Réponse inattendue de Komoot: l'API a peut-être changé. Vérifiez sur komoot.com si la séance est arrivée.") from None
    created = code != 202
    LOG.info("Séance envoyée à Komoot: tour %s (%s)", tour_id, "créé" if created else "déjà présent")
    return {"tour_id": tour_id, "created": created}


def get_gpx(username, token, tour_id):
    """GPX d'un tour (octets)."""
    raw = _request(f"/v007/tours/{int(tour_id)}.gpx", (username, token), "application/gpx+xml", limit=MAX_GPX_BYTES)
    if b"<gpx" not in raw[:2000]:
        raise KomootError("Komoot n'a pas renvoyé un fichier GPX valide.")
    return raw


def gpx_filename(tour_id, name, date=""):
    """Nom de fichier du type de ceux de l'export Komoot (date, identifiant, nom), sans caractère interdit sous Windows."""
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "").strip(" .")[:80] or "tour"
    return f"{date or 'komoot'}_{int(tour_id)}_{clean} - komoot.gpx"
