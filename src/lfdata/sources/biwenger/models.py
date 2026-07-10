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


class SeasonRound(_BiwengerModel):
    """Una jornada del catálogo de la temporada (``season.rounds``).

    Tanto la plantilla (``competitions/.../data``) como el detalle de una jornada
    (``rounds/...``) traen este catálogo con el estado de cada jornada: es la vía
    para saber qué jornada acaba de terminar sin lista manual.
    """

    id: int
    name: str
    short: str | None = None
    status: str | None = None


class Season(_BiwengerModel):
    id: str
    name: str
    slug: str
    rounds: list[SeasonRound] = Field(default_factory=list)


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


# --- Detalle por jugador y temporada: players/{competición}/{slug} ---------
#
# Cada temporada trae un `report` por partido. Los partidos en los que el
# jugador no puntuó (no convocado, futuro) llegan solo con `match`/`status`;
# los que puntuaron traen además `points` y `rawStats`. Los cinco sistemas de
# puntuación se identifican por su id: 1=AS, 2=SofaScore, 3=Estadísticas,
# 5=Media, 6=Social. No todos aparecen siempre (Segunda no publica 5 ni 6).


class Round(_BiwengerModel):
    id: int
    name: str


class ReportMatch(_BiwengerModel):
    id: int
    date: int | None = None
    round: Round


class RawStats(_BiwengerModel):
    minutes_played: int | None = Field(alias="minutesPlayed", default=None)
    # Nota SofaScore: solo La Liga la publica; en Segunda el campo no existe.
    sofascore: float | None = None
    home_score: int | None = Field(alias="homeScore", default=None)
    away_score: int | None = Field(alias="awayScore", default=None)
    win: bool = False
    lost: bool = False
    clean_sheet: bool | None = Field(alias="cleanSheet", default=None)


class Report(_BiwengerModel):
    # `home` indica si el equipo del jugador jugaba en casa (local/visitante).
    home: bool | None = None
    match: ReportMatch
    # Presentes solo cuando el jugador puntuó ese partido.
    points: dict[str, int] | None = None
    raw_stats: RawStats | None = Field(alias="rawStats", default=None)

    @property
    def scored(self) -> bool:
        return self.points is not None and self.raw_stats is not None

    @property
    def points_without_stats(self) -> bool:
        """Puntuó (trae ``points``) pero le falta ``rawStats``: fila incompleta.

        No genera fila en ``fantasy_points`` (dependemos de minutos, nota y
        resultado de ``rawStats``); la ingesta lo cuenta como anomalía en vez de
        saltarlo en silencio.
        """
        return self.points is not None and self.raw_stats is None


class PlayerDetail(_BiwengerModel):
    id: int
    name: str
    slug: str
    birthday: int | None = None
    reports: list[Report] = Field(default_factory=list)
    # Precio diario: pares [fecha AAMMDD, precio].
    prices: list[tuple[int, int]] = Field(default_factory=list)


class PlayerDetailResponse(_BiwengerModel):
    status: int
    data: PlayerDetail


# --- Jornada completa: rounds/{competición}/{round_id}?score=N --------------
#
# Devuelve los partidos de una jornada con **todos** los jugadores que puntuaron
# —incluidos los que ya dejaron la competición— y sus puntos bajo el sistema
# `N` (una petición por sistema). Sin `rawStats`: no trae minutos ni nota. La
# respuesta incluye, además, ``season.rounds`` con el catálogo completo de
# jornadas de esa temporada, del que sale la lista de ids sin lista manual.


class RoundReportPlayer(_BiwengerModel):
    id: int
    name: str
    slug: str
    position: int | None = None


class RoundReport(_BiwengerModel):
    player: RoundReportPlayer
    points: int


class RoundTeam(_BiwengerModel):
    id: int
    name: str
    slug: str
    score: int | None = None
    reports: list[RoundReport] = Field(default_factory=list)


class RoundGame(_BiwengerModel):
    id: int
    date: int | None = None
    status: str | None = None
    home: RoundTeam
    away: RoundTeam


class RoundData(_BiwengerModel):
    id: int
    name: str
    short: str | None = None
    status: str | None = None
    score_id: int = Field(alias="scoreID")
    # Mismo catálogo ``season.rounds`` que la plantilla: reutiliza ``Season``.
    season: Season
    games: list[RoundGame] = Field(default_factory=list)


class RoundResponse(_BiwengerModel):
    status: int
    data: RoundData
