"""Découverte du V650 par mDNS (bibliothèque zeroconf, sans le service Bonjour d'Apple) et connexion."""
import re
import socket
import time
from dataclasses import dataclass, field

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

from .pftp import PftpClient, PftpError

SERVICE = "_polar-ftp._tcp.local."


@dataclass
class Device:
    name: str
    port: int
    addresses: list = field(default_factory=list)
    hint: tuple = None  # (adresse, interface) qui a déjà marché

    @property
    def serial(self):
        """Numéro de série: le long jeton alphanumérique du nom (ignore un éventuel suffixe « (2) » ajouté par mDNS)."""
        m = re.search(r"\b[0-9A-Z]{10,}\b", self.name)
        return m.group(0) if m else "V650"


def find_devices(timeout=8.0):
    found = {}

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=3000)
            if info:
                found[name] = Device(name, info.port, info.parsed_addresses())

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf(ip_version=IPVersion.All)
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        t0 = time.time()
        while not found and time.time() - t0 < timeout:
            time.sleep(0.2)
        time.sleep(1.2)  # laisse arriver les autres enregistrements éventuels (périmés ou non)
    finally:
        zc.close()
    return list(found.values())


def connect_any(devices, per_try=2.0):
    """mDNS peut renvoyer des enregistrements périmés (anciens ports, parfois le « (2) » est le bon): on essaie en parallèle
    tous les couples (service, interface réseau) et on garde le premier qui accepte la connexion, puis on lit la bannière PFTP.

    Retourne (device, client). `device.hint` mémorise l'interface qui a marché (prochaine connexion plus rapide).
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cands = []
    for dev in devices:
        for addr in dev.addresses:
            if addr.startswith("fe80"):
                scopes = _scopes()
                if dev.hint and dev.hint[0] == addr:
                    scopes = [dev.hint[1]] + [s for s in scopes if s != dev.hint[1]]
                cands.extend((dev, addr, s) for s in scopes)
    lock, won = threading.Lock(), []

    def attempt(c):
        dev, addr, scope = c
        try:
            target = socket.getaddrinfo(f"{addr}%{scope}", dev.port, socket.AF_INET6, socket.SOCK_STREAM)[0][4]
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            return None
        try:
            s.settimeout(per_try)
            s.connect(target)
        except OSError:
            s.close()
            return None
        with lock:
            if won:  # un autre essai a déjà gagné
                s.close()
                return None
            won.append(c)
        return s

    errors = []
    pool = ThreadPoolExecutor(max_workers=min(48, max(1, len(cands))))
    try:
        futures = {pool.submit(attempt, c): c for c in cands}
        for fut in as_completed(futures):
            s = fut.result()
            if s is None:
                continue
            dev, addr, scope = futures[fut]
            try:
                client = PftpClient.from_socket(s, timeout=per_try + 3)
            except (PftpError, OSError) as e:
                s.close()
                errors.append(f"{dev.name} port {dev.port}: {e}")
                won.clear()  # service qui accepte mais ne parle pas PFTP: on laisse les autres essais compter
                continue
            dev.hint = (addr, scope)
            return dev, client
    finally:
        pool.shutdown(wait=False)
    raise PftpError("impossible de se connecter au V650. " + " | ".join(errors) + " Débranchez puis rebranchez le câble USB et réessayez.")


def _scopes():
    try:
        return [idx for idx, _ in socket.if_nameindex()]
    except (AttributeError, OSError):
        return [0]


def connect(device, per_try=2.0):
    """Ouvre une session PFTP. L'adresse fe80:: n'a de sens que sur une interface précise: on les essaie toutes."""
    last = None
    for addr in device.addresses:
        if not addr.startswith("fe80"):
            continue
        for scope in _scopes():
            try:
                target = socket.getaddrinfo(f"{addr}%{scope}", device.port, socket.AF_INET6, socket.SOCK_STREAM)[0][4]
                s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s.settimeout(per_try)
                s.connect(target)
            except OSError as e:
                last = e
                continue
            try:
                return PftpClient.from_socket(s, timeout=per_try + 3)
            except (PftpError, OSError) as e:
                last = e
                s.close()
    raise PftpError(f"impossible de se connecter au V650 ({last}). Débranchez puis rebranchez le câble USB et réessayez.")
