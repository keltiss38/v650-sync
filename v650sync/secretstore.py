"""Chiffrement des secrets enregistrés (clé d'API Intervals.icu, jeton Komoot) avec la protection de données de Windows (DPAPI).

Le texte chiffré n'est déchiffrable que par le même compte Windows sur ce PC: copier config.json ailleurs ne divulgue rien.
Une valeur sans préfixe (ancien format en clair) est lue telle quelle. Hors Windows, la valeur reste en clair (préfixe « plain: »).
"""
import base64
import os

PREFIX = "dpapi:"


def _blob(data):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf, DATA_BLOB


def _crypt(data, protect):
    import ctypes

    blob_in, keep, DATA_BLOB = _blob(data)
    blob_out = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("chiffrement Windows impossible")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        del keep


def protect(text):
    if not text:
        return ""
    if os.name == "nt":
        try:
            return PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode()
        except OSError:
            pass
    return "plain:" + text


def unprotect(value):
    if not value:
        return ""
    if value.startswith(PREFIX):
        return _crypt(base64.b64decode(value[len(PREFIX):]), False).decode("utf-8")
    if value.startswith("plain:"):
        return value[6:]
    return value  # ancien format: en clair
