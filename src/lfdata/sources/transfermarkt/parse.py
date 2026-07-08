"""Interpretación de las respuestas de Transfermarkt.

Dos familias:

- HTML (competición, plantilla, perfil, lesiones): parseado con BeautifulSoup
  sobre el parser de la librería estándar. Verificado el 2026-07-07 con Álex
  Forés (docs/experiments/2026-07-07-alex-fores.md).
- JSON `ceapi` (valores, traspasos, rendimiento/disponibilidad): ya validado por
  pydantic en ``models``; aquí solo se aplana a filas, se clasifica el tipo de
  movimiento y se extrae la disponibilidad por partido.

Toda función de parseo lanza ``SourceFormatError`` si la página perdió la
estructura de la que dependemos, para no escribir tablas curadas a medias.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

from lfdata.sources.transfermarkt.models import (
    MarketValueGraph,
    PerformanceResponse,
    TransferHistory,
)

_SPIELER_ID = re.compile(r"/profil/spieler/(\d+)")
_SPIELER_SLUG = re.compile(r"^/([^/]+)/profil/spieler/\d+")
_VEREIN_ID = re.compile(r"/verein/(\d+)")


class SourceFormatError(Exception):
    """Transfermarkt cambió la forma de su respuesta; no se escribe nada curado."""


@dataclass(frozen=True)
class Club:
    id: int
    name: str


@dataclass(frozen=True)
class SquadMember:
    player_id: int
    slug: str
    name: str
    position: str | None
    shirt_number: int | None


@dataclass(frozen=True)
class PlayerProfile:
    player_id: int
    name: str
    birth_date: date | None
    position: str | None


@dataclass(frozen=True)
class Injury:
    player_id: int
    season: str
    injury: str
    from_date: date | None
    until_date: date | None
    days: int | None
    games_missed: int | None


def _soup(payload: bytes | str) -> BeautifulSoup:
    html = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    return BeautifulSoup(html, "html.parser")


def _items_tbody(soup: BeautifulSoup, *, what: str):
    table = soup.find("table", class_="items")
    if table is None or table.find("tbody") is None:
        raise SourceFormatError(f"No encuentro la tabla de {what} (table.items) en Transfermarkt")
    return table.find("tbody")


def _norm(text: str) -> str:
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )
    return stripped.strip().lower()


def _tm_date(value: str) -> date | None:
    """Fecha en formato dd/mm/YYYY; None si viene vacía o con guion."""
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def _club_id_from_href(href: str) -> int | None:
    match = _VEREIN_ID.search(href or "")
    return int(match.group(1)) if match else None


# --- competición → clubes ----------------------------------------------------


def parse_competition_clubs(payload: bytes | str) -> list[Club]:
    """Clubes de la tabla de clasificación/participantes de la competición."""
    tbody = _items_tbody(_soup(payload), what="clubes de la competición")
    clubs: dict[int, Club] = {}
    for row in tbody.find_all("tr", recursive=False):
        cell = row.find("td", class_="hauptlink")
        link = cell.find("a", href=_VEREIN_ID) if cell else None
        if link is None:
            continue
        club_id = _club_id_from_href(link["href"])
        name = link.get_text(strip=True) or (link.get("title") or "")
        if club_id is not None and club_id not in clubs:
            clubs[club_id] = Club(id=club_id, name=name)
    if not clubs:
        raise SourceFormatError("La página de competición no listó ningún club")
    return list(clubs.values())


# --- plantilla (kader) -------------------------------------------------------


def parse_squad(payload: bytes | str) -> list[SquadMember]:
    """Jugadores de la plantilla: id, slug, nombre, posición y dorsal."""
    tbody = _items_tbody(_soup(payload), what="plantilla")
    members: list[SquadMember] = []
    for row in tbody.find_all("tr", recursive=False):
        link = row.find("a", href=_SPIELER_ID)
        if link is None:
            continue
        href = link["href"]
        player_id = int(_SPIELER_ID.search(href).group(1))
        slug_match = _SPIELER_SLUG.match(href)
        name = link.get_text(strip=True)

        position = None
        inline = row.find("table", class_="inline-table")
        if inline is not None:
            inline_rows = inline.find_all("tr")
            if len(inline_rows) >= 2:
                position = inline_rows[1].get_text(strip=True) or None

        number_cell = row.find("div", class_="rn_nummer")
        shirt_number = None
        if number_cell is not None:
            digits = number_cell.get_text(strip=True)
            shirt_number = int(digits) if digits.isdigit() else None

        members.append(
            SquadMember(
                player_id=player_id,
                slug=slug_match.group(1) if slug_match else "",
                name=name,
                position=position,
                shirt_number=shirt_number,
            )
        )
    if not members:
        raise SourceFormatError("La plantilla no contenía ningún jugador")
    return members


# --- perfil ------------------------------------------------------------------


def parse_profile(payload: bytes | str, *, player_id: int) -> PlayerProfile:
    """Datos biográficos del perfil: nombre, fecha de nacimiento y posición."""
    soup = _soup(payload)
    headline = soup.find("h1", class_=re.compile("data-header__headline"))
    if headline is None:
        raise SourceFormatError("El perfil no tiene cabecera (h1.data-header__headline)")
    # El h1 antepone el dorsal (<span class="data-header__shirt-number">#1</span>);
    # lo quitamos para quedarnos solo con el nombre.
    shirt = headline.find(class_=re.compile("shirt-number"))
    if shirt is not None:
        shirt.extract()
    name = headline.get_text(" ", strip=True)

    birth_span = soup.find("span", itemprop="birthDate")
    birth_date = None
    if birth_span is not None:
        # "12/04/2001 (25)" -> nos quedamos con la fecha.
        birth_date = _tm_date(birth_span.get_text(strip=True).split()[0])

    position = None
    for label in soup.find_all("li", class_="data-header__label"):
        if "Posición" in label.get_text():
            content = label.find("span", class_="data-header__content")
            position = content.get_text(strip=True) if content else None
            break

    return PlayerProfile(player_id=player_id, name=name, birth_date=birth_date, position=position)


# --- valores de mercado (JSON ya validado) -----------------------------------


def market_value_rows(graph: MarketValueGraph, *, player_id: int) -> list[dict]:
    rows: list[dict] = []
    for point in graph.points:
        rows.append(
            {
                "player_id": player_id,
                "date": _tm_date(point.date),
                "value": point.value,
                "club_name": point.club_name,
            }
        )
    return rows


# --- traspasos (JSON ya validado) --------------------------------------------


def classify_transfer(fee: str) -> str:
    """Clasifica un movimiento en cesión, fin de cesión o traspaso.

    Transfermarkt.es rotula el coste en español: 'Cesión', 'Fin de cesión', o
    bien un importe / 'Libre' / '-' para un traspaso. Comparamos sin tildes por
    robustez.
    """
    normalized = _norm(fee)
    if "fin de cesion" in normalized:
        return "fin de cesión"
    if "cesion" in normalized:
        return "cesión"
    return "traspaso"


def transfer_rows(history: TransferHistory, *, player_id: int) -> list[dict]:
    rows: list[dict] = []
    for transfer in history.transfers:
        rows.append(
            {
                "player_id": player_id,
                "date": _tm_date_iso(transfer.date),
                "season": transfer.season,
                "type": classify_transfer(transfer.fee),
                "fee": transfer.fee,
                "market_value": transfer.market_value,
                "from_club_id": _club_id_from_href(transfer.from_club.href),
                "from_club_name": transfer.from_club.club_name,
                "to_club_id": _club_id_from_href(transfer.to_club.href),
                "to_club_name": transfer.to_club.club_name,
            }
        )
    return rows


def _tm_date_iso(value: str) -> date | None:
    """Fecha en formato ISO (YYYY-MM-DD) de los traspasos; None si vacía."""
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --- disponibilidad (performance-game, JSON ya validado) ---------------------


def availability_rows(response: PerformanceResponse, *, player_id: int) -> list[dict]:
    """Una fila por jugador-partido con el estado de participación y los minutos.

    De cada partido tomamos disponibilidad (`participation_state`: played /
    in squad / not in squad / injured / absent, entre otros — el conjunto es
    abierto y se guarda tal cual, sin cerrarlo a un enum), minutos,
    titular/suplente, minuto de entrada y de salida, y los marcadores de
    lesión/ausencia. No curamos eventing (goles, tarjetas): la nota de
    Transfermarkt viene siempre null y esa fuente es SofaScore.
    """
    rows: list[dict] = []
    for game in response.data.performance:
        info = game.game_information
        general = game.statistics.general
        playing = game.statistics.playing_time
        rows.append(
            {
                "player_id": player_id,
                "game_id": info.game_id,
                "date": _date_from_iso_datetime(info.date.date_utc),
                "competition_id": info.competition_id,
                "season_id": info.season_id,
                "game_day": info.game_day,
                "club_id": general.primary_club_id,
                "opponent_club_id": _int_or_none(game.clubs_information.opponent.club_id),
                "participation_state": general.participation_state,
                "played_minutes": playing.played_minutes,
                "is_starting": playing.is_starting,
                "substituted_in_minute": (
                    playing.substituted_in.minute if playing.substituted_in else None
                ),
                "substituted_out_minute": (
                    playing.substituted_out.minute if playing.substituted_out else None
                ),
                "injury_id": general.injury_id,
                "absence_id": general.absence_id,
            }
        )
    return rows


def _date_from_iso_datetime(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


# --- historial de lesiones (HTML) --------------------------------------------


def parse_injuries(payload: bytes | str, *, player_id: int) -> list[Injury]:
    """Historial de lesiones de la página `verletzungen`.

    Columnas: temporada, lesión (diagnóstico), desde, hasta, días de baja y
    partidos perdidos. Un jugador sin lesiones registra una lista vacía.
    """
    soup = _soup(payload)
    table = soup.find("table", class_="items")
    if table is None or table.find("tbody") is None:
        # Sin lesiones Transfermarkt no dibuja la tabla; no es un error de formato.
        return []
    injuries: list[Injury] = []
    for row in table.find("tbody").find_all("tr", recursive=False):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        text = [cell.get_text(strip=True) for cell in cells]
        injuries.append(
            Injury(
                player_id=player_id,
                season=text[0],
                injury=text[1],
                from_date=_tm_date(text[2]),
                until_date=_tm_date(text[3]),
                days=_leading_int(text[4]),
                games_missed=_leading_int(text[5]),
            )
        )
    return injuries


def _leading_int(value: str) -> int | None:
    """Primer entero de una celda ('233 dias' -> 233, '-' -> None)."""
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None
