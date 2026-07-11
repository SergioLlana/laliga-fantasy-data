"""Cliente de la API no oficial de SofaScore.

Endpoints verificados el 2026-07-11 (docs/experiments/2026-07-07-alex-fores.md y
docs/implementation/03-sofascore-fotmob-y-backfill.md). SofaScore bloquea por
huella TLS: ``curl`` normal da 403 y solo el transporte con impersonación de
Chrome (curl-cffi) pasa. Toda respuesta se escribe en raw/ antes de intentar
interpretarla.
"""

from __future__ import annotations

from pydantic import ValidationError

from lfdata.sources.http import HttpTransport
from lfdata.sources.sofascore.models import (
    EventPlayerStatisticsResponse,
    EventsResponse,
    LineupsResponse,
    OverallStatisticsResponse,
    RatingsResponse,
    SearchResponse,
    SeasonsResponse,
)
from lfdata.storage import RawStore

API_BASE = "https://api.sofascore.com/api/v1"
WAIT_SECONDS = 3.0
# SofaScore veta por huella TLS (403 con curl normal) y puede cortar por IP ante
# volumen. Se permite desbordar a ScrapeOps (rota IPs y resuelve Cloudflare) solo
# tras confirmar el bloqueo; hasta entonces va directo con curl-cffi (gratis). El
# desbordamiento requiere LFDATA_SCRAPEOPS_KEY; sin clave, directo con reintentos
# normales (ADR 0004).
PROXY_OVERFLOW = True


class SourceFormatError(Exception):
    """La fuente cambió la forma de su respuesta; no se escribe nada curado."""


class SofaScoreClient:
    def __init__(self, transport: HttpTransport, raw_store: RawStore) -> None:
        self._transport = transport
        self._raw_store = raw_store

    def _get(self, url: str, dataset: str, name: str, params: dict | None = None) -> bytes:
        payload = self._transport.get(url, params=params)
        self._raw_store.save("sofascore", dataset, name, payload)
        return payload

    @staticmethod
    def _validate(model, payload: bytes, url: str):
        try:
            return model.model_validate_json(payload)
        except ValidationError as error:
            raise SourceFormatError(
                f"SofaScore cambió la forma de {url}; la respuesta cruda quedó en raw/. "
                f"Detalle: {error}"
            ) from error

    def search_players(self, query: str) -> SearchResponse:
        """Busca jugadores (y otras entidades) por texto libre."""
        url = f"{API_BASE}/search/all"
        payload = self._get(url, "search", _slug(query), params={"q": query})
        return self._validate(SearchResponse, payload, url)

    def fetch_seasons(self, player_id: int) -> SeasonsResponse:
        """Torneos y temporadas disponibles del jugador (cualquier liga)."""
        url = f"{API_BASE}/player/{player_id}/statistics/seasons"
        payload = self._get(url, "player-seasons", str(player_id))
        return self._validate(SeasonsResponse, payload, url)

    def fetch_overall(
        self, player_id: int, tournament_id: int, season_id: int
    ) -> OverallStatisticsResponse:
        """Agregado de 115 campos del jugador en una liga-temporada."""
        url = (
            f"{API_BASE}/player/{player_id}/unique-tournament/{tournament_id}"
            f"/season/{season_id}/statistics/overall"
        )
        payload = self._get(url, "player-overall", f"{player_id}-{tournament_id}-{season_id}")
        return self._validate(OverallStatisticsResponse, payload, url)

    def fetch_ratings(
        self, player_id: int, tournament_id: int, season_id: int
    ) -> RatingsResponse:
        """Nota por partido del jugador en una liga-temporada."""
        url = (
            f"{API_BASE}/player/{player_id}/unique-tournament/{tournament_id}"
            f"/season/{season_id}/ratings"
        )
        payload = self._get(url, "player-ratings", f"{player_id}-{tournament_id}-{season_id}")
        return self._validate(RatingsResponse, payload, url)

    def fetch_event_player_stats(
        self, event_id: int, player_id: int
    ) -> EventPlayerStatisticsResponse:
        """Estadística de evento del jugador en un partido concreto."""
        url = f"{API_BASE}/event/{event_id}/player/{player_id}/statistics"
        payload = self._get(url, "event-player-stats", f"{event_id}-{player_id}")
        return self._validate(EventPlayerStatisticsResponse, payload, url)

    def fetch_events(
        self, tournament_id: int, season_id: int, page: int = 0
    ) -> EventsResponse:
        """Una página del calendario de partidos pasados de una liga-temporada."""
        url = (
            f"{API_BASE}/unique-tournament/{tournament_id}/season/{season_id}"
            f"/events/last/{page}"
        )
        payload = self._get(
            url, "tournament-events", f"{tournament_id}-{season_id}-last-{page}"
        )
        return self._validate(EventsResponse, payload, url)

    def fetch_lineups(self, event_id: int) -> LineupsResponse:
        """Alineaciones de un partido con la estadística de evento por jugador."""
        url = f"{API_BASE}/event/{event_id}/lineups"
        payload = self._get(url, "event-lineups", str(event_id))
        return self._validate(LineupsResponse, payload, url)


def _slug(text: str) -> str:
    """Nombre de fichero raw seguro a partir de una consulta de búsqueda."""
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-") or "query"
