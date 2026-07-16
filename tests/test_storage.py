from datetime import date
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from lfdata.storage import (
    CuratedStore,
    LocalBackend,
    RawStore,
    S3Backend,
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


def test_read_partition_returns_single_partition_or_empty(storage: Storage) -> None:
    storage.curated.write_table(
        "players", pd.DataFrame({"id": [1]}), partition={"competition": "la-liga"}
    )
    read = storage.curated.read_partition("players", partition={"competition": "la-liga"})
    assert read["id"].tolist() == [1]
    assert "competition" not in read.columns  # el fichero tal cual, sin columnas de partición
    missing = storage.curated.read_partition("players", partition={"competition": "premier"})
    assert missing.empty


def test_numeric_looking_season_partition_reads_back_as_str(storage: Storage) -> None:
    # `season` se escribe como str ("2026"); al reconstruir la partición desde la
    # clave se relee como str, sin que pyarrow la infiera como int.
    partition = {"competition": "la-liga", "season": "2026"}
    df = pd.DataFrame({"player_id": [1], "value": [10]})
    storage.curated.write_table("fantasy_points", df, partition=partition)

    read = storage.curated.read_table("fantasy_points")
    assert read["season"].tolist() == ["2026"]
    assert pd.api.types.is_string_dtype(read["season"])


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


class CountingBackend(InMemoryBackend):
    """InMemoryBackend que lleva la cuenta de las llamadas a ``list_keys``."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    def list_keys(self, prefix: str) -> list[str]:
        self.list_calls += 1
        return super().list_keys(prefix)


def test_read_latest_lists_the_dataset_once_per_run() -> None:
    backend = CountingBackend()
    raw = RawStore(backend)
    raw.save("s", "d", "a", b"1", download_date=date(2026, 7, 1))
    raw.save("s", "d", "b", b"2", download_date=date(2026, 7, 1))
    backend.list_calls = 0

    # Curar a varios jugadores del mismo dataset no vuelve a enumerar el prefijo.
    assert raw.read_latest("s", "d", "a") == b"1"
    assert raw.read_latest("s", "d", "b") == b"2"
    assert raw.last_download_date("s", "d", "a") == date(2026, 7, 1)
    assert backend.list_calls == 1


def test_save_keeps_the_cached_listing_in_sync() -> None:
    backend = CountingBackend()
    raw = RawStore(backend)
    raw.save("s", "d", "a", b"1", download_date=date(2026, 7, 1))
    assert raw.read_latest("s", "d", "a") == b"1"  # puebla el caché
    backend.list_calls = 0

    # Un fichero escrito después es visible sin re-listar el prefijo.
    raw.save("s", "d", "b", b"2", download_date=date(2026, 7, 2))
    assert raw.read_latest("s", "d", "b") == b"2"
    assert raw.last_download_date("s", "d", "b") == date(2026, 7, 2)
    assert backend.list_calls == 0


def test_listing_cache_is_scoped_per_dataset() -> None:
    backend = CountingBackend()
    raw = RawStore(backend)
    raw.save("s", "d1", "a", b"1", download_date=date(2026, 7, 1))
    raw.save("s", "d2", "a", b"2", download_date=date(2026, 7, 1))
    backend.list_calls = 0

    assert raw.read_latest("s", "d1", "a") == b"1"
    assert raw.read_latest("s", "d2", "a") == b"2"
    assert backend.list_calls == 2


def test_unsupported_uri_scheme_raises() -> None:
    with pytest.raises(ValueError, match="no soportada"):
        backend_from_uri("ftp://x")
    with pytest.raises(ValueError, match="sin bucket"):
        backend_from_uri("s3://")


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


# --- Backend S3 contra un doble local (moto): sin red ni cuenta AWS real ------

BUCKET = "lfdata-test-bucket"
REGION = "eu-south-2"


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch):
    """Un bucket S3 vacío en moto, con credenciales y región falsas.

    Las variables de entorno evitan que boto3 caiga en un perfil real, de modo
    que ``backend_from_uri("s3://...")`` pueda construir su propio cliente sin
    tocar AWS. moto intercepta toda llamada boto3 emitida dentro del ``with``.
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield


def test_s3_backend_satisfies_protocol() -> None:
    assert isinstance(S3Backend("b"), StorageBackend)


def test_backend_from_uri_builds_s3_backend() -> None:
    assert isinstance(backend_from_uri("s3://lfdata-data-593760774245"), S3Backend)


def test_s3_backend_write_read_and_exists(s3_bucket: None) -> None:
    backend = backend_from_uri(f"s3://{BUCKET}")
    assert backend.exists("raw/x.json") is False
    backend.write_bytes("raw/x.json", b"payload")
    assert backend.exists("raw/x.json") is True
    assert backend.read_bytes("raw/x.json") == b"payload"


def test_s3_backend_list_keys_is_prefix_scoped_and_sorted(s3_bucket: None) -> None:
    backend = backend_from_uri(f"s3://{BUCKET}")
    backend.write_bytes("raw/b.json", b"2")
    backend.write_bytes("raw/a.json", b"1")
    backend.write_bytes("curated/t.parquet", b"3")
    assert backend.list_keys("raw/") == ["raw/a.json", "raw/b.json"]


def test_s3_uri_prefix_is_transparent_to_the_store(s3_bucket: None) -> None:
    # `s3://bucket/sub/dir` coloca los objetos bajo `sub/dir/`, pero la store
    # sigue viendo claves lógicas (`raw/...`) sin enterarse del prefijo.
    backend = backend_from_uri(f"s3://{BUCKET}/sub/dir")
    backend.write_bytes("raw/x.json", b"p")
    assert backend.list_keys("raw/") == ["raw/x.json"]
    assert backend.read_bytes("raw/x.json") == b"p"
    real = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET)
    assert [obj["Key"] for obj in real["Contents"]] == ["sub/dir/raw/x.json"]


def test_stores_run_end_to_end_against_s3(s3_bucket: None) -> None:
    storage = Storage(f"s3://{BUCKET}")

    # RawStore: guardar y localizar la fecha de descarga más reciente.
    storage.raw.save("s", "d", "x", b"1", download_date=date(2026, 7, 1))
    storage.raw.save("s", "d", "x", b"2", download_date=date(2026, 7, 5))
    assert storage.raw.last_download_date("s", "d", "x") == date(2026, 7, 5)
    assert storage.raw.last_download_date("s", "d", "missing") is None

    # CuratedStore: upsert particionado reanudable y lectura reconstruida.
    partition = {"competition": "la-liga"}
    storage.curated.upsert_table(
        "t", pd.DataFrame({"player_id": [1, 2], "value": [10, 20]}), partition=partition
    )
    storage.curated.upsert_table(
        "t", pd.DataFrame({"player_id": [1, 3], "value": [99, 30]}), partition=partition
    )
    read = storage.curated.read_table("t").sort_values("player_id").reset_index(drop=True)
    assert read["player_id"].tolist() == [1, 2, 3]
    assert read["value"].tolist() == [99, 20, 30]
    assert read["competition"].tolist() == ["la-liga"] * 3

    # retain_keys reescribe la partición conservando solo lo indicado.
    removed = storage.curated.retain_keys("t", keep={1, 3}, key="player_id", partition=partition)
    assert removed == 1
    assert sorted(storage.curated.read_table("t")["player_id"]) == [1, 3]


def test_local_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    backend.write_bytes("curated/t/data.parquet", b"payload")
    target = tmp_path / "curated" / "t" / "data.parquet"
    assert target.read_bytes() == b"payload"
    # Ni temporales huérfanos ni ficheros ocultos en el directorio destino.
    siblings = list(target.parent.iterdir())
    assert siblings == [target]
