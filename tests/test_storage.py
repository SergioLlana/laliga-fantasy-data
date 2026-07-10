from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from lfdata.storage import (
    CuratedStore,
    LocalBackend,
    RawStore,
    Storage,
    StorageBackend,
    backend_from_uri,
)


class InMemoryBackend:
    """Backend fake que implementa el protocolo sin tocar disco."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def write_bytes(self, key: str, payload: bytes) -> None:
        self._data[key] = payload

    def read_bytes(self, key: str) -> bytes:
        return self._data[key]

    def exists(self, key: str) -> bool:
        return key in self._data

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path}")


def test_raw_store_writes_bytes_at_dated_key(storage: Storage, tmp_path: Path) -> None:
    key = storage.raw.save(
        "biwenger",
        "competition-data",
        "la-liga",
        b'{"status": 200}',
        download_date=date(2026, 7, 7),
    )
    assert key == "raw/biwenger/competition-data/fecha_descarga=2026-07-07/la-liga.json"
    assert (tmp_path / key).read_bytes() == b'{"status": 200}'


def test_curated_store_roundtrip(storage: Storage) -> None:
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    storage.curated.write_table("players", df)
    pd.testing.assert_frame_equal(storage.curated.read_table("players"), df)


def test_curated_store_partitioned_write_is_readable(storage: Storage, tmp_path: Path) -> None:
    df = pd.DataFrame({"id": [1], "competition": ["la-liga"]})
    key = storage.curated.write_table("players", df, partition={"competition": "la-liga"})
    assert key == "curated/players/competition=la-liga/data.parquet"
    read = storage.curated.read_table("players")
    assert read["id"].tolist() == [1]
    assert read["competition"].astype(str).tolist() == ["la-liga"]
    assert pd.read_parquet(tmp_path / key)["id"].tolist() == [1]


def test_upsert_on_empty_partition_equals_write(storage: Storage) -> None:
    df = pd.DataFrame({"player_id": [1, 2], "value": [10, 20], "competition": ["la-liga"] * 2})
    storage.curated.upsert_table("t", df, partition={"competition": "la-liga"})
    read = storage.curated.read_table("t").sort_values("player_id").reset_index(drop=True)
    assert read["player_id"].tolist() == [1, 2]
    assert read["value"].tolist() == [10, 20]


def test_upsert_refreshes_keys_without_touching_the_rest(storage: Storage) -> None:
    partition = {"competition": "la-liga"}
    first = pd.DataFrame({"player_id": [1, 1, 2], "value": [10, 11, 20]})
    storage.curated.upsert_table("t", first, partition=partition)

    # Re-scrapear solo al jugador 1: su historia entera (dos filas -> una) se
    # reemplaza; el jugador 2 no se toca; el jugador 3 se añade.
    update = pd.DataFrame({"player_id": [1, 3], "value": [99, 30]})
    storage.curated.upsert_table("t", update, partition=partition)

    read = storage.curated.read_table("t").sort_values(["player_id", "value"])
    assert list(zip(read["player_id"], read["value"], strict=True)) == [
        (1, 99),
        (2, 20),
        (3, 30),
    ]


def test_upsert_is_idempotent(storage: Storage) -> None:
    partition = {"competition": "la-liga"}
    df = pd.DataFrame({"player_id": [1, 1, 2], "value": [10, 11, 20]})
    storage.curated.upsert_table("t", df, partition=partition)
    storage.curated.upsert_table("t", df, partition=partition)
    read = storage.curated.read_table("t").sort_values(["player_id", "value"])
    read = read.reset_index(drop=True)
    assert read["player_id"].tolist() == [1, 1, 2]
    assert read["value"].tolist() == [10, 11, 20]


def test_upsert_with_custom_key(storage: Storage) -> None:
    storage.curated.upsert_table("players", pd.DataFrame({"id": [1, 2], "n": ["a", "b"]}), key="id")
    storage.curated.upsert_table("players", pd.DataFrame({"id": [2], "n": ["B"]}), key="id")
    read = storage.curated.read_table("players").sort_values("id").reset_index(drop=True)
    assert read["n"].tolist() == ["a", "B"]


def test_last_download_date_returns_newest(storage: Storage) -> None:
    assert storage.raw.last_download_date("s", "d", "x") is None
    storage.raw.save("s", "d", "x", b"1", download_date=date(2026, 7, 1))
    storage.raw.save("s", "d", "x", b"2", download_date=date(2026, 7, 5))
    storage.raw.save("s", "d", "other", b"3", download_date=date(2026, 7, 9))
    assert storage.raw.last_download_date("s", "d", "x") == date(2026, 7, 5)


def test_unsupported_uri_scheme_raises() -> None:
    with pytest.raises(ValueError, match="no soportada"):
        backend_from_uri("ftp://x")
    with pytest.raises(NotImplementedError, match="issue #5"):
        backend_from_uri("s3://bucket")


def test_in_memory_backend_satisfies_protocol() -> None:
    assert isinstance(InMemoryBackend(), StorageBackend)


def test_stores_run_against_a_fake_backend_without_touching_disk() -> None:
    backend = InMemoryBackend()
    raw = RawStore(backend)
    curated = CuratedStore(backend)

    # RawStore: guardar y localizar la fecha de descarga más reciente.
    raw.save("s", "d", "x", b"1", download_date=date(2026, 7, 1))
    raw.save("s", "d", "x", b"2", download_date=date(2026, 7, 5))
    raw.save("s", "d", "other", b"3", download_date=date(2026, 7, 9))
    assert raw.last_download_date("s", "d", "x") == date(2026, 7, 5)
    assert raw.last_download_date("s", "d", "missing") is None

    # CuratedStore: upsert reanudable y lectura particionada reconstruida.
    partition = {"competition": "la-liga"}
    curated.upsert_table(
        "t", pd.DataFrame({"player_id": [1, 2], "value": [10, 20]}), partition=partition
    )
    curated.upsert_table(
        "t", pd.DataFrame({"player_id": [1, 3], "value": [99, 30]}), partition=partition
    )
    read = curated.read_table("t").sort_values("player_id").reset_index(drop=True)
    assert read["player_id"].tolist() == [1, 2, 3]
    assert read["value"].tolist() == [99, 20, 30]
    # La columna de partición se reconstruye desde la clave como str.
    assert read["competition"].tolist() == ["la-liga"] * 3
    assert pd.api.types.is_string_dtype(read["competition"])

    # retain_keys sobre el fake.
    removed = curated.retain_keys("t", keep={1, 3}, key="player_id", partition=partition)
    assert removed == 1
    assert sorted(curated.read_table("t")["player_id"]) == [1, 3]


def test_read_table_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        CuratedStore(InMemoryBackend()).read_table("nope")


def test_local_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    backend.write_bytes("curated/t/data.parquet", b"payload")
    target = tmp_path / "curated" / "t" / "data.parquet"
    assert target.read_bytes() == b"payload"
    # Ni temporales huérfanos ni ficheros ocultos en el directorio destino.
    siblings = list(target.parent.iterdir())
    assert siblings == [target]
