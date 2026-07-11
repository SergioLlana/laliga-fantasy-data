"""Tests del cliente e ingesta de SofaScore contra fixtures reales, sin red.

Las fixtures se grabaron el 2026-07-11 del caso Álex Forés (player 1086128), el
mismo del experimento docs/experiments/2026-07-07-alex-fores.md. Se recortaron a
LaLiga (ut 8, season 77559 = 25/26) y LaLiga2 (ut 54, season 62048 = 24/25), con
dos partidos por temporada, para un run determinista.
"""

from pathlib import Path

import pandas as pd
import pytest

from lfdata.sources.http import SourceHTTPError
from lfdata.sources.sofascore import SofaScoreClient, SourceFormatError, ingest_player
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "sofascore"
FORES = 1086128


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


class RaisingTransport:
    """Todas las peticiones fallan con el estado dado (bloqueo simulado)."""

    def __init__(self, status: int) -> None:
        self.status = status

    def get(self, url: str, params=None) -> bytes:
        raise SourceHTTPError(url, self.status)


def default_routes() -> dict[str, bytes]:
    return {
        "statistics/seasons": fixture("seasons.json"),
        "search/all": fixture("search.json"),
        "unique-tournament/8/season/77559/statistics/overall": fixture("overall-8-77559.json"),
        "unique-tournament/8/season/77559/ratings": fixture("ratings-8-77559.json"),
        "unique-tournament/54/season/62048/statistics/overall": fixture("overall-54-62048.json"),
        "unique-tournament/54/season/62048/ratings": fixture("ratings-54-62048.json"),
        "/event/": fixture("event-player-stats.json"),
    }


def storage_at(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path / 'data'}")


# --- cliente ----------------------------------------------------------------


def test_client_parses_each_endpoint(tmp_path):
    client = SofaScoreClient(RoutingTransport(default_routes()), storage_at(tmp_path).raw)

    seasons = client.fetch_seasons(FORES)
    ids = {t.unique_tournament.id for t in seasons.unique_tournament_seasons}
    assert {8, 54} <= ids

    overall = client.fetch_overall(FORES, 8, 77559)
    assert overall.statistics["minutesPlayed"] == 382

    ratings = client.fetch_ratings(FORES, 8, 77559)
    assert ratings.season_ratings[0].opponent is not None

    players = client.search_players("Alex Fores").players()
    assert any(p.id == FORES for p in players)


def test_client_raises_source_format_error_on_garbage(tmp_path):
    client = SofaScoreClient(
        RoutingTransport({"statistics/seasons": b"{not json"}), storage_at(tmp_path).raw
    )
    with pytest.raises(SourceFormatError):
        client.fetch_seasons(FORES)


def test_sustained_403_carries_proxy_hint(tmp_path):
    client = SofaScoreClient(RaisingTransport(403), storage_at(tmp_path).raw)
    with pytest.raises(SourceHTTPError) as excinfo:
        client.fetch_seasons(FORES)
    assert "ScrapeOps" in str(excinfo.value)


# --- ingesta bajo demanda ---------------------------------------------------


def test_ingest_by_name_writes_both_tables_and_reproduces_experiment(tmp_path):
    storage = storage_at(tmp_path)
    result = ingest_player(
        storage,
        "Alex Fores",
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )

    # Dos temporadas (LaLiga + LaLiga2), dos partidos cada una.
    assert result.stats["temporadas"] == 2
    assert result.rows["player_season_stats"] == 2
    assert result.rows["player_match_stats"] == 4

    season = storage.curated.read_table("player_season_stats")
    laliga = season[season["competition"] == "8"].iloc[0]
    # Reproduce los números del experimento (LaLiga 25/26).
    assert laliga["minutesPlayed"] == 382
    assert round(float(laliga["rating"]), 2) == 6.48

    # LaLiga2 no publica xG: la columna no existe en esa partición.
    laliga2 = season[season["competition"] == "54"]
    assert "expectedGoals" not in laliga2.dropna(axis=1, how="all").columns


def test_ingest_populates_per_match_event_metrics(tmp_path):
    storage = storage_at(tmp_path)
    ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(default_routes()),
    )

    matches = storage.curated.read_table("player_match_stats")
    assert len(matches) == 4
    row = matches.iloc[0]
    # Métricas de evento por partido (lo que pidió el usuario).
    assert row["minutes"] > 0
    assert row["passes"] > 0
    assert row["shots"] >= 0
    # SofaScore omite los ceros: Forés no marcó en el partido de la fixture.
    assert row["goals"] == 0
    assert row["opponent"] is not None
    assert row["source"] == "sofascore"


def test_unmapped_player_has_empty_canonical_and_is_enqueued(tmp_path):
    mappings = tmp_path / "mappings"
    storage = storage_at(tmp_path)
    ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
    )

    season = storage.curated.read_table("player_season_stats")
    assert (season["canonical_id"] == "").all()

    review = pd.read_csv(mappings / "sofascore-review.csv", dtype=str)
    assert str(FORES) in set(review["sofascore_id"].astype(str))


def test_mapped_player_gets_canonical_id_and_no_review(tmp_path):
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "p00001",
                "fuente": "sofascore",
                "id_en_fuente": str(FORES),
                "metodo": "manual",
                "fecha": "2026-07-11",
            }
        ]
    ).to_csv(mappings / "players.csv", index=False)

    storage = storage_at(tmp_path)
    ingest_player(
        storage,
        str(FORES),
        mappings_dir=str(mappings),
        transport=RoutingTransport(default_routes()),
    )

    season = storage.curated.read_table("player_season_stats")
    assert (season["canonical_id"] == "p00001").all()
    assert not (mappings / "sofascore-review.csv").exists()
