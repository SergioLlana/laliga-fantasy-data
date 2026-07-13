"""Búsqueda de candidatos: funciones puras sobre listas de registros.

No tocan ficheros ni asignan IDs canónicos (de eso se encarga ``run``); solo
deciden qué registros de Transfermarkt son compatibles con uno de Biwenger según
las reglas de nombre y club de ``normalize``.
"""

from __future__ import annotations

from collections.abc import Iterable

from lfdata.mappings.normalize import name_compatible, team_compatible


def team_candidates(biwenger_name: str, tm_clubs: Iterable[dict]) -> list[dict]:
    """Clubes de Transfermarkt cuyo nombre es compatible con el de Biwenger."""
    return [club for club in tm_clubs if team_compatible(biwenger_name, club["club_name"])]


def player_candidates(biwenger_name: str, tm_players: Iterable[dict]) -> list[dict]:
    """Jugadores de Transfermarkt cuyo nombre es compatible con el de Biwenger."""
    return [player for player in tm_players if name_compatible(biwenger_name, player["name"])]


def birthdate_candidates(birth_date: str, tm_players: Iterable[dict]) -> list[dict]:
    """Jugadores de Transfermarkt nacidos exactamente el mismo día.

    El nombre falla donde el apodo no comparte tokens con el nombre de pila
    (``Ez Abde`` / ``Abde Ezzalzouli``, ``Yusi`` / ``Youssef``): dentro de un club
    ya mapeado, la coincidencia de fecha al día identifica al jugador aunque su
    nombre no se parezca.
    """
    return [player for player in tm_players if birthdate_matches(birth_date, player["birth_date"])]


def birthdate_matches(a: str, b: str) -> bool:
    """¿Dos fechas de nacimiento ISO son la misma, y ambas existen?

    Más estricta que :func:`birthdate_compatible`: aquí la fecha *confirma* una
    identidad, así que una fecha ausente no confirma nada.
    """
    return bool(a) and bool(b) and a[:10] == b[:10]


def birthdate_compatible(a: str, b: str) -> bool:
    """¿Dos fechas de nacimiento ISO son compatibles?

    Compatibles si coinciden (por día) o si falta alguna de las dos: Biwenger solo
    publica la fecha en el detalle por jugador, así que muchas filas la tienen
    vacía. Solo una discrepancia real entre fechas presentes descarta un match.
    """
    if not a or not b:
        return True
    return a[:10] == b[:10]
