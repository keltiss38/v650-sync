"""Modification du profil enregistré dans le compteur: poids, taille, fréquences cardiaques, VO2max, FTP (PHYSDATA.BPB) et prénom / nom (USERID.BPB).

On décode le fichier protobuf en liste de champs, on remplace seulement la valeur voulue et on réencode: tout le reste (dates de
dernière modification, identifiants, champs inconnus) est recopié tel quel, octet pour octet. Rien ici ne parle au compteur:
l'écriture, la sauvegarde préalable et la relecture sont dans service.py. L'e-mail et l'identifiant Polar ne sont pas modifiables.
"""
import re
import struct

# champ de PHYSDATA.BPB -> (clé, type de la valeur dans le sous-message: « f » flottant 32 bits, « i » entier)
PHYS = {3: ("weight_kg", "f"), 4: ("height_cm", "f"), 5: ("hr_max", "i"), 6: ("hr_rest", "i"), 10: ("vo2max", "i"), 15: ("ftp", "i")}
USER = {5: "first", 6: "last"}  # champs de USERID.BPB
LIMITS = {"weight_kg": (20, 300), "height_cm": (100, 250), "hr_max": (100, 240), "hr_rest": (25, 120), "vo2max": (10, 100), "ftp": (60, 600)}  # FTP: plage du compteur (manuel)
LABELS = {"weight_kg": "poids", "height_cm": "taille", "hr_max": "FC max", "hr_rest": "FC repos", "vo2max": "VO2max", "ftp": "FTP",
          "first": "prénom", "last": "nom"}
NAME_MAX = 30


def _read_varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def decode(data):
    """[(numéro, type de fil, valeur)] dans l'ordre du fichier; ValueError si ce n'est pas du protobuf valide."""
    out, i = [], 0
    try:
        while i < len(data):
            key, i = _read_varint(data, i)
            num, wire = key >> 3, key & 7
            if wire == 0:
                val, i = _read_varint(data, i)
            elif wire == 2:
                n, i = _read_varint(data, i)
                val = bytes(data[i:i + n])
                if len(val) != n:
                    raise ValueError("champ tronqué")
                i += n
            elif wire == 5:
                val = bytes(data[i:i + 4])
                i += 4
            elif wire == 1:
                val = bytes(data[i:i + 8])
                i += 8
            else:
                raise ValueError(f"type de champ inconnu ({wire})")
            out.append((num, wire, val))
    except IndexError:
        raise ValueError("fichier tronqué") from None
    return out


def encode(fields):
    out = bytearray()
    for num, wire, val in fields:
        out += _varint(num << 3 | wire)
        if wire == 0:
            out += _varint(val)
        elif wire == 2:
            out += _varint(len(val)) + val
        else:
            out += val
    return bytes(out)


def _first(fields, num):
    for n, w, v in fields:
        if n == num:
            return w, v
    return None


def _replace(fields, num, new_value):
    """Copie de `fields` où la première occurrence du champ `num` a la valeur `new_value` (même type de fil)."""
    out, done = [], False
    for n, w, v in fields:
        if n == num and not done:
            out.append((n, w, new_value))
            done = True
        else:
            out.append((n, w, v))
    if not done:
        raise ValueError(f"champ {num} absent")
    return out


def read_values(phys, user):
    """Valeurs actuelles des champs modifiables, d'après les deux fichiers (octets)."""
    vals = {}
    pf = decode(phys)
    for num, (key, kind) in PHYS.items():
        m = _first(pf, num)
        if not m or m[0] != 2:
            continue
        inner = _first(decode(m[1]), 1)
        if not inner:
            continue
        vals[key] = round(struct.unpack("<f", inner[1])[0], 1) if kind == "f" else inner[1]
    uf = decode(user)
    for num, key in USER.items():
        m = _first(uf, num)
        if m and m[0] == 2:
            vals[key] = m[1].decode("utf-8", "replace")
    return vals


def clean(changes):
    """Valide la demande de la page: seules les clés connues, valeurs numériques dans des bornes raisonnables, noms propres. ValueError sinon."""
    out = {}
    for key, raw in (changes or {}).items():
        if key in LIMITS:
            try:
                v = float(str(raw).replace(",", "."))
            except ValueError:
                raise ValueError(f"{LABELS[key]}: valeur invalide") from None
            lo, hi = LIMITS[key]
            if not lo <= v <= hi:
                raise ValueError(f"{LABELS[key]}: entre {lo} et {hi}")
            out[key] = round(v, 1) if key in ("weight_kg", "height_cm") else int(round(v))
        elif key in ("first", "last"):
            s = re.sub(r"\s+", " ", str(raw)).strip()
            if not s or len(s) > NAME_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
                raise ValueError(f"{LABELS[key]}: 1 à {NAME_MAX} caractères, sans caractère de contrôle")
            out[key] = s
        else:
            raise ValueError(f"champ non modifiable: {key}")
    return out


def apply(phys, user, changes):
    """(nouveau PHYSDATA, nouveau USERID, clés réellement modifiées). Les valeurs identiques à l'actuelle ne comptent pas."""
    changes = clean(changes)
    cur = read_values(phys, user)
    changed = [k for k, v in changes.items() if cur.get(k) != v]
    pf, uf = decode(phys), decode(user)
    for num, (key, kind) in PHYS.items():
        if key not in changed:
            continue
        w, v = _first(pf, num) or (None, None)
        if w != 2:
            raise ValueError(f"{LABELS[key]}: champ absent du profil du compteur")
        inner = decode(v)
        if not _first(inner, 1):
            raise ValueError(f"{LABELS[key]}: champ absent du profil du compteur")
        new = struct.pack("<f", changes[key]) if kind == "f" else int(changes[key])
        pf = _replace(pf, num, encode(_replace(inner, 1, new)))
    for num, key in USER.items():
        if key in changed:
            if not _first(uf, num):
                raise ValueError(f"{LABELS[key]}: champ absent du compte du compteur")
            uf = _replace(uf, num, changes[key].encode("utf-8"))
    new_phys = encode(pf) if any(k in changed for k, _ in PHYS.values()) else phys
    new_user = encode(uf) if any(k in changed for k in USER.values()) else user
    return new_phys, new_user, changed
