"""Forma esperada de las respuestas de SofaScore.

Endpoints verificados el 2026-07-11 con el caso Álex Forés (player 1086128), el
mismo del experimento docs/experiments/2026-07-07-alex-fores.md. Si SofaScore
cambia su formato (falta un campo, cambia un tipo), la validación falla con
error explícito y nunca se escribe una tabla curada a medias. Los campos que no
usamos se ignoran.

Las dos respuestas de estadísticas (agregado por temporada y por evento) traen
un bloque ``statistics`` **abierto**: SofaScore omite los campos con valor cero y
publica distinto set según la liga (p. ej. LaLiga2 no da ``expectedGoals``). Por
eso se modela como ``dict`` y la ingesta extrae los campos que le interesan con
``.get``, tratando la ausencia según el campo (0 para conteos, nulo para xG/nota).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _SofaModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- Búsqueda: search/all?q= ------------------------------------------------
#
# ``results`` mezcla tipos (player, team, event...). Solo interpretamos los de
# tipo ``player``; su ``entity`` se valida aparte como :class:`SearchPlayer`.


class SearchTeam(_SofaModel):
    id: int
    name: str
    slug: str | None = None


class SearchPlayer(_SofaModel):
    id: int
    name: str
    slug: str | None = None
    team: SearchTeam | None = None
    position: str | None = None
    date_of_birth_timestamp: int | None = Field(alias="dateOfBirthTimestamp", default=None)


class SearchResult(_SofaModel):
    type: str
    entity: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(_SofaModel):
    results: list[SearchResult] = Field(default_factory=list)

    def players(self) -> list[SearchPlayer]:
        """Solo las entidades de tipo ``player``, ya validadas."""
        return [
            SearchPlayer.model_validate(r.entity) for r in self.results if r.type == "player"
        ]


# --- Temporadas del jugador: player/{id}/statistics/seasons -----------------
#
# Qué torneos y temporadas tiene el jugador (cualquier liga: La Liga, LaLiga2,
# Primera Federación, Copa...). De aquí salen los pares (unique_tournament,
# season) con los que se piden agregado y notas.


class UniqueTournament(_SofaModel):
    id: int
    name: str
    slug: str | None = None


class SeasonRef(_SofaModel):
    id: int
    year: str | None = None
    name: str | None = None


class TournamentSeasons(_SofaModel):
    unique_tournament: UniqueTournament = Field(alias="uniqueTournament")
    seasons: list[SeasonRef] = Field(default_factory=list)


class SeasonsResponse(_SofaModel):
    unique_tournament_seasons: list[TournamentSeasons] = Field(
        alias="uniqueTournamentSeasons", default_factory=list
    )


# --- Agregado por temporada: .../statistics/overall -------------------------
#
# 115 campos agregados de la temporada (minutos, nota media, goles, xG...). El
# set varía por liga; se guarda tal cual en ``statistics``.


class OverallStatisticsResponse(_SofaModel):
    statistics: dict[str, Any] = Field(default_factory=dict)


# --- Nota por partido: .../ratings ------------------------------------------
#
# ``seasonRatings`` da una entrada por partido jugado en esa liga-temporada, con
# la nota y el contexto (rival, local/visitante, fecha). Los eventId de aquí son
# los que luego se piden uno a uno para las métricas por partido.


class RatingOpponent(_SofaModel):
    id: int
    name: str
    slug: str | None = None


class SeasonRating(_SofaModel):
    event_id: int = Field(alias="eventId")
    start_timestamp: int | None = Field(alias="startTimestamp", default=None)
    rating: float | None = None
    is_home: bool | None = Field(alias="isHome", default=None)
    opponent: RatingOpponent | None = None


class RatingsResponse(_SofaModel):
    season_ratings: list[SeasonRating] = Field(alias="seasonRatings", default_factory=list)


# --- Métricas por jugador y partido: event/{id}/player/{id}/statistics ------
#
# Estadística de evento del jugador en ese partido (minutos, pases, remates,
# goles, duelos, xG...). ``statistics`` es abierto: SofaScore omite los campos a
# cero, así que un conteo ausente significa cero.


class EventPlayerStatisticsResponse(_SofaModel):
    statistics: dict[str, Any] = Field(default_factory=dict)
