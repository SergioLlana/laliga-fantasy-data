"""Cliente de la API de Biwenger.

Endpoints verificados el 2026-07-07 (docs/experiments/2026-07-07-alex-fores.md).
Toda respuesta se escribe en raw/ antes de intentar interpretarla.
"""

from __future__ import annotations

from pydantic import ValidationError

from lfdata.sources.biwenger.models import (
    CompetitionDataResponse,
    PlayerDetailResponse,
    RoundResponse,
)
from lfdata.sources.http import HttpTransport
from lfdata.storage import RawStore

API_BASE = "https://cf.biwenger.com/api/v2"
# De Biwenger se ingiere exclusivamente La Liga (ADR 0008): sus ids de jugador y
# equipo son distintos en cada competición, y una segunda competición partiría la
# identidad canónica en dos. Segunda, Copa y demás son ligas de origen y se cubren
# con Transfermarkt/SofaScore.
COMPETITIONS = ("la-liga",)
WAIT_SECONDS = 2.0
# Campos del detalle por jugador: un report por partido (puntos en los cinco
# sistemas, minutos y nota SofaScore vía rawStats) y el precio diario.
PLAYER_FIELDS = "*,reports(points,home,status,match(*,round),rawStats),prices,seasons"
# Biwenger corta con 429 a las ~200 peticiones por ventana e IP, aunque se
# espere 2 s entre ellas (comprobado ingiriendo la-liga 2025: bloqueo limpio en
# el jugador ~200). Se permite desbordar a ScrapeOps (rota IPs) para pasar de las
# ~200 en un run, pero solo tras confirmar el bloqueo: hasta entonces va directo
# (gratis). El desbordamiento requiere LFDATA_SCRAPEOPS_KEY; sin clave, directo
# con reintentos normales (ADR 0004).
PROXY_OVERFLOW = True


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
        """Detalle por jugador y temporada: reports por partido y precios diarios.

        ``season`` es el **año de inicio** de la temporada (2025 = 2025/26), la
        misma convención que Transfermarkt y SofaScore. La API de Biwenger, en
        cambio, identifica la temporada por su **año de fin** (2025/26 lo pide con
        ``season=2026``; su propio ``data.seasons`` nombra al id 2026 como
        "2025/2026 season"). Esa traducción vive aquí y solo afecta a la URL: la
        partición y el nombre del raw se quedan con el año de inicio, para que
        ``season=2025`` sea 2025/26 en todo el almacenamiento.

        Ojo: ``season`` solo aplica a ``reports``. El campo ``prices`` lo ignora
        y devuelve siempre la ventana móvil de los últimos ~366 días (#89), así
        que no hay histórico de precios de temporadas pasadas por esta vía; el
        curado deriva la temporada de cada precio de su fecha.
        """
        if competition not in COMPETITIONS:
            raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
        api_season = str(int(season) + 1)
        url = f"{API_BASE}/players/{competition}/{slug}"
        payload = self._transport.get(url, params={"fields": PLAYER_FIELDS, "season": api_season})
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

    def fetch_round(self, competition: str, round_id: int, score: int) -> RoundResponse:
        """Jornada completa bajo el sistema ``score``: partidos y puntos por jugador.

        Devuelve a **todos** los jugadores que puntuaron en la jornada, incluidos
        los que ya dejaron la competición (cuyo detalle por jugador da 404). La
        respuesta trae además ``season.rounds`` con el catálogo de jornadas de esa
        temporada, del que se descubren los ids sin lista manual.
        """
        if competition not in COMPETITIONS:
            raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
        url = f"{API_BASE}/rounds/{competition}/{round_id}"
        payload = self._transport.get(url, params={"score": score})
        self._raw_store.save(
            "biwenger", "rounds", f"{competition}-{round_id}-score{score}", payload
        )
        try:
            return RoundResponse.model_validate_json(payload)
        except ValidationError as error:
            raise SourceFormatError(
                f"Biwenger cambió la forma de {url}; la respuesta cruda quedó en raw/. "
                f"Detalle: {error}"
            ) from error
