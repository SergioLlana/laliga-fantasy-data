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
