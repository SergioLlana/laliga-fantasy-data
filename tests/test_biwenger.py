"""Tests del cliente e ingesta de Biwenger contra fixtures reales, sin red."""

import json
from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.sources.biwenger import (
    BiwengerClient,
    RoundDiscoveryError,
    SourceFormatError,
    ingest_reports,
    ingest_reports_delta,
    ingest_rounds,
    ingest_squad,
)
from lfdata.sources.biwenger.ingest import COACH_POSITION, REFRESHED, SKIPPED
from lfdata.sources.http import SourceHTTPError
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "biwenger"
FIXTURE = FIXTURES / "competition-data-la-liga.json"
PLAYER_LA_LIGA = FIXTURES / "player-reports-la-liga.json"
PLAYER_SEGUNDA = FIXTURES / "player-reports-segunda-division.json"
ROUND_LA_LIGA = FIXTURES / "round-la-liga.json"


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, params=None) -> bytes:
        self.urls.append(url)
        return self.payload


def _competition_payload(
    *slugs: str,
    rounds: list[tuple[int, str]] | None = None,
    coaches: list[str] | None = None,
) -> bytes:
    """Plantilla mínima con los jugadores dados (solo lo que exige el modelo).

    ``rounds`` inyecta el catálogo ``season.rounds`` (pares id, estado) que el
    refresh por deltas usa para detectar la jornada recién terminada. ``coaches``
    añade entrenadores, que Biwenger publica en la misma lista que los jugadores.
    """
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
    players |= {
        str(i): {
            "id": i,
            "name": slug,
            "slug": slug,
            "position": COACH_POSITION,
            "status": "ok",
            "price": 4420000,
            "priceIncrement": 0,
        }
        for i, slug in enumerate(coaches or [], start=100)
    }
    season = {"id": "2026", "name": "2025/2026", "slug": "2025-2026"}
    if rounds is not None:
        season["rounds"] = [
            {"id": rid, "name": f"J{rid}", "short": f"J{rid}", "status": status}
            for rid, status in rounds
        ]
    payload = {
        "status": 200,
        "data": {
            "id": 1,
            "name": "Primera División",
            "slug": "la-liga",
            "season": season,
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


def test_segunda_division_rejected(storage: Storage) -> None:
    """De Biwenger se ingiere exclusivamente La Liga (ADR 0008).

    Sus ids de jugador y equipo cambian por competición (Boyomo: 33694 en La
    Liga, 22810 en Segunda), así que una segunda competición partiría la
    identidad canónica en dos. Segunda es una liga de origen: Transfermarkt y
    SofaScore.
    """
    client = BiwengerClient(FakeTransport(b"{}"), storage.raw)
    with pytest.raises(ValueError, match="segunda-division"):
        client.fetch_competition_data("segunda-division")
    with pytest.raises(ValueError, match="segunda-division"):
        client.fetch_player_reports("segunda-division", "alex-fores", "2025")
    with pytest.raises(ValueError, match="segunda-division"):
        client.fetch_round("segunda-division", 1, 1)


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


def test_ingest_squad_excludes_coaches(storage: Storage) -> None:
    """Biwenger ficha a los entrenadores como si fueran jugadores; no lo son."""
    payload = _competition_payload("williams", "fores", coaches=["flick", "simeone"])
    result = ingest_squad(storage, "la-liga", transport=FakeTransport(payload))

    assert result.rows["biwenger_players"] == 2
    players = storage.curated.read_table("biwenger_players")
    assert players["slug"].tolist() == ["williams", "fores"]
    assert COACH_POSITION not in players["position"].tolist()


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


def test_player_reports_tolerate_missing_grade_and_systems(storage: Storage) -> None:
    """El modelo admite reports sin nota SofaScore ni sistemas 5/6.

    El fixture es una respuesta real de Segunda (del experimento Forés, cuando
    aún se descargaba): Biwenger ya solo se ingiere para la-liga (ADR 0008),
    pero esa forma incompleta protege contra la variabilidad de la fuente.
    """
    transport = FakeTransport(PLAYER_SEGUNDA.read_bytes())
    detail = (
        BiwengerClient(transport, storage.raw)
        .fetch_player_reports("la-liga", "alex-fores", "2025")
        .data
    )

    scored = [report for report in detail.reports if report.scored]
    assert all(report.raw_stats.sofascore is None for report in scored)
    assert all("5" not in report.points and "6" not in report.points for report in scored)


def test_player_reports_season_is_start_year_translated_to_biwenger_end_year(
    storage: Storage, tmp_path: Path
) -> None:
    """``season`` es el año de inicio (2025 = 2025/26); la API se pide con año+1."""

    class CapturingTransport:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.params: dict | None = None

        def get(self, url, params=None) -> bytes:
            self.params = params
            return self.payload

    transport = CapturingTransport(PLAYER_LA_LIGA.read_bytes())
    BiwengerClient(transport, storage.raw).fetch_player_reports("la-liga", "alex-fores", "2025")

    # La URL usa el año de fin de Biwenger (2025/26 -> season=2026)...
    assert transport.params["season"] == "2026"
    # ...pero el raw se nombra con el año de inicio, coherente con la partición.
    assert any("alex-fores-2025" in p.name for p in raw_files(tmp_path))


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


def test_ingest_reports_leaves_missing_grade_and_systems_null(storage: Storage) -> None:
    """Un detalle sin nota ni sistema 5 (fixture real de Segunda) cura columnas nulas."""
    transport = RoutingTransport(_competition_payload("alex-fores"), PLAYER_SEGUNDA.read_bytes())
    ingest_reports(storage, "la-liga", "2025", transport=transport)
    points = storage.curated.read_table("fantasy_points")
    assert points["sofascore_grade"].isna().all()
    assert points["points_media"].isna().all()  # sistema 5 ausente en el detalle
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


def test_ingest_squad_preserves_birth_dates_from_reports(storage: Storage) -> None:
    """Refrescar la plantilla no borra las birth_date que aportaron los reports."""
    ingest_squad(storage, "la-liga", transport=FakeTransport(_competition_payload("alex-fores")))
    ingest_reports(
        storage,
        "la-liga",
        "2026",
        transport=RoutingTransport(_competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes()),
    )
    ingest_squad(storage, "la-liga", transport=FakeTransport(_competition_payload("alex-fores")))
    players = storage.curated.read_table("biwenger_players")
    assert players[players["id"] == 1].iloc[0]["birth_date"] == "2001-04-12"


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


def test_report_with_null_point_system_parses_and_curates(storage: Storage) -> None:
    # Biwenger sirve en La Liga algún report con un sistema de puntuación en null
    # (visto: el 6/Social) aunque los demás traigan puntos. No es un cambio de
    # forma: el report se cura y esa columna queda nula, no revienta la ingesta.
    detail = _detail_with_reports(
        [_report(10, points={"1": 2, "2": 3, "3": 1, "5": 4, "6": None}, raw_stats=_FULL_STATS)]
    )
    transport = RoutingTransport(_competition_payload("alex-fores"), detail)
    result = ingest_reports(storage, "la-liga", "2026", transport=transport)

    assert result.rows["fantasy_points"] == 1
    row = storage.curated.read_table("fantasy_points").iloc[0]
    assert row["points_as"] == 2
    assert pd.isna(row["points_social"])


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


# --- reanudación del backfill: saltar lo ya descargado en raw/ (#53) ---------


def _player_fetches(transport) -> list[str]:
    return [u for u in transport.urls if "/players/" in u]


def test_ingest_reports_resume_skips_players_in_raw(storage: Storage) -> None:
    """Temporada pasada: relanzar con resume no vuelve a pedir a los ya bajados."""
    squad = _competition_payload("alex-fores", "leo-messi")
    ingest_reports(
        storage, "la-liga", "2025", transport=RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    )

    resumed = RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    result = ingest_reports(storage, "la-liga", "2025", transport=resumed, resume=True)

    assert _player_fetches(resumed) == []  # 0 peticiones de detalle
    assert result.stats == {REFRESHED: 0, SKIPPED: 2}


def test_ingest_reports_since_days_skips_recently_scraped(storage: Storage) -> None:
    """Temporada actual: --since-days salta a los descargados dentro de la ventana."""
    squad = _competition_payload("alex-fores", "leo-messi")
    ingest_reports(
        storage, "la-liga", "2026", transport=RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    )

    resumed = RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    result = ingest_reports(storage, "la-liga", "2026", transport=resumed, since_days=30)

    assert _player_fetches(resumed) == []
    assert result.stats == {REFRESHED: 0, SKIPPED: 2}


def test_ingest_reports_interrupted_player_not_marked_complete(storage: Storage) -> None:
    """Un 404 no escribe raw: al reanudar ese jugador se reintenta, no se salta."""
    squad = _competition_payload("alex-fores", "baja")
    first = Fail404OnPlayerTransport(squad, PLAYER_LA_LIGA.read_bytes(), "baja")
    ingest_reports(storage, "la-liga", "2025", transport=first)

    resumed = RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    result = ingest_reports(storage, "la-liga", "2025", transport=resumed, resume=True)

    # alex-fores tenía raw (se salta); baja no lo tuvo (se reintenta).
    fetched = _player_fetches(resumed)
    assert any(u.endswith("/baja") for u in fetched)
    assert not any(u.endswith("/alex-fores") for u in fetched)
    assert result.stats == {REFRESHED: 1, SKIPPED: 1}


def test_ingest_reports_without_resume_downloads_all(storage: Storage) -> None:
    """Sin resume ni since_days se recorre la plantilla entera aunque haya raw."""
    squad = _competition_payload("alex-fores", "leo-messi")
    ingest_reports(
        storage, "la-liga", "2025", transport=RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    )

    again = RoutingTransport(squad, PLAYER_LA_LIGA.read_bytes())
    result = ingest_reports(storage, "la-liga", "2025", transport=again)

    assert len(_player_fetches(again)) == 2
    assert result.stats == {REFRESHED: 2, SKIPPED: 0}


def test_cli_ingest_reports_resume_and_delta_conflict(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "ingest",
            "biwenger",
            "--competition",
            "la-liga",
            "--season",
            "2026",
            "--delta",
            "--resume",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 2
    assert "no aplican a --delta" in capsys.readouterr().out


def test_cli_ingest_reports_resume_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    def fake_get(self, url, params=None) -> bytes:
        if "/players/" in url:
            return PLAYER_LA_LIGA.read_bytes()
        return _competition_payload("alex-fores")

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    argv = [
        "ingest",
        "biwenger",
        "--competition",
        "la-liga",
        "--season",
        "2025",
        "--resume",
        "--data",
        f"file://{tmp_path}",
    ]
    assert main(argv) == 0  # primer run descarga
    capsys.readouterr()
    assert main(argv) == 0  # segundo run salta
    out = capsys.readouterr().out
    assert f"{SKIPPED}: 1" in out


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


# --- Rounds: puntos por jornada de todos los jugadores (#51) -----------------

# Un jugador que ya dejó la competición: aparece en la jornada aunque su detalle
# por jugador diera 404 y aunque no esté en la plantilla actual.
DEPARTED_PLAYER_ID = 99999


def _round_payload(round_id: int, score: int, *, catalogue_ids: tuple[int, ...]) -> bytes:
    """Jornada sintética con un partido: un jugador local (id 1) y una baja.

    Los puntos valen ``score`` para el local y ``score * 2`` para la baja, para
    poder comprobar que cada una de las cinco peticiones rellena su columna. El
    catálogo ``season.rounds`` lleva ``catalogue_ids`` (lo que se descubre).
    """
    return json.dumps(
        {
            "status": 200,
            "data": {
                "id": round_id,
                "name": f"Round {round_id}",
                "short": "R",
                "status": "finished",
                "scoreID": score,
                "season": {
                    "id": "2025",
                    "name": "2024/2025 season",
                    "slug": "2024-2025",
                    "rounds": [
                        {"id": r, "name": f"Round {r}", "short": "R", "status": "finished"}
                        for r in catalogue_ids
                    ],
                },
                "games": [
                    {
                        "id": round_id * 10,
                        "date": 1700000000,
                        "status": "finished",
                        "home": {
                            "id": 1,
                            "name": "Home",
                            "slug": "home",
                            "score": 2,
                            "reports": [
                                {
                                    "player": {
                                        "id": 1,
                                        "name": "alex-fores",
                                        "slug": "alex-fores",
                                        "position": 4,
                                    },
                                    "points": score,
                                }
                            ],
                        },
                        "away": {
                            "id": 2,
                            "name": "Away",
                            "slug": "away",
                            "score": 0,
                            "reports": [
                                {
                                    "player": {
                                        "id": DEPARTED_PLAYER_ID,
                                        "name": "baja",
                                        "slug": "baja",
                                        "position": 3,
                                    },
                                    "points": score * 2,
                                }
                            ],
                        },
                    }
                ],
            },
        }
    ).encode()


class RoundsTransport:
    """Enruta /data, /players (semilla) y /rounds/{id}?score=N por sistema."""

    def __init__(self, competition: bytes, player: bytes, catalogue_ids: tuple[int, ...]) -> None:
        self.competition = competition
        self.player = player
        self.catalogue_ids = catalogue_ids
        self.round_fetches: list[tuple[int, int]] = []

    def get(self, url, params=None) -> bytes:
        if "/rounds/" in url:
            round_id = int(url.rsplit("/", 1)[1])
            score = int(params["score"])
            self.round_fetches.append((round_id, score))
            return _round_payload(round_id, score, catalogue_ids=self.catalogue_ids)
        if "/players/" in url:
            return self.player
        return self.competition


def test_round_contract_la_liga(storage: Storage) -> None:
    """Fija la forma real de la jornada: partidos, reports y catálogo de rondas."""
    transport = FakeTransport(ROUND_LA_LIGA.read_bytes())
    response = BiwengerClient(transport, storage.raw).fetch_round("la-liga", 4484, 1)

    assert response.data.id == 4484
    assert response.data.score_id == 1
    assert len(response.data.season.rounds) == 3  # catálogo para descubrir jornadas
    game = response.data.games[0]
    assert game.home.slug == "girona"
    assert game.home.score == 1 and game.away.score == 3
    top = game.home.reports[0]
    assert top.player.slug == "portu"
    assert top.points == 5


def test_round_raw_written_before_interpreting(storage: Storage, tmp_path: Path) -> None:
    transport = FakeTransport(b'{"status": 200, "data": {"esto": "mal"}}')
    with pytest.raises(SourceFormatError, match="cambió la forma"):
        BiwengerClient(transport, storage.raw).fetch_round("la-liga", 4484, 1)
    assert len(raw_files(tmp_path)) == 1


def test_ingest_rounds_combines_five_systems(storage: Storage) -> None:
    transport = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
    )
    result = ingest_rounds(storage, "la-liga", "2025", transport=transport)

    # 2 jornadas descubiertas × 2 jugadores por jornada = 4 filas.
    assert result.rows == {"fantasy_round_points": 4}
    points = storage.curated.read_table("fantasy_round_points")
    assert len(points) == 4
    assert {
        "player_id",
        "team_id",
        "match_id",
        "round_id",
        "points_as",
        "points_sofascore",
        "points_stats",
        "points_media",
        "points_social",
        "home",
        "home_score",
        "away_score",
        "result",
    } <= set(points.columns)

    # El local (id 1) en la jornada 4484: cada sistema rellenó su columna.
    home = points[(points["player_id"] == 1) & (points["round_id"] == 4484)].iloc[0]
    assert home["points_as"] == 1  # score=1
    assert home["points_sofascore"] == 2
    assert home["points_stats"] == 3
    assert home["points_media"] == 5
    assert home["points_social"] == 6
    assert bool(home["home"]) is True
    assert (home["home_score"], home["away_score"]) == (2, 0)
    assert home["result"] == "win"


class NullSofascoreRoundsTransport(RoundsTransport):
    """Como RoundsTransport pero el sistema 2 (SofaScore) puntúa `null` al local.

    Reproduce el 2020/21: un jugador con puntos reales en los demás sistemas al
    que SofaScore aún no cubría. La ingesta debe conservar su fila con la columna
    de SofaScore nula, no romper la jornada entera.
    """

    def get(self, url, params=None) -> bytes:
        payload = super().get(url, params=params)
        if "/rounds/" in url and int(params["score"]) == 2:
            data = json.loads(payload)
            data["data"]["games"][0]["home"]["reports"][0]["points"] = None
            return json.dumps(data).encode()
        return payload


def test_ingest_rounds_keeps_player_with_null_points_in_one_system(storage: Storage) -> None:
    transport = NullSofascoreRoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484,)
    )
    ingest_rounds(storage, "la-liga", "2025", transport=transport)

    points = storage.curated.read_table("fantasy_round_points")
    home = points[(points["player_id"] == 1) & (points["round_id"] == 4484)].iloc[0]
    assert pd.isna(home["points_sofascore"])  # el sistema que llegó null
    assert home["points_as"] == 1  # los demás, intactos
    assert home["points_stats"] == 3


def test_ingest_rounds_includes_departed_player(storage: Storage) -> None:
    """El jugador que ya no está en la plantilla actual sí aparece en la jornada."""
    transport = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484,)
    )
    ingest_rounds(storage, "la-liga", "2025", transport=transport)
    points = storage.curated.read_table("fantasy_round_points")
    assert DEPARTED_PLAYER_ID in set(points["player_id"])
    baja = points[points["player_id"] == DEPARTED_PLAYER_ID].iloc[0]
    assert baja["points_as"] == 2  # score * 2 con score=1
    assert baja["result"] == "loss"  # su equipo (visitante) perdió 2-0


def test_ingest_rounds_writes_history_identity(storage: Storage) -> None:
    """La jornada guarda la identidad de jugadores y equipos, incluida la baja."""
    transport = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484,)
    )
    ingest_rounds(storage, "la-liga", "2025", transport=transport)

    players = storage.curated.read_table("biwenger_players_history")
    assert set(players["id"]) == {1, DEPARTED_PLAYER_ID}
    baja = players[players["id"] == DEPARTED_PLAYER_ID].iloc[0]
    assert baja["name"] == "baja" and baja["slug"] == "baja"
    assert baja["position"] == 3 and baja["team_id"] == 2  # club de la jornada

    teams = storage.curated.read_table("biwenger_teams_history")
    assert set(teams["id"]) == {1, 2}
    assert set(teams["slug"]) == {"home", "away"}
    # Particionadas por temporada, como transfermarkt_players.
    assert set(players["season"]) == {"2025"} and set(teams["season"]) == {"2025"}


def test_ingest_rounds_history_is_idempotent(storage: Storage) -> None:
    def run() -> None:
        ingest_rounds(
            storage,
            "la-liga",
            "2025",
            transport=RoundsTransport(
                _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
            ),
        )

    run()
    run()
    players = storage.curated.read_table("biwenger_players_history")
    assert len(players) == players["id"].nunique()  # sin duplicar por reejecución


def test_ingest_rounds_writes_history_before_points(storage: Storage) -> None:
    """Si el run muere tras la identidad y antes de los puntos, ``resume`` rehace.

    El orden identidad→puntos garantiza que un corte nunca deja puntos sin la
    identidad que el matcher necesita: la jornada sin fila en fantasy_round_points
    se reprocesa entera al reanudar.
    """
    writes: list[str] = []
    real_upsert = storage.curated.upsert_table

    def spy(table, df, **kwargs):
        writes.append(table)
        if table == "fantasy_round_points":
            raise RuntimeError("corte tras escribir la identidad")
        return real_upsert(table, df, **kwargs)

    storage.curated.upsert_table = spy  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="corte"):
        ingest_rounds(
            storage,
            "la-liga",
            "2025",
            transport=RoundsTransport(
                _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484,)
            ),
        )
    # La identidad se intentó escribir antes que los puntos.
    assert writes.index("biwenger_players_history") < writes.index("fantasy_round_points")
    assert "biwenger_players_history" in writes and "biwenger_teams_history" in writes


def test_ingest_rounds_logs_request_count(storage: Storage, caplog) -> None:
    transport = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
    )
    import logging

    with caplog.at_level(logging.INFO):
        ingest_rounds(storage, "la-liga", "2025", transport=transport)
    # 1 semilla + 2 jornadas × 5 sistemas = 11 peticiones a rounds.
    assert "11 peticiones" in caplog.text


def test_ingest_rounds_is_idempotent(storage: Storage) -> None:
    def run() -> None:
        ingest_rounds(
            storage,
            "la-liga",
            "2025",
            transport=RoundsTransport(
                _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
            ),
        )

    run()
    run()
    assert len(storage.curated.read_table("fantasy_round_points")) == 4


def test_ingest_rounds_resume_skips_curated_rounds(storage: Storage) -> None:
    ingest_rounds(
        storage,
        "la-liga",
        "2025",
        transport=RoundsTransport(
            _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
        ),
    )
    # Segundo run reanudable: las dos jornadas ya están, no se piden sus sistemas.
    resumed = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484, 4485)
    )
    result = ingest_rounds(storage, "la-liga", "2025", transport=resumed, resume=True)
    assert result.rows == {"fantasy_round_points": 0}
    # Solo la semilla (4484, score 1); ningún sistema de una jornada saltada.
    assert resumed.round_fetches == [(4484, 1)]
    assert len(storage.curated.read_table("fantasy_round_points")) == 4


def test_ingest_rounds_explicit_ids_skip_discovery(storage: Storage) -> None:
    transport = RoundsTransport(
        _competition_payload("alex-fores"), PLAYER_LA_LIGA.read_bytes(), (4484,)
    )
    ingest_rounds(storage, "la-liga", "2025", transport=transport, round_ids=[4485])
    # Sin descubrimiento: ni /data para semilla ni jornada semilla; solo 4485×5.
    assert {rid for rid, _ in transport.round_fetches} == {4485}
    assert len(transport.round_fetches) == 5


def test_ingest_rounds_raises_when_no_veteran(storage: Storage) -> None:
    class NoVeteran:
        def get(self, url, params=None) -> bytes:
            if "/players/" in url:
                raise SourceHTTPError(url, 404)  # toda la plantilla da 404
            return _competition_payload("baja-1", "baja-2")

    with pytest.raises(RoundDiscoveryError, match="descubrir las jornadas"):
        ingest_rounds(storage, "la-liga", "2025", transport=NoVeteran())


def test_cli_ingest_biwenger_rounds_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    def fake_get(self, url, params=None) -> bytes:
        if "/rounds/" in url:
            round_id = int(url.rsplit("/", 1)[1])
            return _round_payload(round_id, int(params["score"]), catalogue_ids=(4484,))
        if "/players/" in url:
            return PLAYER_LA_LIGA.read_bytes()
        return _competition_payload("alex-fores")

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    exit_code = main(
        [
            "ingest",
            "biwenger-rounds",
            "--competition",
            "la-liga",
            "--season",
            "2025",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 0
    assert "fantasy_round_points: 2 filas" in capsys.readouterr().out
    parquet = (
        tmp_path
        / "curated"
        / "fantasy_round_points"
        / "competition=la-liga"
        / "season=2025"
        / "data.parquet"
    )
    assert parquet.exists()
    assert raw_files(tmp_path)


# --- Refresh por deltas post-jornada (#52) -----------------------------------

_DELTA_STATS = {"minutesPlayed": 90, "sofascore": 7.0, "homeScore": 1, "awayScore": 0, "win": True}


def _detail_scoring_round(player_id: int, slug: str, round_id: int, match_id: int) -> bytes:
    """Detalle de un jugador con un report puntuado en ``round_id``."""
    return json.dumps(
        {
            "status": 200,
            "data": {
                "id": player_id,
                "name": slug,
                "slug": slug,
                "birthday": 20010412,
                "reports": [
                    {
                        "home": True,
                        "match": {
                            "id": match_id,
                            "round": {"id": round_id, "name": f"J{round_id}"},
                        },
                        "points": {"1": 5, "2": 4, "3": 3, "5": 9, "6": 0},
                        "rawStats": _DELTA_STATS,
                    }
                ],
                "prices": [[250721, 230000]],
            },
        }
    ).encode()


class DeltaTransport:
    """Enruta /data (con catálogo), /rounds/{id} (quién puntuó) y /players/{slug}."""

    def __init__(
        self, competition: bytes, details: dict[str, bytes], round_payload: bytes | None = None
    ) -> None:
        self.competition = competition
        self.details = details
        self.round_payload = round_payload
        self.detail_slugs: list[str] = []
        self.round_fetches: list[int] = []

    def get(self, url, params=None) -> bytes:
        if "/rounds/" in url:
            self.round_fetches.append(int(url.rsplit("/", 1)[1]))
            return self.round_payload
        if "/players/" in url:
            slug = url.rsplit("/", 1)[1]
            self.detail_slugs.append(slug)
            return self.details[slug]
        return self.competition


def test_delta_refreshes_only_scorers(storage: Storage) -> None:
    # Plantilla: id 1 "scorer" (puntuó J4484) e id 2 "bench" (no puntuó).
    # El catálogo trae 4484 terminada y 4485 pendiente (esta se ignora).
    comp = _competition_payload("scorer", "bench", rounds=[(4484, "finished"), (4485, "pending")])
    # La jornada 4484 lista al id 1 (en plantilla) y al id 99999 (baja, fuera).
    round_payload = _round_payload(4484, 1, catalogue_ids=(4484,))
    transport = DeltaTransport(
        comp, {"scorer": _detail_scoring_round(1, "scorer", 4484, 46124)}, round_payload
    )
    result = ingest_reports_delta(storage, "la-liga", "2026", transport=transport)

    # Solo se pidió el detalle del que puntuó y está en plantilla; nunca "bench".
    assert transport.detail_slugs == ["scorer"]
    assert transport.round_fetches == [4484]  # solo la jornada terminada, no la pendiente
    assert result.stats == {"jugadores refrescados": 1, "jugadores saltados": 1}
    points = storage.curated.read_table("fantasy_points")
    assert set(points["round_id"].astype(int)) == {4484}


def test_delta_no_new_round_fetches_no_details(storage: Storage) -> None:
    comp = _competition_payload("scorer", "bench", rounds=[(4484, "finished")])
    round_payload = _round_payload(4484, 1, catalogue_ids=(4484,))
    details = {"scorer": _detail_scoring_round(1, "scorer", 4484, 46124)}
    # Primer run procesa la jornada 4484.
    ingest_reports_delta(
        storage, "la-liga", "2026", transport=DeltaTransport(comp, details, round_payload)
    )
    # Segundo run: 4484 ya está en fantasy_points -> nada nuevo, cero peticiones.
    second = DeltaTransport(comp, details, round_payload)
    result = ingest_reports_delta(storage, "la-liga", "2026", transport=second)

    assert second.detail_slugs == []
    assert second.round_fetches == []
    assert result.stats == {"jugadores refrescados": 0, "jugadores saltados": 2}


def test_delta_round_rows_match_full_traversal(storage: Storage, tmp_path: Path) -> None:
    """Las filas de la jornada quedan idénticas a las del recorrido completo."""
    comp = _competition_payload("scorer", "bench", rounds=[(4484, "finished")])
    scorer = _detail_scoring_round(1, "scorer", 4484, 46124)
    bench = _detail_scoring_round(2, "bench", 4485, 46200)  # el banquillo puntuó otra jornada

    # Recorrido completo: baja el detalle de los dos jugadores.
    full = DeltaTransport(comp, {"scorer": scorer, "bench": bench})
    ingest_reports(storage, "la-liga", "2026", transport=full)
    assert set(full.detail_slugs) == {"scorer", "bench"}
    full_points = storage.curated.read_table("fantasy_points")

    # Delta en almacenamiento aparte: solo baja al que puntuó la jornada 4484.
    delta_storage = Storage(f"file://{tmp_path}/delta")
    round_payload = _round_payload(4484, 1, catalogue_ids=(4484,))
    ingest_reports_delta(
        delta_storage,
        "la-liga",
        "2026",
        transport=DeltaTransport(comp, {"scorer": scorer}, round_payload),
    )
    delta_points = delta_storage.curated.read_table("fantasy_points")

    def round_slice(df):
        return (
            df[df["round_id"].astype("Int64") == 4484]
            .sort_values("player_id")
            .reset_index(drop=True)
        )

    pd.testing.assert_frame_equal(round_slice(full_points), round_slice(delta_points))


def test_delta_is_idempotent(storage: Storage) -> None:
    comp = _competition_payload("scorer", "bench", rounds=[(4484, "finished")])
    details = {"scorer": _detail_scoring_round(1, "scorer", 4484, 46124)}
    round_payload = _round_payload(4484, 1, catalogue_ids=(4484,))

    def run() -> None:
        ingest_reports_delta(
            storage, "la-liga", "2026", transport=DeltaTransport(comp, details, round_payload)
        )

    run()
    run()
    assert len(storage.curated.read_table("fantasy_points")) == 1


def test_cli_biwenger_delta_reports_refreshed_and_skipped(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    comp = _competition_payload("scorer", "bench", rounds=[(4484, "finished")])
    round_payload = _round_payload(4484, 1, catalogue_ids=(4484,))
    scorer_detail = _detail_scoring_round(1, "scorer", 4484, 46124)

    def fake_get(self, url, params=None) -> bytes:
        if "/rounds/" in url:
            return round_payload
        if "/players/" in url:
            return scorer_detail
        return comp

    monkeypatch.setattr("lfdata.sources.http.HttpTransport.get", fake_get)
    exit_code = main(
        [
            "ingest",
            "biwenger",
            "--competition",
            "la-liga",
            "--season",
            "2026",
            "--delta",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "jugadores refrescados: 1" in out
    assert "jugadores saltados: 1" in out


def test_cli_biwenger_delta_requires_season(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "ingest",
            "biwenger",
            "--competition",
            "la-liga",
            "--delta",
            "--data",
            f"file://{tmp_path}",
        ]
    )
    assert exit_code == 2
    assert "--delta requiere --season" in capsys.readouterr().out
