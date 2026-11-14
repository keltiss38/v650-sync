"""Où le programme range ses données, et réglages propres au PC (hors des données du compteur)."""
import json
import os


def settings_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "V650Sync")


def _settings_file():
    return os.path.join(settings_dir(), "settings.json")


def load_settings():
    try:
        with open(_settings_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    os.makedirs(settings_dir(), exist_ok=True)
    with open(_settings_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def default_data_dir():
    """Dossier de données: variable V650SYNC_DATA, sinon le dossier choisi dans la page, sinon ~/V650 Sync.

    Reprise d'un ancien emplacement: si ~/Polar V650/sauvegarde existe déjà et pas ~/V650 Sync, on continue de l'utiliser.
    """
    home = os.path.expanduser("~")
    env = os.environ.get("V650SYNC_DATA")
    if env:
        return env
    chosen = load_settings().get("data_dir")
    if chosen:
        return chosen
    new, old = os.path.join(home, "V650 Sync"), os.path.join(home, "Polar V650")
    return old if os.path.isdir(os.path.join(old, "sauvegarde")) and not os.path.isdir(new) else new


def is_first_run(root):
    """Premier lancement: aucun dossier choisi ni variable d'environnement, et le dossier par défaut n'existe pas encore."""
    return not os.environ.get("V650SYNC_DATA") and not load_settings().get("data_dir") and not os.path.isdir(root)


def pick_folder(start):
    """Boîte de dialogue Windows « Choisir un dossier » (via PowerShell, présent sur Windows 10 et 11). Retourne le chemin ou None."""
    import subprocess

    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;$o.ShowInTaskbar=$false;"
              "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
              "$f.Description='Dossier où ranger les copies du compteur, les exports et la configuration';"
              "$f.ShowNewFolderButton=$true;$d=$env:V650_START;"
              "while($d -and -not (Test-Path -LiteralPath $d)){$d=Split-Path -Parent $d};if($d){$f.SelectedPath=$d};"
              "if($f.ShowDialog($o) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;Write-Output $f.SelectedPath}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", script], capture_output=True,
                             env={**os.environ, "V650_START": start or ""}, timeout=600,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.decode("utf-8", "ignore").strip()
    return text or None


def set_data_dir(path):
    """Mémorise le dossier de données choisi (chemin absolu, créé si besoin). Retourne le chemin normalisé."""
    path = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
    os.makedirs(path, exist_ok=True)
    s = load_settings()
    s["data_dir"] = path
    save_settings(s)
    return path
