"""Point d'entrée de l'exécutable Windows (sans console): un double-clic ouvre la fenêtre du programme.

La ligne de commande reste disponible avec « python -m v650sync ... ». Les erreurs sont écrites dans
%LOCALAPPDATA%\\V650Sync\\journal.txt et signalées dans une boîte de message.
"""
import os
import sys
import traceback


def _log_path():
    from v650sync.paths import settings_dir

    os.makedirs(settings_dir(), exist_ok=True)
    return os.path.join(settings_dir(), "journal.txt")


def _alert(text):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "V650 Sync", 0x10)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        from v650sync import journal

        journal.setup()  # journal horodaté avec rotation (4 × 1 Mo), données sensibles masquées
        if sys.stdout is None or sys.stderr is None:  # exécutable sans console: print() et les erreurs vont dans le journal
            sys.stdout = journal.LogStream()
            sys.stderr = journal.LogStream(40)
    except Exception:  # noqa: BLE001 - le journal ne doit jamais empêcher le démarrage
        if sys.stdout is None or sys.stderr is None:
            sys.stdout = sys.stderr = open(_log_path(), "a", encoding="utf-8", buffering=1)
    try:
        from v650sync.__main__ import main
        from v650sync.pftp import PftpError

        try:
            main()
        except PftpError as e:
            _alert(f"Erreur: {e}")
            sys.exit(f"Erreur: {e}")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            from v650sync import journal

            journal.get("journal").error("Arrêt sur erreur", exc_info=True)
        except Exception:  # noqa: BLE001
            pass
        _alert("Le programme a rencontré une erreur. Détails dans :\n" + _log_path())
        sys.exit(1)
