"""Sauvegarde en lecture seule: copie les fichiers du V650 dans un dossier local, sans rien écrire sur l'appareil."""
import os
import re

from .pftp import PftpError

SESSION_FILE = re.compile(r"^/U/\d+/\d{8}/E/\d{6}/")

DEFAULT_ROOTS = ("/U/",)
SINGLE_FILES = ("/DEVICE.BPB",)
# Cartes hors-ligne (un fichier de ~325 Mo sur ce V650): FlowSync ne les lit pas, on les ignore par défaut.
SKIP_PREFIXES = ("/U/0/MAP/",)
TRUST_SIZE_ABOVE = 64 * 1024  # au-delà, un fichier local de même taille est considéré à jour (cartes, séances)


def _local_path(dest, remote):
    parts = [p for p in remote.split("/") if p]
    if any(p in (".", "..") or ":" in p or "\\" in p for p in parts):
        raise PftpError(f"chemin distant refusé: {remote!r}")
    return os.path.join(dest, *parts)


def _progress(remote, size, log):
    """Affiche l'avancement tous les 10 % pour les très gros fichiers (cartes)."""
    if not size or size < 20_000_000:
        return None
    state = {"next": 10}

    def cb(done):
        pct = done * 100 // size
        if pct >= state["next"]:
            log(f"    {remote}: {pct} % ({done // 1_000_000} Mo)")
            state["next"] = pct // 10 * 10 + 10

    return cb


def walk(client, root, skip_dir=None):
    """Parcourt un dossier distant, produit (chemin, taille) des fichiers. `skip_dir(chemin)` vrai: dossier ignoré."""
    for name, size in client.list_dir(root):
        path = name if name.startswith("/") else root + name
        if path.endswith("/"):
            if skip_dir and skip_dir(path):
                continue
            yield from walk(client, path, skip_dir)
        else:
            yield path, size


COMPLETE = ".complete"  # marqueur dans une séance déjà copiée en entier: inutile de la relister (un listing coûte ~0,16 s)
SESSION_DIR = re.compile(r"^/U/\d+/\d{8}/E/\d{6}/$")


def backup(client, dest, roots=DEFAULT_ROOTS, log=print, skip_prefixes=SKIP_PREFIXES, progress=None, should_stop=None):
    stats = {"new": 0, "skipped": 0, "bytes": 0, "errors": 0}
    items = [(p, None) for p in SINGLE_FILES]
    def known_session(path):
        return bool(SESSION_DIR.match(path)) and os.path.isfile(os.path.join(_local_path(dest, path), COMPLETE))

    for root in roots:
        items.extend(it for it in walk(client, root, known_session) if not it[0].startswith(tuple(skip_prefixes)))
    log(f"{len(items)} fichiers à vérifier sur le compteur")
    stopped = False
    for n, (remote, size) in enumerate(items, 1):
        if should_stop and should_stop():
            # arrêt demandé (fermeture de l'application): on s'arrête entre deux fichiers, la connexion est refermée proprement
            log("Lecture interrompue à la demande de l'utilisateur.")
            stopped = True
            break
        if progress:
            progress(n, len(items))
        local = _local_path(dest, remote)
        # La taille seule ne prouve pas qu'un petit fichier (.BPB) n'a pas changé (favoris, profils: même taille, autre
        # contenu). On ne se fie donc à la taille que pour les gros fichiers; les autres sont relus et comparés.
        # Les fichiers d'une séance terminée (/U/0/AAAAMMJJ/E/HHMMSS/...) ne changent plus: la taille suffit aussi (gain de temps)
        trusted = size is not None and (size >= TRUST_SIZE_ABOVE or SESSION_FILE.match(remote))
        if trusted and os.path.isfile(local) and os.path.getsize(local) == size:
            stats["skipped"] += 1
            continue
        try:
            data = client.get_file(remote, progress=_progress(remote, size, log))
        except (PftpError, OSError) as e:
            # après une erreur de flux, la session n'est plus synchronisée: on s'arrête proprement
            log(f"  erreur sur {remote} (taille annoncée {size}): {type(e).__name__}: {e}")
            stats["errors"] += 1
            break
        if size is not None and len(data) != size:
            log(f"  taille inattendue pour {remote}: reçu {len(data)}, annoncé {size}")
            stats["errors"] += 1
            continue
        if os.path.isfile(local) and os.path.getsize(local) == len(data):
            with open(local, "rb") as f:
                if f.read() == data:
                    stats["skipped"] += 1
                    continue
        os.makedirs(os.path.dirname(local), exist_ok=True)
        tmp = local + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, local)
        stats["new"] += 1
        stats["bytes"] += len(data)
        log(f"  {remote} ({len(data)} octets)")
    if not stopped and stats["errors"] == 0:  # copie complète et sans erreur: on marque les séances pour ne plus les relister
        for d in {m.group(0) for r, _ in items if (m := re.match(r"^/U/\d+/\d{8}/E/\d{6}/", r))}:
            marker = os.path.join(_local_path(dest, d), COMPLETE)
            if os.path.isdir(os.path.dirname(marker)) and not os.path.isfile(marker):
                open(marker, "w").close()
    return stats
