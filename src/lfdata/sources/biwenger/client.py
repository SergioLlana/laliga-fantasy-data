"""Cliente de la API de Biwenger.

Endpoints verificados el 2026-07-07 (docs/experiments/2026-07-07-alex-fores.md).
Toda respuesta se escribe en raw/ antes de intentar interpretarla.
"""

from __future__ import annotations

from pydantic import ValidationError

from lfdata.sources.biwenger.models import CompetitionDataResponse
from lfdata.sources.http import HttpTransport
from lfdata.storage import RawStore

API_BASE = "https://cf.biwenger.com/api/v2"
COMPETITIONS = ("la-liga", "segunda-division")
WAIT_SECONDS = 2.0


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
