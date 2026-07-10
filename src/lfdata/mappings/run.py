"""Orquestación de ``lfdata map``: genera candidatos y aplica decisiones.

Anclamos la identidad en el universo de Biwenger (los jugadores y equipos que la
plataforma necesita) y buscamos su contraparte en Transfermarkt:

1. **Equipos primero** — cada club de Biwenger se mapea por nombre a un club de
   Transfermarkt; los jugadores se buscan luego dentro del club ya mapeado.
2. **Jugadores** — dentro del club canónico del jugador, un único candidato con
   nombre compatible se aprueba solo (``auto``); varios candidatos, o candidatos
   en otro club (posible cesión), o ninguno, van al fichero de revisión.

Antes de regenerar candidatos se aplican las ``decision`` que un humano haya
rellenado en los ficheros de revisión (``y`` = este candidato; ``skip`` = sin
contraparte en Transfermarkt, se le da ID canónico solo con Biwenger). El proceso
es idempotente: lo ya aprobado se conserva y no se vuelve a proponer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from lfdata.mappings.matcher import player_candidates, team_candidates
from lfdata.mappings.store import (
    BIWENGER,
    TRANSFERMARKT,
    MappingStore,
)
from lfdata.storage import Storage

_YES = frozenset({"y", "yes", "si", "sí", "x", "1", "true", "ok"})
_SKIP = frozenset({"skip", "none", "no-tm", "biwenger-only", "solo-biwenger"})


@dataclass
class MapReport:
    """Resumen de una ejecución de ``lfdata map`` para imprimir."""

    teams_total: int = 0
    teams_mapped: int = 0
    teams_review: int = 0
    players_total: int = 0
    players_auto: int = 0
    players_manual: int = 0
    players_review: int = 0

    @property
    def players_mapped(self) -> int:
        return self.players_auto + self.players_manual

    @property
    def players_pending(self) -> int:
        return self.players_total - self.players_mapped - self.players_review

    @property
    def auto_pct(self) -> float:
        return 100.0 * self.players_auto / self.players_total if self.players_total else 0.0

    def render(self) -> str:
        lines = [
            f"Equipos: {self.teams_mapped}/{self.teams_total} mapeados, "
            f"{self.teams_review} en revisión",
            f"Jugadores: {self.players_mapped}/{self.players_total} mapeados "
            f"({self.auto_pct:.0f}% automático) — "
            f"{self.players_auto} auto, {self.players_manual} manual, "
            f"{self.players_review} en revisión, {self.players_pending} pendientes",
        ]
        return "\n".join(lines)


def _decision(value: str) -> str | None:
    token = str(value).strip().lower()
    if token in _YES:
        return "yes"
    if token in _SKIP:
        return "skip"
    return None


def _today() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def _read_curated(storage: Storage, table: str, columns: list[str]) -> pd.DataFrame:
    try:
        df = storage.curated.read_table(table)
    except (FileNotFoundError, OSError):
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns]


def run_map(storage: Storage, mappings_dir, *, today: str | None = None) -> MapReport:
    """Regenera candidatos y aplica decisiones; devuelve el resumen."""
    today = today or _today()
    biw_players = _read_curated(
        storage, "biwenger_players", ["id", "name", "team_id", "competition"]
    )
    biw_teams = _read_curated(storage, "biwenger_teams", ["id", "name", "competition"])
    tm_players = _read_curated(
        storage,
        "transfermarkt_players",
        ["id", "name", "club_id", "club_name", "birth_date", "position", "competition"],
    )

    store = MappingStore(mappings_dir)
    store.load()

    _map_teams(store, biw_teams, tm_players, today)
    _map_players(store, biw_players, tm_players, today)

    store.save()
    return _report(store, biw_teams, biw_players)


# --- equipos -----------------------------------------------------------------


def _tm_clubs(tm_players: pd.DataFrame) -> list[dict]:
    if tm_players.empty:
        return []
    clubs = tm_players.dropna(subset=["club_id"]).drop_duplicates(subset=["club_id"])
    return [
        {
            "club_id": str(int(row.club_id)),
            "club_name": str(row.club_name),
            "competition": str(row.competition),
        }
        for row in clubs.itertuples()
    ]


def _map_teams(
    store: MappingStore, biw_teams: pd.DataFrame, tm_players: pd.DataFrame, today: str
) -> None:
    clubs = _tm_clubs(tm_players)
    approved = store.approved_ids(store.teams, BIWENGER)
    taken = store.approved_ids(store.teams, TRANSFERMARKT)

    _apply_team_decisions(store, approved, taken, today)

    review_rows: list[dict] = []
    for team in biw_teams.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(team.id))
        if biw_id in approved:
            continue
        competition = str(team.competition)
        scope = [c for c in clubs if c["competition"] == competition and c["club_id"] not in taken]
        cands = team_candidates(str(team.name), scope)
        if len(cands) == 1:
            canonical = store.new_team_canonical()
            store.add_team(
                canonical,
                [(BIWENGER, biw_id), (TRANSFERMARKT, cands[0]["club_id"])],
                method="auto",
                date=today,
            )
            approved.add(biw_id)
            taken.add(cands[0]["club_id"])
        else:
            review_rows += _team_review_rows(biw_id, team, competition, cands)

    store.teams_review = pd.DataFrame(review_rows, columns=store.teams_review.columns)


def _apply_team_decisions(
    store: MappingStore, approved: set[str], taken: set[str], today: str
) -> None:
    for biw_id, rows in store.teams_review.groupby("biwenger_id"):
        biw_id = str(biw_id)
        if biw_id in approved:
            continue
        decisions = [(row, _decision(row.decision)) for row in rows.itertuples()]
        chosen = [row for row, d in decisions if d == "yes"]
        skipped = any(d == "skip" for _, d in decisions)
        if len(chosen) == 1 and chosen[0].tm_club_id:
            tm_id = str(chosen[0].tm_club_id)
            store.add_team(
                store.new_team_canonical(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, tm_id)],
                method="manual",
                date=today,
            )
            approved.add(biw_id)
            taken.add(tm_id)
        elif skipped and not chosen:
            store.add_team(
                store.new_team_canonical(), [(BIWENGER, biw_id)], method="manual", date=today
            )
            approved.add(biw_id)


def _team_review_rows(biw_id: str, team, competition: str, cands: list[dict]) -> list[dict]:
    if not cands:
        return [
            {
                "biwenger_id": biw_id,
                "biwenger_name": str(team.name),
                "competition": competition,
                "tm_club_id": "",
                "tm_club_name": "",
                "motivo": "sin-candidato",
                "decision": "",
            }
        ]
    return [
        {
            "biwenger_id": biw_id,
            "biwenger_name": str(team.name),
            "competition": competition,
            "tm_club_id": club["club_id"],
            "tm_club_name": club["club_name"],
            "motivo": "varios-candidatos",
            "decision": "",
        }
        for club in cands
    ]


# --- jugadores ---------------------------------------------------------------


def _map_players(
    store: MappingStore, biw_players: pd.DataFrame, tm_players: pd.DataFrame, today: str
) -> None:
    # Club de Transfermarkt -> equipo canónico -> jugadores de ese equipo.
    tm_club_to_canonical = store.canonical_by_source(store.teams, TRANSFERMARKT)
    biw_team_to_canonical = store.canonical_by_source(store.teams, BIWENGER)

    tm_by_canonical: dict[str, list[dict]] = defaultdict(list)
    tm_all: list[dict] = []
    for row in tm_players.dropna(subset=["id"]).itertuples():
        record = {
            "id": str(int(row.id)),
            "name": str(row.name),
            "club_name": "" if pd.isna(row.club_name) else str(row.club_name),
            "birth_date": "" if pd.isna(row.birth_date) else str(row.birth_date)[:10],
            "position": "" if pd.isna(row.position) else str(row.position),
        }
        tm_all.append(record)
        if not pd.isna(row.club_id):
            canonical = tm_club_to_canonical.get(str(int(row.club_id)))
            if canonical:
                tm_by_canonical[canonical].append(record)

    approved = store.approved_ids(store.players, BIWENGER)
    taken = store.approved_ids(store.players, TRANSFERMARKT)

    _apply_player_decisions(store, approved, taken, today)

    review_rows: list[dict] = []
    for player in biw_players.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(player.id))
        if biw_id in approved:
            continue
        canonical_team = (
            biw_team_to_canonical.get(str(int(player.team_id)))
            if not pd.isna(player.team_id)
            else None
        )
        in_club = [
            c
            for c in (tm_by_canonical.get(canonical_team, []) if canonical_team else [])
            if c["id"] not in taken
        ]
        cands = player_candidates(str(player.name), in_club)
        if len(cands) == 1:
            store.add_player(
                store.new_player_canonical(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, cands[0]["id"])],
                method="auto",
                date=today,
            )
            approved.add(biw_id)
            taken.add(cands[0]["id"])
            continue
        review_rows += _player_review_rows(
            store, biw_id, player, canonical_team, cands, tm_all, taken
        )

    store.players_review = pd.DataFrame(review_rows, columns=store.players_review.columns)


def _apply_player_decisions(
    store: MappingStore, approved: set[str], taken: set[str], today: str
) -> None:
    for biw_id, rows in store.players_review.groupby("biwenger_id"):
        biw_id = str(biw_id)
        if biw_id in approved:
            continue
        decisions = [(row, _decision(row.decision)) for row in rows.itertuples()]
        chosen = [row for row, d in decisions if d == "yes"]
        skipped = any(d == "skip" for _, d in decisions)
        if len(chosen) == 1 and chosen[0].tm_id:
            tm_id = str(chosen[0].tm_id)
            store.add_player(
                store.new_player_canonical(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, tm_id)],
                method="manual",
                date=today,
            )
            approved.add(biw_id)
            taken.add(tm_id)
        elif skipped and not chosen:
            store.add_player(
                store.new_player_canonical(), [(BIWENGER, biw_id)], method="manual", date=today
            )
            approved.add(biw_id)


def _player_review_rows(
    store: MappingStore,
    biw_id: str,
    player,
    canonical_team: str | None,
    in_club: list[dict],
    tm_all: list[dict],
    taken: set[str],
) -> list[dict]:
    if len(in_club) > 1:
        return [_player_row(biw_id, player, c, "varios-en-club") for c in in_club]
    # Ningún candidato dentro del club: quizá esté cedido o el equipo no se mapeó.
    # Ofrecemos como evidencia los homónimos en cualquier club (regla del dudoso).
    motivo = "equipo-sin-mapear" if canonical_team is None else "fuera-de-club"
    cross = [c for c in player_candidates(str(player.name), tm_all) if c["id"] not in taken]
    if not cross:
        return [_player_row(biw_id, player, None, "sin-candidato")]
    return [_player_row(biw_id, player, c, motivo) for c in cross]


def _player_row(biw_id: str, player, cand: dict | None, motivo: str) -> dict:
    return {
        "biwenger_id": biw_id,
        "biwenger_name": str(player.name),
        "biwenger_team": "" if pd.isna(player.team_id) else str(int(player.team_id)),
        "tm_id": cand["id"] if cand else "",
        "tm_name": cand["name"] if cand else "",
        "tm_club": cand["club_name"] if cand else "",
        "tm_birth_date": cand["birth_date"] if cand else "",
        "tm_position": cand["position"] if cand else "",
        "motivo": motivo,
        "decision": "",
    }


# --- informe y verificación --------------------------------------------------


def _report(store: MappingStore, biw_teams: pd.DataFrame, biw_players: pd.DataFrame) -> MapReport:
    team_ids = {str(int(v)) for v in biw_teams["id"].dropna()}
    player_ids = {str(int(v)) for v in biw_players["id"].dropna()}
    approved_teams = store.approved_ids(store.teams, BIWENGER) & team_ids
    approved_players = store.players[store.players["fuente"] == BIWENGER]
    approved_players = approved_players[approved_players["id_en_fuente"].isin(player_ids)]

    return MapReport(
        teams_total=len(team_ids),
        teams_mapped=len(approved_teams),
        teams_review=store.teams_review["biwenger_id"].nunique(),
        players_total=len(player_ids),
        players_auto=int((approved_players["metodo"] == "auto").sum()),
        players_manual=int((approved_players["metodo"] == "manual").sum()),
        players_review=store.players_review["biwenger_id"].nunique(),
    )


def check_mappings(storage: Storage, mappings_dir) -> list[str]:
    """Devuelve los problemas de cobertura; lista vacía = todo mapeado.

    Falla (para CI y pipeline) si algún jugador o equipo de Biwenger presente en
    las tablas curadas no tiene un ID canónico aprobado. Sin datos curados (p. ej.
    en CI, donde ``data/`` está en .gitignore) no hay nada que verificar y pasa.
    """
    biw_players = _read_curated(storage, "biwenger_players", ["id", "name", "competition"])
    biw_teams = _read_curated(storage, "biwenger_teams", ["id", "name", "competition"])

    store = MappingStore(mappings_dir)
    store.load()

    problems: list[str] = []
    problems += _missing(biw_teams, store.approved_ids(store.teams, BIWENGER), "equipo")
    problems += _missing(biw_players, store.approved_ids(store.players, BIWENGER), "jugador")
    return problems


def _missing(df: pd.DataFrame, approved: set[str], kind: str) -> list[str]:
    problems = []
    for row in df.itertuples():
        if pd.isna(row.id):
            continue
        source_id = str(int(row.id))
        if source_id not in approved:
            competition = getattr(row, "competition", "")
            problems.append(f"{kind} sin mapping: {row.name} (biwenger {source_id}, {competition})")
    return problems
