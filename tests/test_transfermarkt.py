"""Tests del cliente e ingesta de Transfermarkt contra fixtures reales, sin red.

Las fixtures se grabaron el 2026-07-08 del caso Álex Forés (spieler 709380) y la
plantilla del Real Oviedo (verein 2497), el mismo caso del experimento
docs/experiments/2026-07-07-alex-fores.md.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.sources.http import SourceHTTPError
from lfdata.sources.transfermarkt import (
    SourceFormatError,
    TransfermarktClient,
    ingest_clubs,
    ingest_player,
    ingest_squad_values,
    ingest_squads,
)
from lfdata.sources.transfermarkt.parse import (
    _tm_market_value,
    availability_rows,
    classify_transfer,
    market_value_rows,
    parse_competition_clubs,
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


# --- valor de plantilla por club (issue #69) ---------------------------------


def test_tm_market_value_parses_units() -> None:
    assert _tm_market_value("€1.30bn") == 1_300_000_000
    assert _tm_market_value("€29.45m") == 29_450_000
    assert _tm_market_value("€500k") == 500_000
    assert _tm_market_value("€900Th.") == 900_000
    assert _tm_market_value("-") is None
    assert _tm_market_value("") is None


def test_parse_competition_clubs_extracts_squad_value() -> None:
    clubs = parse_competition_clubs(fixture("competition-clubs-ES1.html"))
    assert len(clubs) == 20
    # El primero de la fixture es el Real Madrid, con €1.30bn de valor de plantilla.
    madrid = clubs[0]
    assert madrid.name == "Real Madrid"
    assert madrid.squad_value == 1_300_000_000
    assert all(club.squad_value and club.squad_value > 0 for club in clubs)


def _seed_team_mapping(mappings: Path, tm_club_id: int, canonical_id: str) -> None:
    mappings.mkdir(exist_ok=True)
    pd.DataFrame(
        [
            {
                "canonical_id": canonical_id,
                "fuente": "transfermarkt",
                "id_en_fuente": str(tm_club_id),
                "metodo": "manual",
                "fecha": "2026-07-17",
            }
        ]
    ).to_csv(mappings / "teams.csv", index=False)


def test_ingest_squad_values_writes_table_with_league_average(
    storage: Storage, tmp_path: Path
) -> None:
    clubs = parse_competition_clubs(fixture("competition-clubs-ES1.html"))
    madrid_id = clubs[0].id
    mappings = tmp_path / "mappings"
    _seed_team_mapping(mappings, madrid_id, "t001")

    result = ingest_squad_values(
        storage,
        season=2025,
        leagues=["la-liga"],
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
    )
    assert result.rows["squad_values"] == 20
    assert result.stats["ligas"] == 1

    values = storage.curated.read_table("squad_values")
    assert len(values) == 20
    assert set(values["competition"].unique()) == {"la-liga"}
    madrid = values[values["club_id"] == madrid_id].iloc[0]
    assert madrid["squad_value"] == 1_300_000_000
    # El club de La Liga con mapping queda resuelto a canónico; el resto, vacío.
    assert madrid["canonical_team_id"] == "t001"
    assert (values.loc[values["club_id"] != madrid_id, "canonical_team_id"] == "").all()

    # El nivel de liga sale de un promedio simple (acceptance criterion).
    league_level = values["squad_value"].mean()
    assert league_level > 0


def test_ingest_squad_values_partitions_by_competition_and_season(
    storage: Storage, tmp_path: Path
) -> None:
    # Con la misma página-fixture para dos ligas, cada una es su propia partición.
    ingest_squad_values(
        storage,
        season=2025,
        leagues=["la-liga", "premier-league"],
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )
    values = storage.curated.read_table("squad_values")
    assert set(values["competition"].unique()) == {"la-liga", "premier-league"}
    assert set(values["season"].astype(str).unique()) == {"2025"}
    # Los clubes extranjeros conservan su id de Transfermarkt, sin canónico.
    premier = values[values["competition"] == "premier-league"]
    assert (premier["canonical_team_id"] == "").all()


def test_ingest_squad_values_cached_recures_without_refetching(
    storage: Storage, tmp_path: Path
) -> None:
    ingest_squad_values(
        storage,
        season=2025,
        leagues=["la-liga"],
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )
    transport = RoutingTransport(default_routes())
    result = ingest_squad_values(
        storage,
        season=2025,
        leagues=["la-liga"],
        mappings_dir=str(tmp_path / "mappings"),
        transport=transport,
        cached=True,
    )
    assert result.rows["squad_values"] == 20
    assert transport.urls == []  # se re-curó desde raw/, sin pedir nada


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


def test_segunda_backfill_curates_into_its_own_partition_without_duplicating_history(
    storage: Storage, tmp_path: Path
) -> None:
    """El backfill opcional de Segunda (#93) cura en ``competition=segunda-division`` y
    no duplica el historial de un jugador ya alcanzado desde La Liga: la invariante de
    partición única lo retira de la-liga al reescribirlo en segunda-division (ADR 0013).
    La pertenencia a plantilla (``transfermarkt_players``), en cambio, sí coexiste por
    (competición, temporada): un jugador puede haber estado en ambas.
    """
    # Se alcanza a los jugadores primero desde La Liga (mismo kader-fixture).
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    reached = set(
        storage.curated.read_partition("market_values_tm", partition={"competition": "la-liga"})[
            "player_id"
        ]
    )
    assert reached

    # Ahora el backfill de Segunda alcanza a esos mismos jugadores.
    result = ingest_squads(
        storage,
        "segunda-division",
        season=2025,
        transport=RoutingTransport(default_routes()),
        max_clubs=1,
    )
    assert result.rows["market_values_tm"] > 0

    # El historial (carrera completa) vive en una sola partición: se movió a Segunda.
    market = storage.curated.read_table("market_values_tm")
    moved = market[market["player_id"].isin(reached)]
    assert moved["competition"].unique().tolist() == ["segunda-division"]

    # La pertenencia a plantilla sí queda en ambas particiones de competición.
    players = storage.curated.read_table("transfermarkt_players")
    assert set(players["competition"].astype(str).unique()) == {"la-liga", "segunda-division"}


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


def test_since_days_recures_from_raw_without_refetching(storage: Storage) -> None:
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    before = storage.curated.read_table("transfermarkt_players")

    # Todos se descargaron hoy: con una ventana amplia, no se vuelve a pedir a la
    # fuente a ninguno... pero se les cura igual, parseando el raw ya guardado.
    transport = RoutingTransport(default_routes())
    result = ingest_squads(
        storage,
        "la-liga",
        season=2025,
        transport=transport,
        max_clubs=1,
        since_days=30,
    )
    assert result.rows["transfermarkt_players"] == len(before)
    assert not [url for url in transport.urls if "spieler" in url or "ceapi" in url]
    after = storage.curated.read_table("transfermarkt_players")
    assert len(after) == len(before)


def test_since_days_refills_a_player_missing_from_curated(storage: Storage) -> None:
    """El jugador ya bajado pero ausente de la tabla vuelve a entrar (regresión).

    Antes, ``--since-days`` saltaba al jugador *antes* de curarlo: quien hubiera
    desaparecido de la partición (p. ej. podado por el refresh de otra temporada)
    no volvía a entrar nunca, porque su raw reciente hacía que se le saltara.
    """
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    partition = {"competition": "la-liga", "season": "2025"}
    full = storage.curated.read_partition("transfermarkt_players", partition=partition)
    storage.curated.write_table("transfermarkt_players", full.iloc[2:], partition=partition)
    missing = set(full.iloc[:2]["id"])
    assert not missing & set(
        storage.curated.read_partition("transfermarkt_players", partition=partition)["id"]
    )

    transport = RoutingTransport(default_routes())
    ingest_squads(storage, "la-liga", season=2025, transport=transport, max_clubs=1, since_days=30)

    players = storage.curated.read_partition("transfermarkt_players", partition=partition)
    assert missing <= set(players["id"])  # los dos que faltaban están de vuelta
    assert len(players) == len(full)
    assert not [url for url in transport.urls if "spieler" in url]  # sin re-scrapear


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
    partition = {"competition": "la-liga", "season": "2025"}
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


def test_full_refresh_of_a_season_leaves_other_seasons_intact(storage: Storage) -> None:
    """Ingerir una temporada no poda a los jugadores de otra (regresión).

    ``transfermarkt_players`` estuvo particionada solo por competición, así que
    el refresh completo de 2023 retiraba a todo el que no jugara en 2023 —
    incluidos los de la temporada en curso—, y ``--since-days`` impedía después
    que volvieran. Cada temporada es ahora su propia partición.
    """
    ingest_squads(storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()))
    before = storage.curated.read_partition(
        "transfermarkt_players", partition={"competition": "la-liga", "season": "2025"}
    )
    assert len(before) == 39

    # Refresh completo de otra temporada, con las mismas plantillas-fixture.
    ingest_squads(storage, "la-liga", season=2023, transport=RoutingTransport(default_routes()))

    after = storage.curated.read_partition(
        "transfermarkt_players", partition={"competition": "la-liga", "season": "2025"}
    )
    assert set(after["id"]) == set(before["id"])  # 2025 sigue entera
    seasons = storage.curated.read_table("transfermarkt_players")["season"]
    assert set(seasons.astype(str)) == {"2025", "2023"}


def test_partial_run_never_retires(storage: Storage) -> None:
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    partition = {"competition": "la-liga", "season": "2025"}
    seeded = storage.curated.read_table("transfermarkt_players")
    phantom = seeded.iloc[[0]].copy()
    phantom["id"] = 999999
    storage.curated.upsert_table("transfermarkt_players", phantom, key="id", partition=partition)

    # Un run con max_clubs no es refresh completo: no retira a nadie visto ni al fantasma.
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    assert 999999 in set(storage.curated.read_table("transfermarkt_players")["id"])


def test_ingest_clubs_refreshes_only_the_clubs_asked_for(storage: Storage) -> None:
    # El refresh dirigido del detector de fichajes: una plantilla, no las veinte.
    transport = RoutingTransport(default_routes())

    result = ingest_clubs(storage, "la-liga", [2497], season=2025, transport=transport)

    kaders = [url for url in transport.urls if "kader/verein" in url]
    assert len(kaders) == 1
    assert "verein/2497" in kaders[0]
    assert result.rows["transfermarkt_players"] == 39


def test_ingest_clubs_never_retires(storage: Storage) -> None:
    ingest_squads(storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()))
    partition = {"competition": "la-liga", "season": "2025"}
    seeded = storage.curated.read_table("transfermarkt_players")
    phantom = seeded.iloc[[0]].copy()
    phantom["id"] = 999999
    storage.curated.upsert_table("transfermarkt_players", phantom, key="id", partition=partition)

    # Solo se ha visto un club: retirar al resto sería borrar a quien ni se miró.
    ingest_clubs(
        storage, "la-liga", [2497], season=2025, transport=RoutingTransport(default_routes())
    )

    assert 999999 in set(storage.curated.read_table("transfermarkt_players")["id"])


def test_ingest_clubs_warns_about_a_club_outside_the_competition(storage: Storage, caplog) -> None:
    result = ingest_clubs(
        storage, "la-liga", [123456], season=2025, transport=RoutingTransport(default_routes())
    )

    assert result.rows["transfermarkt_players"] == 0
    assert "123456" in caplog.text


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


# --- ingesta por jugador (fuera de plantilla) --------------------------------


def _seed_player_mapping(
    mappings: Path, *, canonical_id: str, tm_id: int, biwenger_id: int | None = None
) -> None:
    mappings.mkdir(exist_ok=True)
    rows = [
        {
            "canonical_id": canonical_id,
            "fuente": "transfermarkt",
            "id_en_fuente": str(tm_id),
            "metodo": "manual",
            "fecha": "2026-07-17",
        }
    ]
    if biwenger_id is not None:
        rows.append(
            {
                "canonical_id": canonical_id,
                "fuente": "biwenger",
                "id_en_fuente": str(biwenger_id),
                "metodo": "manual",
                "fecha": "2026-07-17",
            }
        )
    pd.DataFrame(rows).to_csv(mappings / "players.csv", index=False)


def test_ingest_player_curates_only_history_tables(storage: Storage, tmp_path: Path) -> None:
    transport = RoutingTransport(default_routes())
    result = ingest_player(
        storage, str(FORES), mappings_dir=str(tmp_path / "mappings"), transport=transport
    )

    # Las cuatro tablas de historial se curan; transfermarkt_players NUNCA.
    assert set(result.rows) == {"market_values_tm", "transfers", "availability_tm", "injuries_tm"}
    assert result.rows["market_values_tm"] == 15
    assert result.rows["transfers"] == 11
    assert result.rows["availability_tm"] == 5
    assert result.rows["injuries_tm"] == 1
    with pytest.raises(FileNotFoundError):
        storage.curated.read_table("transfermarkt_players")

    # Aterriza en la partición centinela bajo-demanda (ADR 0013).
    values = storage.curated.read_table("market_values_tm")
    assert values["competition"].unique().tolist() == ["bajo-demanda"]
    assert set(values["player_id"]) == {FORES}


def test_ingest_player_accepts_url(storage: Storage, tmp_path: Path) -> None:
    url = f"https://www.transfermarkt.com/alex-fores/profil/spieler/{FORES}"
    result = ingest_player(
        storage,
        url,
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )
    assert result.rows["transfers"] == 11


def test_ingest_player_accepts_canonical_id(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_player_mapping(mappings, canonical_id="p00001", tm_id=FORES)
    result = ingest_player(
        storage, "p00001", mappings_dir=str(mappings), transport=RoutingTransport(default_routes())
    )
    assert set(storage.curated.read_table("transfers")["player_id"]) == {FORES}
    assert result.rows["injuries_tm"] == 1


def test_ingest_player_canonical_without_tm_mapping_errors(
    storage: Storage, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="no tiene mapping a Transfermarkt"):
        ingest_player(
            storage,
            "p09999",
            mappings_dir=str(tmp_path / "mappings"),
            transport=RoutingTransport(default_routes()),
        )


def test_ingest_player_cached_recures_without_refetching(storage: Storage, tmp_path: Path) -> None:
    mappings = str(tmp_path / "mappings")
    ingest_player(
        storage, str(FORES), mappings_dir=mappings, transport=RoutingTransport(default_routes())
    )
    transport = RoutingTransport(default_routes())
    result = ingest_player(
        storage, str(FORES), mappings_dir=mappings, transport=transport, cached=True
    )
    assert result.rows["transfers"] == 11
    assert transport.urls == []  # re-curado desde raw/, sin pedir nada


def _seed_biwenger_birth_date(storage: Storage, biwenger_id: int, birth_date: str) -> None:
    df = pd.DataFrame(
        {
            "id": [biwenger_id],
            "name": ["Álex Forés"],
            "birth_date": pd.to_datetime([birth_date]),
            "team_id": [1],
        }
    )
    storage.curated.write_table("biwenger_players", df, partition={"competition": "la-liga"})


def test_ingest_player_blocks_on_birthdate_discrepancy(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_player_mapping(mappings, canonical_id="p00001", tm_id=FORES, biwenger_id=42)
    # Biwenger dice una fecha distinta de la del perfil de Transfermarkt (2001-04-12).
    _seed_biwenger_birth_date(storage, 42, "1999-01-01")

    result = ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
    )
    # No se cura nada; se cuenta como anomalía visible.
    assert result.rows["transfers"] == 0
    assert result.anomalies
    with pytest.raises(FileNotFoundError):
        storage.curated.read_table("transfers")


def test_ingest_player_force_ingests_despite_discrepancy(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_player_mapping(mappings, canonical_id="p00001", tm_id=FORES, biwenger_id=42)
    _seed_biwenger_birth_date(storage, 42, "1999-01-01")

    result = ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
        force=True,
    )
    assert result.rows["transfers"] == 11
    assert set(storage.curated.read_table("transfers")["player_id"]) == {FORES}


def test_ingest_player_matching_birthdate_curates(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_player_mapping(mappings, canonical_id="p00001", tm_id=FORES, biwenger_id=42)
    _seed_biwenger_birth_date(storage, 42, "2001-04-12")  # coincide con el perfil
    result = ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
    )
    assert result.rows["transfers"] == 11


def test_ingest_player_moves_active_player_out_of_league_partition(
    storage: Storage, tmp_path: Path
) -> None:
    """El upsert global no deja al jugador duplicado si ya estaba en otra partición."""
    # Primero se le alcanza desde La Liga (ingesta por competición).
    ingest_squads(
        storage, "la-liga", season=2025, transport=RoutingTransport(default_routes()), max_clubs=1
    )
    # Forés (709380) no está en el Madrid-fixture, así que lo sembramos en la-liga a mano.
    seeded = pd.DataFrame(
        {"player_id": [FORES], "date": pd.to_datetime(["2025-01-01"]), "value": [1]}
    )
    storage.curated.upsert_unique_partition(
        "market_values_tm", seeded, partition={"competition": "la-liga"}
    )
    assert (storage.curated.read_table("market_values_tm")["player_id"] == FORES).sum() == 1

    # Ahora por id: se mueve a bajo-demanda, sin quedar en dos particiones.
    ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )
    market = storage.curated.read_table("market_values_tm")
    fores = market[market["player_id"] == FORES]
    assert fores["competition"].unique().tolist() == ["bajo-demanda"]


def test_cli_ingest_player_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
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
            "transfermarkt-player",
            "--player",
            str(FORES),
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "transfers: 11 filas" in out
    parquet = tmp_path / "curated" / "transfers" / "competition=bajo-demanda" / "data.parquet"
    assert parquet.exists()
    assert not (tmp_path / "curated" / "transfermarkt_players").exists()


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
