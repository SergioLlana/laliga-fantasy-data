"""Tests de la ingesta de copas/UEFA de SofaScore (issue #68), contra fixtures sin red.

El fixture ``cup-events-champions.json`` tiene dos partidos de Champions 25/26: uno del
Celta (equipo de La Liga, id 2821, presente en el catálogo) contra un extranjero, y otro
entre dos extranjeros. Las alineaciones reusan ``lineups.json`` (46 jugadores con
estadística). El calendario de liga reusa ``events-8-77559-last-0.json`` (Celta y Betis).
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from lfdata.sources.sofascore import backfill_cups_for_year, rebuild_cups
from lfdata.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures" / "sofascore"
CELTA = 2821  # equipo de La Liga presente en el catálogo y en las copas
LEAGUE_TEAMS = [(2821, "Celta Vigo"), (2849, "Levante UD"), (2816, "Real Betis"), (2846, "Elche")]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def storage_at(tmp_path: Path) -> Storage:
    return Storage(f"file://{tmp_path / 'data'}")


class RoutingTransport:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def get(self, url: str, params=None) -> bytes:
        self.urls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"URL sin fixture en el test: {url}")


def cup_routes() -> dict[str, bytes]:
    return {
        "unique-tournament/7/seasons": fixture("cup-tournament-seasons-7.json"),
        "season/77000/events/last/0": fixture("cup-events-champions.json"),
        "/lineups": fixture("lineups.json"),
    }


def _seed_la_liga_catalog(storage: Storage, season: str = "2025") -> None:
    """Publica sofascore_teams (la-liga) para que las copas sepan quiénes son 'los nuestros'."""
    df = pd.DataFrame(LEAGUE_TEAMS, columns=["team_id", "team_name"])
    storage.curated.write_table(
        "sofascore_teams", df, partition={"competition": "la-liga", "season": season}
    )


def _seed_raw_for_rebuild(storage: Storage) -> None:
    """Siembra el raw que lee la re-cura: calendario de liga + copa + alineación de copa."""
    storage.raw.save(
        "sofascore", "tournament-events", "8-77559-last-0", fixture("events-8-77559-last-0.json")
    )
    storage.raw.save(
        "sofascore", "cup-events", "7-77000-last-0", fixture("cup-events-champions.json")
    )
    storage.raw.save("sofascore", "cup-lineups", "14090001", fixture("lineups.json"))


# --- descarga (backfill) -----------------------------------------------------


def test_backfill_only_downloads_matches_with_a_la_liga_team(tmp_path):
    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    transport = RoutingTransport(cup_routes())

    result = backfill_cups_for_year(storage, "champions-league", 2025, transport=transport)

    # Solo el partido del Celta se baja; el cruce extranjero-extranjero ni se pide.
    assert result.stats["partidos"] == 1
    assert result.stats["partidos_sin_la_liga"] == 1
    lineup_calls = [u for u in transport.urls if "/lineups" in u]
    assert len(lineup_calls) == 1
    assert "14090001" in lineup_calls[0]
    # La alineación quedó en el dataset raw propio de copas, no en event-lineups.
    assert storage.raw.last_download_date("sofascore", "cup-lineups", "14090001") is not None
    assert storage.raw.last_download_date("sofascore", "event-lineups", "14090001") is None


def test_backfill_is_resumable_by_raw_presence(tmp_path):
    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    backfill_cups_for_year(
        storage, "champions-league", 2025, transport=RoutingTransport(cup_routes())
    )

    transport = RoutingTransport(cup_routes())
    again = backfill_cups_for_year(storage, "champions-league", 2025, transport=transport)
    assert again.stats["partidos"] == 0
    assert again.stats["partidos_saltados"] == 1
    assert not [u for u in transport.urls if "/lineups" in u]  # no se re-descarga


# --- re-cura a fixtures + cup_minutes ---------------------------------------


def test_rebuild_builds_fixtures_and_cup_minutes(tmp_path):
    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    _seed_raw_for_rebuild(storage)

    result = rebuild_cups(storage, mappings_dir=str(tmp_path / "mappings"))

    # fixtures: dos partidos de liga (Celta, Betis) + uno de Champions.
    fixtures = storage.curated.read_table("fixtures")
    assert len(fixtures) == 3
    assert set(fixtures["competition"].unique()) == {"la-liga", "champions-league"}
    assert result.rows["fixtures"] == 3

    # cup_minutes: solo el lado del Celta (23 jugadores); el rival extranjero, no.
    minutes = storage.curated.read_table("cup_minutes")
    assert len(minutes) == 23
    assert (minutes["team_id"] == CELTA).all()
    assert set(minutes["competition"].unique()) == {"champions-league"}
    assert minutes["minutes"].notna().any()
    assert set(minutes["is_starting"].unique()) <= {True, False}
    # Sin mappings, el canónico va vacío (se resuelve luego con `lfdata map`).
    assert (minutes["canonical_id"] == "").all()


def test_cup_minutes_gets_canonical_id_from_mappings(tmp_path):
    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    _seed_raw_for_rebuild(storage)

    # Aprueba el mapping de uno de los jugadores del lado del Celta en la alineación.
    import json

    lineups = json.loads(fixture("lineups.json"))
    player_id = next(p["player"]["id"] for p in lineups["home"]["players"] if p.get("statistics"))
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_id": "p00001",
                "fuente": "sofascore",
                "id_en_fuente": str(player_id),
                "metodo": "manual",
                "fecha": "2026-07-17",
            }
        ]
    ).to_csv(mappings / "players.csv", index=False)

    rebuild_cups(storage, mappings_dir=str(mappings))

    minutes = storage.curated.read_table("cup_minutes")
    mapped = minutes[minutes["sofascore_player_id"] == player_id]
    assert not mapped.empty
    assert (mapped["canonical_id"] == "p00001").all()


def test_calendar_density_spans_league_and_cup_end_to_end(tmp_path):
    """Acceptance: la densidad de calendario de un equipo con Europa, de punta a punta."""
    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    _seed_raw_for_rebuild(storage)
    rebuild_cups(storage, mappings_dir=str(tmp_path / "mappings"))

    fixtures = storage.curated.read_table("fixtures")
    # Todos los partidos del Celta, de cualquier competición, en una sola tabla.
    celta = fixtures[(fixtures["home_team_id"] == CELTA) | (fixtures["away_team_id"] == CELTA)]
    dates = sorted(datetime.fromisoformat(d).date() for d in celta["date"])
    comps = set(celta["competition"])
    assert comps == {"la-liga", "champions-league"}  # liga y Europa entre semana

    # "Días desde el último partido de cualquier competición" antes del de liga.
    assert len(dates) == 2
    days_between = (dates[1] - dates[0]).days
    assert days_between == 7  # Champions el 2026-05-05, liga el 2026-05-12


# --- CLI ---------------------------------------------------------------------


def test_cli_curate_cups(tmp_path, capsys):
    from lfdata.cli import main

    storage = storage_at(tmp_path)
    _seed_la_liga_catalog(storage)
    _seed_raw_for_rebuild(storage)

    exit_code = main(["curate", "sofascore-cups", "--data", f"file://{tmp_path / 'data'}"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "fixtures: 3 filas" in out
    assert "cup_minutes: 23 filas" in out
