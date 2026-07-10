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
