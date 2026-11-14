"""Vérification des mises à jour: demande à GitHub quelle est la dernière version publiée (une seule requête GET anonyme, au plus une fois par jour).

Rien n'est téléchargé ni remplacé: la page affiche seulement un petit repère « mise à jour disponible » qui mène à la page de la version sur GitHub.
Aucune donnée personnelle n'est envoyée (pas d'identifiant, pas de chemin; juste le nom du programme et son numéro de version dans l'en-tête User-Agent).
Désactivable dans l'onglet Compteur, section « À propos » (réglage « update_check » du fichier settings.json).
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import __version__, journal, paths

LOG = journal.get("maj")
REPO = "keltiss38/v650-sync"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ONE_DAY = 24 * 3600


def parse_version(text):
    """« v1.2.3 », « 1.2 » ou « 1.2.3-beta » -> (1, 2, 3) ; None si ce n'est pas un numéro de version."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})(?:[-+].*)?\s*$", str(text or ""))
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest, current=__version__):
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def safe_url(url):
    """Seule une page de versions du dépôt est acceptée (le contenu vient d'Internet, jamais ouvert tel quel)."""
    return url if isinstance(url, str) and url.startswith(RELEASES_URL) and " " not in url else RELEASES_URL


def fetch_latest(timeout=8):
    """(tag, url) de la dernière version publiée, ou (None, None) s'il n'y en a pas encore (dépôt privé ou sans version)."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": f"V650Sync/{__version__}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(200_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (403, 404):  # 404: aucune version publiée (ou dépôt privé); 403: limite de requêtes
            return None, None
        raise
    tag = str(data.get("tag_name") or "")
    return (tag, safe_url(data.get("html_url"))) if parse_version(tag) else (None, None)


def state(settings=None):
    """État enregistré, sans réseau: ce que la page affiche."""
    s = settings if settings is not None else paths.load_settings()
    latest = s.get("update_latest") or ""
    return {"enabled": s.get("update_check", True) is not False, "current": __version__, "last": s.get("update_last") or 0,
            "latest": latest, "available": bool(latest) and is_newer(latest), "url": safe_url(s.get("update_url"))}


def check(force=False):
    """Interroge GitHub si c'est activé et dû (ou si force). Ne lève jamais: en cas d'échec, l'état précédent est gardé."""
    s = paths.load_settings()
    st = state(s)
    if not force and (not st["enabled"] or time.time() - float(st["last"] or 0) < ONE_DAY):
        return st
    try:
        tag, url = fetch_latest()
    except (OSError, ValueError) as e:  # réseau absent, délai dépassé, réponse illisible
        LOG.info("Vérification des mises à jour impossible: %s", e)
        st["error"] = True
        return st
    s["update_last"] = time.time()
    s["update_latest"] = tag or ""
    s["update_url"] = url or ""
    paths.save_settings(s)
    LOG.info("Dernière version publiée: %s (actuelle: %s)", tag or "aucune", __version__)
    return state(s)


def set_enabled(enabled):
    s = paths.load_settings()
    s["update_check"] = bool(enabled)
    paths.save_settings(s)
    return state(s)
