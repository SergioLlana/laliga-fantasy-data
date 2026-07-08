"""Tests del cliente e ingesta de Biwenger contra fixtures reales, sin red."""

import json
from pathlib import Path

import pandas as pd
import pytest

from lfdata.cli import main
from lfdata.sources.biwenger import BiwengerClient, SourceFormatError, ingest_squad
from lfdata.storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "biwenger" / "competition-data-la-liga.json"


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, params=None) -> bytes:
        self.urls.append(url)
        return self.payload


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
