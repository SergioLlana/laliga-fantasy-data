"""Tests del catálogo de identidad de SofaScore y del re-estampado de canónicos.

``sofascore_players``/``sofascore_teams`` se reconstruyen desde raw/ (las
alineaciones y el calendario que el backfill ya descargó), sin peticiones. El
re-estampado rellena ``canonical_id`` en el eventing curado cruzándolo con los
mappings, sin releer raw/. Se usan las fixtures reales del caso Álex Forés.
"""

from pathlib import Path

import pandas as pd

from lfdata.cli import main
from lfdata.sources.sofascore import build_catalog, restamp_canonical
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "sofascore"

# El calendario (events-8-77559-last-0) trae dos partidos de LaLiga 25/26; la
# alineación (lineups) se graba bajo el event_id del primero, Celta 2-3 Levante.
EVENT_ID = 14083613


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def storage_at(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path / 'data'}")


def _seed_raw(storage: Storage) -> None:
    storage.raw.save(
        "sofascore", "tournament-events", "8-77559-last-0", fixture("events-8-77559-last-0.json")
    )
    storage.raw.save("sofascore", "event-lineups", str(EVENT_ID), fixture("lineups.json"))


def test_build_catalog_publishes_players_and_teams_from_raw(tmp_path):
    storage = storage_at(tmp_path)
    _seed_raw(storage)

    result = build_catalog(storage)

    players = storage.curated.read_table("sofascore_players")
    teams = storage.curated.read_table("sofascore_teams")
    assert result.rows["sofascore_players"] == len(players)
    assert result.rows["sofascore_teams"] == len(teams)

    # Competición en slug del proyecto (no el id opaco) y temporada en año de inicio.
    assert set(players["competition"]) == {"la-liga"}
    assert set(players["season"]) == {"2025"}
    assert set(teams["competition"]) == {"la-liga"}

    # Los cuatro equipos de los dos partidos del calendario.
    assert {"2821", "2849", "2816", "2846"} <= set(teams["team_id"].astype(str))


def test_catalog_player_carries_birthdate_and_side_team(tmp_path):
    storage = storage_at(tmp_path)
    _seed_raw(storage)
    build_catalog(storage)

    players = storage.curated.read_table("sofascore_players")
    # La fecha de nacimiento (que solo viene en las alineaciones) llega ISO.
    radu = players[players["sofascore_player_id"] == 815202].iloc[0]
    assert radu["birth_date"] == "1997-05-28"
    # El equipo sale del lado del lineup, uno de los dos del partido (no el teamId
    # ruidoso por jugador): Celta (local) o Levante (visitante).
    assert set(players["team_name"]) <= {"Celta Vigo", "Levante UD"}


def test_build_catalog_makes_no_requests(tmp_path):
    """El catálogo es puro raw→curado: nunca toca el transporte."""
    storage = storage_at(tmp_path)
    _seed_raw(storage)
    # build_catalog no recibe transporte ni cliente: si intentara pedir, fallaría.
    build_catalog(storage)
    assert not storage.curated.read_table("sofascore_players").empty


def test_lineup_without_event_metadata_is_skipped(tmp_path):
    """Una alineación cuyo evento no está en el calendario no se puede ubicar."""
    storage = storage_at(tmp_path)
    # Solo el lineup, sin su tournament-events: falta competición/temporada/equipo.
    storage.raw.save("sofascore", "event-lineups", str(EVENT_ID), fixture("lineups.json"))
    result = build_catalog(storage)
    assert result.rows["sofascore_players"] == 0


def test_cli_curate_sofascore_catalog(tmp_path, capsys):
    storage = storage_at(tmp_path)
    _seed_raw(storage)
    code = main(["curate", "sofascore-catalog", "--data", storage.base_uri])
    assert code == 0
    assert "sofascore_players" in capsys.readouterr().out
    assert not storage.curated.read_table("sofascore_players").empty


# --- re-estampado del canonical_id ------------------------------------------


def _seed_eventing(storage: Storage, canonical: str = "") -> None:
    """Siembra player_match_stats y player_season_stats con un id de SofaScore."""
    for table, extra in (
        ("player_match_stats", {"date": "2025-05-10", "minutes": 90}),
        ("player_season_stats", {"minutesPlayed": 382}),
    ):
        storage.curated.write_table(
            table,
            pd.DataFrame([{"canonical_id": canonical, "sofascore_player_id": 1086128, **extra}]),
            partition={"competition": "8", "season": "77559"},
        )


def _seed_mapping(tmp_path: Path) -> str:
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "p00001",
                "fuente": "sofascore",
                "id_en_fuente": "1086128",
                "metodo": "manual",
                "fecha": "2026-07-11",
            }
        ]
    ).to_csv(mappings / "players.csv", index=False)
    return str(mappings)


def test_restamp_fills_canonical_without_rereading_raw(tmp_path):
    storage = storage_at(tmp_path)
    _seed_eventing(storage, canonical="")
    mappings = _seed_mapping(tmp_path)

    result = restamp_canonical(storage, mappings_dir=mappings)

    assert result.rows["player_match_stats"] == 1
    assert result.rows["player_season_stats"] == 1
    for table in ("player_match_stats", "player_season_stats"):
        df = storage.curated.read_table(table)
        assert (df["canonical_id"] == "p00001").all()
        # No se re-descargó ni releyó raw: no hay raw en este test y aun así funciona.
        assert "sofascore_player_id" in df.columns


def test_restamp_is_idempotent(tmp_path):
    storage = storage_at(tmp_path)
    _seed_eventing(storage, canonical="p00001")
    mappings = _seed_mapping(tmp_path)

    restamp_canonical(storage, mappings_dir=mappings)
    df = storage.curated.read_table("player_match_stats")
    assert (df["canonical_id"] == "p00001").all()


def test_restamp_without_tables_is_noop(tmp_path):
    storage = storage_at(tmp_path)
    mappings = _seed_mapping(tmp_path)
    result = restamp_canonical(storage, mappings_dir=mappings)
    assert result.rows == {"player_match_stats": 0, "player_season_stats": 0}
