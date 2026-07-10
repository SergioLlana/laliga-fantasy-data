"""Tests de la capa de mappings: normalización, matching y verificación.

El matching se ancla en Biwenger y busca contraparte en Transfermarkt por club +
nombre normalizado (Biwenger no publica fecha de nacimiento en las tablas
curadas, decisión registrada en el issue #8). Los casos se montan con tablas
curadas sintéticas pequeñas, sin red.
"""

from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.mappings import check_mappings, run_map
from lfdata.mappings.normalize import name_compatible, normalize, team_compatible
from lfdata.mappings.store import MappingStore
from lfdata.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path}")


def seed(
    storage: Storage,
    *,
    competition: str = "la-liga",
    teams: list[dict],
    players: list[dict],
    tm_players: list[dict],
) -> None:
    partition = {"competition": competition}
    storage.curated.write_table("biwenger_teams", pd.DataFrame(teams), partition=partition)
    players_df = pd.DataFrame(players, columns=["id", "name", "team_id"]).astype(
        {"team_id": "Int64"}
    )
    storage.curated.write_table("biwenger_players", players_df, partition=partition)
    tm = pd.DataFrame(tm_players).astype({"club_id": "Int64"})
    tm["birth_date"] = pd.to_datetime(tm["birth_date"])
    storage.curated.write_table("transfermarkt_players", tm, partition=partition)


# --- normalización -----------------------------------------------------------


def test_normalize_strips_accents_and_punctuation() -> None:
    assert normalize("Álex Forés") == "alex fores"
    assert normalize("Vinícius Júnior") == "vinicius junior"


def test_name_compatible_by_surname_subset() -> None:
    assert name_compatible("Forés", "Álex Forés")
    assert name_compatible("Catena", "Óscar Catena")
    assert name_compatible("Vinícius", "Vinícius Júnior")


def test_name_incompatible_when_no_subset() -> None:
    assert not name_compatible("Aitor Fernández", "Pablo Fernández Pérez")
    assert not name_compatible("García", "López")


def test_team_compatible_ignores_generic_words() -> None:
    assert team_compatible("Athletic", "Athletic Bilbao")
    assert team_compatible("Atlético", "Atlético de Madrid")
    assert team_compatible("Barcelona", "FC Barcelona")
    assert not team_compatible("Real Madrid", "Real Sociedad")


# --- casos de match ----------------------------------------------------------


BASIC_TEAMS = [{"id": 1, "name": "Athletic"}, {"id": 2, "name": "Oviedo"}]
BASIC_TM = [
    {"id": 10, "name": "Iñaki Williams", "club_id": 1, "club_name": "Athletic Bilbao",
     "birth_date": "1994-06-15", "position": "Right Winger"},
    {"id": 709380, "name": "Álex Forés", "club_id": 2, "club_name": "Real Oviedo",
     "birth_date": "2001-04-12", "position": "Centre-Forward"},
]  # fmt: skip


def test_auto_match_team_and_player(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_total == 1
    assert report.players_auto == 1
    assert report.auto_pct == 100.0

    store = MappingStore(tmp_path / "mappings")
    store.load()
    # El jugador comparte canonical_id con su contraparte de Transfermarkt.
    biw = store.players[store.players["fuente"] == "biwenger"].iloc[0]
    tm = store.players[store.players["fuente"] == "transfermarkt"].iloc[0]
    assert biw["canonical_id"] == tm["canonical_id"]
    assert biw["id_en_fuente"] == "100"
    assert tm["id_en_fuente"] == "10"
    assert biw["metodo"] == "auto"


def test_fores_case_maps_to_current_club(storage: Storage, tmp_path: Path) -> None:
    """El caso Forés: 'Forés' en el Oviedo ↔ Transfermarkt 709380 (Real Oviedo)."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 200, "name": "Forés", "team_id": 2}],
        tm_players=BASIC_TM,
    )
    run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    canonical = store.canonical_by_source(store.players, "biwenger")["200"]
    tm_id = next(
        r["id_en_fuente"]
        for _, r in store.players.iterrows()
        if r["canonical_id"] == canonical and r["fuente"] == "transfermarkt"
    )
    assert tm_id == "709380"


def test_ambiguous_player_goes_to_review(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=[
            {
                "id": 10,
                "name": "Iñaki Williams",
                "club_id": 1,
                "club_name": "Athletic Bilbao",
                "birth_date": "1994-06-15",
                "position": "Right Winger",
            },
            {
                "id": 11,
                "name": "Nico Williams",
                "club_id": 1,
                "club_name": "Athletic Bilbao",
                "birth_date": "2002-07-12",
                "position": "Left Winger",
            },
        ],  # fmt: skip
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 0
    assert report.players_review == 1

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.players_review
    assert set(review["tm_id"]) == {"10", "11"}
    assert set(review["motivo"]) == {"varios-en-club"}
    assert (review["decision"] == "").all()
    # La evidencia incluye la fecha de nacimiento de Transfermarkt para desempatar.
    assert set(review["tm_birth_date"]) == {"1994-06-15", "2002-07-12"}


def test_loaned_player_offered_cross_club(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Forés", "team_id": 1}],  # Biwenger lo pone en Athletic
        tm_players=BASIC_TM,  # pero en Transfermarkt está en el Oviedo
    )
    run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.players_review
    assert review["tm_id"].tolist() == ["709380"]
    assert review["motivo"].tolist() == ["fuera-de-club"]


def test_no_candidate_player(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Fantasma", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.players_review
    assert review["motivo"].tolist() == ["sin-candidato"]
    assert review["tm_id"].tolist() == [""]


def test_team_without_candidate_goes_to_review(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=[{"id": 5, "name": "Desconocido"}],
        players=[],
        tm_players=BASIC_TM,
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.teams_mapped == 0
    assert report.teams_review == 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.teams_review["motivo"].tolist() == ["sin-candidato"]


# --- idempotencia y decisiones manuales --------------------------------------


def test_run_is_idempotent(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[
            {"id": 100, "name": "Williams", "team_id": 1},
            {"id": 101, "name": "Zzz", "team_id": 1},
        ],
        tm_players=BASIC_TM,
    )
    mappings = tmp_path / "mappings"
    run_map(storage, mappings)
    first = {
        name: (mappings / name).read_text()
        for name in ("players.csv", "teams.csv", "players-review.csv", "teams-review.csv")
    }
    run_map(storage, mappings)
    second = {name: (mappings / name).read_text() for name in first}
    assert second == first


def test_manual_decision_promotes_from_review(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=[
            {
                "id": 10,
                "name": "Iñaki Williams",
                "club_id": 1,
                "club_name": "Athletic Bilbao",
                "birth_date": "1994-06-15",
                "position": "Right Winger",
            },
            {
                "id": 11,
                "name": "Nico Williams",
                "club_id": 1,
                "club_name": "Athletic Bilbao",
                "birth_date": "2002-07-12",
                "position": "Left Winger",
            },
        ],  # fmt: skip
    )
    mappings = tmp_path / "mappings"
    run_map(storage, mappings)

    # Un humano marca en el fichero de revisión que el candidato correcto es Nico (11).
    review = pd.read_csv(mappings / "players-review.csv", dtype=str)
    review.loc[review["tm_id"] == "11", "decision"] = "y"
    review.to_csv(mappings / "players-review.csv", index=False)

    report = run_map(storage, mappings)
    assert report.players_manual == 1
    assert report.players_review == 0

    store = MappingStore(mappings)
    store.load()
    tm = store.players[store.players["fuente"] == "transfermarkt"].iloc[0]
    assert tm["id_en_fuente"] == "11"
    assert tm["metodo"] == "manual"


def test_skip_decision_maps_biwenger_only(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Fantasma", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    mappings = tmp_path / "mappings"
    run_map(storage, mappings)

    review = pd.read_csv(mappings / "players-review.csv", dtype=str, keep_default_na=False)
    review["decision"] = "skip"
    review.to_csv(mappings / "players-review.csv", index=False)

    report = run_map(storage, mappings)
    assert report.players_manual == 1

    store = MappingStore(mappings)
    store.load()
    rows = store.players[store.players["id_en_fuente"] == "100"]
    assert rows["fuente"].tolist() == ["biwenger"]  # sin fila de Transfermarkt


# --- verificación (--check) --------------------------------------------------


def test_check_passes_without_curated_data(storage: Storage, tmp_path: Path) -> None:
    # CI: data/ en .gitignore, no hay tablas curadas -> nada que verificar.
    assert check_mappings(storage, tmp_path / "mappings") == []


def test_check_reports_unmapped_players(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Fantasma", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    problems = check_mappings(storage, tmp_path / "mappings")
    assert any("Fantasma" in p for p in problems)


def test_check_passes_after_full_mapping(storage: Storage, tmp_path: Path) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[
            {"id": 100, "name": "Williams", "team_id": 1},
            {"id": 200, "name": "Forés", "team_id": 2},
        ],
        tm_players=BASIC_TM,
    )
    mappings = tmp_path / "mappings"
    run_map(storage, mappings)
    # Ambos jugadores y sus equipos se mapean solos.
    assert check_mappings(storage, mappings) == []


# --- CLI ---------------------------------------------------------------------


def test_cli_map_and_check(storage: Storage, tmp_path: Path, capsys) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    mappings = tmp_path / "mappings"
    data_uri = storage.base_uri

    assert main(["map", "--data", data_uri, "--mappings", str(mappings)]) == 0
    assert "automático" in capsys.readouterr().out

    assert main(["map", "--check", "--data", data_uri, "--mappings", str(mappings)]) == 0
    assert "tienen mapping" in capsys.readouterr().out
