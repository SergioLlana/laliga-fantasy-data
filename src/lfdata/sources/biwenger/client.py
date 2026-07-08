"""Cliente de la API de Biwenger.

Endpoints verificados el 2026-07-07 (docs/experiments/2026-07-07-alex-fores.md).
Toda respuesta se escribe en raw/ antes de intentar interpretarla.
"""

from __future__ import annotations

from pydantic import ValidationError

from lfdata.sources.biwenger.models import CompetitionDataResponse, PlayerDetailResponse
from lfdata.sources.http import HttpTransport
from lfdata.storage import RawStore

API_BASE = "https://cf.biwenger.com/api/v2"
COMPETITIONS = ("la-liga", "segunda-division")
WAIT_SECONDS = 2.0
# Campos del detalle por jugador: un report por partido (puntos en los cinco
# sistemas, minutos y nota SofaScore vía rawStats) y el precio diario.
PLAYER_FIELDS = "*,reports(points,home,status,match(*,round),rawStats),prices,seasons"
# Biwenger es una API JSON que no bloquea con esperas educadas: no enruta por
# ScrapeOps. Las fuentes que sí lo necesiten ponen esto a True (ver #28).
PROXY_ENABLED = False


class SourceFormatError(Exception):
    """La fuente cambió la forma de su respuesta; no se escribe nada curado."""


class BiwengerClient:
    def __init__(self, transport: HttpTransport, raw_store: RawStore) -> None:
        self._transport = transport
        self._raw_store = raw_store

    def fetch_competition_data(self, competition: str) -> CompetitionDataResponse:
        """Plantilla completa de la competición: jugadores, equipos y jornadas."""
        if competition not in COMPETITIONS:
            raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
        url = f"{API_BASE}/competitions/{competition}/data"
        payload = self._transport.get(url, params={"lang": "es", "score": 1})
        self._raw_store.save("biwenger", "competition-data", competition, payload)
        try:
            return CompetitionDataResponse.model_validate_json(payload)
        except ValidationError as error:
            raise SourceFormatError(
                f"Biwenger cambió la forma de {url}; la respuesta cruda quedó en raw/. "
                f"Detalle: {error}"
            ) from error

    def fetch_player_reports(
        self, competition: str, slug: str, season: str
    ) -> PlayerDetailResponse:
        """Detalle por jugador y temporada: reports por partido y precios diarios."""
        if competition not in COMPETITIONS:
            raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
        url = f"{API_BASE}/players/{competition}/{slug}"
        payload = self._transport.get(url, params={"fields": PLAYER_FIELDS, "season": season})
        self._raw_store.save(
            "biwenger", "player-reports", f"{competition}-{slug}-{season}", payload
        )
        try:
            return PlayerDetailResponse.model_validate_json(payload)
        except ValidationError as error:
            raise SourceFormatError(
                f"Biwenger cambió la forma de {url}; la respuesta cruda quedó en raw/. "
                f"Detalle: {error}"
            ) from error
