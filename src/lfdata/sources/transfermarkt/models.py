"""Forma esperada de las respuestas JSON `ceapi` de Transfermarkt.

Solo se modelan los endpoints JSON (histórico de valor y traspasos). El HTML
(competición, plantilla, perfil) se interpreta en ``parse.py`` con estructuras
propias. Si Transfermarkt cambia el formato, la validación falla con un error
explícito y nunca se escribe una tabla curada a medias; los campos que no
usamos se ignoran.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _TMModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- marketValueDevelopment/graph -------------------------------------------


class MarketValuePoint(_TMModel):
    value: int = Field(alias="y")
    date: str = Field(alias="datum_mw")  # dd/mm/YYYY
    club_name: str = Field(alias="verein")


class MarketValueGraph(_TMModel):
    points: list[MarketValuePoint] = Field(alias="list", default_factory=list)


# --- transferHistory/list ----------------------------------------------------


class ClubRef(_TMModel):
    club_name: str = Field(alias="clubName")
    href: str = ""


class Transfer(_TMModel):
    date: str = Field(alias="dateUnformatted")  # YYYY-MM-DD
    season: str = ""
    fee: str = ""
    market_value: str = Field(alias="marketValue", default="")
    from_club: ClubRef = Field(alias="from")
    to_club: ClubRef = Field(alias="to")


class TransferHistory(_TMModel):
    transfers: list[Transfer] = Field(default_factory=list)


# --- performance-game --------------------------------------------------------
#
# Una fila por jugador-partido de toda la carrera. Solo modelamos lo que cura
# `availability_tm` (disponibilidad): la nota (`grade`) viene siempre null, así
# que el eventing sigue siendo SofaScore. El resto de estadística se conserva en
# raw/ pero no se cura aquí.


class _GameDate(_TMModel):
    date_utc: str | None = Field(alias="dateTimeUTC", default=None)


class GameInformation(_TMModel):
    game_id: str = Field(alias="gameId", default="")
    competition_id: str = Field(alias="competitionId", default="")
    season_id: int | None = Field(alias="seasonId", default=None)
    game_day: int | None = Field(alias="gameDay", default=None)
    date: _GameDate = Field(default_factory=_GameDate)


class _ClubSide(_TMModel):
    club_id: str | None = Field(alias="clubId", default=None)


class ClubsInformation(_TMModel):
    club: _ClubSide = Field(default_factory=_ClubSide)
    opponent: _ClubSide = Field(default_factory=_ClubSide)


class GeneralStatistics(_TMModel):
    participation_state: str = Field(alias="participationState", default="")
    primary_club_id: int | None = Field(alias="primaryClubId", default=None)
    injury_id: int = Field(alias="injuryId", default=0)
    absence_id: int = Field(alias="absenceId", default=0)
    shirt_number: int | None = Field(alias="shirtNumber", default=None)


class _Substitution(_TMModel):
    minute: int | None = None


class PlayingTimeStatistics(_TMModel):
    played_minutes: int | None = Field(alias="playedMinutes", default=None)
    is_starting: bool = Field(alias="isStarting", default=False)
    substituted_in: _Substitution | None = Field(alias="substitutedIn", default=None)
    substituted_out: _Substitution | None = Field(alias="substitutedOut", default=None)


class GameStatistics(_TMModel):
    general: GeneralStatistics = Field(alias="generalStatistics")
    playing_time: PlayingTimeStatistics = Field(
        alias="playingTimeStatistics", default_factory=PlayingTimeStatistics
    )


class PerformanceGame(_TMModel):
    game_information: GameInformation = Field(alias="gameInformation")
    clubs_information: ClubsInformation = Field(
        alias="clubsInformation", default_factory=ClubsInformation
    )
    statistics: GameStatistics


class _PerformanceData(_TMModel):
    player_id: str = Field(alias="playerId", default="")
    performance: list[PerformanceGame] = Field(default_factory=list)


class PerformanceResponse(_TMModel):
    data: _PerformanceData = Field(default_factory=_PerformanceData)
