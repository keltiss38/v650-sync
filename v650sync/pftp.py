"""Protocole PFTP du V650, déduit de captures de FlowSync (voir recap.md).

Trame: en-tête 16 bits LE = (L << 2) | flags, puis L octets de charge utile.
Requête: charge utile = [len16 LE][PbPFtpOperation][données]. GET (lecture), PUT et REMOVE (écriture, y compris en
plusieurs trames pour les gros fichiers comme la carte).
Réponse à un fichier: trames de contenu terminées par une trame vide.
Réponse à un dossier: une trame PbPFtpDirectory, sans trame vide ensuite.
"""
import socket
import struct
import time

from . import journal

LOG = journal.get("pftp")
GET = 0  # PbPFtpOperation.Command.GET; PUT/MERGE/REMOVE volontairement non exposés
MAX_FRAME = 16383  # longueur maximale d'une trame (champ de 14 bits)
CONTINUE = b"\x00\x00\x00\x00"  # deux trames vides: demande la trame suivante d'un gros fichier
TOKEN = b"\x00\x02\x00\x00"  # jeton du compteur avant chaque trame de suite d'une écriture en plusieurs trames


class PftpError(Exception):
    pass


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def encode_get(path):
    p = path.encode()
    op = b"\x08" + _varint(GET) + b"\x12" + _varint(len(p)) + p
    payload = struct.pack("<H", len(op)) + op
    return struct.pack("<H", len(payload) << 2) + payload


def parse_directory(data):
    """PbPFtpDirectory: entries (champ 1) = {name (1, string), size (2, varint)}."""
    entries, i = [], 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        if tag & 7 != 2:
            raise PftpError("dossier illisible")
        n, i = _read_varint(data, i)
        ent, i = data[i:i + n], i + n
        j, name, size = 0, None, 0
        while j < len(ent):
            t, j = _read_varint(ent, j)
            wt = t & 7
            if wt == 0:
                v, j = _read_varint(ent, j)
                if t >> 3 == 2:
                    size = v
            elif wt == 2:
                m, j = _read_varint(ent, j)
                if t >> 3 == 1:
                    name = ent[j:j + m].decode("utf8", "replace")
                j += m
            else:
                break
        if name is not None:
            entries.append((name, size))
    return entries


def parse_strings(data):
    """Chaînes (champs de type 2 imprimables) d'un message protobuf, pour un affichage informatif."""
    out, i = [], 0
    try:
        while i < len(data):
            t, i = _read_varint(data, i)
            wt = t & 7
            if wt == 0:
                _, i = _read_varint(data, i)
            elif wt == 2:
                m, i = _read_varint(data, i)
                chunk = data[i:i + m]
                i += m
                try:
                    s = chunk.decode("ascii")
                    if s.isprintable() and s:
                        out.append((t >> 3, s))
                except UnicodeDecodeError:
                    pass
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            else:
                break
    except IndexError:
        pass
    return out


class PftpClient:
    def __init__(self, sock, banner):
        self.sock = sock
        self.banner = banner

    @classmethod
    def from_socket(cls, sock, timeout=10):
        sock.settimeout(timeout)
        banner = b""
        while not banner.endswith(b"\n"):
            c = sock.recv(1)
            if not c:
                raise PftpError("connexion fermée avant la bannière")
            banner += c
            if len(banner) > 200:
                raise PftpError("bannière inattendue")
        if not banner.startswith(b"Polar FTP server"):
            raise PftpError(f"bannière inattendue: {banner!r}")
        return cls(sock, banner.decode("ascii", "replace").strip())

    def close(self):
        """Ferme la session proprement (FIN envoyé au compteur): il ne garde pas de connexion fantôme."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise PftpError("connexion fermée")
            buf += c
        return bytes(buf)

    def _frame(self):
        h = struct.unpack("<H", self._recv_exact(2))[0]
        length, flags = h >> 2, h & 3
        return flags, self._recv_exact(length) if length else b""

    def get_file(self, path, progress=None):
        """Lit un fichier.

        Petit fichier: une trame de contenu puis une trame vide. Fichier de plus de 16 383 octets: trames pleines
        de 16 383 octets, et le compteur n'envoie la suivante qu'après réception de deux trames vides
        (b"\\x00\\x00\\x00\\x00"), comme FlowSync le fait. La dernière trame, partielle, n'est pas suivie d'une trame vide.
        """
        LOG.debug("GET %s", path)
        self.sock.sendall(encode_get(path))
        data = bytearray()
        multi = False
        while True:
            _, payload = self._frame()
            if not payload:
                return bytes(data)  # trame vide: fin d'un petit fichier (ou d'un fichier multiple exact de 16 383)
            data += payload
            if progress:
                progress(len(data))
            if len(payload) == MAX_FRAME:
                self.sock.sendall(CONTINUE)
                multi = True
                continue
            if multi:
                return bytes(data)
            self._frame()  # trame vide qui suit un fichier tenant en une seule trame
            return bytes(data)

    def _write_op(self, command, path, data=b"", progress=None):
        """Écriture (PUT/REMOVE) tracée dans le journal: chemin, taille, durée, résultat (jamais le contenu)."""
        what = "PUT" if command == 1 else "REMOVE"
        t0 = time.time()
        LOG.info("%s %s (%d octets)", what, path, len(data))
        try:
            self._write_op_raw(command, path, data, progress)
        except Exception as e:
            LOG.error("%s %s ÉCHEC après %.1f s: %s: %s", what, path, time.time() - t0, type(e).__name__, e)
            raise
        LOG.info("%s %s accepté (%.1f s)", what, path, time.time() - t0)

    def _write_op_raw(self, command, path, data=b"", progress=None):
        """PUT (1) ou REMOVE (3): charge utile = [len16][PbPFtpOperation][données]; réponse = deux trames vides.

        Une écriture qui tient en une trame (16 383 octets, en-tête compris) part telle quelle. Une plus grosse (la carte)
        est découpée comme FlowSync le fait (capture de la mise à jour de carte): trames de 16 383 octets, drapeau 0 pour la
        première, drapeau 1 pour les suivantes, la dernière étant plus courte. Le compteur envoie un jeton de 4 octets
        (TOKEN) avant chaque trame de suite et FlowSync n'envoie la trame suivante qu'après l'avoir reçu: on fait pareil.
        """
        p = path.encode()
        op = b"\x08" + _varint(command) + b"\x12" + _varint(len(p)) + p
        head = struct.pack("<H", len(op)) + op
        total = len(head) + len(data)
        if total <= MAX_FRAME:
            self.sock.sendall(struct.pack("<H", total << 2) + head + data)
        else:
            if total % MAX_FRAME == 0:
                # on ne sait pas comment le compteur reconnaît la fin quand la dernière trame est pleine: on refuse
                raise PftpError("taille d'écriture non gérée (multiple exact de 16 383 octets avec l'en-tête)")
            view = memoryview(data)
            first = MAX_FRAME - len(head)
            self.sock.sendall(struct.pack("<H", MAX_FRAME << 2) + head + view[:first])
            sent = first
            old = self.sock.gettimeout()
            self.sock.settimeout(60)  # l'attente d'un jeton est normalement de quelques millisecondes
            try:
                while sent < len(data):
                    tok = self._recv_exact(4)
                    if tok != TOKEN:
                        raise PftpError(f"jeton attendu avant la trame suivante, reçu {tok.hex()} (après {sent} octets de {path})")
                    chunk = view[sent:sent + MAX_FRAME]
                    self.sock.sendall(struct.pack("<H", len(chunk) << 2 | 1) + chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, len(data))
            finally:
                self.sock.settimeout(old)
        # Le compteur acquitte chaque écriture par DEUX trames vides (4 octets 00 00 00 00), comme on le voit dans la capture
        # de FlowSync, qui attend cet acquittement avant la requête suivante. Envoyer la suite trop tôt fait ajouter les
        # octets suivants au fichier en cours d'écriture (constaté: un PROUTE de 6721 octets est devenu 6788).
        # Après une écriture en plusieurs trames, un éventuel dernier jeton peut précéder l'acquittement: on l'ignore.
        old = self.sock.gettimeout()
        self.sock.settimeout(60 if total > MAX_FRAME else old)
        try:
            while True:
                grp = self._recv_exact(4)
                if grp == TOKEN and total > MAX_FRAME:
                    continue
                if grp != b"\x00\x00\x00\x00":
                    # Refus: 00 00 puis un code d'erreur sur 2 octets au lieu du second 00 00 (observé: 6a 00 = 106; codes PFTP
                    # de Polar: 100 erreur inconnue, 101 commande invalide, 102 paramètre invalide, 103 fichier ou dossier
                    # introuvable, 104 dossier existant, 105 fichier existant, 106 opération non permise).
                    if grp[:2] == b"\x00\x00":
                        code = struct.unpack("<H", grp[2:4])[0]
                        noms = {100: "erreur inconnue", 101: "commande invalide", 102: "paramètre invalide",
                                103: "fichier ou dossier introuvable", 104: "dossier déjà existant", 105: "fichier déjà existant",
                                106: "opération non permise"}
                        raise PftpError(f"refusé par le compteur ({'écriture' if command == 1 else 'suppression'} de {path}): "
                                        f"code {code} ({noms.get(code, 'inconnu')})")
                    raise PftpError(f"réponse inattendue à l'opération {command} sur {path}: {grp.hex()}")
                break
        finally:
            self.sock.settimeout(old)

    def query(self, query_id, params=b""):
        """Requête courte (PbPFtpQuery), par ex. 5 = espace disque, 8 = batterie. Charge utile: [0x8000 | len][id][params].

        Renvoie les octets de la première trame de réponse (protobuf) ; les trames vides qui suivent sont consommées.
        """
        LOG.debug("QUERY %d", query_id)
        payload = struct.pack("<H", 0x8000 | (1 + len(params))) + bytes([query_id]) + params
        self.sock.sendall(struct.pack("<H", len(payload) << 2) + payload)
        _, answer = self._frame()
        old = self.sock.gettimeout()
        self.sock.settimeout(0.4)
        try:
            while True:
                _, extra = self._frame()
                if extra:
                    answer += extra
        except (socket.timeout, TimeoutError):
            pass
        finally:
            self.sock.settimeout(old)
        return answer

    def put(self, path, data=b"", progress=None):
        """Écrit un fichier (ou crée un dossier si le chemin se termine par « / » et sans données). ÉCRIT SUR LE COMPTEUR.

        `progress(octets_envoyés, total)` est appelé après chaque trame d'une écriture en plusieurs trames."""
        self._write_op(1, path, data, progress)

    def remove(self, path):
        """Supprime un fichier ou un dossier (sans « / » final, comme FlowSync). ÉCRIT SUR LE COMPTEUR."""
        self._write_op(3, path)

    def list_dir(self, path):
        """Liste un dossier. Le chemin doit se terminer par '/'."""
        if not path.endswith("/"):
            path += "/"
        LOG.debug("LIST %s", path)
        self.sock.sendall(encode_get(path))
        _, data = self._frame()
        data = bytearray(data)
        old = self.sock.gettimeout()
        self.sock.settimeout(0.15)  # d'éventuelles trames suivantes d'un gros dossier
        try:
            while True:
                _, more = self._frame()
                if not more:
                    break
                data += more
        except (socket.timeout, TimeoutError):
            pass
        finally:
            self.sock.settimeout(old)
        return parse_directory(bytes(data))
