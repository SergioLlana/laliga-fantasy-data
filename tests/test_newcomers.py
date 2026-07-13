"""Tests del detector de jugador nuevo, contra las fixtures ya grabadas, sin red.

El escenario es el caso Álex Forés (docs/experiments/2026-07-07-alex-fores.md)
puesto del revés: en vez de pedirlo a mano, aparece **solo** en la plantilla de
Biwenger de la temporada nueva, sin puntos en las anteriores, y el detector tiene
que traerle identidad (Transfermarkt) e historial (SofaScore) sin que nadie
intervenga.
"""

from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.newcomers import (
    DOWNLOADED,
    NO_HISTORY,
    TABLE,
    detect_newcomers,
    ingest_newcomers,
)
from lfdata.sources.http import SourceHTTPError
from lfdata.storage import Storage

SOFASCORE_FIXTURES = Path(__file__).parent / "fixtures" / "sofascore"
TRANSFERMARKT_FIXTURES = Path(__file__).parent / "fixtures" / "transfermarkt"

# Forés en cada fuente, y el club (Real Oviedo) cuya plantilla está grabada.
BIWENGER_FORES = 2
BIWENGER_VETERAN = 1
BIWENGER_TEAM = 10
TM_CLUB = 2497
TM_FORES = 709380
SEASON = 2026


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


class RaisingTransport:
    """Todas las peticiones fallan con el estado dado (fuente caída)."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.urls: list[str] = []

    def get(self, url: str, params=None) -> bytes:
        self.urls.append(url)
        raise SourceHTTPError(url, self.status)


def sofascore_routes(search: bytes | None = None) -> dict[str, bytes]:
    def fixture(name: str) -> bytes:
        return (SOFASCORE_FIXTURES / name).read_bytes()

    return {
        "statistics/seasons": fixture("seasons.json"),
        "search/all": search if search is not None else fixture("search.json"),
        "unique-tournament/8/season/77559/statistics/overall": fixture("overall-8-77559.json"),
        "unique-tournament/8/season/77559/ratings": fixture("ratings-8-77559.json"),
        "unique-tournament/54/season/62048/statistics/overall": fixture("overall-54-62048.json"),
        "unique-tournament/54/season/62048/ratings": fixture("ratings-54-62048.json"),
        "/event/": fixture("event-player-stats.json"),
    }


def transfermarkt_routes() -> dict[str, bytes]:
    def fixture(name: str) -> bytes:
        return (TRANSFERMARKT_FIXTURES / name).read_bytes()

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
    """Almacén con la plantilla de La Liga y los puntos de la temporada pasada.

    Dos jugadores: un veterano que puntuó en 2025 y Forés, que llega sin puntos.
    """
    storage = Storage(f"file://{tmp_path / 'data'}")
    squad = pd.DataFrame(
        [
            {"id": BIWENGER_VETERAN, "name": "Veterano", "team_id": BIWENGER_TEAM},
            {"id": BIWENGER_FORES, "name": "Álex Forés", "team_id": BIWENGER_TEAM},
        ]
    )
    storage.curated.write_table("biwenger_players", squad, partition={"competition": "la-liga"})
    storage.curated.write_table(
        "fantasy_points",
        pd.DataFrame([{"player_id": BIWENGER_VETERAN, "points_as": 7}]),
        partition={"competition": "la-liga", "season": "2025"},
    )
    return storage


@pytest.fixture
def mappings(tmp_path: Path) -> Path:
    """Mappings con el equipo ya resuelto (Biwenger 10 ↔ Transfermarkt 2497)."""
    root = tmp_path / "mappings"
    root.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "t001",
                "fuente": fuente,
                "id_en_fuente": source_id,
                "metodo": "manual",
                "fecha": "2026-07-01",
            }
            for fuente, source_id in (("biwenger", BIWENGER_TEAM), ("transfermarkt", TM_CLUB))
        ]
    ).to_csv(root / "teams.csv", index=False)
    return root


def approve_player(mappings: Path, canonical_id: str = "p00001") -> None:
    """Deja el fichaje ya mapeado a su contraparte de Transfermarkt."""
    pd.DataFrame(
        [
            {
                "canonical_id": canonical_id,
                "fuente": fuente,
                "id_en_fuente": source_id,
                "metodo": "manual",
                "fecha": "2026-07-01",
            }
            for fuente, source_id in (("biwenger", BIWENGER_FORES), ("transfermarkt", TM_FORES))
        ]
    ).to_csv(mappings / "players.csv", index=False)


def run(storage: Storage, mappings: Path, sofascore, transfermarkt, **kwargs):
    return ingest_newcomers(
        storage,
        "la-liga",
        SEASON,
        mappings_dir=str(mappings),
        sofascore_transport=sofascore,
        transfermarkt_transport=transfermarkt,
        **kwargs,
    )


# --- detección ---------------------------------------------------------------


def test_squad_player_without_past_points_is_a_newcomer(storage: Storage) -> None:
    newcomers = detect_newcomers(storage, "la-liga", SEASON)

    assert [n.player_id for n in newcomers] == [BIWENGER_FORES]
    assert newcomers[0].name == "Álex Forés"
    assert newcomers[0].team_id == BIWENGER_TEAM


def test_points_in_the_current_season_do_not_disqualify_a_newcomer(storage: Storage) -> None:
    # Ya ha jugado dos jornadas de la temporada en curso: sigue siendo un fichaje,
    # porque lo que le falta es historial del que proyectar, no minutos.
    storage.curated.write_table(
        "fantasy_points",
        pd.DataFrame([{"player_id": BIWENGER_FORES, "points_as": 4}]),
        partition={"competition": "la-liga", "season": str(SEASON)},
    )

    assert [n.player_id for n in detect_newcomers(storage, "la-liga", SEASON)] == [BIWENGER_FORES]


def test_player_promoted_from_segunda_is_a_newcomer(storage: Storage) -> None:
    # Segunda es una liga de origen más: que tenga puntos de Biwenger allí no le
    # da historial de La Liga, así que su baseline sale del mismo sitio que el del
    # que llega del Brasileirão (eventing de SofaScore + valor de Transfermarkt,
    # corregidos por el nivel de la liga). Sus puntos de Segunda sirven para
    # validar el método, no para alimentarlo.
    storage.curated.write_table(
        "fantasy_points",
        pd.DataFrame([{"player_id": BIWENGER_FORES, "points_as": 9}]),
        partition={"competition": "segunda-division", "season": "2025"},
    )

    assert [n.player_id for n in detect_newcomers(storage, "la-liga", SEASON)] == [BIWENGER_FORES]


def test_points_in_a_past_season_of_the_same_competition_disqualify(storage: Storage) -> None:
    # El veterano de la fixture puntuó en La Liga 2025; el que vuelve tras un año
    # cedido en Segunda tampoco es un fichaje si ya jugó La Liga antes.
    storage.curated.write_table(
        "fantasy_points",
        pd.DataFrame([{"player_id": BIWENGER_FORES, "points_as": 5}]),
        partition={"competition": "la-liga", "season": "2024"},
    )

    assert detect_newcomers(storage, "la-liga", SEASON) == []


# --- descarga bajo demanda ---------------------------------------------------


def test_newcomer_ends_up_with_curated_history_without_intervention(
    storage: Storage, mappings: Path
) -> None:
    approve_player(mappings)
    sofascore = RoutingTransport(sofascore_routes())

    result = run(storage, mappings, sofascore, RoutingTransport(transfermarkt_routes()))

    # Historial curado: sus partidos de SofaScore, en las dos ligas por las que ha
    # pasado. Las filas llevan su id de SofaScore; el enlace al ID canónico lo
    # aprueba la ronda de matching de SofaScore (que sigue siendo manual), y
    # mientras tanto el jugador queda encolado en sofascore-review.csv.
    matches = storage.curated.read_table("player_match_stats")
    assert not matches.empty
    assert set(matches["sofascore_player_id"]) == {1086128}

    # Y su registro de fichaje, con el historial marcado como descargado.
    registered = storage.curated.read_table(TABLE)
    assert list(registered["player_id"]) == [BIWENGER_FORES]
    assert list(registered["history"]) == [DOWNLOADED]
    assert list(registered["canonical_id"]) == ["p00001"]
    assert list(registered["season"]) == [str(SEASON)]
    assert result.rows[TABLE] == 1
    assert not result.failures


def test_transfermarkt_refresh_covers_only_the_arriving_club(
    storage: Storage, mappings: Path
) -> None:
    approve_player(mappings)
    transfermarkt = RoutingTransport(transfermarkt_routes())

    run(storage, mappings, RoutingTransport(sofascore_routes()), transfermarkt)

    # Una sola plantilla pedida (la del club de llegada), no las 20 de la competición.
    kaders = [url for url in transfermarkt.urls if "kader/verein" in url]
    assert len(kaders) == 1
    assert f"verein/{TM_CLUB}" in kaders[0]
    assert not storage.curated.read_table("transfermarkt_players").empty


def test_doubtful_mapping_is_enqueued_and_the_run_survives(
    storage: Storage, mappings: Path
) -> None:
    # Sin mapping previo: el matcher no puede resolver al fichaje por sí solo
    # (la plantilla de Transfermarkt le ofrece varios candidatos), así que lo
    # encola a revisión. El run sigue y su historial se descarga igualmente.
    result = run(
        storage,
        mappings,
        RoutingTransport(sofascore_routes()),
        RoutingTransport(transfermarkt_routes()),
    )

    review = pd.read_csv(mappings / "players-review.csv", dtype=str)
    assert str(BIWENGER_FORES) in set(review["biwenger_id"])
    assert result.stats["fichajes encolados a revisión de mapping"] == 1

    registered = storage.curated.read_table(TABLE)
    assert list(registered["history"]) == [DOWNLOADED]
    assert list(registered["canonical_id"]) == [""]
    assert not storage.curated.read_table("player_match_stats").empty


def test_unmapped_sofascore_player_is_enqueued_for_review(storage: Storage, mappings: Path) -> None:
    approve_player(mappings)

    run(
        storage,
        mappings,
        RoutingTransport(sofascore_routes()),
        RoutingTransport(transfermarkt_routes()),
    )

    review = pd.read_csv(mappings / "sofascore-review.csv", dtype=str)
    assert "1086128" in set(review["sofascore_id"])


def test_player_without_sofascore_profile_is_recorded_and_retried(
    storage: Storage, mappings: Path
) -> None:
    approve_player(mappings)
    routes = sofascore_routes(search=b'{"results": []}')

    result = run(
        storage, mappings, RoutingTransport(routes), RoutingTransport(transfermarkt_routes())
    )

    assert list(storage.curated.read_table(TABLE)["history"]) == [NO_HISTORY]
    assert result.anomalies["fichajes sin ficha en SofaScore"] == 1
    assert not result.failures  # que la fuente no le conozca no es un fallo del run

    # Sin historial no se da por hecho: el run siguiente vuelve a intentarlo.
    again = run(
        storage,
        mappings,
        RoutingTransport(sofascore_routes()),
        RoutingTransport(transfermarkt_routes()),
    )
    assert list(storage.curated.read_table(TABLE)["history"]) == [DOWNLOADED]
    assert again.stats["fichajes ya registrados"] == 0


def test_sofascore_failure_does_not_abort_the_run(storage: Storage, mappings: Path) -> None:
    approve_player(mappings)

    result = run(storage, mappings, RaisingTransport(503), RoutingTransport(transfermarkt_routes()))

    assert list(storage.curated.read_table(TABLE)["history"]) == ["fallo"]
    assert [f.status for f in result.failures] == [503]


def test_team_without_mapping_still_gets_the_history(storage: Storage, tmp_path: Path) -> None:
    # Equipo recién ascendido, aún sin mapping: no hay plantilla de Transfermarkt
    # que refrescar, pero el fichaje no se queda sin historial por eso.
    empty_mappings = tmp_path / "mappings-vacios"
    empty_mappings.mkdir()
    transfermarkt = RoutingTransport(transfermarkt_routes())

    result = run(storage, empty_mappings, RoutingTransport(sofascore_routes()), transfermarkt)

    assert transfermarkt.urls == []
    assert list(storage.curated.read_table(TABLE)["history"]) == [DOWNLOADED]
    assert result.rows[TABLE] == 1


def test_second_run_downloads_nothing(storage: Storage, mappings: Path) -> None:
    approve_player(mappings)
    run(
        storage,
        mappings,
        RoutingTransport(sofascore_routes()),
        RoutingTransport(transfermarkt_routes()),
    )

    sofascore = RoutingTransport(sofascore_routes())
    transfermarkt = RoutingTransport(transfermarkt_routes())
    result = run(storage, mappings, sofascore, transfermarkt)

    assert sofascore.urls == []
    assert transfermarkt.urls == []
    assert result.stats["fichajes ya registrados"] == 1
    assert result.rows[TABLE] == 0
    assert len(storage.curated.read_table(TABLE)) == 1


def test_max_newcomers_defers_the_rest_to_the_next_run(storage: Storage, mappings: Path) -> None:
    # Con el histórico de puntos a medias, media plantilla parece recién llegada:
    # el tope es lo que evita que un run anómalo dispare cientos de descargas.
    storage.curated.write_table(
        "biwenger_players",
        pd.DataFrame(
            [
                {"id": BIWENGER_FORES, "name": "Álex Forés", "team_id": BIWENGER_TEAM},
                {"id": 3, "name": "Otro fichaje", "team_id": BIWENGER_TEAM},
            ]
        ),
        partition={"competition": "la-liga"},
    )
    sofascore = RoutingTransport(sofascore_routes())

    result = run(
        storage,
        mappings,
        sofascore,
        RoutingTransport(transfermarkt_routes()),
        max_newcomers=1,
    )

    assert result.stats["fichajes detectados"] == 2
    assert result.stats["fichajes aplazados al siguiente run (tope)"] == 1
    assert list(storage.curated.read_table(TABLE)["player_id"]) == [BIWENGER_FORES]


def test_dry_run_detects_without_downloading(storage: Storage, mappings: Path) -> None:
    sofascore = RoutingTransport(sofascore_routes())
    transfermarkt = RoutingTransport(transfermarkt_routes())

    result = run(storage, mappings, sofascore, transfermarkt, dry_run=True)

    assert result.stats["fichajes detectados"] == 1
    assert sofascore.urls == []
    assert transfermarkt.urls == []
    with pytest.raises(FileNotFoundError):
        storage.curated.read_table(TABLE)


def test_cli_dry_run_reports_the_newcomer(storage: Storage, mappings: Path, capsys) -> None:
    code = main(
        [
            "newcomers",
            "--competition",
            "la-liga",
            "--season",
            str(SEASON),
            "--dry-run",
            "--data",
            storage.base_uri,
            "--mappings",
            str(mappings),
        ]
    )

    assert code == 0
    assert "fichajes detectados: 1" in capsys.readouterr().out
