"""Tests del cliente e ingesta de Transfermarkt contra fixtures reales, sin red.

Las fixtures se grabaron el 2026-07-08 del caso Álex Forés (spieler 709380) y la
plantilla del Real Oviedo (verein 2497), el mismo caso del experimento
docs/experiments/2026-07-07-alex-fores.md.
"""

from datetime import date
from pathlib import Path

import pytest

from lfdata.cli import main
from lfdata.sources.http import SourceHTTPError
from lfdata.sources.transfermarkt import SourceFormatError, TransfermarktClient, ingest_squads
from lfdata.sources.transfermarkt.parse import (
    availability_rows,
    classify_transfer,
    market_value_rows,
    parse_injuries,
    parse_profile,
    transfer_rows,
)
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "transfermarkt"
FORES = 709380


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class RoutingTransport:
    """Devuelve una fixture según el trozo de URL pedido; registra las llamadas."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def get(self, url: str, params=None) -> bytes:
        self.urls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"URL sin fixture en el test: {url}")


def default_routes() -> dict[str, bytes]:
    return {
        "startseite/wettbewerb": fixture("competition-clubs-ES1.html"),
        "kader/verein": fixture("kader-2497.html"),
        "profil/spieler": fixture("profile-709380.html"),
        "marketValueDevelopment": fixture("marketvalue-709380.json"),
        "transferHistory": fixture("transfers-709380.json"),
        "performance-game": fixture("performance-709380.json"),
        "verletzungen/spieler": fixture("injuries-709380.html"),
    }


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path}")


def raw_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "raw"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# --- parseo HTML -------------------------------------------------------------


def test_fetch_competition_clubs(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    clubs = TransfermarktClient(transport, storage.raw).fetch_competition_clubs(
        "la-liga", season=2025
    )
    assert len(clubs) == 20
    ids = {club.id for club in clubs}
    assert 2497 in ids  # Real Oviedo
    oviedo = next(club for club in clubs if club.id == 2497)
    assert oviedo.name == "Real Oviedo"


def test_unknown_competition_rejected(storage: Storage) -> None:
    client = TransfermarktClient(RoutingTransport({}), storage.raw)
    with pytest.raises(ValueError, match="premier"):
        client.fetch_competition_clubs("premier", season=2025)


def test_competition_clubs_raw_names_include_season(storage: Storage, tmp_path: Path) -> None:
    # Dos temporadas ingeridas el mismo día no se pisan: el nombre lleva la temporada.
    client = TransfermarktClient(RoutingTransport(default_routes()), storage.raw)
    client.fetch_competition_clubs("la-liga", season=2024)
    client.fetch_competition_clubs("la-liga", season=2025)
    names = sorted(p.name for p in raw_files(tmp_path) if "competition-clubs" in p.as_posix())
    assert names == ["ES1-saison-2024.html", "ES1-saison-2025.html"]


def test_fetch_squad(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    squad = TransfermarktClient(transport, storage.raw).fetch_squad(2497, season=2025)
    assert len(squad) == 39
    keeper = squad[0]
    assert keeper.name == "Aarón Escandell"
    assert keeper.position == "Goalkeeper"
    assert keeper.slug == "aaron-escandell"
    assert keeper.shirt_number == 13


def test_fetch_player_profile(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    profile = TransfermarktClient(transport, storage.raw).fetch_player_profile(
        FORES, slug="alex-fores"
    )
    assert profile.name == "Álex Forés"
    assert profile.birth_date == date(2001, 4, 12)
    assert profile.position == "Centre-Forward"


def test_empty_slug_falls_back_to_placeholder(storage: Storage) -> None:
    # Un jugador con slug vacío en la plantilla no debe producir URLs con `//`.
    transport = RoutingTransport(default_routes())
    client = TransfermarktClient(transport, storage.raw)
    client.fetch_player_profile(FORES, slug="")
    client.fetch_injuries(FORES, slug="   ")  # solo espacios cuenta como vacío
    for url in transport.urls:
        assert "//" not in url.removeprefix("https://")
    assert f"/spieler/profil/spieler/{FORES}" in transport.urls[0]
    assert f"/spieler/verletzungen/spieler/{FORES}" in transport.urls[1]


def test_profile_name_drops_shirt_number() -> None:
    # El h1 del perfil antepone el dorsal; el nombre no debe incluirlo.
    html = (
        '<h1 class="data-header__headline-wrapper">'
        '<span class="data-header__shirt-number"> #1 </span>'
        "Thibaut <strong>Courtois</strong></h1>"
    )
    profile = parse_profile(html, player_id=108390)
    assert profile.name == "Thibaut Courtois"


# --- valores de mercado (ceapi JSON) -----------------------------------------


def test_market_values(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    graph = TransfermarktClient(transport, storage.raw).fetch_market_value(FORES)
    rows = market_value_rows(graph, player_id=FORES)
    assert len(rows) == 15
    first = rows[0]
    assert first == {
        "player_id": FORES,
        "date": date(2021, 10, 13),
        "value": 50000,
        "club_name": "Villarreal CF B",
    }


# --- traspasos y cesiones (ceapi JSON): el caso Forés ------------------------


def test_transfers_reproduce_loan_chain(storage: Storage) -> None:
    """El acceptance criterion: la cadena de cesiones del experimento Forés."""
    transport = RoutingTransport(default_routes())
    history = TransfermarktClient(transport, storage.raw).fetch_transfers(FORES)
    rows = transfer_rows(history, player_id=FORES)
    chain = {(row["date"], row["type"], row["from_club_name"], row["to_club_name"]) for row in rows}
    assert (date(2025, 1, 20), "loan", "Villarreal", "Levante") in chain
    assert (date(2025, 6, 30), "end of loan", "Levante", "Villarreal") in chain
    assert (date(2025, 7, 24), "loan", "Villarreal", "Real Oviedo") in chain
    assert (date(2026, 6, 30), "end of loan", "Real Oviedo", "Villarreal") in chain
    # Un traspaso real (subida al primer equipo) queda como 'transfer'.
    types = {row["type"] for row in rows}
    assert types == {"loan", "end of loan", "transfer"}


def test_transfers_carry_club_ids(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    history = TransfermarktClient(transport, storage.raw).fetch_transfers(FORES)
    loan = next(
        row for row in transfer_rows(history, player_id=FORES) if row["date"] == date(2025, 7, 24)
    )
    assert loan["from_club_id"] == 1050  # Villarreal
    assert loan["to_club_id"] == 2497  # Real Oviedo


def test_classify_transfer() -> None:
    assert classify_transfer("loan transfer") == "loan"
    assert classify_transfer("End of loan") == "end of loan"
    assert classify_transfer("€1.50m") == "transfer"
    assert classify_transfer("free transfer") == "transfer"
    assert classify_transfer("-") == "transfer"


# --- disponibilidad (performance-game) ---------------------------------------


def test_availability_covers_all_participation_states(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    response = TransfermarktClient(transport, storage.raw).fetch_performance(FORES)
    rows = availability_rows(response, player_id=FORES)
    assert {row["participation_state"] for row in rows} == {
        "played",
        "in squad",
        "not in squad",
        "injured",
    }


def test_availability_playing_time_and_injury_markers(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    response = TransfermarktClient(transport, storage.raw).fetch_performance(FORES)
    by_state: dict[str, dict] = {}
    for row in availability_rows(response, player_id=FORES):
        by_state.setdefault(row["participation_state"], row)

    starter = by_state["played"]
    assert starter["is_starting"] is True
    assert starter["played_minutes"] == 45
    assert starter["substituted_out_minute"] == 46

    injured = by_state["injured"]
    assert injured["played_minutes"] is None
    assert injured["injury_id"] != 0


# --- historial de lesiones ---------------------------------------------------


def test_fetch_injuries(storage: Storage) -> None:
    transport = RoutingTransport(default_routes())
    injuries = TransfermarktClient(transport, storage.raw).fetch_injuries(FORES, slug="alex-fores")
    assert len(injuries) == 1
    injury = injuries[0]
    assert injury.injury == "Broken tibia"
    assert injury.from_date == date(2024, 5, 14)
    assert injury.until_date == date(2025, 1, 1)
    assert injury.days == 233
    assert injury.games_missed == 26


def test_injuries_empty_when_no_table() -> None:
    # Un jugador sin lesiones: Transfermarkt no dibuja la tabla; lista vacía, sin error.
    assert parse_injuries(b"<html><body>Sin lesiones</body></html>", player_id=1) == []


# --- raw antes de interpretar ------------------------------------------------


def test_raw_written_before_interpreting_json(storage: Storage, tmp_path: Path) -> None:
    transport = RoutingTransport({"transferHistory": b'{"transfers": 42}'})
    with pytest.raises(SourceFormatError, match="cambió la forma"):
        TransfermarktClient(transport, storage.raw).fetch_transfers(FORES)
    files = raw_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == b'{"transfers": 42}'


def test_raw_written_before_interpreting_html(storage: Storage, tmp_path: Path) -> None:
    transport = RoutingTransport({"kader/verein": b"<html><body>sin tabla</body></html>"})
    with pytest.raises(SourceFormatError, match="plantilla"):
        TransfermarktClient(transport, storage.raw).fetch_squad(2497, season=2025)
    assert len(raw_files(tmp_path)) == 1


# --- ingesta completa a tablas curadas ---------------------------------------


def test_ingest_squads_writes_curated_tables(storage: Storage, tmp_path: Path) -> None:
    transport = RoutingTransport(default_routes())
    result = ingest_squads(storage, "la-liga", season=2025, transport=transport, max_clubs=1)

    # 1 club (Real Madrid, primero de la competición) con la plantilla-fixture de 39.
    rows = result.rows
    assert rows["transfermarkt_players"] == 39
    assert rows["market_values_tm"] == 39 * 15
    assert rows["transfers"] == 39 * 11
    assert rows["availability_tm"] == 39 * 5
    assert rows["injuries_tm"] == 39 * 1

    players = storage.curated.read_table("transfermarkt_players")
    assert {"id", "slug", "name", "birth_date", "position", "club_id", "competition"} <= set(
        players.columns
    )
    assert players["competition"].astype(str).unique().tolist() == ["la-liga"]

    parquet = tmp_path / "curated" / "transfers" / "competition=la-liga" / "data.parquet"
    assert parquet.exists()

    transfers = storage.curated.read_table("transfers")
    assert set(transfers["type"].unique()) == {"loan", "end of loan", "transfer"}

    availability = storage.curated.read_table("availability_tm")
    assert {"player_id", "game_id", "participation_state", "played_minutes", "competition"} <= set(
        availability.columns
    )
    assert set(availability["participation_state"].unique()) == {
        "played",
        "in squad",
        "not in squad",
        "injured",
    }

    injuries = storage.curated.read_table("injuries_tm")
    assert {"player_id", "injury", "from_date", "days", "games_missed", "competition"} <= set(
        injuries.columns
    )


class FailAfterFirstClubTransport(RoutingTransport):
    """Como RoutingTransport, pero cae al pedir la plantilla del segundo club."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        super().__init__(routes)
        self.squad_fetches = 0

    def get(self, url: str, params=None) -> bytes:
        if "kader/verein" in url:
            self.squad_fetches += 1
            if self.squad_fetches > 1:
                raise RuntimeError("red caída a mitad de run")
        return super().get(url, params)


def test_ingest_preserves_progress_of_written_clubs_on_failure(
    storage: Storage, tmp_path: Path
) -> None:
    transport = FailAfterFirstClubTransport(default_routes())
    with pytest.raises(RuntimeError, match="red caída"):
        ingest_squads(storage, "la-liga", season=2025, transport=transport, max_clubs=2)

    # El primer club se escribió por upsert antes de que fallara el segundo.
    players = storage.curated.read_table("transfermarkt_players")
    assert len(players) == 39


def test_ingest_is_idempotent(storage: Storage) -> None:
    first = ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    second = ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    assert second == first  # correr al mismo jugador dos veces no duplica

    players = storage.curated.read_table("transfermarkt_players")
    assert len(players) == first.rows["transfermarkt_players"]
    values = storage.curated.read_table("market_values_tm")
    assert len(values) == first.rows["market_values_tm"]


def test_ingest_since_days_skips_recently_scraped(storage: Storage) -> None:
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    before = storage.curated.read_table("transfermarkt_players")

    # Todos se descargaron hoy: con una ventana amplia, se saltan todos.
    result = ingest_squads(
        storage,
        "la-liga",
        season=2025,
        transport=RoutingTransport(default_routes()),
        max_clubs=1,
        since_days=30,
    )
    assert result.rows == dict.fromkeys(result.rows, 0)
    after = storage.curated.read_table("transfermarkt_players")
    assert len(after) == len(before)  # la partición queda intacta


# --- resiliencia: 404 por jugador y refresh que retira a quien salió (#36) ---


class Fail404OnProfileTransport(RoutingTransport):
    """Como RoutingTransport, pero devuelve 404 en el perfil del slug indicado."""

    def __init__(self, routes: dict[str, bytes], fail_slug: str) -> None:
        super().__init__(routes)
        self.fail_slug = fail_slug

    def get(self, url: str, params=None) -> bytes:
        if f"{self.fail_slug}/profil/spieler" in url:
            raise SourceHTTPError(url, 404)
        return super().get(url, params)


def test_ingest_404_skips_player_and_curates_rest(storage: Storage) -> None:
    # El primer jugador de la plantilla-fixture es el portero Aarón Escandell.
    transport = Fail404OnProfileTransport(default_routes(), "aaron-escandell")
    result = ingest_squads(storage, "la-liga", season=2025, transport=transport, max_clubs=1)

    assert len(result.failures) == 1
    assert result.failures[0].player == "aaron-escandell"
    assert result.failures[0].status == 404
    players = storage.curated.read_table("transfermarkt_players")
    assert len(players) == 38  # 39 menos el que dio 404
    assert "aaron-escandell" not in set(players["slug"])


def test_full_refresh_retires_departed_player(storage: Storage) -> None:
    # Siembra la partición y le añade un jugador fantasma que ya no juega.
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    partition = {"competition": "la-liga"}
    seeded = storage.curated.read_table("transfermarkt_players")
    phantom = seeded.iloc[[0]].copy()
    phantom["id"] = 999999
    phantom["slug"] = "fantasma"
    storage.curated.upsert_table("transfermarkt_players", phantom, key="id", partition=partition)
    assert 999999 in set(storage.curated.read_table("transfermarkt_players")["id"])

    # Refresh completo (sin max_clubs): recorre la competición y retira al ausente.
    ingest_squads(storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()))
    players = storage.curated.read_table("transfermarkt_players")
    assert 999999 not in set(players["id"])  # el fantasma salió
    assert len(players) == 39  # solo los vistos en plantilla


def test_partial_run_never_retires(storage: Storage) -> None:
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    partition = {"competition": "la-liga"}
    seeded = storage.curated.read_table("transfermarkt_players")
    phantom = seeded.iloc[[0]].copy()
    phantom["id"] = 999999
    storage.curated.upsert_table("transfermarkt_players", phantom, key="id", partition=partition)

    # Un run con max_clubs no es refresh completo: no retira a nadie visto ni al fantasma.
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    assert 999999 in set(storage.curated.read_table("transfermarkt_players")["id"])


class Fail502OnSquadTransport(RoutingTransport):
    """Como RoutingTransport, pero devuelve 502 en la plantilla del club N-ésimo."""

    def __init__(self, routes: dict[str, bytes], *, fail_on_fetch: int) -> None:
        super().__init__(routes)
        self.fail_on_fetch = fail_on_fetch
        self.squad_fetches = 0

    def get(self, url: str, params=None) -> bytes:
        if "kader/verein" in url:
            self.squad_fetches += 1
            if self.squad_fetches == self.fail_on_fetch:
                raise SourceHTTPError(url, 502)
        return super().get(url, params)


def test_ingest_502_on_squad_skips_club_and_curates_rest(storage: Storage) -> None:
    # La plantilla del segundo club da 502: se salta y se registra como fallo,
    # pero el primero se curó igual (no aborta el run entero).
    transport = Fail502OnSquadTransport(default_routes(), fail_on_fetch=2)
    result = ingest_squads(storage, "la-liga", season=2025, transport=transport, max_clubs=2)

    assert len(result.failures) == 1
    assert result.failures[0].status == 502
    assert result.failures[0].player.startswith("club ")
    players = storage.curated.read_table("transfermarkt_players")
    assert len(players) == 39  # solo el primer club (ambos comparten la fixture)


def test_squad_failure_skips_retirement_in_full_refresh(storage: Storage) -> None:
    # Siembra la partición y un fantasma que un refresh limpio retiraría.
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    partition = {"competition": "la-liga"}
    seeded = storage.curated.read_table("transfermarkt_players")
    phantom = seeded.iloc[[0]].copy()
    phantom["id"] = 999999
    storage.curated.upsert_table("transfermarkt_players", phantom, key="id", partition=partition)

    # Refresh completo donde la primera plantilla falla: se omite la retirada,
    # así un 502 transitorio no borra a jugadores que no se pudieron ver.
    transport = Fail502OnSquadTransport(default_routes(), fail_on_fetch=1)
    result = ingest_squads(storage, "la-liga", season=2025, transport=transport)

    assert any(f.status == 502 for f in result.failures)
    assert 999999 in set(storage.curated.read_table("transfermarkt_players")["id"])


def test_cli_ingest_404_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    routes = default_routes()

    def fake_get(self, url, params=None):
        if "aaron-escandell/profil/spieler" in url:
            raise SourceHTTPError(url, 404)
        for needle, payload in routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"URL sin fixture: {url}")

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    exit_code = main(
        [
            "ingest",
            "transfermarkt",
            "--competition",
            "la-liga",
            "--max-clubs",
            "1",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "1 jugadores fallaron" in out
    assert "aaron-escandell" in out


def test_cli_ingest_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    routes = default_routes()

    def fake_get(self, url, params=None):
        for needle, payload in routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"URL sin fixture: {url}")

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    exit_code = main(
        [
            "ingest",
            "transfermarkt",
            "--competition",
            "la-liga",
            "--max-clubs",
            "1",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "transfermarkt_players: 39 filas" in out
    values_parquet = (
        tmp_path / "curated" / "market_values_tm" / "competition=la-liga" / "data.parquet"
    )
    assert values_parquet.exists()
    assert raw_files(tmp_path)
