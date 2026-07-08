from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from lfdata.storage import Storage, backend_from_uri


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


def test_unsupported_uri_scheme_raises() -> None:
    with pytest.raises(ValueError, match="no soportada"):
        backend_from_uri("ftp://x")
    with pytest.raises(NotImplementedError, match="issue #5"):
        backend_from_uri("s3://bucket")
