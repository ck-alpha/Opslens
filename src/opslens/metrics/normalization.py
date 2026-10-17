"""Shared deterministic normalization and phrase boundaries; no fuzzy matching."""

import re
import unicodedata


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    # Keep identifier/version syntax; all other punctuation separates phrases.
    text = "".join(
        " " if unicodedata.category(c).startswith("P") and c not in "_@." else c for c in text
    )
    return " ".join(text.split())


def phrase_spans(phrase: str, query: str) -> list[tuple[int, int]]:
    """ASCII letters/digits/underscores cannot match inside another ASCII token."""
    phrase = normalize_text(phrase)
    if not phrase:
        return []
    left = (
        r"(?<![a-z0-9_])"
        if phrase[0].isascii() and (phrase[0].isalnum() or phrase[0] == "_")
        else ""
    )
    right = (
        r"(?![a-z0-9_])"
        if phrase[-1].isascii() and (phrase[-1].isalnum() or phrase[-1] == "_")
        else ""
    )
    return [m.span() for m in re.finditer(left + re.escape(phrase) + right, query)]
