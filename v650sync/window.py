"""Fenêtre d'application: la page s'ouvre dans une fenêtre sans onglets ni barre d'adresse (mode « application » d'Edge ou de Chrome).

Edge est présent sur tous les Windows 10 et 11. La fenêtre a son propre profil (dossier à part): fermer la fenêtre ferme ce
processus, et le programme s'arrête aussitôt (il referme proprement la session du compteur).
Sans Edge ni Chrome, on retombe sur le navigateur par défaut.
"""
import os
import shutil
import subprocess

from .paths import settings_dir


def find_browser():
    roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for r in rel:
        for root in roots:
            if root and os.path.isfile(os.path.join(root, r)):
                return os.path.join(root, r)
    return shutil.which("msedge") or shutil.which("chrome")


def open_app_window(url, size=(1280, 860)):
    """Ouvre la fenêtre. Retourne le processus, ou None si aucun navigateur compatible n'a été trouvé."""
    exe = find_browser()
    if not exe:
        return None
    profile = os.path.join(settings_dir(), "fenetre")
    os.makedirs(profile, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", f"--window-size={size[0]},{size[1]}",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebarV2",
            "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        return None
