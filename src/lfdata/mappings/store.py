"""Lectura y escritura de los ficheros de mappings, versionados en git.

Cuatro ficheros bajo ``mappings/`` (nunca en S3: el trabajo manual de revisión
es código, se revisa en pull request):

- ``players.csv`` / ``teams.csv`` — mappings **aprobados**, formato largo: una
  fila por (fuente, id en la fuente), todas compartiendo el ``canonical_id`` de
  la misma identidad.
- ``players-review.csv`` / ``teams-review.csv`` — **candidatos dudosos** con sus
  evidencias y una columna ``decision`` vacía que un humano rellena a mano.

El ``canonical_id`` es propio (``p00001`` / ``t001``), no el de ninguna fuente
(ADR 0001): se asigna al aprobar y se preserva entre ejecuciones.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

APPROVED_COLUMNS = ["canonical_id", "fuente", "id_en_fuente", "metodo", "fecha"]

PLAYER_REVIEW_COLUMNS = [
    "biwenger_id",
    "biwenger_name",
    "biwenger_team",
    "biwenger_birth_date",
    "tm_id",
    "tm_name",
    "tm_club",
    "tm_birth_date",
    "tm_position",
    "motivo",
    "decision",
]

TEAM_REVIEW_COLUMNS = [
    "biwenger_id",
    "biwenger_name",
    "competition",
    "tm_club_id",
    "tm_club_name",
    "motivo",
    "decision",
]

# Revisión de SofaScore: como la de Transfermarkt pero con la evidencia de ambos
# lados (nombre, equipo y fecha de nacimiento de Biwenger y de SofaScore), porque
# SofaScore se cuelga del canónico que Biwenger ya tiene (no crea identidades).
SOFASCORE_PLAYER_REVIEW_COLUMNS = [
    "biwenger_id",
    "biwenger_name",
    "biwenger_team",
    "biwenger_birth_date",
    "sofascore_id",
    "sofascore_name",
    "sofascore_team",
    "sofascore_birth_date",
    "motivo",
    "decision",
]

SOFASCORE_TEAM_REVIEW_COLUMNS = [
    "biwenger_id",
    "biwenger_name",
    "competition",
    "sofascore_team_id",
    "sofascore_team_name",
    "motivo",
    "decision",
]

# Registro negativo de SofaScore (ADR 0011): "este canónico no tiene contraparte en
# SofaScore". A diferencia de Transfermarkt —donde el skip crea un canónico
# solo-Biwenger que ya persiste en players.csv—, aquí el canónico ya existe y no hay
# dónde anotar la ausencia, así que va a su propio fichero, keyed por canónico. Un
# solo fichero cubre jugadores y equipos: el prefijo del canonical_id (``p…``/``t…``)
# distingue el tipo.
SOFASCORE_SKIP_COLUMNS = ["canonical_id", "biwenger_name", "fecha"]

BIWENGER = "biwenger"
TRANSFERMARKT = "transfermarkt"
SOFASCORE = "sofascore"

_ID_SUFFIX = re.compile(r"(\d+)$")

# Formato reconocible del canonical_id propio (ADR 0001): prefijo + dígitos.
_PLAYER_CANONICAL = re.compile(r"^p\d+$")
_TEAM_CANONICAL = re.compile(r"^t\d+$")


class MappingIntegrityError(Exception):
    """Los ficheros de aprobados violan la relación del dominio (ADR 0001).

    Un **Jugador/Equipo canónico** tiene como máximo un **Mapping** por
    **Fuente**. Un CSV editado a mano que rompa esto (mismo id de fuente en dos
    identidades, dos ids de la misma fuente bajo un canónico, o un canonical_id
    con formato irreconocible) hace fallar el comando señalando las filas.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


def _integrity_problems(df: pd.DataFrame, fichero: str, pattern: re.Pattern[str]) -> list[str]:
    """Problemas de integridad de un fichero de aprobados; vacío = correcto."""
    problems: list[str] = []
    if df.empty:
        return problems

    # (fuente, id_en_fuente) único en todo el fichero: un id de fuente no puede
    # apuntar a dos identidades canónicas.
    dup_source = df[df.duplicated(subset=["fuente", "id_en_fuente"], keep=False)]
    for (fuente, source_id), group in dup_source.groupby(["fuente", "id_en_fuente"]):
        canonicals = ", ".join(sorted(set(group["canonical_id"])))
        problems.append(
            f"{fichero}: ({fuente}, {source_id}) aparece en varias identidades canónicas: "
            f"{canonicals}"
        )

    # Cada canonical_id, como máximo una fila por fuente.
    dup_canonical = df[df.duplicated(subset=["canonical_id", "fuente"], keep=False)]
    for (canonical_id, fuente), group in dup_canonical.groupby(["canonical_id", "fuente"]):
        source_ids = ", ".join(sorted(group["id_en_fuente"]))
        problems.append(f"{fichero}: {canonical_id} tiene varios ids en {fuente}: {source_ids}")

    # Formato del canonical_id reconocible.
    bad = sorted({str(c) for c in df["canonical_id"] if not pattern.match(str(c))})
    for canonical_id in bad:
        problems.append(f"{fichero}: canonical_id con formato no reconocible: {canonical_id!r}")

    return problems


def _sofascore_skip_problems(
    skips: pd.DataFrame, players: pd.DataFrame, teams: pd.DataFrame
) -> list[str]:
    """Problemas de integridad de ``sofascore-skips.csv``; vacío = correcto.

    Extiende la relación de ADR 0001 con el hecho negativo: un canónico tiene como
    máximo un mapping **o un skip** por fuente. Un fichero editado a mano que rompa
    esto hace fallar el comando señalando el canónico.
    """
    problems: list[str] = []
    if skips.empty:
        return problems

    dup = skips[skips.duplicated(subset=["canonical_id"], keep=False)]
    for canonical_id in sorted(set(dup["canonical_id"])):
        problems.append(f"sofascore-skips.csv: canonical_id repetido: {canonical_id}")

    for canonical_id in sorted(set(skips["canonical_id"])):
        cid = str(canonical_id)
        if not (_PLAYER_CANONICAL.match(cid) or _TEAM_CANONICAL.match(cid)):
            problems.append(
                f"sofascore-skips.csv: canonical_id con formato no reconocible: {cid!r}"
            )

    # Contradicción: un canónico no puede estar a la vez skipeado y mapeado a SofaScore.
    mapped = set(players.loc[players["fuente"] == SOFASCORE, "canonical_id"]) | set(
        teams.loc[teams["fuente"] == SOFASCORE, "canonical_id"]
    )
    for canonical_id in sorted(set(skips["canonical_id"]) & mapped):
        problems.append(
            f"sofascore-skips.csv: {canonical_id} tiene skip y a la vez un mapping de sofascore"
        )
    return problems


def _read_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    return df[columns]


class MappingStore:
    """Estado de los cuatro ficheros de mappings en memoria."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.players = pd.DataFrame(columns=APPROVED_COLUMNS)
        self.teams = pd.DataFrame(columns=APPROVED_COLUMNS)
        self.players_review = pd.DataFrame(columns=PLAYER_REVIEW_COLUMNS)
        self.teams_review = pd.DataFrame(columns=TEAM_REVIEW_COLUMNS)
        self.players_review_sofascore = pd.DataFrame(columns=SOFASCORE_PLAYER_REVIEW_COLUMNS)
        self.teams_review_sofascore = pd.DataFrame(columns=SOFASCORE_TEAM_REVIEW_COLUMNS)
        self.sofascore_skips = pd.DataFrame(columns=SOFASCORE_SKIP_COLUMNS)

    # --- IO ------------------------------------------------------------------

    def load(self) -> None:
        self.players = _read_csv(self.root / "players.csv", APPROVED_COLUMNS)
        self.teams = _read_csv(self.root / "teams.csv", APPROVED_COLUMNS)
        self.players_review = _read_csv(self.root / "players-review.csv", PLAYER_REVIEW_COLUMNS)
        self.teams_review = _read_csv(self.root / "teams-review.csv", TEAM_REVIEW_COLUMNS)
        self.players_review_sofascore = _read_csv(
            self.root / "sofascore-review.csv", SOFASCORE_PLAYER_REVIEW_COLUMNS
        )
        self.teams_review_sofascore = _read_csv(
            self.root / "sofascore-teams-review.csv", SOFASCORE_TEAM_REVIEW_COLUMNS
        )
        self.sofascore_skips = _read_csv(self.root / "sofascore-skips.csv", SOFASCORE_SKIP_COLUMNS)
        self.validate()

    def validate(self) -> None:
        """Falla si los aprobados violan la integridad del dominio (ADR 0001)."""
        problems = _integrity_problems(self.players, "players.csv", _PLAYER_CANONICAL)
        problems += _integrity_problems(self.teams, "teams.csv", _TEAM_CANONICAL)
        problems += _sofascore_skip_problems(self.sofascore_skips, self.players, self.teams)
        if problems:
            raise MappingIntegrityError(problems)

    def save(self) -> None:
        self.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        self._save(self.players.sort_values(["canonical_id", "fuente"]), "players.csv")
        self._save(self.teams.sort_values(["canonical_id", "fuente"]), "teams.csv")
        self._save(self.players_review.sort_values("biwenger_id"), "players-review.csv")
        self._save(self.teams_review.sort_values("biwenger_id"), "teams-review.csv")
        # Los de SofaScore solo se escriben si hay algo que revisar (o el fichero ya
        # existía): una pasada sin catálogo de SofaScore no debe crear ficheros vacíos.
        self._save_if_relevant(
            self.teams_review_sofascore.sort_values("biwenger_id"), "sofascore-teams-review.csv"
        )
        self._save_if_relevant(
            self.players_review_sofascore.sort_values("biwenger_id"), "sofascore-review.csv"
        )
        self._save_if_relevant(
            self.sofascore_skips.sort_values("canonical_id"), "sofascore-skips.csv"
        )

    def _save(self, df: pd.DataFrame, name: str) -> None:
        df.to_csv(self.root / name, index=False)

    def _save_if_relevant(self, df: pd.DataFrame, name: str) -> None:
        if not df.empty or (self.root / name).exists():
            self._save(df, name)

    # --- consultas de estado -------------------------------------------------

    @staticmethod
    def approved_ids(df: pd.DataFrame, fuente: str) -> set[str]:
        return set(df.loc[df["fuente"] == fuente, "id_en_fuente"])

    @staticmethod
    def canonical_by_source(df: pd.DataFrame, fuente: str) -> dict[str, str]:
        rows = df[df["fuente"] == fuente]
        return dict(zip(rows["id_en_fuente"], rows["canonical_id"], strict=True))

    @staticmethod
    def _next_number(df: pd.DataFrame) -> int:
        nums = [
            int(m.group(1)) for value in df["canonical_id"] if (m := _ID_SUFFIX.search(str(value)))
        ]
        return max(nums, default=0) + 1

    def new_player_canonical(self) -> str:
        return f"p{self._next_number(self.players):05d}"

    def new_team_canonical(self) -> str:
        return f"t{self._next_number(self.teams):03d}"

    # --- alta de aprobados ---------------------------------------------------

    def add_player(
        self, canonical_id: str, pairs: list[tuple[str, str]], *, method: str, date: str
    ) -> None:
        """Añade filas aprobadas ``(fuente, id_en_fuente)`` de un jugador canónico."""
        self.players = self._append(self.players, canonical_id, pairs, method, date)

    def add_team(
        self, canonical_id: str, pairs: list[tuple[str, str]], *, method: str, date: str
    ) -> None:
        self.teams = self._append(self.teams, canonical_id, pairs, method, date)

    def add_sofascore_skip(self, canonical_id: str, biwenger_name: str, date: str) -> None:
        """Registra que ``canonical_id`` no tiene contraparte en SofaScore (ADR 0011).

        Persiste el ``skip`` de la revisión de SofaScore para que ``map`` no vuelva a
        proponer al jugador/equipo entre ejecuciones. Idempotente: no duplica un
        canónico ya skipeado. Reabrir el skip = borrar su fila del fichero.
        """
        if canonical_id in set(self.sofascore_skips["canonical_id"]):
            return
        row = {"canonical_id": canonical_id, "biwenger_name": biwenger_name, "fecha": date}
        self.sofascore_skips = pd.concat(
            [self.sofascore_skips, pd.DataFrame([row], columns=SOFASCORE_SKIP_COLUMNS)],
            ignore_index=True,
        )

    @staticmethod
    def _append(
        df: pd.DataFrame, canonical_id: str, pairs: list[tuple[str, str]], method: str, date: str
    ) -> pd.DataFrame:
        rows = [
            {
                "canonical_id": canonical_id,
                "fuente": fuente,
                "id_en_fuente": str(source_id),
                "metodo": method,
                "fecha": date,
            }
            for fuente, source_id in pairs
        ]
        return pd.concat([df, pd.DataFrame(rows, columns=APPROVED_COLUMNS)], ignore_index=True)
