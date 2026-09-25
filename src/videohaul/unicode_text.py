from __future__ import annotations

import codecs
import locale
import os
import unicodedata


def decode_external_bytes(value: bytes | bytearray | memoryview | str, declared_encoding: str | None = None) -> str:
    if isinstance(value, str):
        return value
    raw = bytes(value)
    if declared_encoding:
        try:
            return raw.decode(str(declared_encoding), errors="strict")
        except (LookupError, UnicodeDecodeError):
            pass
    if raw.startswith(codecs.BOM_UTF32_LE):
        return raw.decode("utf-32")
    if raw.startswith(codecs.BOM_UTF32_BE):
        return raw.decode("utf-32")
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = [locale.getpreferredencoding(False)]
    if os.name == "nt":
        candidates.append("mbcs")
    candidates.extend(["gb18030", "cp932", "cp949", "cp950", "cp1256", "cp1255", "cp1251", "cp1253", "cp1254", "cp1250", "cp1252", "cp1257", "cp1258"])
    seen = {"utf-8"}
    for encoding in candidates:
        key = str(encoding or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="backslashreplace")


def normalize_unicode(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _is_extend(character: str) -> bool:
    codepoint = ord(character)
    category = unicodedata.category(character)
    return (
        category in {"Mn", "Mc", "Me"}
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def grapheme_like_clusters(value: str) -> list[str]:
    text = normalize_unicode(value)
    clusters: list[str] = []
    current = ""
    join_next = False
    regional_count = 0
    for character in text:
        codepoint = ord(character)
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        if not current:
            current = character
            regional_count = 1 if regional else 0
            join_next = codepoint == 0x200D
            continue
        if join_next or codepoint == 0x200D or _is_extend(character):
            current += character
            join_next = codepoint == 0x200D
            if regional:
                regional_count += 1
            continue
        if regional and regional_count == 1:
            current += character
            regional_count = 2
            continue
        clusters.append(current)
        current = character
        regional_count = 1 if regional else 0
        join_next = codepoint == 0x200D
    if current:
        clusters.append(current)
    return clusters


def truncate_filename_component(value: str, max_utf8_bytes: int = 180, max_utf16_units: int = 180) -> str:
    result = ""
    used_utf8 = 0
    used_utf16 = 0
    for cluster in grapheme_like_clusters(value):
        utf8_size = len(cluster.encode("utf-8"))
        utf16_size = len(cluster.encode("utf-16-le")) // 2
        if result and (used_utf8 + utf8_size > max_utf8_bytes or used_utf16 + utf16_size > max_utf16_units):
            break
        if not result and (utf8_size > max_utf8_bytes or utf16_size > max_utf16_units):
            encoded = cluster.encode("utf-8")[:max_utf8_bytes]
            return encoded.decode("utf-8", errors="ignore")
        result += cluster
        used_utf8 += utf8_size
        used_utf16 += utf16_size
    return result
