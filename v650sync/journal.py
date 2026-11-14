"""Journal du programme: un fichier horodaté avec rotation (%LOCALAPPDATA%\\V650Sync\\journal.txt), et un rapport de bug en un clic.

Ce qui est écrit: démarrage et arrêt, chaque appel de la page (méthode, adresse, résultat, durée), les tâches de fond et leur déroulé,
les ouvertures de session et les écritures sur le compteur (chemins et tailles, jamais le contenu), Komoot (adresses et résultats),
la fabrication des cartes (commande et sortie complète d'Osmosis dans un fichier à part), les erreurs avec leur trace.
Ce qui n'est JAMAIS écrit: mots de passe, jetons, clé Intervals, contenu de USERID.BPB. Les adresses e-mail et les motifs
« mot de passe / jeton / clé = valeur » sont masqués, et le dossier personnel de Windows est remplacé par « ~ ».
Niveau détaillé (lectures fichier par fichier): variable d'environnement V650SYNC_DEBUG=1.
"""
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import zipfile

from . import __version__, paths

ROOT = "v650sync"
_READY = {"done": False}


class _Redact(logging.Filter):
    EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    SECRET = re.compile(r"(?i)\b(token|jeton|password|mot de passe|passwd|authorization|api[_ -]?key|cl[ée] api|secret)\b\s*[=:]\s*\S+")
    BASIC = re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._-]{8,}")

    def __init__(self):
        super().__init__()
        self.home = os.path.expanduser("~")

    def filter(self, rec):
        msg = rec.getMessage()
        if rec.exc_info:  # la trace est formatée plus tard: on la passe aussi dans le masque
            rec.exc_text = None
        msg = self.BASIC.sub(lambda m: m.group(1) + " ***", msg)
        msg = self.SECRET.sub(lambda m: m.group(1) + "=***", msg)
        msg = self.EMAIL.sub("***@***", msg)
        if self.home and len(self.home) > 3:
            msg = msg.replace(self.home, "~")
        rec.msg, rec.args = msg, ()
        return True


class _Formatter(logging.Formatter):
    def formatException(self, ei):  # trace masquée comme le reste
        text = super().formatException(ei)
        home = os.path.expanduser("~")
        return text.replace(home, "~") if home and len(home) > 3 else text


def path():
    return os.path.join(paths.settings_dir(), "journal.txt")


def get(name):
    return logging.getLogger(f"{ROOT}.{name}")


def mask_serial(s):
    s = str(s or "")
    return ("…" + s[-4:]) if len(s) > 6 else s


def setup(data_dir=None):
    """Ouvre le journal (une seule fois par exécution) et y écrit l'en-tête: version, système, outils."""
    if _READY["done"]:
        return get("journal")
    os.makedirs(paths.settings_dir(), exist_ok=True)
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    h = logging.handlers.RotatingFileHandler(path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setLevel(logging.DEBUG if os.environ.get("V650SYNC_DEBUG") else logging.INFO)
    h.setFormatter(_Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    h.addFilter(_Redact())
    root.addHandler(h)
    _READY["done"] = True
    log = get("journal")
    log.info("=" * 72)
    for line in header(data_dir).splitlines():
        log.info(line)

    def hook(exc_type, exc, tb):
        log.error("Exception non rattrapée", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def thook(args):
        log.error("Exception non rattrapée dans le fil %s", args.thread.name if args.thread else "?", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook, threading.excepthook = hook, thook
    return log


def header(data_dir=None):
    import shutil

    lines = [f"V650 Sync {__version__} · Python {platform.python_version()} · {platform.platform()}",
             f"exécutable: {'oui (PyInstaller)' if getattr(sys, 'frozen', False) else 'non (python -m v650sync)'} · {sys.executable}"]
    if data_dir:
        try:
            free = shutil.disk_usage(data_dir if os.path.isdir(data_dir) else os.path.dirname(data_dir)).free / 1e9
            lines.append(f"dossier de données: {data_dir} · espace libre {free:.1f} Go")
        except OSError:
            lines.append(f"dossier de données: {data_dir}")
    wv = [p for p in (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application", r"C:\Program Files\Microsoft\EdgeWebView\Application") if os.path.isdir(p)]
    edge = [p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application", r"C:\Program Files\Microsoft\Edge\Application") if os.path.isdir(p)]
    lines.append(f"WebView2: {'présent' if wv else 'absent'} · Edge: {'présent' if edge else 'absent'}")
    try:
        from . import mapgen, mapservice

        st = mapgen.tools_status(mapservice.tools_dir())
        lines.append("outils de carte: " + ", ".join(f"{i['label']} {'ok' if i['installed'] else 'absent'}" for i in st["items"]))
    except Exception as e:  # noqa: BLE001
        lines.append(f"outils de carte: état illisible ({e})")
    return "\n".join(lines)


class LogStream:
    """Remplace sys.stdout / sys.stderr quand il n'y a pas de console (exécutable): chaque ligne imprimée va au journal."""

    def __init__(self, level=logging.INFO):
        self.level, self.log, self.buf = level, get("console"), ""

    def write(self, s):
        self.buf += s if isinstance(s, str) else s.decode("utf-8", "replace")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.log.log(self.level, line.rstrip())
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def make_report(dirs):
    """Rapport de bug: un zip dans <données>/rapports avec le journal, un résumé du système et la fin des journaux d'Osmosis.

    Pas de coordonnées GPS, pas de mots de passe, pas de jetons. Contient des chemins de fichiers et des noms de parcours:
    à relire avant de l'envoyer."""
    out_dir = os.path.join(dirs.root, "rapports")
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"rapport-v650sync-{time.strftime('%Y%m%d-%H%M%S')}.zip")
    red = _Redact()

    def clean(text):
        rec = logging.LogRecord("x", logging.INFO, "", 0, text.replace("%", "%%"), (), None)
        red.filter(rec)
        return rec.msg.replace("%%", "%")

    summary = [header(dirs.root)]
    try:
        from . import mapservice, routelib

        data = mapservice.load(dirs)
        summary.append(f"cartes fabriquées: {len(data['maps'])} · installée: {'oui' if data['installed'] else 'non'} · écriture en cours: {'oui' if data['pending_write'] else 'non'}")
        for m in data["maps"]:
            summary.append(f"  carte {m['id']}: {m.get('size', 0) / 1e6:.0f} Mo, type {m.get('kind')}, détail {m.get('profile', '?')}, créée {m.get('created')}")
        items = routelib.build(dirs)
        summary.append(f"parcours: {len(items)} (sur le compteur {sum(1 for i in items if i['on_device'])}, synchronisés {sum(1 for i in items if i['status'] == 'sync')}, "
                       f"non synchronisés {sum(1 for i in items if i['status'] == 'diff')})")
    except Exception as e:  # noqa: BLE001
        summary.append(f"état des cartes et des parcours illisible: {e}")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("systeme.txt", clean("\n".join(summary)))
        base = path()
        for name in [base] + [f"{base}.{i}" for i in (1, 2, 3)]:
            if os.path.isfile(name):
                with open(name, encoding="utf-8", errors="replace") as f:
                    z.writestr(os.path.basename(name), clean(f.read()))
        logs = sorted((os.path.join(r, f) for r, _, fs in os.walk(os.path.join(dirs.root, "cartes")) for f in fs if f == "osmosis.log"), key=os.path.getmtime)[-3:]
        for lg in logs:
            with open(lg, encoding="utf-8", errors="replace") as f:
                z.writestr(f"osmosis-{os.path.basename(os.path.dirname(lg))}.log", clean(f.read()[-300_000:]))
    get("journal").info("Rapport de bug créé: %s", dest)
    return dest
