"""Ligne de commande: python -m v650sync {info,ls,backup,export,routes,route-from-gpx,put-route,remove-route}.

Seuls put-route et remove-route écrivent sur le compteur, et uniquement avec --yes."""
import argparse
import os
import random
import sys
import time

from . import __version__, route
from .backup import SKIP_PREFIXES, backup
from .discover import connect_any, find_devices
from .export import export_all
from .pftp import PftpError, parse_strings


from .paths import default_data_dir  # noqa: E402


def _open():
    print("Recherche du V650 (mDNS)...")
    devices = find_devices()
    if not devices:
        raise PftpError("aucun V650 trouvé. Vérifiez le câble USB et que le compteur est allumé.")
    print("Services vus: " + ", ".join(f"{d.name.split('._polar-ftp')[0]}:{d.port}" for d in devices))
    dev, client = connect_any(devices)
    print(f"Connecté à {dev.name.split('._polar-ftp')[0]} (port {dev.port}): {client.banner}")
    return dev, client


def main(argv=None):
    p = argparse.ArgumentParser(prog="v650sync", description="Synchronisation du Polar V650 (lecture seule, sauf put-route et remove-route avec --yes)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")  # sans commande (double-clic sur l'exe): ouvre la page web
    sub.add_parser("info", help="infos sur l'appareil")
    ls = sub.add_parser("ls", help="liste un dossier du compteur")
    ls.add_argument("path", nargs="?", default="/U/0/")
    bk = sub.add_parser("backup", help="copie les fichiers du compteur")
    bk.add_argument("--dest", default=os.path.join(os.path.expanduser("~"), "Polar V650 sauvegarde"))
    bk.add_argument("--maps", action="store_true", help="inclut aussi la carte hors ligne (environ 325 Mo)")
    ex = sub.add_parser("export", help="exporte en GPX les séances déjà copiées (sans le compteur)")
    ex.add_argument("--src", required=True, help="dossier de sauvegarde du compteur (celui qui contient U/0/...)")
    ex.add_argument("--out", required=True, help="dossier de destination des fichiers GPX")
    rg = sub.add_parser("route-from-gpx", help="convertit un GPX en fichier PROUTE.BPB (sans le compteur)")
    rg.add_argument("gpx")
    rg.add_argument("--out", required=True, help="fichier PROUTE.BPB à écrire")
    rg.add_argument("--name", help="nom affiché sur le compteur (par défaut: nom du GPX)")
    sub.add_parser("routes", help="liste les itinéraires du compteur (lecture seule)")
    pr = sub.add_parser("put-route", help="AJOUTE un itinéraire GPX sur le compteur (ÉCRIT sur le compteur)")
    pr.add_argument("gpx")
    pr.add_argument("--name")
    pr.add_argument("--max-points", type=int, default=route.MAX_POINTS,
                    help=f"nombre maximal de points du tracé (défaut {route.MAX_POINTS}, comme FlowSync; 723 points testés sans plantage)")
    pr.add_argument("--hd", action="store_true", help="tracé le plus détaillé qui tient dans une trame d'écriture (environ 800 à 900 points)")
    pr.add_argument("--yes", action="store_true", help="confirme l'écriture sur le compteur")
    rr = sub.add_parser("remove-route", help="SUPPRIME un itinéraire du compteur (ÉCRIT sur le compteur)")
    rr.add_argument("slot", type=int)
    rr.add_argument("--fav", type=int, help="supprime aussi le favori FAV/NN correspondant")
    rr.add_argument("--yes", action="store_true", help="confirme la suppression")
    st = sub.add_parser("set-time", help="RÈGLE l'heure du compteur sur celle du PC (ÉCRIT sur le compteur)")
    st.add_argument("--yes", action="store_true", help="confirme")
    ui = sub.add_parser("ui", help="ouvre la page web locale (séances, parcours, Intervals.icu, compteur)")
    ui.add_argument("--data", default=default_data_dir(), help="dossier de données (sauvegarde du compteur, exports, config)")
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--keep-alive", action="store_true", help="ne pas se fermer tout seul quand l'onglet est fermé")
    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv or ["ui"])  # sans argument (double-clic sur l'exe): ouvre la page web

    if args.cmd == "set-time":
        from . import service
        if not args.yes:
            print("Rien n'a été changé. Ajoutez --yes pour régler l'heure du compteur sur celle du PC.")
            return
        print(service.set_clock())
        return
    if args.cmd == "ui":
        from .webui import serve
        serve(args.data, open_browser=not args.no_browser, port=args.port, keep_alive=args.keep_alive)
        return
    if args.cmd == "export":
        export_all(args.src, args.out)
        return
    if args.cmd == "route-from-gpx":
        r, tol = route.gpx_to_route(args.gpx, name=args.name)
        blob = route.encode(r)
        with open(args.out, "wb") as f:
            f.write(blob)
        print(f"{r['name']!r}: {len(r['points'])} points (tolérance {tol:.1f} m), {len(blob)} octets, longueur {r['length'] / 1000:.1f} km -> {args.out}")
        return

    dev, client = _open()
    try:
        if args.cmd == "routes":
            for entry, _ in client.list_dir("/U/0/ROUTES/"):
                raw = client.get_file(entry.rstrip("/") + "/PROUTE.BPB")
                r = route.decode(raw)
                print(f"  {entry}  {r['name']!r}  {len(r['points'])} points  {r['length'] / 1000:.1f} km")
        elif args.cmd == "put-route":
            def _num(entry):
                return entry.rstrip("/").rsplit("/", 1)[-1]

            slots = sorted(int(_num(e)) for e, _ in client.list_dir("/U/0/ROUTES/") if _num(e).isdigit())
            favs = sorted(int(_num(e)) for e, _ in client.list_dir("/U/0/FAV/") if _num(e).isdigit())
            # un dossier d'itinéraire vide (reste d'un essai raté) est réutilisé
            empty = [s for s in slots if not client.list_dir(f"/U/0/ROUTES/{s}/")]
            slot = empty[0] if empty else (max(slots) + 1 if slots else 0)
            fav = max(favs) + 1 if favs else 0
            if args.hd:
                r, tol = route.hd_route(args.gpx, name=args.name)
            else:
                r, tol = route.gpx_to_route(args.gpx, name=args.name, max_points=args.max_points)
            blob = route.encode(r)
            now = r["created"]
            files = {
                f"/U/0/ROUTES/{slot}/PROUTE.BPB": blob,
                f"/U/0/FAV/{fav:02d}/TST.BPB": route.encode_fav_tst(r["name"], r["route_id"]),
                f"/U/0/FAV/{fav:02d}/ID.BPB": route.encode_fav_id(random.randint(84_000_000, 84_999_999), now),
            }
            print(f"Ajout de {r['name']!r}: {len(r['points'])} points, longueur {r['length'] / 1000:.1f} km (id d'itinéraire {r['route_id']})")
            for path, data in files.items():
                print(f"  écrira {path} ({len(data)} octets)")
            if not args.yes:
                print("Rien n'a été écrit. Ajoutez --yes pour écrire sur le compteur.")
                return
            if slot not in slots:
                client.put(f"/U/0/ROUTES/{slot}/")
            client.put(f"/U/0/FAV/{fav:02d}/")
            for path, data in files.items():
                client.put(path, data)
            time.sleep(1.0)
            for path, data in files.items():
                back = client.get_file(path)
                print(f"  relu {path}:", "identique" if back == data else f"DIFFÉRENT ({len(back)} octets relus)")
        elif args.cmd == "remove-route":
            targets = [f"/U/0/ROUTES/{args.slot}"] + ([f"/U/0/FAV/{args.fav:02d}"] if args.fav is not None else [])
            print("Suppression de " + " et ".join(targets))
            if not args.yes:
                print("Rien n'a été supprimé. Ajoutez --yes pour confirmer.")
                return
            for target in targets:
                client.remove(target)
            print("Supprimé.")
        elif args.cmd == "info":
            for field, text in parse_strings(client.get_file("/DEVICE.BPB")):
                print(f"  champ {field}: {text}")
        elif args.cmd == "ls":
            for name, size in client.list_dir(args.path):
                print(f"{size:>10}  {name}")
        elif args.cmd == "backup":
            dest = os.path.join(args.dest, dev.serial)
            print(f"Destination: {dest}")
            s = backup(client, dest, skip_prefixes=() if args.maps else SKIP_PREFIXES)
            print(f"Terminé: {s['new']} copiés, {s['skipped']} déjà à jour, {s['errors']} erreurs, {s['bytes']} octets")
            big = s.get("too_big_files", [])
            if big:
                print(f"{len(big)} fichiers > 16 383 octets non copiés (lecture pas encore possible):")
                for path, size in big:
                    print(f"  {size:>9}  {path}")
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except PftpError as e:
        sys.exit(f"Erreur: {e}")
