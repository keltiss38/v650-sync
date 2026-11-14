"""Opérations de haut niveau partagées par la page web (webui) et la ligne de commande.

Le compteur ne répond qu'à un seul client à la fois: chaque opération ouvre une connexion, travaille, puis la referme
proprement (sinon il faut rebrancher le câble). `LOCK` sérialise les opérations.
"""
import os
import random
import re
import struct
import threading
import time
from contextlib import contextmanager

from . import export, journal, komoot, mapinfo, polar_decode as pd, route, uploads
from .backup import SKIP_PREFIXES, backup
from .discover import connect_any, find_devices
from .pftp import PftpError, parse_strings

LOG = journal.get("service")

LOCK = threading.Lock()
ABORT = threading.Event()  # demandé à la fermeture: les lectures longues s'arrêtent entre deux fichiers
_ACTIVE = set()  # sessions ouvertes sur le compteur, refermées proprement à l'arrêt


def shutdown(wait_s=20):
    """Arrêt propre: interrompt les lectures en cours, attend la fin de l'opération, puis ferme toute session restante."""
    ABORT.set()
    got = LOCK.acquire(timeout=wait_s)  # l'opération en cours rend le verrou quand elle a refermé sa session
    for c in list(_ACTIVE):
        c.close()
    _ACTIVE.clear()
    if got:
        LOCK.release()


class DataDirs:
    def __init__(self, root):
        self.root = root
        self.backup = os.path.join(root, "sauvegarde")
        self.export = os.path.join(root, "export")
        self.routes_in = os.path.join(root, "komoot")


_LAST_DEVICES = []  # dernier service mDNS qui a répondu: évite de refaire la recherche (2 s minimum) à chaque opération


def _open_device():
    if _LAST_DEVICES:
        try:
            return connect_any(_LAST_DEVICES, per_try=1.0)
        except PftpError:
            _LAST_DEVICES.clear()  # câble rebranché: le port a pu changer, on refait la recherche
    devices = find_devices(timeout=6.0)
    LOG.info("Recherche mDNS: %d appareil(s) trouvé(s)", len(devices))
    if not devices:
        raise PftpError("Aucun V650 trouvé. Vérifiez le câble USB et que le compteur est allumé.")
    dev, client = connect_any(devices)
    _LAST_DEVICES[:] = [dev]
    return dev, client


@contextmanager
def device():
    """Ouvre le compteur (mDNS + PFTP), cède (dev, client), referme toujours la connexion."""
    with LOCK:
        t0 = time.time()
        try:
            dev, client = _open_device()
        except Exception as e:
            LOG.error("Compteur injoignable (%.1f s): %s: %s", time.time() - t0, type(e).__name__, e)
            raise
        LOG.info("Session ouverte sur le compteur (%.1f s) · %s", time.time() - t0, client.banner)
        _ACTIVE.add(client)
        try:
            yield dev, client
        except Exception as e:
            LOG.error("Erreur pendant la session: %s: %s", type(e).__name__, e)
            raise
        finally:
            client.close()
            _ACTIVE.discard(client)
            LOG.info("Session refermée (%.1f s)", time.time() - t0)


def _status_from(dev, c):
    info = parse_strings(c.get_file("/DEVICE.BPB"))
    fields = {k: v for k, v in info}
    out = {"connected": True, "serial": dev.serial, "banner": c.banner,
           "model": fields.get(7, "Polar V650"), "device_id": fields.get(6), "firmware": fields.get(8)}
    try:
        m = pd.parse(c.query(8))
        out["battery"] = m.get(1, [None])[0]
        m = pd.parse(c.query(5))
        out["free_gb"] = round(m[3][0] * m[1][0] / 1e9, 2)
        out["total_gb"] = round(m[2][0] * m[1][0] / 1e9, 2)
    except Exception:
        pass
    return out


def status():
    """État du compteur: connecté ou non, infos, batterie, espace libre."""
    try:
        with device() as (dev, c):
            return _status_from(dev, c)
    except (PftpError, OSError) as e:
        return {"connected": False, "error": str(e)}


def _f32(b):
    return struct.unpack("<f", b)[0]


def read_profile():
    """Profil physique, préférences, heure et titulaire du compteur (lecture seule). Ni date de naissance, ni jeton."""
    with device() as (dev, c):
        return _profile_from(c)


def _profile_from(c):
    phys = pd.parse(c.get_file("/U/0/S/PHYSDATA.BPB"))
    prefs = pd.parse(c.get_file("/U/0/S/PREFS.BPB"))
    local = pd.parse(c.query(4))
    system = pd.parse(c.query(2))
    out = _profile_dict(phys, prefs, local, system)
    try:  # titulaire du compteur (affiché dans l'onglet Compteur seulement, jamais écrit dans le journal)
        out["owner"] = owner_from(c.get_file("/U/0/USERID.BPB"))
    except Exception:  # noqa: BLE001 - confort d'affichage, jamais bloquant
        out["owner"] = None
    try:  # état de la carte hors ligne (petit fichier); absent si aucune carte n'est installée
        out["map"] = mapinfo.as_dict(mapinfo.decode(c.get_file(mapinfo.PATH)))
        LOG.info("MAPINFO du compteur: installée %s UTC · %s", out["map"]["installed_utc"], "zone demandée, pas encore installée" if out["map"]["pending"] else "carte installée")
    except Exception:  # noqa: BLE001 - confort d'affichage, jamais bloquant
        out["map"] = None
    return out


def owner_from(data):
    """Titulaire du compteur d'après USERID.BPB: prénom (champ 5), nom (6), e-mail (2), identifiant Polar (1). Les autres champs
    (identifiant binaire du compte, date) ne sont pas lus. Ces valeurs ne vont que dans la page locale, jamais dans le journal."""
    f = pd.parse(data)

    def text(k):
        return f[k][0].decode("utf-8", "replace") if k in f and isinstance(f[k][0], (bytes, bytearray)) else ""

    pid = f[1][0] if 1 in f and isinstance(f[1][0], int) else None
    return {"first": text(5), "last": text(6), "email": text(2), "polar_id": pid}


def _profile_dict(phys, prefs, local, system):

    def hr(k):
        return pd.parse(phys[k][0])[1][0] if k in phys else None

    def clock(m):
        d, t = pd.parse(m[1][0]), pd.parse(m[2][0])
        return f"{d[1][0]}-{d[2][0]:02d}-{d[3][0]:02d} {t[1][0]:02d}:{t[2][0]:02d}:{t[3][0]:02d}"

    loc = pd.parse(prefs[1][0]) if 1 in prefs else {}
    from datetime import datetime, timezone

    dev_utc = datetime.strptime(clock(system), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return {
        "drift_s": round((dev_utc - datetime.now(timezone.utc)).total_seconds()),
        "weight_kg": round(_f32(pd.parse(phys[3][0])[1][0]), 1) if 3 in phys else None,
        "height_cm": round(_f32(pd.parse(phys[4][0])[1][0]), 1) if 4 in phys else None,
        "hr_max": hr(5), "hr_rest": hr(6), "vo2max": hr(10), "ftp": hr(15),
        "language": pd.parse(loc[1][0])[1][0].decode() if 1 in loc else None,
        "metric": loc.get(2, [None])[0] == 1,
        "clock_24h": loc.get(3, [None])[0] == 1,
        "local_time": clock(local), "system_time_utc": clock(system),
    }


def _vi(field, n):
    out = bytearray()
    out += bytes([field << 3])
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field, payload):
    return bytes([field << 3 | 2, len(payload)]) + payload


def _clock_params(dt, last):
    """PbPFtpSetSystemTimeParams (last = trusted) / PbPFtpSetLocalTimeParams (last = décalage horaire), comme FlowSync."""
    date = _vi(1, dt.year) + _vi(2, dt.month) + _vi(3, dt.day)
    time_ = _vi(1, dt.hour) + _vi(2, dt.minute) + _vi(3, dt.second) + _vi(4, dt.microsecond // 1000)
    return _ld(1, date) + _ld(2, time_) + _vi(3, last)


def set_clock():
    """RÈGLE l'heure du compteur sur celle du PC (requêtes 1 = heure système UTC, 3 = heure locale), puis relit l'heure."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    local_now = datetime.now().astimezone()
    offset_s = local_now.utcoffset().total_seconds()
    if offset_s % 3600:
        raise PftpError("décalage horaire non entier en heures: non géré")
    tz_hours = int(offset_s // 3600)
    with device() as (dev, c):
        c.query(1, _clock_params(now, 1))  # heure système (UTC), trusted
        c.query(3, _clock_params(local_now.replace(tzinfo=None), tz_hours))  # heure locale + décalage
    return {"set_to_local": local_now.strftime("%Y-%m-%d %H:%M:%S"), "utc_offset_hours": tz_hours,
            "device": read_profile()["local_time"]}


PHYS_PATH = "/U/0/S/PHYSDATA.BPB"
USER_PATH = "/U/0/USERID.BPB"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def _profile_backup_root(dirs):
    return os.path.join(dirs.root, "sauvegarde-profil")


def profile_backups(dirs):
    """Sauvegardes de profil disponibles (la plus récente d'abord): [{'stamp', 'files'}]."""
    root = _profile_backup_root(dirs)
    out = []
    for name in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
        d = os.path.join(root, name)
        if _STAMP.match(name) and os.path.isfile(os.path.join(d, "PHYSDATA.BPB")) and os.path.isfile(os.path.join(d, "USERID.BPB")):
            out.append({"stamp": name, "files": ["PHYSDATA.BPB", "USERID.BPB"]})
    return out


def _put_checked(c, items):
    """ÉCRIT [(chemin, ancien, nouveau)] sur le compteur, relit et compare. Si une écriture échoue ou si la relecture diffère,
    remet les fichiers déjà écrits comme avant. Renvoie la liste des fichiers écrits (chemins)."""
    written = []
    try:
        for path, old, new in items:
            if new != old:
                c.put(path, new)
                written.append((path, old))
        time.sleep(1.0)
        for path, old, new in items:
            if new != old and c.get_file(path) != new:
                raise PftpError(f"la relecture de {path} ne correspond pas à ce qui a été écrit")
    except Exception:
        for path, old in written:  # au mieux: on remet l'ancien contenu
            try:
                c.put(path, old)
                LOG.warning("Profil: %s remis dans son état d'avant", path)
            except Exception as e:  # noqa: BLE001
                LOG.error("Profil: impossible de remettre %s: %s: %s", path, type(e).__name__, e)
        raise
    return [p for p, _ in written]


def edit_profile(dirs, changes):
    """MODIFIE le profil du compteur (PHYSDATA.BPB et/ou USERID.BPB) après une copie des deux fichiers actuels dans
    `sauvegarde-profil/<date>/`, puis relit pour vérifier. `changes`: voir profileedit.clean. ÉCRIT SUR LE COMPTEUR."""
    from . import profileedit

    changes = profileedit.clean(changes)
    with device() as (dev, c):
        phys, user = c.get_file(PHYS_PATH), c.get_file(USER_PATH)
        new_phys, new_user, changed = profileedit.apply(phys, user, changes)
        if not changed:
            return {"changed": [], "note": "Rien à modifier : les valeurs sont déjà celles du compteur."}
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        d = os.path.join(_profile_backup_root(dirs), stamp)
        os.makedirs(d, exist_ok=True)
        for name, data in (("PHYSDATA.BPB", phys), ("USERID.BPB", user)):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        LOG.info("Profil du compteur: modification de %s (copie préalable dans sauvegarde-profil/%s)", ", ".join(changed), stamp)
        written = _put_checked(c, [(PHYS_PATH, phys, new_phys), (USER_PATH, user, new_user)])
        LOG.info("Profil du compteur modifié et relu: %s", ", ".join(written))
        return {"changed": changed, "backup": stamp, "verified": True,
                "note": "Éteignez puis rallumez le compteur pour voir le changement."}


def restore_profile(dirs, stamp=None):
    """REMET le profil d'une sauvegarde (la plus récente par défaut) sur le compteur, puis relit. ÉCRIT SUR LE COMPTEUR."""
    backups = profile_backups(dirs)
    if stamp is None:
        if not backups:
            raise PftpError("Aucune sauvegarde de profil trouvée.")
        stamp = backups[0]["stamp"]
    if not _STAMP.match(stamp) or stamp not in [b["stamp"] for b in backups]:
        raise PftpError("Sauvegarde de profil introuvable.")
    d = os.path.join(_profile_backup_root(dirs), stamp)
    with open(os.path.join(d, "PHYSDATA.BPB"), "rb") as f:
        old_phys = f.read()
    with open(os.path.join(d, "USERID.BPB"), "rb") as f:
        old_user = f.read()
    with device() as (dev, c):
        phys, user = c.get_file(PHYS_PATH), c.get_file(USER_PATH)
        LOG.info("Profil du compteur: retour à la sauvegarde %s", stamp)
        written = _put_checked(c, [(PHYS_PATH, phys, old_phys), (USER_PATH, user, old_user)])
        return {"restored": stamp, "files": written, "verified": True,
                "note": "Éteignez puis rallumez le compteur pour voir le changement."}


def sync(dirs, log=print, progress=None):
    """Lit les fichiers nouveaux ou modifiés du compteur (sans la carte) et les enregistre dans la sauvegarde locale."""
    with device() as (dev, c):
        dest = os.path.join(dirs.backup, dev.serial)
        if progress:
            progress(0, 0)
        stats = backup(c, dest, skip_prefixes=SKIP_PREFIXES, log=log, progress=progress, should_stop=ABORT.is_set)
    log(f"Lecture terminée: {stats['new']} fichiers nouveaux ou modifiés, {stats['errors']} erreurs")
    return {"copied": stats["new"], "errors": stats["errors"], "auto": _auto_after_read(dirs, stats, log)}


def _auto_after_read(dirs, stats, log):
    if stats["errors"]:  # lecture incomplète: une séance peut être à moitié copiée, on n'envoie rien
        return None
    return auto_process(dirs, log)


AUTO_DEFAULT = {"intervals": False, "gpx": False, "komoot": False, "baseline": ""}


def auto_settings(dirs):
    return {**AUTO_DEFAULT, **config(dirs).data.get("auto", {})}


def set_auto(dirs, intervals, gpx, komoot_on=False):
    """Active ou coupe l'envoi automatique. À l'activation, les séances déjà copiées sur le PC sont considérées comme connues:
    seules les séances plus récentes seront envoyées (pas d'envoi en masse de l'historique)."""
    cfg = config(dirs)
    cur = {**AUTO_DEFAULT, **cfg.data.get("auto", {})}
    was_on = cur["intervals"] or cur["gpx"] or cur["komoot"]
    cur["intervals"], cur["gpx"], cur["komoot"] = bool(intervals), bool(gpx), bool(komoot_on)
    now_on = cur["intervals"] or cur["gpx"] or cur["komoot"]
    if now_on and not was_on:
        ids = [s["id"] for s in list_sessions(dirs)]
        cur["baseline"] = max(ids) if ids else time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime()) + "Z"
        LOG.info("Envoi automatique activé (intervals=%s, gpx=%s, komoot=%s); séances connues jusqu'à %s", cur["intervals"], cur["gpx"], cur["komoot"], cur["baseline"])
    elif not now_on:
        LOG.info("Envoi automatique coupé")
    cfg.data["auto"] = cur
    cfg.save()
    return cur


def auto_process(dirs, log=print):
    """Après une lecture du compteur: envoie à Intervals.icu (FIT) et/ou écrit en GPX les séances plus récentes que la dernière connue.

    Une séance n'est marquée envoyée qu'après un envoi réussi: en cas d'échec (internet coupé), elle sera retentée à la lecture suivante.
    Ne touche jamais au compteur."""
    auto = auto_settings(dirs)
    if not (auto["intervals"] or auto["gpx"] or auto["komoot"]):
        return {"intervals": 0, "gpx": 0, "komoot": 0, "errors": 0}
    cfg = config(dirs)
    sent = cfg.data.get("uploaded", {})
    done = {"intervals": 0, "gpx": 0, "komoot": 0, "errors": 0}
    can_iv = auto["intervals"] and bool(cfg.data.get("intervals", {}).get("api_key"))
    if auto["intervals"] and not can_iv:
        log("Envoi automatique à Intervals.icu activé mais Intervals.icu n'est pas configuré: ignoré")
    can_kom = auto["komoot"] and bool(cfg.data.get("komoot", {}).get("token"))
    if auto["komoot"] and not can_kom:
        log("Envoi automatique à Komoot activé mais Komoot n'est pas connecté: ignoré")
    for s in sorted(list_sessions(dirs), key=lambda x: x["id"]):
        sid = s["id"]
        if sid <= auto["baseline"]:
            continue
        if can_kom and "komoot" not in sent.get(sid, {}):
            try:
                r = upload_session(dirs, sid, "komoot") or {}
                done["komoot"] += 1
                log(f"Séance {s['start_local']} envoyée à Komoot" + (" (déjà présente sur Komoot)" if r.get("created") is False else ""))
            except Exception as e:  # noqa: BLE001 - réseau, jeton refusé...: retenté à la prochaine lecture
                done["errors"] += 1
                log(f"Séance {s['start_local']}: envoi à Komoot impossible ({e}); nouvel essai à la prochaine lecture")
                LOG.error("Envoi automatique de %s à Komoot en échec: %s", sid, e, exc_info=True)
        if can_iv and "intervals" not in sent.get(sid, {}):
            try:
                upload_session(dirs, sid, "intervals")
                done["intervals"] += 1
                log(f"Séance {s['start_local']} envoyée à Intervals.icu")
            except Exception as e:  # noqa: BLE001 - réseau, clé refusée...: retenté à la prochaine lecture
                done["errors"] += 1
                log(f"Séance {s['start_local']}: envoi à Intervals.icu impossible ({e}); nouvel essai à la prochaine lecture")
                LOG.error("Envoi automatique de %s en échec: %s", sid, e, exc_info=True)
        if auto["gpx"] and not s["gpx"]:
            try:
                name, data = session_file(dirs, sid, "gpx")
                os.makedirs(dirs.export, exist_ok=True)
                with open(os.path.join(dirs.export, name), "wb") as f:
                    f.write(data)
                done["gpx"] += 1
                log(f"Séance {s['start_local']} exportée en GPX")
            except Exception as e:  # noqa: BLE001
                done["errors"] += 1
                log(f"Séance {s['start_local']}: export GPX impossible ({e})")
                LOG.error("Export GPX automatique de %s en échec: %s", sid, e, exc_info=True)
    return done


def connect_sync(dirs, log=print, progress=None, publish=None):
    """Ce qui se passe au branchement, en UNE seule session: état, heure et profil, puis lecture des nouvelles séances.

    `publish(nom, valeur)` transmet l'état et le profil dès qu'ils sont lus (la page peut proposer de régler l'heure
    pendant que la lecture continue).
    """
    with device() as (dev, c):
        st = _status_from(dev, c)
        if publish:
            publish("status", st)
        try:
            prof = _profile_from(c)
            if publish:
                publish("profile", prof)
        except Exception as e:  # noqa: BLE001 - le profil est un confort: on continue la lecture sans lui
            log(f"Profil non lu: {e}")
        dest = os.path.join(dirs.backup, dev.serial)
        if progress:
            progress(0, 0)
        stats = backup(c, dest, skip_prefixes=SKIP_PREFIXES, log=log, progress=progress, should_stop=ABORT.is_set)
    log(f"Lecture terminée: {stats['new']} fichiers nouveaux ou modifiés, {stats['errors']} erreurs")
    return {"copied": stats["new"], "errors": stats["errors"], "auto": _auto_after_read(dirs, stats, log)}


def _backup_root(dirs):
    subs = [d for d in os.listdir(dirs.backup)] if os.path.isdir(dirs.backup) else []
    return os.path.join(dirs.backup, subs[0]) if subs else None


_SHAPES = {}


def _shape(sess_dir):
    """Miniature du tracé: ≤ 48 points normalisés dans un carré 0..100 (mémorisée, calculée une seule fois)."""
    if sess_dir not in _SHAPES:
        try:
            r = pd.decode_route(pd.read_gz(os.path.join(sess_dir, "00", "ROUTE.GZB")))
            pts = list(zip(r["lon"], r["lat"]))[:: max(1, len(r["lat"]) // 48)]
        except (OSError, KeyError, ValueError):
            pts = []
        if len(pts) > 1:
            import math

            k = math.cos(math.radians(sum(p[1] for p in pts) / len(pts)))
            xs, ys = [p[0] * k for p in pts], [p[1] for p in pts]
            span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
            _SHAPES[sess_dir] = [[round((x - min(xs)) / span * 100), round(100 - (y - min(ys)) / span * 100)] for x, y in zip(xs, ys)]
        else:
            _SHAPES[sess_dir] = []
    return _SHAPES[sess_dir]


def list_sessions(dirs):
    root = _backup_root(dirs)
    out = []
    if not root:
        return out
    sent = config(dirs).data.get("uploaded", {})
    for sess in export.find_sessions(root):
        try:
            local, tz = pd.decode_session_start(open(os.path.join(sess, "TSESS.BPB"), "rb").read())
            s = pd.decode_samples(pd.read_gz(os.path.join(sess, "00", "SAMPLES.GZB")))
            start = pd.start_utc(local, tz)
            hr = [h for h in s["heart_rate"] if h]
            gpx = os.path.join(dirs.export, f"{start:%Y-%m-%d_%H-%M-%S}Z.gpx")
            out.append({
                "id": f"{start:%Y-%m-%d_%H-%M-%S}Z",
                "start_local": local.strftime("%Y-%m-%d %H:%M"),
                "duration_s": int(len(s["heart_rate"]) * (s["interval_ms"] or 1000) / 1000),
                "distance_km": round((s["distance"][-1] if s["distance"] else 0) / 1000, 1),
                "hr_avg": round(sum(hr) / len(hr)) if hr else None,
                "gpx": os.path.isfile(gpx), "shape": _shape(sess),
                "sent": sorted(sent.get(f"{start:%Y-%m-%d_%H-%M-%S}Z", {})),
            })
        except (OSError, KeyError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda x: x["start_local"], reverse=True)


_CONFIGS = {}


def config(dirs):
    """Le même objet de réglages pour tout le programme (voir uploads.Config), relu s'il a changé sur le disque."""
    path = os.path.join(dirs.root, "config.json")
    with uploads.Config.LOCK:
        cfg = _CONFIGS.get(path)
        if cfg is None:
            cfg = _CONFIGS[path] = uploads.Config(path)
            LOG.info("Réglages chargés: Intervals.icu %s, Komoot %s", "enregistré" if cfg.data.get("intervals", {}).get("api_key") else "non configuré",
                     "connecté" if cfg.data.get("komoot", {}).get("token") else "non connecté")
        else:
            cfg.refresh()
        return cfg


def upload_session(dirs, session_id, target):
    """Envoie le FIT d'une séance à Intervals.icu (target = « intervals ») ou à Komoot (« komoot »). Mémorise l'envoi dans config.json."""
    if target not in ("intervals", "komoot"):
        raise uploads.UploadError("service inconnu")
    try:
        fname, data = session_file(dirs, session_id, "fit")
    except KeyError:
        raise uploads.UploadError("Séance introuvable: lancez d'abord la lecture du compteur.") from None
    cfg = config(dirs)
    name = None
    for s in list_sessions(dirs):
        if s["id"] == session_id:
            name = f"Polar V650 {s['start_local']}"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if target == "komoot":
        try:
            user, token = _komoot_creds(dirs)
            res = komoot.upload_activity(user, token, data, name or "Polar V650", "fit", "racebike")  # le sport est lu dans le FIT (cyclisme sur route)
        except komoot.KomootError as e:
            raise uploads.UploadError(str(e)) from None
        cfg.data.setdefault("uploaded", {}).setdefault(session_id, {})["komoot"] = {"at": stamp, **res}
        cfg.save()
        return res
    os.makedirs(dirs.export, exist_ok=True)
    gpx = os.path.join(dirs.export, fname)  # (nom conservé: c'est un fichier FIT)
    with open(gpx, "wb") as f:
        f.write(data)
    res = uploads.intervals_upload(cfg, gpx, name=name)
    cfg.data.setdefault("uploaded", {}).setdefault(session_id, {})[target] = {"at": stamp, **res}
    cfg.save()
    return res


def _session_dirs(dirs):
    """{id de séance: dossier} d'après la sauvegarde locale."""
    root = _backup_root(dirs)
    out = {}
    for sess in export.find_sessions(root) if root else []:
        try:
            local, tz = pd.decode_session_start(open(os.path.join(sess, "TSESS.BPB"), "rb").read())
            out[f"{pd.start_utc(local, tz):%Y-%m-%d_%H-%M-%S}Z"] = sess
        except (OSError, KeyError, ValueError):
            continue
    return out


def _thin(seq, target):
    step = max(1, len(seq) // target)
    return seq[::step]


def session_detail(dirs, sid):
    """Tracé, statistiques clés et courbes (FC, vitesse, altitude) d'une séance, pour la page web."""
    path = _session_dirs(dirs).get(sid)
    if not path:
        raise KeyError(sid)
    S = export.load_session(path)
    recs = S["records"]
    track = [[round(r["lat"], 6), round(r["lon"], 6)] for r in recs if r["lat"] is not None]
    step = max(1, len(recs) // 500)
    series = [{"t": i, "km": round((r["distance_m"] or 0) / 1000, 3), "hr": r["hr"] or None,
               "kmh": round((r["speed_ms"] or 0) * 3.6, 1), "alt": round(r["alt_m"], 1) if r["alt_m"] is not None else None}
              for i, r in enumerate(recs) if i % step == 0]
    sm = S["summary"]
    return {
        "id": sid, "start_local": S["local"].strftime("%Y-%m-%d %H:%M"), "track": _thin(track, 1500), "series": series,
        "pauses": len(S["pauses"]),
        "stats": {"timer_s": int(sm["timer_s"]), "elapsed_s": int(sm["duration_s"]), "distance_km": round(sm["distance_m"] / 1000, 2),
                  "avg_kmh": round(sm["avg_speed_ms"] * 3.6, 1), "max_kmh": round(sm["max_speed_ms"] * 3.6, 1),
                  "hr_avg": sm["avg_hr"], "hr_max": sm["max_hr"], "hr_min": sm["min_hr"], "cad_avg": sm["avg_cadence"],
                  "cad_max": sm["max_cadence"], "ascent": round(sm["ascent_m"] or 0), "descent": round(sm["descent_m"] or 0),
                  "calories": sm["calories"], "alt_max": round(sm["alt_max"]) if sm["alt_max"] is not None else None},
    }


def session_file(dirs, sid, fmt):
    """(nom de fichier, octets) du GPX ou du FIT d'une séance, générés à la demande."""
    path = _session_dirs(dirs).get(sid)
    if not path:
        raise KeyError(sid)
    if fmt == "fit":
        return f"{sid}.fit", export.session_to_fit(path)
    return f"{sid}.gpx", export.session_to_gpx(path)[0].encode("utf-8")


def zip_sessions(dirs, ids, fmt):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sid in ids:
            name, data = session_file(dirs, sid, fmt)
            z.writestr(name, data)
    return buf.getvalue()


def list_routes():
    """Itinéraires présents sur le compteur: [{slot, name, points, km}], dans l'ordre des dossiers."""
    out = []
    with device() as (dev, c):
        for entry, _ in c.list_dir("/U/0/ROUTES/"):
            slot = entry.rstrip("/").rsplit("/", 1)[-1]
            if not slot.isdigit():
                continue
            try:
                r = route.decode(c.get_file(f"/U/0/ROUTES/{slot}/PROUTE.BPB"))
                ll = route.route_to_latlon(r)
                stats = route.gpx_stats([(la, lo, r["alt"] + p[3]) for (la, lo), p in zip(ll, r["points"])])
                out.append({"slot": int(slot), "name": r["name"], "points": len(r["points"]), "km": round(r["length"] / 1000, 1),
                            "stats": stats, "track": [[round(la, 5), round(lo, 5)] for la, lo in ll], "source_id": r["source_id"]})
            except Exception:
                out.append({"slot": int(slot), "name": "(fichier illisible ou vide)", "points": 0, "km": 0})
    return sorted(out, key=lambda x: x["slot"])


def preview_route(gpx_path):
    """Aperçu hors ligne: points et taille en qualité standard et HD."""
    pts, name = route.read_gpx(gpx_path)
    std, tol_s = route.gpx_to_route(gpx_path)
    hd, tol_h = route.hd_route(gpx_path)
    stats = route.gpx_stats(pts)
    step = max(1, len(pts) // 400)
    idx = list(range(0, len(pts), step))
    if idx[-1] != len(pts) - 1:
        idx.append(len(pts) - 1)
    track = [[round(pts[i][0], 5), round(pts[i][1], 5)] for i in idx]
    return {"name": route.safe_name(name or route.name_from_filename(gpx_path)), "original_name": name or route.name_from_filename(gpx_path),
            "gpx_points": len(pts), "km": stats["km"], "stats": stats, "track": track,
            "standard":{"points": len(std["points"]), "tolerance_m": round(tol_s, 1), "bytes": len(route.encode(std))},
            "hd": {"points": len(hd["points"]), "tolerance_m": round(tol_h, 1), "bytes": len(route.encode(hd))}}


def _komoot_creds(dirs):
    from .secretstore import unprotect

    k = config(dirs).data.get("komoot", {})
    if not k.get("token"):
        raise komoot.KomootError("Komoot n'est pas connecté.")
    return k["username"], unprotect(k["token"])


def komoot_login(dirs, email, password):
    """Connexion à Komoot: seul le jeton est conservé (chiffré), jamais le mot de passe."""
    from .secretstore import protect

    res = komoot.login(email, password)
    cfg = config(dirs)
    cfg.data["komoot"] = {"username": res["username"], "token": protect(res["token"])}
    cfg.save()
    return {"connected": True, "username": res["username"]}


def komoot_logout(dirs):
    cfg = config(dirs)
    cfg.data["komoot"] = {}
    cfg.save()
    return {"connected": False}


def komoot_tours(dirs, kind="planned"):
    user, token = _komoot_creds(dirs)
    try:
        return {"tours": komoot.list_tours(user, token, "tour_recorded" if kind == "recorded" else "tour_planned")}
    except komoot.KomootError as e:
        if "refusé" in str(e):  # jeton révoqué: on l'oublie pour proposer une nouvelle connexion
            komoot_logout(dirs)
        raise


def komoot_fetch(dirs, tour_id, name="", date="", preview=True):
    """Télécharge le GPX d'un tour Komoot dans le dossier des parcours et renvoie son aperçu (comme un GPX déposé à la main)."""
    user, token = _komoot_creds(dirs)
    data = komoot.get_gpx(user, token, int(tour_id))
    os.makedirs(dirs.routes_in, exist_ok=True)
    fname = komoot.gpx_filename(tour_id, name, str(date)[:10])
    dest = os.path.join(dirs.routes_in, fname)
    with open(dest, "wb") as f:
        f.write(data)
    if not preview:
        return {"file": fname}
    out = preview_route(dest)
    out["file"] = fname
    return out


def _source_id_of(gpx_path):
    """Identifiant Komoot d'un GPX téléchargé (dans le nom du fichier), inscrit dans l'itinéraire pour le rapprocher de Komoot ensuite."""
    import re

    m = re.match(r"\d{4}-\d{2}-\d{2}_(\d+)_", os.path.basename(gpx_path))
    return m.group(1) if m else None


def put_route(gpx_path, name=None, hd=False):
    """ÉCRIT un itinéraire (et son favori) sur le compteur. Renvoie le résumé et la vérification de relecture."""
    with device() as (dev, c):
        def num(entry):
            return entry.rstrip("/").rsplit("/", 1)[-1]

        slots = sorted(int(num(e)) for e, _ in c.list_dir("/U/0/ROUTES/") if num(e).isdigit())
        favs = sorted(int(num(e)) for e, _ in c.list_dir("/U/0/FAV/") if num(e).isdigit())
        empty = [s for s in slots if not c.list_dir(f"/U/0/ROUTES/{s}/")]
        slot = empty[0] if empty else (max(slots) + 1 if slots else 0)
        fav = max(favs) + 1 if favs else 0
        name = route.safe_name(name) if name else None  # même règle que dans la page, vérifiée ici aussi
        sid = _source_id_of(gpx_path)
        r, tol = route.hd_route(gpx_path, name=name, source_id=sid) if hd else route.gpx_to_route(gpx_path, name=name, source_id=sid)
        files = {
            f"/U/0/ROUTES/{slot}/PROUTE.BPB": route.encode(r),
            f"/U/0/FAV/{fav:02d}/TST.BPB": route.encode_fav_tst(r["name"], r["route_id"]),
            f"/U/0/FAV/{fav:02d}/ID.BPB": route.encode_fav_id(random.randint(84_000_000, 84_999_999), r["created"]),
        }
        if slot not in slots:
            c.put(f"/U/0/ROUTES/{slot}/")
        c.put(f"/U/0/FAV/{fav:02d}/")
        for path, data in files.items():
            c.put(path, data)
        time.sleep(1.0)
        ok = all(c.get_file(path) == data for path, data in files.items())
        return {"name": r["name"], "slot": slot, "favorite": fav, "points": len(r["points"]), "verified": ok,
                "note": "Éteignez puis rallumez le compteur pour voir l'itinéraire."}


def refresh_local_routes(dirs):
    """Remet à jour la copie locale des parcours après une écriture ou une suppression (une courte session): la bibliothèque la lit sans le compteur."""
    import shutil

    with device() as (dev, c):
        dest = os.path.join(dirs.backup, dev.serial, "U", "0", "ROUTES")
        slots = set()
        for entry, _ in c.list_dir("/U/0/ROUTES/"):
            slot = entry.rstrip("/").rsplit("/", 1)[-1]
            if not slot.isdigit():
                continue
            slots.add(slot)
            try:
                raw = c.get_file(f"/U/0/ROUTES/{slot}/PROUTE.BPB")
            except PftpError:
                continue
            os.makedirs(os.path.join(dest, slot), exist_ok=True)
            with open(os.path.join(dest, slot, "PROUTE.BPB"), "wb") as f:
                f.write(raw)
        if os.path.isdir(dest):
            for d in os.listdir(dest):
                if d not in slots:
                    shutil.rmtree(os.path.join(dest, d), ignore_errors=True)


def update_route(dirs, slot, gpx_path, hd=False):
    """REMPLACE la trace d'un parcours du compteur par celle d'un GPX (le parcours a changé dans Komoot). ÉCRIT SUR LE COMPTEUR.

    On garde le dossier, le nom, l'identifiant et la date de création: le favori qui y renvoie continue de fonctionner. Comme FlowSync,
    on supprime le dossier puis on le réécrit. L'ancien fichier est d'abord copié sur le PC (parcours/avant-mise-a-jour)."""
    import shutil

    with device() as (dev, c):
        raw = c.get_file(f"/U/0/ROUTES/{slot}/PROUTE.BPB")
        old = route.decode(raw)
        keep = os.path.join(dirs.root, "parcours", "avant-mise-a-jour")
        os.makedirs(keep, exist_ok=True)
        with open(os.path.join(keep, f"{time.strftime('%Y%m%d-%H%M%S')}_dossier{slot}_PROUTE.BPB"), "wb") as f:
            f.write(raw)
        r, tol = route.hd_route(gpx_path) if hd else route.gpx_to_route(gpx_path)
        r.update(route_id=old["route_id"], name=old["name"], created=old["created"], source_id=_source_id_of(gpx_path) or old["source_id"])
        data = route.encode(r)
        c.remove(f"/U/0/ROUTES/{slot}")
        c.put(f"/U/0/ROUTES/{slot}/")
        c.put(f"/U/0/ROUTES/{slot}/PROUTE.BPB", data)
        time.sleep(1.0)
        ok = c.get_file(f"/U/0/ROUTES/{slot}/PROUTE.BPB") == data
        return {"slot": slot, "name": old["name"], "points": len(r["points"]), "verified": ok,
                "note": "Éteignez puis rallumez le compteur pour voir la nouvelle trace."}


def komoot_refresh(dirs, kind="planned"):
    """Relit la liste des tours Komoot et la garde en cache. Renvoie le nombre de tours."""
    from . import routelib

    tours = komoot_tours(dirs, kind)["tours"]
    routelib.save_komoot_cache(dirs, tours)
    return {"count": len(tours)}


def komoot_download(dirs, ids, log=print, progress=None):
    """Télécharge (ou met à jour) le GPX de tours Komoot dans le dossier des parcours, avec une courte pause entre deux requêtes."""
    from . import routelib

    cache = {str(t["id"]): t for t in routelib.komoot_cache(dirs)["tours"]}
    done = 0
    for i, tid in enumerate(ids):
        t = cache.get(str(tid))
        if not t:
            continue
        log(f"GPX de « {t['name']} »")
        komoot_fetch(dirs, int(tid), t["name"], t.get("date", ""), preview=False)
        done += 1
        if progress:
            progress(i + 1, len(ids))
        time.sleep(0.3)
    return {"downloaded": done}


def remove_route(slot):
    """SUPPRIME un itinéraire et le favori qui le référence."""
    with device() as (dev, c):
        r = route.decode(c.get_file(f"/U/0/ROUTES/{slot}/PROUTE.BPB"))
        fav = None
        for entry, _ in c.list_dir("/U/0/FAV/"):
            num = entry.rstrip("/").rsplit("/", 1)[-1]
            try:
                if route.decode_fav_tst(c.get_file(f"/U/0/FAV/{num}/TST.BPB"))["route_id"] == r["route_id"]:
                    fav = num
            except Exception:
                continue
        c.remove(f"/U/0/ROUTES/{slot}")
        if fav is not None:
            c.remove(f"/U/0/FAV/{fav}")
        return {"removed_route": slot, "removed_favorite": fav}
