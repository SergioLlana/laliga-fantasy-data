"""Cliente de Transfermarkt.

Mezcla HTML (competición, plantilla, perfil) y JSON interno `ceapi` (histórico
de valor y traspasos). Endpoints verificados el 2026-07-07
(docs/experiments/2026-07-07-alex-fores.md). Toda respuesta se escribe en raw/
antes de intentar interpretarla.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from lfdata.sources.http import HttpTransport
from lfdata.sources.transfermarkt import parse
from lfdata.sources.transfermarkt.models import (
    MarketValueGraph,
    PerformanceResponse,
    TransferHistory,
)
from lfdata.sources.transfermarkt.parse import (
    Club,
    Injury,
    PlayerProfile,
    SourceFormatError,
    SquadMember,
)
from lfdata.storage import RawStore

logger = logging.getLogger(__name__)

# Transfermarkt resuelve el perfil por el ID, no por el slug de la URL; cuando la
# plantilla no trae slug usamos este marcador para no emitir URLs con doble barra
# (``//profil/spieler/...``), que confunden al proxy y a los logs.
SLUG_PLACEHOLDER = "spieler"

# Usamos el host .com a propósito: sus respuestas (posiciones, tipo de traspaso,
# diagnósticos de lesión, nombres de club) vienen en inglés. Todo lo que se cura
# queda así en inglés, no en español. Los endpoints y el formato son idénticos a
# los de cualquier otro host de Transfermarkt (fechas en dd/mm/YYYY igualmente).
BASE = "https://www.transfermarkt.com"
# Transfermarkt no bloquea con UA de navegador y espera educada de 4 s; el
# transporte común ya impersona Chrome. Sin desbordamiento a ScrapeOps (ver #28).
WAIT_SECONDS = 4.0
PROXY_OVERFLOW = False

# Competición de la plataforma -> (slug de URL, código de wettbewerb de Transfermarkt).
COMPETITIONS = {
    "la-liga": ("laliga", "ES1"),
    "segunda-division": ("laliga2", "ES2"),
}


class TransfermarktClient:
    """Cada ``fetch_*`` acepta ``cached``: servirse del raw ya guardado en vez de
    pedir a la fuente. Es lo que permite volver a curar sin re-scrapear cuando la
    respuesta ya está bajada (``--since-days``, reconstrucción de una tabla). Si
    ``cached`` y el raw no tiene nada, se pide igualmente: la caché acelera, no
    limita lo que se puede curar.
    """

    def __init__(self, transport: HttpTransport, raw_store: RawStore) -> None:
        self._transport = transport
        self._raw_store = raw_store

    # --- HTML ----------------------------------------------------------------

    def fetch_competition_clubs(
        self, competition: str, *, season: int, cached: bool = False
    ) -> list[Club]:
        """Clubes que participan en la competición esa temporada."""
        if competition not in COMPETITIONS:
            raise ValueError(
                f"Competición desconocida: {competition!r} (usa {tuple(COMPETITIONS)})"
            )
        slug, code = COMPETITIONS[competition]
        url = f"{BASE}/{slug}/startseite/wettbewerb/{code}/saison_id/{season}"
        # La temporada va en el nombre (como en kader): dos temporadas ingeridas
        # el mismo día conviven en raw/ en vez de pisarse.
        payload = self._payload(
            url, "competition-clubs", f"{code}-saison-{season}", extension="html", cached=cached
        )
        return parse.parse_competition_clubs(payload)

    def fetch_squad(self, club_id: int, *, season: int, cached: bool = False) -> list[SquadMember]:
        """Plantilla de un club en una temporada (saison_id de Transfermarkt)."""
        url = f"{BASE}/x/kader/verein/{club_id}/saison_id/{season}"
        payload = self._payload(
            url, "kader", f"verein-{club_id}-saison-{season}", extension="html", cached=cached
        )
        return parse.parse_squad(payload)

    def fetch_player_profile(
        self, player_id: int, *, slug: str, cached: bool = False
    ) -> PlayerProfile:
        """Perfil de un jugador: nombre, fecha de nacimiento y posición."""
        url = f"{BASE}/{self._url_slug(slug, player_id)}/profil/spieler/{player_id}"
        payload = self._payload(
            url, "profile", f"spieler-{player_id}", extension="html", cached=cached
        )
        return parse.parse_profile(payload, player_id=player_id)

    def fetch_injuries(self, player_id: int, *, slug: str, cached: bool = False) -> list[Injury]:
        """Historial de lesiones (HTML; no hay endpoint JSON de lesiones)."""
        url = f"{BASE}/{self._url_slug(slug, player_id)}/verletzungen/spieler/{player_id}"
        payload = self._payload(
            url, "injuries", f"spieler-{player_id}", extension="html", cached=cached
        )
        return parse.parse_injuries(payload, player_id=player_id)

    # --- JSON ceapi ----------------------------------------------------------

    def fetch_market_value(self, player_id: int, *, cached: bool = False) -> MarketValueGraph:
        """Histórico de valor de mercado (JSON limpio, con club en cada fecha)."""
        url = f"{BASE}/ceapi/marketValueDevelopment/graph/{player_id}"
        payload = self._payload(url, "market-value", str(player_id), cached=cached)
        return self._validate(MarketValueGraph, payload, url)

    def fetch_transfers(self, player_id: int, *, cached: bool = False) -> TransferHistory:
        """Traspasos y cesiones (JSON limpio, con tipo y clubes)."""
        url = f"{BASE}/ceapi/transferHistory/list/{player_id}"
        payload = self._payload(url, "transfers", str(player_id), cached=cached)
        return self._validate(TransferHistory, payload, url)

    def fetch_performance(self, player_id: int, *, cached: bool = False) -> PerformanceResponse:
        """Rendimiento partido a partido (JSON); insumo de disponibilidad."""
        url = f"{BASE}/ceapi/performance-game/{player_id}"
        payload = self._payload(url, "performance-game", str(player_id), cached=cached)
        return self._validate(PerformanceResponse, payload, url)

    # --- transporte y caché --------------------------------------------------

    def _payload(
        self, url: str, dataset: str, name: str, *, extension: str = "json", cached: bool = False
    ) -> bytes:
        """Respuesta cruda: la del raw si ``cached`` la tiene, si no la de la fuente.

        Servida desde raw no se vuelve a guardar: la ``fecha_descarga`` sigue
        siendo la de la descarga real, no la de esta lectura.
        """
        if cached:
            payload = self._raw_store.read_latest(
                "transfermarkt", dataset, name, extension=extension
            )
            if payload is not None:
                return payload
            logger.debug("transfermarkt %s/%s no está en raw; se pide a la fuente", dataset, name)
        payload = self._transport.get(url)
        self._raw_store.save("transfermarkt", dataset, name, payload, extension=extension)
        return payload

    @staticmethod
    def _url_slug(slug: str, player_id: int) -> str:
        """Slug para la URL, con fallback genérico si la plantilla no lo trae."""
        clean = (slug or "").strip()
        if clean:
            return clean
        logger.warning(
            "transfermarkt spieler %d sin slug en la plantilla; uso %r en la URL",
            player_id,
            SLUG_PLACEHOLDER,
        )
        return SLUG_PLACEHOLDER

    @staticmethod
    def _validate(model, payload: bytes, url: str):
        try:
            return model.model_validate_json(payload)
        except ValidationError as error:
            raise SourceFormatError(
                f"Transfermarkt cambió la forma de {url}; la respuesta cruda quedó en raw/. "
                f"Detalle: {error}"
            ) from error
