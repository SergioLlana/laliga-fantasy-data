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
from lfdata.sources.sofascore import (
    SofaScoreClient,
    SourceFormatError,
    backfill_league_season,
    crossvalidate_minutes,
    ingest_player,
    resolve_season_id,
    season_year_label,
)
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


# --- backfill por liga-temporada --------------------------------------------


def backfill_routes() -> dict[str, bytes]:
    return {
        "events/last/0": fixture("events-8-77559-last-0.json"),
        "/lineups": fixture("lineups.json"),
    }


def test_season_year_label_uses_start_year():
    # 2025 = temporada 2025/26, como en Transfermarkt.
    assert season_year_label(2025) == "25/26"
    assert season_year_label(2021) == "21/22"


def test_resolve_season_id_maps_start_year_to_sofascore_id(tmp_path):
    routes = {"unique-tournament/8/seasons": fixture("tournament-seasons-8.json")}
    client = SofaScoreClient(RoutingTransport(routes), storage_at(tmp_path).raw)
    assert resolve_season_id(client, 8, 2025) == 77559  # 25/26
    assert resolve_season_id(client, 8, 2024) == 61643  # 24/25


def test_resolve_season_id_rejects_unknown_year(tmp_path):
    routes = {"unique-tournament/8/seasons": fixture("tournament-seasons-8.json")}
    client = SofaScoreClient(RoutingTransport(routes), storage_at(tmp_path).raw)
    with pytest.raises(ValueError, match="30/31"):
        resolve_season_id(client, 8, 2030)


def test_backfill_writes_a_row_per_player_per_match(tmp_path):
    storage = storage_at(tmp_path)
    result = backfill_league_season(
        storage,
        8,
        77559,
        season_year="25/26",
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(backfill_routes()),
    )

    # Dos partidos en la fixture, 46 jugadores con estadística cada uno.
    assert result.stats["partidos"] == 2
    assert result.rows["player_match_stats"] == 92

    matches = storage.curated.read_table("player_match_stats")
    assert len(matches) == 92
    assert {True, False} <= set(matches["is_home"])
    assert matches["opponent"].notna().all()
    assert (matches["source"] == "sofascore").all()
    assert (matches["canonical_id"] == "").all()


def test_backfill_is_resumable_by_raw_presence(tmp_path):
    storage = storage_at(tmp_path)
    mappings = str(tmp_path / "mappings")
    routes = backfill_routes()
    backfill_league_season(
        storage, 8, 77559, mappings_dir=mappings, transport=RoutingTransport(routes)
    )

    again = backfill_league_season(
        storage, 8, 77559, mappings_dir=mappings, transport=RoutingTransport(routes)
    )
    # Segunda pasada: los partidos ya están en raw/, no se re-descargan.
    assert again.stats["partidos"] == 0
    assert again.stats["partidos_saltados"] == 2
    assert again.rows["player_match_stats"] == 0
    assert len(storage.curated.read_table("player_match_stats")) == 92


def test_backfill_respects_max_matches(tmp_path):
    storage = storage_at(tmp_path)
    result = backfill_league_season(
        storage,
        8,
        77559,
        max_matches=1,
        mappings_dir=str(tmp_path / "mappings"),
        transport=RoutingTransport(backfill_routes()),
    )
    assert result.stats["partidos"] == 1
    assert result.rows["player_match_stats"] == 46


# --- cruce de minutos vs Biwenger -------------------------------------------


def _seed_crosscheck(tmp_path, sofascore_minutes, biwenger_minutes):
    """Siembra player_match_stats (SofaScore) y fantasy_points (Biwenger) + mapping."""
    storage = storage_at(tmp_path)
    so = pd.DataFrame(
        [
            {"canonical_id": "p00001", "source": "sofascore", "date": d, "minutes": m}
            for d, m in sofascore_minutes
        ]
    )
    storage.curated.write_table(
        "player_match_stats", so, partition={"competition": "8", "season": "77559"}
    )
    bi = pd.DataFrame([{"player_id": 111, "date": d, "minutes": m} for d, m in biwenger_minutes])
    storage.curated.write_table(
        "fantasy_points", bi, partition={"competition": "la-liga", "season": "2025"}
    )
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "p00001",
                "fuente": "biwenger",
                "id_en_fuente": "111",
                "metodo": "manual",
                "fecha": "2026-07-11",
            }
        ]
    ).to_csv(mappings / "players.csv", index=False)
    return storage, str(mappings)


def test_crosscheck_flags_minutes_discrepancies(tmp_path):
    storage, mappings = _seed_crosscheck(
        tmp_path,
        sofascore_minutes=[("2025-05-10", 90), ("2025-05-17", 90)],
        biwenger_minutes=[("2025-05-10", 88), ("2025-05-17", 50)],
    )
    report = crossvalidate_minutes(storage, mappings_dir=mappings)
    assert report.common_rows == 2
    assert report.within_tolerance == 1  # 90 vs 88 entra; 90 vs 50 no
    assert not report.passes
    assert report.discrepancies[0]["minutes_biwenger"] == 50


def test_crosscheck_without_mappings_reports_zero_common(tmp_path):
    storage = storage_at(tmp_path)
    storage.curated.write_table(
        "player_match_stats",
        pd.DataFrame(
            [{"canonical_id": "", "source": "sofascore", "date": "2025-05-10", "minutes": 90}]
        ),
        partition={"competition": "8", "season": "77559"},
    )
    report = crossvalidate_minutes(storage, mappings_dir=str(tmp_path / "mappings"))
    assert report.common_rows == 0
    assert not report.passes
    assert "0 filas comunes" in report.summary()
