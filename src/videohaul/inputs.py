from __future__ import annotations

import re


URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ").,;!?]}"


def extract_urls(text: str) -> list[str]:
    values = []
    seen = set()
    for match in URL_PATTERN.findall(str(text or "")):
        value = match.rstrip(TRAILING_PUNCTUATION)
        key = value.casefold()
        if value and key not in seen:
            values.append(value)
            seen.add(key)
    return values
