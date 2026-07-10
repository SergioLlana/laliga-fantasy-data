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
from lfdata.sources.http import SourceHTTPError
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
    result = ingest_squad(storage, "la-liga", transport=FakeTransport(FIXTURE.read_bytes()))
    assert result.rows == {"biwenger_players": 9, "biwenger_teams": 4}
    assert result.failures == []

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
    result = ingest_reports(storage, "la-liga", "2026", transport=transport)
    assert result.rows == {"fantasy_points": 3, "biwenger_prices": 4}

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


# --- fecha de nacimiento desde el detalle (#37) -----------------------------


def test_ingest_squad_leaves_birth_date_empty(storage: Storage) -> None:
    """La plantilla no trae fecha: la columna existe pero queda vacía."""
    ingest_squad(storage, "la-liga", transport=FakeTransport(FIXTURE.read_bytes()))
    players = storage.curated.read_table("biwenger_players")
    assert "birth_date" in players.columns
    assert players["birth_date"].isna().all()


def test_ingest_reports_fills_birth_date_on_players(storage: Storage) -> None:
    """Reports rellena birth_date en biwenger_players desde el detalle, sin perder el resto."""
    ingest_squad(storage, "la-liga", transport=FakeTransport(_competition_payload("alex-fores")))
    ingest_reports(
        storage,
        "la-liga",
        "2026",
        transport=RoutingTransport(_competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes()),
    )
    players = storage.curated.read_table("biwenger_players")
    row = players[players["id"] == 1].iloc[0]
    assert row["birth_date"] == "2001-04-12"  # birthday 20010412 del detalle
    assert row["name"] == "alex-fores"  # el resto de la fila se conserva


def test_birthday_zero_means_unknown_not_year_zero() -> None:
    # Biwenger publica birthday 0 cuando no conoce la fecha (p. ej. anselmi):
    # no es date(0, 0, 0), es ausencia. Antes reventaba la ingesta entera.
    from lfdata.sources.biwenger.ingest import _birthday_to_iso

    assert _birthday_to_iso(0) is None
    assert _birthday_to_iso(None) is None
    assert _birthday_to_iso(20010412) == "2001-04-12"


# --- reports con puntos pero sin rawStats (#39) -----------------------------


def _detail_with_reports(reports: list[dict]) -> bytes:
    return json.dumps(
        {
            "status": 200,
            "data": {
                "id": 1,
                "name": "alex-fores",
                "slug": "alex-fores",
                "birthday": 20010412,
                "reports": reports,
                "prices": [[250721, 230000]],
            },
        }
    ).encode()


def _report(match_id: int, *, points: dict | None, raw_stats: dict | None) -> dict:
    report: dict = {"home": True, "match": {"id": match_id, "round": {"id": 1, "name": "J1"}}}
    if points is not None:
        report["points"] = points
    if raw_stats is not None:
        report["rawStats"] = raw_stats
    return report


_FULL_STATS = {"minutesPlayed": 90, "sofascore": 7.0, "homeScore": 1, "awayScore": 0, "win": True}


def test_reports_with_points_but_no_rawstats_are_counted(storage: Storage) -> None:
    detail = _detail_with_reports(
        [
            _report(10, points={"1": 2}, raw_stats=_FULL_STATS),  # completo -> fila
            _report(20, points={"1": 3}, raw_stats=None),  # puntos sin rawStats -> anomalía
        ]
    )
    transport = RoutingTransport(_competition_payload("alex-fores"), detail)
    result = ingest_reports(storage, "la-liga", "2026", transport=transport)

    # Solo el report completo llega a curated; el otro se cuenta, no se silencia.
    assert result.rows["fantasy_points"] == 1
    assert result.anomalies == {"reports con puntos sin rawStats": 1}
    assert len(storage.curated.read_table("fantasy_points")) == 1


def test_cli_ingest_reports_reports_anomaly(tmp_path: Path, monkeypatch, capsys) -> None:
    detail = _detail_with_reports([_report(20, points={"1": 3}, raw_stats=None)])

    def fake_get(self, url, params=None) -> bytes:
        return detail if "/players/" in url else _competition_payload("alex-fores")

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
    # Es un aviso de calidad, no un fallo: no cambia el código de salida.
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "anomalía: 1 reports con puntos sin rawStats" in out


# --- resiliencia: un fallo por jugador no aborta el run (#36) ---------------


class Fail404OnPlayerTransport:
    """Plantilla y detalle normales, salvo un 404 en el slug indicado."""

    def __init__(self, competition: bytes, player: bytes, fail_slug: str) -> None:
        self.competition = competition
        self.player = player
        self.fail_slug = fail_slug
        self.urls: list[str] = []

    def get(self, url, params=None) -> bytes:
        self.urls.append(url)
        if "/players/" not in url:
            return self.competition
        if url.endswith(f"/{self.fail_slug}"):
            raise SourceHTTPError(url, 404)
        return self.player


class FailHardOnSecondPlayer:
    """Devuelve el detalle del primer jugador y revienta (no-HTTP) en el segundo."""

    def __init__(self, competition: bytes, player: bytes) -> None:
        self.competition = competition
        self.player = player
        self.player_calls = 0

    def get(self, url, params=None) -> bytes:
        if "/players/" not in url:
            return self.competition
        self.player_calls += 1
        if self.player_calls > 1:
            raise RuntimeError("red caída a mitad de run")
        return self.player


def test_ingest_reports_404_skips_player_and_curates_rest(storage: Storage) -> None:
    transport = Fail404OnPlayerTransport(
        _competition_payload("alex-fores", "baja"), PLAYER_LA_LIGA.read_bytes(), "baja"
    )
    result = ingest_reports(storage, "la-liga", "2026", transport=transport)

    # Solo alex-fores se curó; "baja" quedó como fallo, no abortó el run.
    assert result.rows == {"fantasy_points": 3, "biwenger_prices": 4}
    assert len(result.failures) == 1
    assert result.failures[0].player == "baja"
    assert result.failures[0].status == 404
    assert len(storage.curated.read_table("fantasy_points")) == 3


def test_ingest_reports_incremental_preserves_progress_on_crash(storage: Storage) -> None:
    transport = FailHardOnSecondPlayer(
        _competition_payload("alex-fores", "otro"), PLAYER_LA_LIGA.read_bytes()
    )
    with pytest.raises(RuntimeError, match="red caída"):
        ingest_reports(storage, "la-liga", "2026", transport=transport, batch_size=1)

    # El primer jugador se volcó por lote antes de que el segundo reventara.
    assert len(storage.curated.read_table("fantasy_points")) == 3
    assert len(storage.curated.read_table("biwenger_prices")) == 4


def test_cli_ingest_reports_404_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    def fake_get(self, url, params=None):
        if "/players/" not in url:
            return _competition_payload("alex-fores", "baja")
        if url.endswith("/baja"):
            raise SourceHTTPError(url, 404)
        return PLAYER_LA_LIGA.read_bytes()

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
    assert exit_code == 1  # hubo un fallo
    out = capsys.readouterr().out
    assert "1 jugadores fallaron" in out
    assert "baja" in out


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
