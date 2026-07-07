"""Forma esperada de las respuestas de Biwenger.

Si Biwenger cambia su formato (falta un campo, cambia un tipo), la
validación falla con error explícito y nunca se escribe una tabla
curada a medias. Los campos que no usamos se ignoran.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _BiwengerModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Player(_BiwengerModel):
    id: int
    name: str
    slug: str
    team_id: int | None = Field(alias="teamID", default=None)
    position: int
    status: str
    price: int
    price_increment: int = Field(alias="priceIncrement")
    fantasy_price: int | None = Field(alias="fantasyPrice", default=None)
    number: int | None = None
    points: int | None = None
    points_home: int | None = Field(alias="pointsHome", default=None)
    points_away: int | None = Field(alias="pointsAway", default=None)
    played_home: int | None = Field(alias="playedHome", default=None)
    played_away: int | None = Field(alias="playedAway", default=None)
    points_last_season: int | None = Field(alias="pointsLastSeason", default=None)


class Team(_BiwengerModel):
    id: int
    name: str
    slug: str


class Season(_BiwengerModel):
    id: str
    name: str
    slug: str


class CompetitionData(_BiwengerModel):
    id: int
    name: str
    slug: str
    season: Season
    players: dict[str, Player]
    teams: dict[str, Team]


class CompetitionDataResponse(_BiwengerModel):
    status: int
    data: CompetitionData
