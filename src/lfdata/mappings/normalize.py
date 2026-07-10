"""Normalización de nombres para el matching de identidad.

El match automático seguro exige "nombre normalizado compatible": mismo jugador
aunque una fuente lo escriba con tilde y otra sin ella, o con el nombre completo
frente a solo el apellido. Normalizamos a minúsculas sin tildes y comparamos por
conjuntos de tokens significativos (ignorando partículas como "de la"), de modo
que un subconjunto de tokens en cualquier dirección cuenta como compatible:
``Catena`` ⊆ ``Óscar Catena`` y ``Vinícius`` ⊆ ``Vinícius Júnior``.
"""

from __future__ import annotations

import re
import unicodedata

# Partículas de nombres de persona que no discriminan identidad ("de la Fuente").
_NAME_PARTICLES = frozenset(
    {"de", "la", "del", "las", "los", "da", "das", "dos", "van", "von",
     "der", "den", "di", "le", "el", "al", "y", "e"}
)  # fmt: skip

# Sufijos/prefijos genéricos de club que no discriminan ("FC Barcelona").
_TEAM_STOPWORDS = frozenset(
    {"fc", "cf", "cd", "ud", "sd", "rc", "ad", "cp", "sad", "club",
     "de", "b", "ii"}
)  # fmt: skip

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    """Quita tildes y diacríticos descomponiendo en Unicode NFKD."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Minúsculas, sin tildes, sin puntuación y con espacios colapsados."""
    folded = strip_accents(str(text)).lower()
    return _NON_ALNUM.sub(" ", folded).strip()


def _tokens(text: str, stopwords: frozenset[str]) -> frozenset[str]:
    return frozenset(tok for tok in normalize(text).split() if tok and tok not in stopwords)


def name_tokens(name: str) -> frozenset[str]:
    """Tokens significativos de un nombre de persona (sin partículas)."""
    return _tokens(name, _NAME_PARTICLES)


def team_tokens(name: str) -> frozenset[str]:
    """Tokens significativos del nombre de un club (sin sufijos genéricos)."""
    return _tokens(name, _TEAM_STOPWORDS)


def _compatible(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return a <= b or b <= a


def name_compatible(a: str, b: str) -> bool:
    """¿Los nombres de dos personas son compatibles (subconjunto de tokens)?"""
    return _compatible(name_tokens(a), name_tokens(b))


def team_compatible(a: str, b: str) -> bool:
    """¿Los nombres de dos clubes son compatibles (subconjunto de tokens)?"""
    return _compatible(team_tokens(a), team_tokens(b))
