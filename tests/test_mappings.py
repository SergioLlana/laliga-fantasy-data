"""Tests de la capa de mappings: normalización, matching y verificación.

El matching se ancla en Biwenger y busca contraparte en Transfermarkt por club +
nombre normalizado, endurecido con la fecha de nacimiento que la ingesta de
reports rellena en `biwenger_players` (issue #37): un homónimo único en el club
solo se auto-aprueba si las fechas coinciden o falta alguna. Los casos se montan
con tablas curadas sintéticas pequeñas, sin red.
"""

from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.mappings import check_mappings, run_map
from lfdata.mappings.normalize import name_compatible, normalize, team_compatible
from lfdata.mappings.store import MappingIntegrityError, MappingStore
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
    players_df = pd.DataFrame(players, columns=["id", "name", "team_id", "birth_date"]).astype(
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


# --- desempate por fecha de nacimiento (#37) ---------------------------------


def test_birthdate_confirms_auto_match(storage: Storage, tmp_path: Path) -> None:
    """Homónimo único en el club con fecha coincidente: se aprueba solo."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    assert report.players_review == 0


def test_birthdate_missing_is_tolerant(storage: Storage, tmp_path: Path) -> None:
    """Sin fecha en Biwenger, un homónimo único sigue aprobándose solo."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1}],  # birth_date ausente
        tm_players=BASIC_TM,
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    assert report.players_review == 0


def test_birthdate_mismatch_degrades_to_review(storage: Storage, tmp_path: Path) -> None:
    """Homónimo único pero con fecha discrepante: no se auto-aprueba, va a revisión."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1990-01-01"}],
        tm_players=BASIC_TM,  # Iñaki Williams (10) nació 1994-06-15
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 0
    assert report.players_review == 1

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.players_review
    assert review["motivo"].tolist() == ["fecha-discrepante"]
    assert review["tm_id"].tolist() == ["10"]
    # La fila muestra ambas fechas como evidencia del desempate manual.
    assert review["biwenger_birth_date"].tolist() == ["1990-01-01"]
    assert review["tm_birth_date"].tolist() == ["1994-06-15"]
    assert (review["decision"] == "").all()


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


# --- matching biunívoco: candidato compartido (#40) --------------------------

# Caso filial en Segunda: eliminados los stopwords de club "b"/"ii", tanto
# "Real Madrid" como "Real Madrid Castilla" son compatibles con "Real Madrid" y
# con "Real Madrid B" de Transfermarkt. Ninguna pareja es biunívoca.
FILIAL_TM = [
    {"id": 20, "name": "Jugador Primer Equipo", "club_id": 100,
     "club_name": "Real Madrid", "birth_date": "2000-01-01", "position": "Midfielder"},
    {"id": 21, "name": "Jugador Filial", "club_id": 101,
     "club_name": "Real Madrid B", "birth_date": "2004-01-01", "position": "Midfielder"},
]  # fmt: skip


def test_shared_team_candidate_goes_to_review_not_greedy(storage: Storage, tmp_path: Path) -> None:
    """Dos equipos de Biwenger compatibles con el mismo club: ambos a revisión."""
    seed(
        storage,
        teams=[{"id": 1, "name": "Real Madrid"}, {"id": 2, "name": "Real Madrid Castilla"}],
        players=[],
        tm_players=FILIAL_TM,
    )
    report = run_map(storage, tmp_path / "mappings")

    # Ninguno se auto-aprueba: el reparto es ambiguo por ambos lados.
    assert report.teams_mapped == 0
    assert report.teams_review == 2

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.teams_review
    assert set(review["biwenger_id"]) == {"1", "2"}
    assert set(review["motivo"]) == {"candidato-compartido"}
    assert store.teams.empty  # nada aprobado


def test_shared_player_candidate_goes_to_review(storage: Storage, tmp_path: Path) -> None:
    """Dos jugadores de Biwenger compatibles con el mismo jugador de TM: revisión."""
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        # Ambos ('Williams') son compatibles con el único Iñaki Williams (10).
        players=[
            {"id": 100, "name": "Williams", "team_id": 1},
            {"id": 101, "name": "Williams", "team_id": 1},
        ],
        tm_players=[
            {
                "id": 10,
                "name": "Iñaki Williams",
                "club_id": 1,
                "club_name": "Athletic Bilbao",
                "birth_date": "1994-06-15",
                "position": "Right Winger",
            },
        ],  # fmt: skip
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 0
    assert report.players_review == 2

    store = MappingStore(tmp_path / "mappings")
    store.load()
    review = store.players_review
    assert set(review["biwenger_id"]) == {"100", "101"}
    assert set(review["motivo"]) == {"candidato-compartido"}
    # Ningún jugador con contraparte de Transfermarkt aprobada.
    assert store.players[store.players["fuente"] == "transfermarkt"].empty


def test_map_result_is_independent_of_id_order(storage: Storage, tmp_path: Path) -> None:
    """El grafo se resuelve global: invertir los ids no cambia el resultado."""

    def run_with_ids(id_a: int, id_b: int, root: Path) -> set[tuple[str, str]]:
        seed(
            storage,
            teams=[
                {"id": id_a, "name": "Real Madrid"},
                {"id": id_b, "name": "Real Madrid Castilla"},
            ],
            players=[],
            tm_players=FILIAL_TM,
        )
        run_map(storage, root)
        store = MappingStore(root)
        store.load()
        return {(r.biwenger_name, r.motivo) for r in store.teams_review.itertuples()}

    low_first = run_with_ids(1, 2, tmp_path / "a")
    high_first = run_with_ids(2, 1, tmp_path / "b")
    assert low_first == high_first
    assert all(motivo == "candidato-compartido" for _, motivo in low_first)


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


# --- decisiones no aplicables: se conservan y se reportan ---------------------

TWO_WILLIAMS_TM = [
    {"id": 10, "name": "Iñaki Williams", "club_id": 1, "club_name": "Athletic Bilbao",
     "birth_date": "1994-06-15", "position": "Right Winger"},
    {"id": 11, "name": "Nico Williams", "club_id": 1, "club_name": "Athletic Bilbao",
     "birth_date": "2002-07-12", "position": "Left Winger"},
]  # fmt: skip


def _seed_ambiguous(storage: Storage) -> None:
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=TWO_WILLIAMS_TM,
    )


def _edit_review(mappings: Path, edits: list[tuple[str, str]]) -> None:
    """Aplica ``(tm_id, decision)`` sobre players-review.csv y lo reescribe."""
    review = pd.read_csv(mappings / "players-review.csv", dtype=str, keep_default_na=False)
    for tm_id, value in edits:
        review.loc[review["tm_id"] == tm_id, "decision"] = value
    review.to_csv(mappings / "players-review.csv", index=False)


def _decisions_column(mappings: Path, tm_id: str) -> str:
    review = pd.read_csv(mappings / "players-review.csv", dtype=str, keep_default_na=False)
    return review.loc[review["tm_id"] == tm_id, "decision"].iloc[0]


def test_two_yes_decisions_preserved_and_reported(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_ambiguous(storage)
    run_map(storage, mappings)
    _edit_review(mappings, [("10", "y"), ("11", "y")])

    report = run_map(storage, mappings)

    assert report.players_manual == 0
    assert {u.motivo for u in report.unapplied} == {"varios-y"}
    assert len(report.unapplied) == 2
    # El trabajo manual no se borra: ambas decisiones siguen en el fichero.
    assert _decisions_column(mappings, "10") == "y"
    assert _decisions_column(mappings, "11") == "y"


def test_yes_without_candidate_preserved_and_reported(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Fantasma", "team_id": 1}],
        tm_players=BASIC_TM,
    )
    run_map(storage, mappings)  # -> fila sin-candidato, tm_id vacío
    _edit_review(mappings, [("", "y")])

    report = run_map(storage, mappings)

    assert report.players_manual == 0
    assert [u.motivo for u in report.unapplied] == ["y-sin-candidato"]
    assert _decisions_column(mappings, "") == "y"


def test_yes_with_skip_preserved_and_reported(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_ambiguous(storage)
    run_map(storage, mappings)
    _edit_review(mappings, [("10", "y"), ("11", "skip")])

    report = run_map(storage, mappings)

    assert report.players_manual == 0
    assert {u.motivo for u in report.unapplied} == {"y-con-skip"}
    assert _decisions_column(mappings, "10") == "y"
    assert _decisions_column(mappings, "11") == "skip"


def test_unrecognized_token_preserved_and_reported(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_ambiguous(storage)
    run_map(storage, mappings)
    _edit_review(mappings, [("10", "yep")])  # typo, no reconocido

    report = run_map(storage, mappings)

    assert report.players_manual == 0
    assert any(u.motivo == "token-no-reconocido" for u in report.unapplied)
    assert _decisions_column(mappings, "10") == "yep"


def test_manual_decision_rejects_taken_tm_id(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[
            {"id": 100, "name": "Williams", "team_id": 1},
            {"id": 101, "name": "Williams", "team_id": 1},
        ],
        tm_players=TWO_WILLIAMS_TM,
    )
    run_map(storage, mappings)

    # Dos jugadores canónicos apuntan al mismo id de Transfermarkt (10).
    review = pd.read_csv(mappings / "players-review.csv", dtype=str, keep_default_na=False)
    review.loc[(review["biwenger_id"] == "100") & (review["tm_id"] == "10"), "decision"] = "y"
    review.loc[(review["biwenger_id"] == "101") & (review["tm_id"] == "10"), "decision"] = "y"
    review.to_csv(mappings / "players-review.csv", index=False)

    report = run_map(storage, mappings)

    # Solo uno se promueve; el segundo se rechaza sin duplicar el tm_id 10.
    assert report.players_manual == 1
    assert any(u.motivo == "tm-id-ya-tomado" and u.biwenger_id == "101" for u in report.unapplied)
    store = MappingStore(mappings)
    store.load()
    tm10 = store.players[
        (store.players["fuente"] == "transfermarkt") & (store.players["id_en_fuente"] == "10")
    ]
    assert len(tm10) == 1


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


# --- integridad de los aprobados ---------------------------------------------


def _write_approved(mappings: Path, name: str, rows: list[dict]) -> None:
    mappings.mkdir(parents=True, exist_ok=True)
    columns = ["canonical_id", "fuente", "id_en_fuente", "metodo", "fecha"]
    pd.DataFrame(rows, columns=columns).to_csv(mappings / name, index=False)


def test_load_fails_on_duplicate_source_id(tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    # El mismo id de Transfermarkt (10) en dos identidades canónicas.
    _write_approved(
        mappings,
        "players.csv",
        [
            {
                "canonical_id": "p00001",
                "fuente": "transfermarkt",
                "id_en_fuente": "10",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
            {
                "canonical_id": "p00002",
                "fuente": "transfermarkt",
                "id_en_fuente": "10",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
        ],  # fmt: skip
    )
    store = MappingStore(mappings)
    with pytest.raises(MappingIntegrityError) as excinfo:
        store.load()
    assert any("10" in p for p in excinfo.value.problems)


def test_load_fails_on_two_source_ids_per_canonical(tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _write_approved(
        mappings,
        "players.csv",
        [
            {
                "canonical_id": "p00001",
                "fuente": "transfermarkt",
                "id_en_fuente": "10",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
            {
                "canonical_id": "p00001",
                "fuente": "transfermarkt",
                "id_en_fuente": "11",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
        ],  # fmt: skip
    )
    store = MappingStore(mappings)
    with pytest.raises(MappingIntegrityError):
        store.load()


def test_load_fails_on_unrecognizable_canonical_id(tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _write_approved(
        mappings,
        "players.csv",
        [
            {
                "canonical_id": "xyz",
                "fuente": "biwenger",
                "id_en_fuente": "100",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
        ],  # fmt: skip
    )
    store = MappingStore(mappings)
    with pytest.raises(MappingIntegrityError):
        store.load()


def test_cli_map_fails_on_broken_integrity(storage: Storage, tmp_path: Path, capsys) -> None:
    mappings = tmp_path / "mappings"
    _write_approved(
        mappings,
        "players.csv",
        [
            {
                "canonical_id": "p00001",
                "fuente": "transfermarkt",
                "id_en_fuente": "10",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
            {
                "canonical_id": "p00002",
                "fuente": "transfermarkt",
                "id_en_fuente": "10",
                "metodo": "manual",
                "fecha": "2026-07-10",
            },
        ],  # fmt: skip
    )
    data_uri = storage.base_uri
    assert main(["map", "--data", data_uri, "--mappings", str(mappings)]) == 1
    assert "Integridad" in capsys.readouterr().out
    # --check también falla por integridad, no solo por cobertura.
    assert main(["map", "--check", "--data", data_uri, "--mappings", str(mappings)]) == 1
