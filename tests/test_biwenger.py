"""Tests del cliente e ingesta de Biwenger contra fixtures reales, sin red."""

import json
from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.sources.biwenger import (
    BiwengerClient,
    SourceFormatError,
    ingest_reports,
    ingest_squad,
)
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "biwenger"
FIXTURE = FIXTURES / "competition-data-la-liga.json"
PLAYER_LA_LIGA = FIXTURES / "player-reports-la-liga.json"
PLAYER_SEGUNDA = FIXTURES / "player-reports-segunda-division.json"


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, params=None) -> bytes:
        self.urls.append(url)
        return self.payload


def _competition_payload(*slugs: str) -> bytes:
    """Plantilla mínima con los jugadores dados (solo lo que exige el modelo)."""
    players = {
        str(i): {
            "id": i,
            "name": slug,
            "slug": slug,
            "position": 4,
            "status": "ok",
            "price": 100000,
            "priceIncrement": 0,
        }
        for i, slug in enumerate(slugs, start=1)
    }
    payload = {
        "status": 200,
        "data": {
            "id": 1,
            "name": "Primera División",
            "slug": "la-liga",
            "season": {"id": "2026", "name": "2025/2026", "slug": "2025-2026"},
            "players": players,
            "teams": {},
        },
    }
    return json.dumps(payload).encode()


class RoutingTransport:
    """Devuelve la plantilla para /data y el detalle para /players/{slug}."""

    def __init__(self, competition: bytes, player: bytes) -> None:
        self.competition = competition
        self.player = player
        self.urls: list[str] = []

    def get(self, url, params=None) -> bytes:
        self.urls.append(url)
        return self.player if "/players/" in url else self.competition


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path}")


def raw_files(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "raw").rglob("*.json")) if (tmp_path / "raw").exists() else []


def test_fetch_validates_real_fixture(storage: Storage) -> None:
    transport = FakeTransport(FIXTURE.read_bytes())
    response = BiwengerClient(transport, storage.raw).fetch_competition_data("la-liga")
    assert response.status == 200
    assert response.data.slug == "la-liga"
    assert len(response.data.players) == 9
    assert len(response.data.teams) == 4
    mumin = response.data.players["28082"]
    assert mumin.team_id is None  # jugador sin equipo


def test_raw_written_before_interpreting(storage: Storage, tmp_path: Path) -> None:
    transport = FakeTransport(b'{"esto": "no es una plantilla"}')
    with pytest.raises(SourceFormatError, match="cambió la forma"):
        BiwengerClient(transport, storage.raw).fetch_competition_data("la-liga")
    files = raw_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == b'{"esto": "no es una plantilla"}'


def test_missing_field_fails_with_clear_error(storage: Storage) -> None:
    payload = json.loads(FIXTURE.read_text())
    for player in payload["data"]["players"].values():
        del player["slug"]
    transport = FakeTransport(json.dumps(payload).encode())
    with pytest.raises(SourceFormatError, match="slug"):
        BiwengerClient(transport, storage.raw).fetch_competition_data("la-liga")


def test_unknown_competition_rejected(storage: Storage) -> None:
    client = BiwengerClient(FakeTransport(b"{}"), storage.raw)
    with pytest.raises(ValueError, match="premier"):
        client.fetch_competition_data("premier")


def test_ingest_squad_writes_curated_tables(storage: Storage, tmp_path: Path) -> None:
    rows = ingest_squad(storage, "la-liga", transport=FakeTransport(FIXTURE.read_bytes()))
    assert rows == {"biwenger_players": 9, "biwenger_teams": 4}

    parquet = tmp_path / "curated" / "biwenger_players" / "competition=la-liga" / "data.parquet"
    assert len(pd.read_parquet(parquet)) == 9

    players = storage.curated.read_table("biwenger_players")
    assert len(players) == 9
    assert {"id", "slug", "name", "position", "team_id", "status", "price", "competition"} <= set(
        players.columns
    )
    assert players["competition"].astype(str).unique().tolist() == ["la-liga"]

    teams = storage.curated.read_table("biwenger_teams")
    assert len(teams) == 4
    assert {"id", "slug", "name", "competition"} <= set(teams.columns)


def test_cli_ingest_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "lfdata.sources.http.HttpTransport.get",
        lambda self, url, params=None: FIXTURE.read_bytes(),
    )
    exit_code = main(
        ["ingest", "biwenger", "--competition", "la-liga", "--data", f"file://{tmp_path}"]
    )
    assert exit_code == 0
    assert "biwenger_players: 9 filas" in capsys.readouterr().out
    parquet = tmp_path / "curated" / "biwenger_players" / "competition=la-liga" / "data.parquet"
    assert parquet.exists()
    assert raw_files(tmp_path)


# --- Detalle por jugador: fantasy_points y biwenger_prices (#3) -------------


def test_player_reports_contract_la_liga(storage: Storage) -> None:
    """Fija la forma real del detalle en La Liga: 5 sistemas y nota SofaScore."""
    transport = FakeTransport(PLAYER_LA_LIGA.read_bytes())
    detail = (
        BiwengerClient(transport, storage.raw)
        .fetch_player_reports("la-liga", "alex-fores", "2026")
        .data
    )

    assert detail.id == 37714
    scored = [report for report in detail.reports if report.scored]
    assert len(scored) == 3  # partidos no jugados quedan fuera
    first = scored[0]
    assert set(first.points) == {"1", "2", "3", "5", "6"}
    assert first.raw_stats.minutes_played == 58
    assert first.raw_stats.sofascore == 6.5  # La Liga sí publica nota
    assert len(detail.prices) == 4


def test_player_reports_contract_segunda_has_no_sofascore(storage: Storage) -> None:
    """En Segunda el detalle no trae nota SofaScore ni los sistemas 5/6."""
    transport = FakeTransport(PLAYER_SEGUNDA.read_bytes())
    detail = (
        BiwengerClient(transport, storage.raw)
        .fetch_player_reports("segunda-division", "alex-fores", "2025")
        .data
    )

    scored = [report for report in detail.reports if report.scored]
    assert all(report.raw_stats.sofascore is None for report in scored)
    assert all("5" not in report.points and "6" not in report.points for report in scored)


def test_player_reports_raw_written_before_interpreting(storage: Storage, tmp_path: Path) -> None:
    transport = FakeTransport(b'{"status": 200, "data": {"esto": "mal"}}')
    with pytest.raises(SourceFormatError, match="cambió la forma"):
        BiwengerClient(transport, storage.raw).fetch_player_reports("la-liga", "x", "2026")
    assert len(raw_files(tmp_path)) == 1


def test_ingest_reports_writes_both_tables(storage: Storage, tmp_path: Path) -> None:
    transport = RoutingTransport(_competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes())
    rows = ingest_reports(storage, "la-liga", "2026", transport=transport)
    assert rows == {"fantasy_points": 3, "biwenger_prices": 4}

    points = storage.curated.read_table("fantasy_points")
    assert {
        "player_id",
        "match_id",
        "round_id",
        "points_as",
        "points_sofascore",
        "points_stats",
        "points_media",
        "points_social",
        "minutes",
        "sofascore_grade",
        "home",
        "home_score",
        "away_score",
        "result",
    } <= set(points.columns)

    row = points[points["match_id"] == 46156].iloc[0]
    assert row["points_as"] == 2
    assert row["points_social"] == 0
    assert row["minutes"] == 58
    assert row["sofascore_grade"] == 6.5
    assert bool(row["home"]) is False
    assert (row["home_score"], row["away_score"]) == (2, 0)
    assert row["result"] == "loss"

    prices = storage.curated.read_table("biwenger_prices")
    assert set(prices.columns) >= {"player_id", "date", "price"}
    first = prices.sort_values("date").iloc[0]
    assert str(first["date"]) == "2025-07-21"  # AAMMDD 250721
    assert first["price"] == 230000


def test_ingest_reports_segunda_leaves_sofascore_null(storage: Storage) -> None:
    transport = RoutingTransport(_competition_payload("alex-fores"), PLAYER_SEGUNDA.read_bytes())
    ingest_reports(storage, "segunda-division", "2025", transport=transport)
    points = storage.curated.read_table("fantasy_points")
    assert points["sofascore_grade"].isna().all()
    assert points["points_media"].isna().all()  # sistema 5 ausente en Segunda
    assert "draw" in set(points["result"])  # el 1-1 queda como empate


def test_ingest_reports_is_idempotent(storage: Storage) -> None:
    def run() -> None:
        ingest_reports(
            storage,
            "la-liga",
            "2026",
            transport=RoutingTransport(
                _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes()
            ),
        )

    run()
    run()
    # La partición (competition, season) se reescribe entera: sin duplicados.
    assert len(storage.curated.read_table("fantasy_points")) == 3
    assert len(storage.curated.read_table("biwenger_prices")) == 4


def test_cli_ingest_with_season_adds_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    def fake_get(self, url, params=None) -> bytes:
        if "/players/" in url:
            return PLAYER_LA_LIGA.read_bytes()
        return _competition_payload("alex-fores")

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    exit_code = main(
        [
            "ingest",
            "biwenger",
            "--competition",
            "la-liga",
            "--season",
            "2026",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "fantasy_points: 3 filas" in out
    assert "biwenger_prices: 4 filas" in out
