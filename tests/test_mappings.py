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
from lfdata.sources.transfermarkt import DEFAULT_SEASON
from lfdata.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path}")


def seed(
    storage: Storage,
    *,
    competition: str = "la-liga",
    season: int = DEFAULT_SEASON,
    teams: list[dict],
    players: list[dict],
    tm_players: list[dict],
    tm_past: list[dict] | None = None,
) -> None:
    """Siembra las tablas curadas del caso.

    ``tm_past`` puebla la plantilla de la temporada anterior: es donde vive la
    contraparte de quien ya no juega en la liga pero cuya ficha Biwenger conserva.
    """
    partition = {"competition": competition}
    storage.curated.write_table("biwenger_teams", pd.DataFrame(teams), partition=partition)
    players_df = pd.DataFrame(players, columns=["id", "name", "team_id", "birth_date"]).astype(
        {"team_id": "Int64"}
    )
    storage.curated.write_table("biwenger_players", players_df, partition=partition)
    # transfermarkt_players es la plantilla *de una temporada* (ver ingest).
    for year, rows in ((season, tm_players), (season - 1, tm_past or [])):
        if not rows:
            continue
        tm = pd.DataFrame(rows).astype({"club_id": "Int64"})
        tm["birth_date"] = pd.to_datetime(tm["birth_date"])
        storage.curated.write_table(
            "transfermarkt_players",
            tm,
            partition={"competition": competition, "season": str(year)},
        )


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
    """Al cedido se le ofrece su homónimo de otro club, pero sin fecha no se aprueba."""
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
    assert review["motivo"].tolist() == ["sin-fecha-que-verificar"]


def test_loaned_player_auto_matched_when_birthdate_confirms(
    storage: Storage, tmp_path: Path
) -> None:
    """El club es una pista, no un filtro: con la fecha coincidente, es él aunque esté en otro."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Forés", "team_id": 1, "birth_date": "2001-04-12"}],
        tm_players=BASIC_TM,  # Forés está en el Oviedo, no en el Athletic
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.approved_ids(store.players, "transfermarkt") == {"709380"}


# --- el club como pista: apodos y fichas sin equipo ---------------------------


def test_nickname_in_club_rescued_by_birthdate(storage: Storage, tmp_path: Path) -> None:
    """'Ez Abde' no comparte ningún token con 'Abde Ezzalzouli': lo salva la fecha."""
    seed(
        storage,
        teams=[{"id": 1, "name": "Betis"}],
        players=[{"id": 100, "name": "Ez Abde", "team_id": 1, "birth_date": "2001-12-17"}],
        tm_players=[
            {
                "id": 724520,
                "name": "Abde Ezzalzouli",
                "club_id": 1,
                "club_name": "Real Betis",
                "birth_date": "2001-12-17",
                "position": "Left Winger",
            },
            {
                "id": 461617,
                "name": "Rodrigo Riquelme",
                "club_id": 1,
                "club_name": "Real Betis",
                "birth_date": "2000-04-02",
                "position": "Left Winger",
            },
        ],  # fmt: skip
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.approved_ids(store.players, "transfermarkt") == {"724520"}


def test_player_without_team_found_in_past_season(storage: Storage, tmp_path: Path) -> None:
    """Biwenger conserva la ficha del que ya no juega la liga: su contraparte está en el pasado."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        # Sin team_id: ya no está en ninguna plantilla de Biwenger.
        players=[{"id": 100, "name": "Griezmann", "team_id": None, "birth_date": "1991-03-21"}],
        tm_players=BASIC_TM,  # no está en la plantilla de la temporada actual...
        tm_past=[
            {
                "id": 125781,
                "name": "Antoine Griezmann",
                "club_id": 3,
                "club_name": "Atlético de Madrid",
                "birth_date": "1991-03-21",
                "position": "Second Striker",
            },
        ],  # fmt: skip  ...pero sí en la anterior
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.approved_ids(store.players, "transfermarkt") == {"125781"}


def test_confirmed_by_birthdate_stops_disputing(storage: Storage, tmp_path: Path) -> None:
    """Una ficha huérfana de nombre genérico no puede bloquear a quien la fecha identifica.

    'Thomas' (ficha vieja sin equipo) es compatible con todos los Thomas de
    Transfermarkt, Lemar incluido. Pero Lemar nació el día exacto de Thomas Lemar:
    la fecha lo confirma, así que deja de disputar y se aprueba. El genérico se
    queda en revisión, que es donde debe estar.
    """
    seed(
        storage,
        teams=[{"id": 1, "name": "Atlético"}],
        players=[
            {"id": 100, "name": "Lemar", "team_id": 1, "birth_date": "1995-11-12"},
            {"id": 200, "name": "Thomas", "team_id": None, "birth_date": None},
        ],
        tm_players=[
            {
                "id": 316,
                "name": "Thomas Lemar",
                "club_id": 1,
                "club_name": "Atlético de Madrid",
                "birth_date": "1995-11-12",
                "position": "Left Winger",
            },
            {
                "id": 148,
                "name": "Thomas Partey",
                "club_id": 1,
                "club_name": "Atlético de Madrid",
                "birth_date": "1993-06-13",
                "position": "Defensive Midfield",
            },
        ],  # fmt: skip
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.canonical_by_source(store.players, "transfermarkt") == {
        "316": store.canonical_by_source(store.players, "biwenger")["100"]
    }
    assert store.players_review["biwenger_id"].unique().tolist() == ["200"]


def test_global_match_rejected_when_birthdate_differs(storage: Storage, tmp_path: Path) -> None:
    """Fuera del club el pool son miles: un homónimo con otra fecha es otra persona."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        # 'Luismi' de Biwenger (1992) no es 'Luismi Quirant' (2004): mismo apodo, otro jugador.
        players=[{"id": 100, "name": "Luismi", "team_id": None, "birth_date": "1992-05-05"}],
        tm_players=BASIC_TM,
        tm_past=[
            {
                "id": 610461,
                "name": "Luismi",
                "club_id": 3,
                "club_name": "Levante UD",
                "birth_date": "2004-10-28",
                "position": "Midfielder",
            },
        ],  # fmt: skip
    )
    report = run_map(storage, tmp_path / "mappings")

    assert report.players_auto == 0
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.players_review["motivo"].tolist() == ["fecha-discrepante"]


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


# --- histórico de rounds: identidad de quien ya dejó la competición ----------


def seed_history(
    storage: Storage, *, season: int, team: dict, player: dict, competition: str = "la-liga"
) -> None:
    """Siembra ``biwenger_*_history`` de una temporada, como haría ``ingest_rounds``."""
    partition = {"competition": competition, "season": str(season)}
    storage.curated.write_table("biwenger_teams_history", pd.DataFrame([team]), partition=partition)
    players_df = pd.DataFrame(
        [player], columns=["id", "name", "slug", "position", "team_id"]
    ).astype({"position": "Int64", "team_id": "Int64"})
    storage.curated.write_table("biwenger_players_history", players_df, partition=partition)


def test_history_player_auto_mapped_in_its_season(storage: Storage, tmp_path: Path) -> None:
    """El jugador de un club descendido, solo visto en rounds y sin fecha, se mapea.

    No está en la plantilla actual (su club tampoco), así que sin las tablas de
    histórico sería invisible al matcher. Con ellas, la pasada de su temporada lo
    ve en su club de aquel año y lo auto-aprueba por homónimo único (ADR 0005).
    """
    past = DEFAULT_SEASON - 1
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 1, "name": "Williams", "team_id": 1}],
        tm_players=BASIC_TM[:1],
        tm_past=[
            {
                "id": 55,
                "name": "José Luis Morales",
                "club_id": 3,
                "club_name": "Levante UD",
                "birth_date": "1987-07-23",
                "position": "Left Winger",
            },
        ],  # fmt: skip
    )
    # Levante (id 3) no está en la plantilla actual: solo lo aporta el histórico.
    seed_history(
        storage,
        season=past,
        team={"id": 3, "name": "Levante", "slug": "levante"},
        player={"id": 900, "name": "Morales", "slug": "morales", "position": 4, "team_id": 3},
    )
    report = run_map(storage, tmp_path / "mappings", season=past)

    assert report.players_auto >= 1
    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert "55" in store.approved_ids(store.players, "transfermarkt")
    morales = store.players[
        (store.players["fuente"] == "biwenger") & (store.players["id_en_fuente"] == "900")
    ].iloc[0]
    assert morales["metodo"] == "auto"


def test_history_of_other_seasons_absent_from_pass(storage: Storage, tmp_path: Path) -> None:
    """En la pasada de una temporada, el histórico de otra no entra ni a revisión."""
    other = DEFAULT_SEASON - 2
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 1, "name": "Williams", "team_id": 1}],
        tm_players=BASIC_TM[:1],
    )
    seed_history(
        storage,
        season=other,
        team={"id": 3, "name": "Levante", "slug": "levante"},
        player={"id": 900, "name": "Morales", "slug": "morales", "position": 4, "team_id": 3},
    )
    run_map(storage, tmp_path / "mappings", season=DEFAULT_SEASON)  # pasada de otra temporada

    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert "900" not in set(store.players_review["biwenger_id"])
    assert "900" not in store.approved_ids(store.players, "biwenger")


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


# --- token de spieler_id en la revisión de jugadores de Transfermarkt ---------


def _seed_sincandidato(storage: Storage) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Fantasma", "team_id": 1}],
        tm_players=BASIC_TM,
    )


def test_spieler_id_token_maps_sincandidato_player(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_sincandidato(storage)
    run_map(storage, mappings)  # -> fila sin-candidato, tm_id vacío
    _edit_review(mappings, [("", "888888")])  # id pegado en la fila sin candidato

    report = run_map(storage, mappings)

    assert report.players_manual == 1
    store = MappingStore(mappings)
    store.load()
    rows = store.players[store.players["id_en_fuente"] == "100"]
    assert rows["fuente"].tolist() == ["biwenger"]  # su canónico
    canonical = rows.iloc[0]["canonical_id"]
    tm = store.players[
        (store.players["canonical_id"] == canonical) & (store.players["fuente"] == "transfermarkt")
    ]
    assert tm["id_en_fuente"].tolist() == ["888888"]


def test_profile_url_token_maps_sincandidato_player(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_sincandidato(storage)
    run_map(storage, mappings)
    url = "https://www.transfermarkt.com/x/profil/spieler/777123"
    _edit_review(mappings, [("", url)])

    report = run_map(storage, mappings)

    assert report.players_manual == 1
    store = MappingStore(mappings)
    store.load()
    assert "777123" in set(store.players["id_en_fuente"])


def test_spieler_id_in_row_with_candidate_reported(storage: Storage, tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    _seed_ambiguous(storage)
    run_map(storage, mappings)
    _edit_review(mappings, [("10", "888888")])  # id en una fila que ya trae candidato

    report = run_map(storage, mappings)

    assert report.players_manual == 0
    assert [u.motivo for u in report.unapplied] == ["id-en-fila-con-candidato"]
    assert _decisions_column(mappings, "10") == "888888"  # el trabajo manual no se borra


def test_spieler_id_token_not_recognized_for_teams(storage: Storage, tmp_path: Path) -> None:
    """El token de id solo vale en jugadores de Transfermarkt; en equipos, no."""
    mappings = tmp_path / "mappings"
    seed(storage, teams=[{"id": 5, "name": "Desconocido"}], players=[], tm_players=BASIC_TM)
    run_map(storage, mappings)  # -> equipo sin-candidato

    review = pd.read_csv(mappings / "teams-review.csv", dtype=str, keep_default_na=False)
    review["decision"] = "888888"  # un id numérico, no un sinónimo de y/skip
    review.to_csv(mappings / "teams-review.csv", index=False)

    report = run_map(storage, mappings)
    assert {u.motivo for u in report.unapplied} == {"token-no-reconocido"}
    store = MappingStore(mappings)
    store.load()
    assert "888888" not in set(store.teams["id_en_fuente"])


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


# --- SofaScore: se cuelga del canónico que Biwenger ya tiene -----------------


def seed_sofascore(
    storage: Storage,
    *,
    competition: str = "la-liga",
    season: int = DEFAULT_SEASON,
    teams: list[dict],
    players: list[dict],
) -> None:
    """Siembra el catálogo ``sofascore_teams``/``sofascore_players`` de un caso.

    La competición y la temporada van en la partición (como en el catálogo real,
    slug del proyecto y año de inicio), no en las filas.
    """
    partition = {"competition": competition, "season": str(season)}
    storage.curated.write_table(
        "sofascore_teams",
        pd.DataFrame(teams, columns=["team_id", "team_name"]).astype({"team_id": "Int64"}),
        partition=partition,
    )
    storage.curated.write_table(
        "sofascore_players",
        pd.DataFrame(
            players,
            columns=["sofascore_player_id", "name", "birth_date", "team_id", "team_name"],
        ).astype({"sofascore_player_id": "Int64", "team_id": "Int64"}),
        partition=partition,
    )


ATHLETIC_SO_TEAM = [{"team_id": 900, "team_name": "Athletic Bilbao"}]


def test_sofascore_auto_attaches_to_existing_canonical(storage: Storage, tmp_path: Path) -> None:
    """Un par biunívoco cuelga el id de SofaScore del canónico que Biwenger ya tiene."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1994-06-15",
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )
    report = run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    canonical = store.canonical_by_source(store.players, "biwenger")["100"]
    sofascore = store.players[store.players["fuente"] == "sofascore"].iloc[0]
    assert sofascore["id_en_fuente"] == "5000"
    assert sofascore["canonical_id"] == canonical  # el mismo de Biwenger↔Transfermarkt
    assert sofascore["metodo"] == "auto"
    # El equipo también cuelga del canónico del equipo de Biwenger.
    assert "900" in store.approved_ids(store.teams, "sofascore")
    assert report.sofascore_players_mapped == 1
    assert report.sofascore_teams_mapped == 1
    assert report.sofascore_unresolved == 0


def test_sofascore_discrepant_birthdate_not_auto_even_if_only_homonym(
    storage: Storage, tmp_path: Path
) -> None:
    """Fecha discrepante: no se auto-aprueba aunque sea el único homónimo del club."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1990-01-01",  # discrepa con Biwenger (1994-06-15)
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )
    run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.players[store.players["fuente"] == "sofascore"].empty  # nada auto-aprobado
    review = store.players_review_sofascore
    assert review["motivo"].tolist() == ["fecha-discrepante"]
    assert review["sofascore_id"].tolist() == ["5000"]
    # Ambas fechas quedan como evidencia del desempate manual.
    assert review["biwenger_birth_date"].tolist() == ["1994-06-15"]
    assert review["sofascore_birth_date"].tolist() == ["1990-01-01"]
    assert (review["decision"] == "").all()


def test_sofascore_manual_decision_attaches_and_survives_regeneration(
    storage: Storage, tmp_path: Path
) -> None:
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1990-01-01",
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )
    mappings = tmp_path / "mappings"
    run_map(storage, mappings)

    # Un humano confirma en la revisión que el candidato de SofaScore es el correcto.
    review = pd.read_csv(mappings / "sofascore-review.csv", dtype=str, keep_default_na=False)
    review.loc[review["sofascore_id"] == "5000", "decision"] = "y"
    review.to_csv(mappings / "sofascore-review.csv", index=False)

    report = run_map(storage, mappings)

    store = MappingStore(mappings)
    store.load()
    sofascore = store.players[store.players["fuente"] == "sofascore"].iloc[0]
    assert sofascore["id_en_fuente"] == "5000"
    assert sofascore["metodo"] == "manual"
    assert report.sofascore_players_review == 0


def test_sofascore_waits_for_biwenger_canonical(storage: Storage, tmp_path: Path) -> None:
    """Sin canónico en Biwenger (su Transfermarkt sigue en duda) no hay de qué colgar."""
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=TWO_WILLIAMS_TM,  # dos homónimos: Biwenger 100 queda sin canónico
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1994-06-15",
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )
    report = run_map(storage, tmp_path / "mappings")

    store = MappingStore(tmp_path / "mappings")
    store.load()
    assert store.players[store.players["fuente"] == "sofascore"].empty
    assert report.sofascore_unresolved == 1  # el id 5000 sigue sin resolver


# --- skip persistente de SofaScore (registro negativo, ADR 0011 / #94) -------


def _seed_sofascore_review_candidate(storage: Storage) -> None:
    """Williams mapeado a TM; un candidato de SofaScore con fecha discrepante va a revisión."""
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1990-01-01",  # discrepa: va a revisión, no se auto-aprueba
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )


def test_sofascore_skip_persists_across_runs(storage: Storage, tmp_path: Path) -> None:
    """Regresión #94: un `skip` de SofaScore no reaparece en la siguiente pasada."""
    mappings = tmp_path / "mappings"
    _seed_sofascore_review_candidate(storage)
    run_map(storage, mappings)

    review = pd.read_csv(mappings / "sofascore-review.csv", dtype=str, keep_default_na=False)
    review.loc[review["sofascore_id"] == "5000", "decision"] = "skip"
    review.to_csv(mappings / "sofascore-review.csv", index=False)

    report = run_map(storage, mappings)

    store = MappingStore(mappings)
    store.load()
    canonical = store.canonical_by_source(store.players, "biwenger")["100"]
    assert store.sofascore_skips["canonical_id"].tolist() == [canonical]
    assert store.players[store.players["fuente"] == "sofascore"].empty  # el skip no cuelga nada
    assert store.players_review_sofascore.empty  # ya no está en revisión
    assert report.sofascore_skipped == 1
    assert report.sofascore_players_review == 0

    # Segunda pasada: el skip persiste, no reaparece en revisión ni se duplica.
    report2 = run_map(storage, mappings)
    store2 = MappingStore(mappings)
    store2.load()
    assert store2.players_review_sofascore.empty
    assert store2.sofascore_skips["canonical_id"].tolist() == [canonical]
    assert report2.sofascore_skipped == 1


def test_sofascore_skip_without_canonical_reported(storage: Storage, tmp_path: Path) -> None:
    """Un `skip` sobre un Biwenger sin canónico (su TM en duda) no se aplica; se reporta."""
    mappings = tmp_path / "mappings"
    seed(
        storage,
        teams=[{"id": 1, "name": "Athletic"}],
        players=[{"id": 100, "name": "Williams", "team_id": 1}],
        tm_players=TWO_WILLIAMS_TM,  # dos homónimos: biw 100 queda sin canónico
    )
    seed_sofascore(
        storage,
        teams=ATHLETIC_SO_TEAM,
        players=[
            {
                "sofascore_player_id": 5000,
                "name": "Iñaki Williams",
                "birth_date": "1994-06-15",
                "team_id": 900,
                "team_name": "Athletic Bilbao",
            }
        ],
    )
    run_map(storage, mappings)
    # La revisión de SofaScore no genera fila sin canónico; la escribimos a mano.
    pd.DataFrame(
        [
            {
                "biwenger_id": "100",
                "biwenger_name": "Williams",
                "biwenger_team": "1",
                "biwenger_birth_date": "",
                "sofascore_id": "5000",
                "sofascore_name": "Iñaki Williams",
                "sofascore_team": "Athletic Bilbao",
                "sofascore_birth_date": "1994-06-15",
                "motivo": "manual",
                "decision": "skip",
            }
        ]
    ).to_csv(mappings / "sofascore-review.csv", index=False)

    report = run_map(storage, mappings)

    store = MappingStore(mappings)
    store.load()
    assert store.sofascore_skips.empty  # nada persistido sin canónico
    assert [u.motivo for u in report.unapplied] == ["biwenger-sin-canonico"]
    kept = pd.read_csv(mappings / "sofascore-review.csv", dtype=str, keep_default_na=False)
    assert kept.loc[kept["sofascore_id"] == "5000", "decision"].iloc[0] == "skip"  # no se borra


def test_sofascore_skip_contradicts_mapping_fails_integrity(tmp_path: Path) -> None:
    """Un canónico con skip y a la vez un mapping de sofascore es una contradicción."""
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "p00001",
                "fuente": "biwenger",
                "id_en_fuente": "100",
                "metodo": "manual",
                "fecha": "2026-01-01",
            },
            {
                "canonical_id": "p00001",
                "fuente": "sofascore",
                "id_en_fuente": "5000",
                "metodo": "manual",
                "fecha": "2026-01-01",
            },
        ]  # fmt: skip
    ).to_csv(mappings / "players.csv", index=False)
    pd.DataFrame(
        [{"canonical_id": "p00001", "biwenger_name": "Williams", "fecha": "2026-01-01"}]
    ).to_csv(mappings / "sofascore-skips.csv", index=False)

    store = MappingStore(mappings)
    with pytest.raises(MappingIntegrityError, match="skip y a la vez"):
        store.load()


def test_sofascore_skip_duplicate_canonical_fails_integrity(tmp_path: Path) -> None:
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {"canonical_id": "p00001", "biwenger_name": "A", "fecha": "2026-01-01"},
            {"canonical_id": "p00001", "biwenger_name": "A", "fecha": "2026-01-02"},
        ]
    ).to_csv(mappings / "sofascore-skips.csv", index=False)

    store = MappingStore(mappings)
    with pytest.raises(MappingIntegrityError, match="repetido"):
        store.load()


def test_sofascore_team_skip_persists(storage: Storage, tmp_path: Path) -> None:
    """El skip de SofaScore también persiste para equipos (código compartido)."""
    mappings = tmp_path / "mappings"
    seed(
        storage,
        teams=BASIC_TEAMS,
        players=[{"id": 100, "name": "Williams", "team_id": 1, "birth_date": "1994-06-15"}],
        tm_players=BASIC_TM,
    )
    seed_sofascore(storage, teams=ATHLETIC_SO_TEAM, players=[])  # sin equipo que case con Oviedo
    run_map(storage, mappings)
    # Oviedo (biw 2, con canónico) no tiene contraparte en el catálogo: lo skipeamos a mano.
    pd.DataFrame(
        [
            {
                "biwenger_id": "2",
                "biwenger_name": "Oviedo",
                "competition": "la-liga",
                "sofascore_team_id": "",
                "sofascore_team_name": "",
                "motivo": "sin-candidato",
                "decision": "skip",
            }
        ]
    ).to_csv(mappings / "sofascore-teams-review.csv", index=False)

    run_map(storage, mappings)

    store = MappingStore(mappings)
    store.load()
    canonical = store.canonical_by_source(store.teams, "biwenger")["2"]
    assert canonical.startswith("t")
    assert store.sofascore_skips["canonical_id"].tolist() == [canonical]
    assert store.teams_review_sofascore.empty


# --- verificación (--check) --------------------------------------------------


def test_check_fails_on_unmapped_sofascore_id_in_curated(storage: Storage, tmp_path: Path) -> None:
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
    run_map(storage, mappings)  # Biwenger y Transfermarkt quedan mapeados

    # Eventing curado con un id de SofaScore que no tiene canónico aprobado.
    storage.curated.write_table(
        "player_match_stats",
        pd.DataFrame([{"canonical_id": "", "sofascore_player_id": 9999, "date": "2025-05-10"}]),
        partition={"competition": "8", "season": "77559"},
    )
    problems = check_mappings(storage, mappings)
    assert any("9999" in p for p in problems)


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
