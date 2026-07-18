"""Orquestación de ``lfdata map``: genera candidatos y aplica decisiones.

Anclamos la identidad en el universo de Biwenger (los jugadores y equipos que la
plataforma necesita) y buscamos su contraparte en Transfermarkt:

1. **Equipos primero** — cada club de Biwenger se mapea por nombre a un club de
   Transfermarkt; los jugadores se buscan luego dentro del club ya mapeado.
2. **Jugadores** — el club es una *pista*, no un filtro: acota el pool para que un
   homónimo único baste, pero quien no esté en él se busca igual en todas las
   temporadas descargadas. La fecha de nacimiento es la que gradúa la confianza:
   dentro del club solo descarta (y rescata al que el apodo escondía), mientras
   que en el pool global tiene que confirmar la identidad para aprobar.

La asignación automática es **global, no greedy por orden de id** (issue #40): se
calculan todas las compatibilidades sobre un scope fijo y solo se auto-aprueban
los pares **biunívocos** (un lado de Biwenger compatible con exactamente un lado
de Transfermarkt que, a su vez, no es compatible con ningún otro de Biwenger).
Cuando dos entidades de Biwenger se disputan la misma contraparte, ninguna se
auto-aprueba: todas las implicadas van a revisión con motivo ``candidato-compartido``
para que el revisor vea el cuadro completo, en vez de que el orden decida.

Antes de regenerar candidatos se aplican las ``decision`` que un humano haya
rellenado en los ficheros de revisión (``y`` = este candidato; ``skip`` = sin
contraparte en Transfermarkt, se le da ID canónico solo con Biwenger). El proceso
es idempotente: lo ya aprobado se conserva y no se vuelve a proponer.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from lfdata.mappings.matcher import (
    birthdate_candidates,
    birthdate_compatible,
    birthdate_matches,
    player_candidates,
    team_candidates,
)
from lfdata.mappings.store import (
    BIWENGER,
    PLAYER_REVIEW_COLUMNS,
    SOFASCORE,
    SOFASCORE_PLAYER_REVIEW_COLUMNS,
    SOFASCORE_TEAM_REVIEW_COLUMNS,
    TEAM_REVIEW_COLUMNS,
    TRANSFERMARKT,
    MappingStore,
)
from lfdata.sources.transfermarkt import DEFAULT_SEASON
from lfdata.storage import Storage

_YES = frozenset({"y", "yes", "si", "sí", "x", "1", "true", "ok"})
_SKIP = frozenset({"skip", "none", "no-tm", "biwenger-only", "solo-biwenger"})

# Token de identidad de Transfermarkt: una URL de perfil (``.../profil/spieler/NNN``)
# o un id numérico pegados en ``decision`` valen como "mapea este Biwenger a ese
# tm_id" (ergonomía para los ``sin-candidato``, que no traen candidato en la fila).
# Solo se reconoce en la revisión de jugadores de Transfermarkt (``allow_id``);
# en equipos y SofaScore sigue siendo ``token-no-reconocido``.
_SPIELER_RE = re.compile(r"/spieler/(\d+)")


@dataclass
class UnappliedDecision:
    """Una ``decision`` escrita por un humano que no pudo aplicarse, y su motivo.

    Se preserva en el fichero de revisión (no se borra) y se reporta para que el
    humano la corrija en vez de reescribirla de cero.
    """

    kind: str  # "jugador" | "equipo"
    biwenger_id: str
    biwenger_name: str
    tm_id: str
    decision: str
    motivo: str

    def render(self) -> str:
        objetivo = self.tm_id or "—"
        return (
            f"  {self.kind} biwenger {self.biwenger_id} ({self.biwenger_name}): "
            f"decision={self.decision!r} tm={objetivo} — {self.motivo}"
        )


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
    # SofaScore (se cuelga del canónico de Biwenger, no crea identidades).
    sofascore_present: bool = False
    sofascore_teams_mapped: int = 0
    sofascore_teams_review: int = 0
    sofascore_players_mapped: int = 0
    sofascore_players_review: int = 0
    sofascore_skipped: int = 0
    sofascore_unresolved: int = 0
    unapplied: list[UnappliedDecision] = field(default_factory=list)

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
        if self.sofascore_present:
            revisar = f"{self.sofascore_teams_review + self.sofascore_players_review} en revisión"
            if self.sofascore_skipped:
                revisar += f", {self.sofascore_skipped} sin contraparte"
            lines.append(
                f"SofaScore: {self.sofascore_teams_mapped} equipos y "
                f"{self.sofascore_players_mapped} jugadores colgados del canónico "
                f"({revisar}) — "
                f"{self.sofascore_unresolved} IDs de SofaScore sin resolver"
            )
        if self.unapplied:
            lines.append("")
            lines.append(
                f"Decisiones no aplicadas ({len(self.unapplied)}) — "
                "se conservan en el fichero de revisión; corrígelas y re-ejecuta:"
            )
            lines += [u.render() for u in self.unapplied]
        return "\n".join(lines)


def _decision(value: str, *, allow_id: bool = False):
    """Interpreta una ``decision`` escrita a mano.

    Devuelve ``"yes"``, ``"skip"``, ``None`` (token no reconocido) o, cuando
    ``allow_id`` y el texto es una URL de perfil o un id numérico, la tupla
    ``("id", tm_id)`` —un mapping manual a ese ``tm_id``, aunque la fila no traiga
    candidato—.
    """
    token = str(value).strip()
    low = token.lower()
    if low in _YES:
        return "yes"
    if low in _SKIP:
        return "skip"
    if allow_id:
        tm_id = _spieler_id(token)
        if tm_id is not None:
            return ("id", tm_id)
    return None


def _spieler_id(token: str) -> str | None:
    """``tm_id`` de una URL de perfil o de un id numérico; ``None`` si no lo es."""
    match = _SPIELER_RE.search(token)
    if match:
        return match.group(1)
    if token.isdigit():
        return token
    return None


def _classify_group(marked: list[tuple], tm_id_attr: str, taken: set[str]):
    """Clasifica las decisiones marcadas de un ``biwenger_id``.

    Devuelve ``(accion, problemas)`` donde ``accion`` es ``("yes", tm_id)``,
    ``("skip", None)`` o ``None`` si no se puede aplicar; ``problemas`` es la
    lista de ``(fila, motivo)`` de las decisiones que no se aplican.

    Un token de id (``("id", tm_id)``) es un positivo como el ``y``, pero apunta a
    su propio ``tm_id`` en vez de al candidato de la fila; si se pega en una fila
    que **ya** trae candidato, es ambiguo y se rechaza con ``id-en-fila-con-candidato``.
    """
    unknown = [row for row, d in marked if d is None]
    if unknown:
        return None, [(row, "token-no-reconocido") for row, _ in marked]

    yes = [row for row, d in marked if d == "yes"]
    skip = [row for row, d in marked if d == "skip"]
    ids = [(row, d[1]) for row, d in marked if isinstance(d, tuple)]
    positives = len(yes) + len(ids)

    if positives and skip:
        return None, [(row, "y-con-skip") for row, _ in marked]
    if positives > 1:
        return None, [(row, "varios-y") for row in yes + [r for r, _ in ids]]
    if len(yes) == 1:
        tm_id = str(getattr(yes[0], tm_id_attr) or "")
        if not tm_id:
            return None, [(yes[0], "y-sin-candidato")]
        if tm_id in taken:
            return None, [(yes[0], "tm-id-ya-tomado")]
        return ("yes", tm_id), []
    if len(ids) == 1:
        row, tm_id = ids[0]
        if str(getattr(row, tm_id_attr) or ""):
            return None, [(row, "id-en-fila-con-candidato")]
        if tm_id in taken:
            return None, [(row, "tm-id-ya-tomado")]
        return ("yes", tm_id), []
    return ("skip", None), []


def _apply_decisions(
    review_df: pd.DataFrame,
    approved: set[str],
    taken: set[str],
    *,
    kind: str,
    tm_id_attr: str,
    add_fn,
    new_canonical_fn,
    today: str,
    allow_id: bool = False,
) -> list[UnappliedDecision]:
    """Promueve las decisiones válidas a aprobados; reporta las que no lo son.

    Las filas de un ``biwenger_id`` con ``decision`` no vacía se clasifican en
    conjunto: o promueven la identidad (un único ``y`` con candidato libre, un id
    de Transfermarkt pegado, o uno o varios ``skip``), o quedan sin aplicar con su
    motivo. ``allow_id`` habilita el token de id (solo jugadores de Transfermarkt).
    """
    unapplied: list[UnappliedDecision] = []
    for biw_id, rows in review_df.groupby("biwenger_id"):
        biw_id = str(biw_id)
        if biw_id in approved:
            continue
        marked = [
            (row, _decision(row.decision, allow_id=allow_id))
            for row in rows.itertuples()
            if str(row.decision).strip()
        ]
        if not marked:
            continue

        action, problems = _classify_group(marked, tm_id_attr, taken)
        unapplied += [
            UnappliedDecision(
                kind=kind,
                biwenger_id=biw_id,
                biwenger_name=str(row.biwenger_name),
                tm_id=str(getattr(row, tm_id_attr) or ""),
                decision=str(row.decision),
                motivo=motivo,
            )
            for row, motivo in problems
        ]
        if action is None:
            continue

        verb, tm_id = action
        if verb == "yes":
            add_fn(
                new_canonical_fn(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, tm_id)],
                method="manual",
                date=today,
            )
            approved.add(biw_id)
            taken.add(tm_id)
        else:  # skip: identidad canónica solo con Biwenger
            add_fn(new_canonical_fn(), [(BIWENGER, biw_id)], method="manual", date=today)
            approved.add(biw_id)
    return unapplied


def _preserve_decisions(
    new_rows: list[dict],
    old_review: pd.DataFrame,
    approved: set[str],
    columns: list[str],
    key_cols: tuple[str, ...],
) -> pd.DataFrame:
    """Regenera el fichero de revisión sin perder ``decision`` escritas a mano.

    Para cada ``biwenger_id`` no promovido a aprobado, conserva el valor original
    de ``decision``: lo reasigna a la fila regenerada equivalente (misma clave) y,
    si la fila ya no se regenera (p. ej. su candidato quedó tomado por otro),
    re-añade la fila antigua tal cual para que el trabajo manual no se borre.
    """
    new_df = pd.DataFrame(new_rows, columns=columns)
    if old_review.empty:
        return new_df

    kept = old_review[
        (old_review["decision"].astype(str).str.strip() != "")
        & (~old_review["biwenger_id"].astype(str).isin(approved))
    ]
    if kept.empty:
        return new_df

    old_by_key = {tuple(str(row[c]) for c in key_cols): row for _, row in kept.iterrows()}
    records = new_df.to_dict("records")
    seen = set()
    for record in records:
        key = tuple(str(record[c]) for c in key_cols)
        seen.add(key)
        if key in old_by_key:
            record["decision"] = old_by_key[key]["decision"]
    for key, row in old_by_key.items():
        if key not in seen:
            records.append({c: row[c] for c in columns})
    return pd.DataFrame(records, columns=columns)


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


def _with_history(current: pd.DataFrame, history: pd.DataFrame, season: int) -> pd.DataFrame:
    """Suma a la plantilla actual los jugadores/equipos que solo vio ``rounds``.

    ``rounds`` observa a quien ya dejó la competición; su identidad se guarda por
    temporada en las tablas ``*_history``. En una pasada de ``season`` se añaden
    los de esa temporada que no están ya en la plantilla actual —esta gana: trae
    fecha de nacimiento y es más fresca—, de modo que el matcher los vea con el
    club de aquel año como pista (el único desempate sin fecha, ADR 0005). El
    histórico de otras temporadas no entra: se mapea en su propia pasada.
    """
    if history.empty:
        return current
    extra = history[history["season"].astype(str) == str(season)]
    extra = extra[~extra["id"].isin(set(current["id"]))]
    return pd.concat([current, extra[current.columns]], ignore_index=True)


def run_map(
    storage: Storage,
    mappings_dir,
    *,
    season: int = DEFAULT_SEASON,
    today: str | None = None,
) -> MapReport:
    """Regenera candidatos y aplica decisiones; devuelve el resumen.

    ``transfermarkt_players`` está particionada por temporada porque la
    pertenencia a un club lo está, pero la **identidad no tiene temporada**. Por
    eso ``season`` decide de qué plantillas salen los clubes (la actual por
    defecto) y no a quién se puede mapear: la contraparte de un jugador se busca
    en todas las temporadas descargadas. Si solo se mirase la temporada pedida,
    quien ya no está en la plantilla actual —Biwenger conserva su ficha— no
    tendría contraparte posible, cuando sí la tiene en la temporada en que jugó.
    """
    today = today or _today()
    biw_players = _with_history(
        _read_curated(
            storage, "biwenger_players", ["id", "name", "team_id", "birth_date", "competition"]
        ),
        _read_curated(
            storage,
            "biwenger_players_history",
            ["id", "name", "team_id", "birth_date", "competition", "season"],
        ),
        season,
    )
    biw_teams = _with_history(
        _read_curated(storage, "biwenger_teams", ["id", "name", "competition"]),
        _read_curated(storage, "biwenger_teams_history", ["id", "name", "competition", "season"]),
        season,
    )
    tm_all = _read_curated(
        storage,
        "transfermarkt_players",
        ["id", "name", "club_id", "club_name", "birth_date", "position", "competition", "season"],
    )
    tm_season = tm_all[tm_all["season"].astype(str) == str(season)]

    so_teams_all = _read_curated(
        storage, "sofascore_teams", ["team_id", "team_name", "competition", "season"]
    )
    so_players_all = _read_curated(
        storage,
        "sofascore_players",
        [
            "sofascore_player_id",
            "name",
            "birth_date",
            "team_id",
            "team_name",
            "competition",
            "season",
        ],
    )
    so_teams_season = so_teams_all[so_teams_all["season"].astype(str) == str(season)]
    so_players_season = so_players_all[so_players_all["season"].astype(str) == str(season)]

    store = MappingStore(mappings_dir)
    store.load()

    unapplied = _map_teams(store, biw_teams, tm_season, today)
    unapplied += _map_players(store, biw_players, tm_season, tm_all, today)
    # SofaScore va después: se cuelga del canónico que Biwenger ya obtuvo de
    # Transfermarkt (equipos primero, luego jugadores dentro del club canónico).
    unapplied += _map_teams_sofascore(store, biw_teams, so_teams_season, today)
    unapplied += _map_players_sofascore(
        store, biw_players, so_players_season, so_players_all, today
    )

    store.save()
    report = _report(store, biw_teams, biw_players, so_players_all)
    report.unapplied = unapplied
    return report


# --- equipos -----------------------------------------------------------------


def _clubs(players: pd.DataFrame, *, id_col: str, name_col: str) -> list[dict]:
    """Clubes de una fuente por competición, deduplicados por su id.

    Neutral a la fuente: Transfermarkt pasa ``club_id``/``club_name`` y SofaScore
    ``team_id``/``team_name``; ambos alimentan el mismo grafo de equipos.
    """
    if players.empty:
        return []
    clubs = players.dropna(subset=[id_col]).drop_duplicates(subset=[id_col])
    return [
        {
            "club_id": str(int(getattr(row, id_col))),
            "club_name": str(getattr(row, name_col)),
            "competition": str(row.competition),
        }
        for row in clubs.itertuples()
    ]


def _map_teams(
    store: MappingStore, biw_teams: pd.DataFrame, tm_players: pd.DataFrame, today: str
) -> list[UnappliedDecision]:
    clubs = _clubs(tm_players, id_col="club_id", name_col="club_name")
    approved = store.approved_ids(store.teams, BIWENGER)
    taken = store.approved_ids(store.teams, TRANSFERMARKT)
    old_review = store.teams_review

    unapplied = _apply_decisions(
        old_review,
        approved,
        taken,
        kind="equipo",
        tm_id_attr="tm_club_id",
        add_fn=store.add_team,
        new_canonical_fn=store.new_team_canonical,
        today=today,
    )

    # Grafo bipartito Biwenger↔Transfermarkt por competición sobre un scope fijo
    # (clubs no tomados por aprobados/manuales). Calcular todo antes de aprobar
    # hace el resultado independiente del orden de los ids.
    pending: list[tuple[str, object, str, list[dict]]] = []
    club_suitors: dict[str, set[str]] = defaultdict(set)
    for team in biw_teams.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(team.id))
        if biw_id in approved:
            continue
        competition = str(team.competition)
        scope = [c for c in clubs if c["competition"] == competition and c["club_id"] not in taken]
        cands = team_candidates(str(team.name), scope)
        pending.append((biw_id, team, competition, cands))
        for club in cands:
            club_suitors[club["club_id"]].add(biw_id)

    review_rows: list[dict] = []
    for biw_id, team, competition, cands in pending:
        if len(cands) == 1 and len(club_suitors[cands[0]["club_id"]]) == 1:
            # Par biunívoco: un único candidato que a su vez no reclama nadie más.
            store.add_team(
                store.new_team_canonical(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, cands[0]["club_id"])],
                method="auto",
                date=today,
            )
            approved.add(biw_id)
        else:
            review_rows += _team_review_rows(biw_id, team, competition, cands, club_suitors)

    store.teams_review = _preserve_decisions(
        review_rows, old_review, approved, TEAM_REVIEW_COLUMNS, ("biwenger_id", "tm_club_id")
    )
    return unapplied


def _team_review_rows(
    biw_id: str, team, competition: str, cands: list[dict], club_suitors: dict[str, set[str]]
) -> list[dict]:
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
    # Si algún candidato lo reclama también otro equipo de Biwenger, el conflicto
    # es de reparto (candidato-compartido) y prima sobre la mera ambigüedad local.
    shared = any(len(club_suitors[club["club_id"]]) > 1 for club in cands)
    motivo = "candidato-compartido" if shared else "varios-candidatos"
    return [
        {
            "biwenger_id": biw_id,
            "biwenger_name": str(team.name),
            "competition": competition,
            "tm_club_id": club["club_id"],
            "tm_club_name": club["club_name"],
            "motivo": motivo,
            "decision": "",
        }
        for club in cands
    ]


# --- jugadores ---------------------------------------------------------------


def _source_record(row, *, id_col: str, club_col: str, position_col: str | None = None) -> dict:
    """Registro neutral de un jugador de una fuente para el matcher.

    Todas las fuentes se proyectan a la misma forma (``id``/``name``/``club_name``/
    ``birth_date``/``position``/``season``) para que ``_candidates``/``_resolve`` no
    distingan de dónde vienen. SofaScore no publica posición aquí (``position_col``
    None → cadena vacía).
    """
    position = getattr(row, position_col) if position_col else None
    return {
        "id": str(int(getattr(row, id_col))),
        "name": str(row.name),
        "club_name": "" if pd.isna(getattr(row, club_col)) else str(getattr(row, club_col)),
        "birth_date": "" if pd.isna(row.birth_date) else str(row.birth_date)[:10],
        "position": "" if position is None or pd.isna(position) else str(position),
        "season": "" if pd.isna(row.season) else str(row.season),
    }


def _tm_record(row) -> dict:
    return _source_record(row, id_col="id", club_col="club_name", position_col="position")


def _global_pool(players: pd.DataFrame, *, id_col: str, record) -> list[dict]:
    """Un registro por jugador de la fuente, con su club más reciente.

    El mismo jugador aparece en una fila por temporada en la que estuvo en una
    plantilla; para el matching solo importa su identidad, así que nos quedamos
    con la fila de la temporada más alta (el club que mostramos al revisor es
    entonces el último que se le conoce).
    """
    rows = players.dropna(subset=[id_col])
    if rows.empty:
        return []
    latest = rows.sort_values("season").drop_duplicates(subset=[id_col], keep="last")
    return [record(row) for row in latest.itertuples()]


def _candidates(
    name: str, birth_date: str, in_club: list[dict], pool: list[dict]
) -> tuple[list[dict], str]:
    """Candidatos de Transfermarkt para un jugador de Biwenger, y de dónde salen.

    Tres niveles de evidencia, de más a menos concluyente:

    - ``club`` — homónimo dentro de su club ya mapeado (el caso normal).
    - ``club-fecha`` — nadie con nombre compatible en el club, pero sí alguien
      nacido el mismo día: el apodo de Biwenger no comparte tokens con el nombre
      de Transfermarkt (``Ez Abde`` / ``Abde Ezzalzouli``) y la fecha lo delata.
    - ``global`` — sin club (Biwenger conserva la ficha de quien ya no juega en la
      liga) o sin nadie compatible en él: se busca en todas las temporadas.
    """
    by_name = player_candidates(name, in_club)
    if by_name:
        return by_name, "club"
    by_birth = birthdate_candidates(birth_date, in_club)
    if by_birth:
        return by_birth, "club-fecha"
    return player_candidates(name, pool), "global"


@dataclass
class _Proposal:
    """Un jugador de Biwenger y sus candidatos de la fuente, antes de resolver."""

    left_id: str
    birth_date: str
    cands: list[dict]
    scope: str

    @property
    def confirmed(self) -> list[dict]:
        """Candidatos que la fecha de nacimiento confirma (derivado de ``cands``)."""
        return [c for c in self.cands if birthdate_matches(self.birth_date, c["birth_date"])]


def _reserved_by_birthdate(proposals: list[tuple[str, list[dict]]]) -> dict[str, str]:
    """Candidatos que la fecha de nacimiento adjudica a un único jugador de Biwenger.

    Quien tiene su identidad probada por la fecha se lleva a su candidato, y los
    demás lo pierden de su lista: si no, una ficha huérfana de nombre genérico
    —Biwenger conserva un ``Thomas`` y un ``Adrián`` sin equipo— reclama a todos
    sus homónimos y bloquea a quien está identificado sin ninguna duda. Lemar nació
    el día exacto de Thomas Lemar, pero ``Thomas`` le disputaba el candidato y
    ninguno de los dos se aprobaba. Un candidato confirmado por dos no lo adjudica
    nadie: esa disputa es real y va a revisión.

    ``proposals`` es una lista de ``(left_id, confirmed)`` para ser neutral a la
    fuente: lo comparten las pasadas de Transfermarkt y de SofaScore.
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for left_id, confirmed in proposals:
        if len(confirmed) == 1:
            claims[confirmed[0]["id"]].add(left_id)
    return {
        source_id: next(iter(owners)) for source_id, owners in claims.items() if len(owners) == 1
    }


def _resolve_graph(
    proposals: list[_Proposal],
) -> list[tuple[_Proposal, dict | None, list[dict], str]]:
    """Resuelve el grafo bipartito Biwenger↔fuente sobre un scope fijo (issue #40).

    Motor neutral a la fuente. Devuelve, por propuesta, una tupla
    ``(proposal, match, cands, motivo)``: si ``match`` no es ``None`` el par es
    biunívoco y se auto-aprueba con ese candidato; si es ``None`` va a revisión con
    ``cands`` (posiblemente vacío) y el ``motivo``. Calcula todas las
    compatibilidades antes de decidir, de modo que dos jugadores de Biwenger que se
    disputen el mismo candidato vayan ambos a revisión sin que el orden decida.
    """
    reserved = _reserved_by_birthdate([(p.left_id, p.confirmed) for p in proposals])
    trimmed: list[tuple[_Proposal, list[dict]]] = []
    suitors: dict[str, set[str]] = defaultdict(set)
    for p in proposals:
        if len(p.confirmed) == 1 and reserved.get(p.confirmed[0]["id"]) == p.left_id:
            cands = p.confirmed
        else:
            cands = [c for c in p.cands if reserved.get(c["id"], p.left_id) == p.left_id]
        trimmed.append((p, cands))
        for c in cands:
            suitors[c["id"]].add(p.left_id)

    resolved: list[tuple[_Proposal, dict | None, list[dict], str]] = []
    for p, cands in trimmed:
        if any(len(suitors[c["id"]]) > 1 for c in cands):
            # Algún candidato lo reclama también otro jugador de Biwenger: nadie se
            # auto-aprueba; todos a revisión con el cuadro completo.
            resolved.append((p, None, cands, "candidato-compartido"))
            continue
        match, motivo = _resolve(p.birth_date, cands, p.scope)
        if match is not None:
            resolved.append((p, match, [match], ""))
        else:
            resolved.append((p, None, cands, motivo))
    return resolved


def _map_players(
    store: MappingStore,
    biw_players: pd.DataFrame,
    tm_season: pd.DataFrame,
    tm_all: pd.DataFrame,
    today: str,
) -> list[UnappliedDecision]:
    # Club de Transfermarkt -> equipo canónico -> jugadores de ese equipo. El club
    # sale de la temporada pedida (la pertenencia a una plantilla es de un año);
    # la identidad, de todas (``pool``).
    tm_club_to_canonical = store.canonical_by_source(store.teams, TRANSFERMARKT)
    biw_team_to_canonical = store.canonical_by_source(store.teams, BIWENGER)

    tm_by_canonical: dict[str, list[dict]] = defaultdict(list)
    for row in tm_season.dropna(subset=["id"]).itertuples():
        if not pd.isna(row.club_id):
            canonical = tm_club_to_canonical.get(str(int(row.club_id)))
            if canonical:
                tm_by_canonical[canonical].append(_tm_record(row))
    pool = _global_pool(tm_all, id_col="id", record=_tm_record)

    approved = store.approved_ids(store.players, BIWENGER)
    taken = store.approved_ids(store.players, TRANSFERMARKT)
    old_review = store.players_review

    unapplied = _apply_decisions(
        old_review,
        approved,
        taken,
        kind="jugador",
        tm_id_attr="tm_id",
        add_fn=store.add_player,
        new_canonical_fn=store.new_player_canonical,
        today=today,
        allow_id=True,
    )

    # Grafo bipartito sobre un scope fijo (los no tomados), resuelto por el motor
    # neutral: se calcula todo antes de aprobar para que el resultado no dependa
    # del orden de los ids.
    free_pool = [c for c in pool if c["id"] not in taken]
    proposals: list[_Proposal] = []
    players_by_id: dict[str, object] = {}
    for player in biw_players.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(player.id))
        if biw_id in approved:
            continue
        canonical_team = (
            biw_team_to_canonical.get(str(int(player.team_id)))
            if not pd.isna(player.team_id)
            else None
        )
        biw_birth = "" if pd.isna(player.birth_date) else str(player.birth_date)[:10]
        in_club = [
            c
            for c in (tm_by_canonical.get(canonical_team, []) if canonical_team else [])
            if c["id"] not in taken
        ]
        cands, scope = _candidates(str(player.name), biw_birth, in_club, free_pool)
        proposals.append(_Proposal(biw_id, biw_birth, cands, scope))
        players_by_id[biw_id] = player

    review_rows: list[dict] = []
    for proposal, match, cands, motivo in _resolve_graph(proposals):
        biw_id, biw_birth = proposal.left_id, proposal.birth_date
        player = players_by_id[biw_id]
        if match is not None:
            store.add_player(
                store.new_player_canonical(),
                [(BIWENGER, biw_id), (TRANSFERMARKT, match["id"])],
                method="auto",
                date=today,
            )
            approved.add(biw_id)
            continue
        if not cands:
            review_rows.append(_player_row(biw_id, player, None, motivo, biw_birth))
            continue
        review_rows += [_player_row(biw_id, player, c, motivo, biw_birth) for c in cands]

    store.players_review = _preserve_decisions(
        review_rows, old_review, approved, PLAYER_REVIEW_COLUMNS, ("biwenger_id", "tm_id")
    )
    return unapplied


def _resolve(biw_birth: str, cands: list[dict], scope: str) -> tuple[dict | None, str]:
    """Decide si los candidatos identifican a una sola persona; si no, el motivo.

    Cuánta evidencia exigimos depende de dónde salieron los candidatos: dentro de
    un club ya mapeado el pool es de ~25 jugadores y un homónimo único basta
    (la fecha solo descarta), pero en el pool global son miles y un apellido
    suelto no identifica a nadie, así que ahí la fecha tiene que **confirmar**.
    """
    if not cands:
        return None, "sin-candidato"

    if scope == "club":
        if len(cands) > 1:
            return None, "varios-en-club"
        only = cands[0]
        if birthdate_compatible(biw_birth, only["birth_date"]):
            return only, ""
        # Homónimo único en el club pero con fecha discrepante: no se aprueba
        # solo; va a revisión con ambas fechas como evidencia del desempate.
        return None, "fecha-discrepante"

    if scope == "club-fecha":
        # Nacidos el mismo día dentro del mismo club: si es uno, es él.
        return (cands[0], "") if len(cands) == 1 else (None, "varios-misma-fecha")

    # scope global: el nombre solo propone; la fecha decide.
    confirmed = [c for c in cands if birthdate_matches(biw_birth, c["birth_date"])]
    if len(confirmed) == 1:
        return confirmed[0], ""
    if confirmed:
        return None, "varios-misma-fecha"
    if len(cands) > 1:
        return None, "varios-candidatos"
    if not biw_birth or not cands[0]["birth_date"]:
        # Único homónimo en toda la historia de Transfermarkt, pero a alguna de las
        # dos fuentes le falta la fecha: nada confirma que sea él.
        return None, "sin-fecha-que-verificar"
    return None, "fecha-discrepante"


def _player_row(biw_id: str, player, cand: dict | None, motivo: str, biw_birth: str) -> dict:
    return {
        "biwenger_id": biw_id,
        "biwenger_name": str(player.name),
        "biwenger_team": "" if pd.isna(player.team_id) else str(int(player.team_id)),
        "biwenger_birth_date": biw_birth,
        "tm_id": cand["id"] if cand else "",
        "tm_name": cand["name"] if cand else "",
        "tm_club": cand["club_name"] if cand else "",
        "tm_birth_date": cand["birth_date"] if cand else "",
        "tm_position": cand["position"] if cand else "",
        "motivo": motivo,
        "decision": "",
    }


# --- SofaScore: se cuelga del canónico que Biwenger ya tiene ------------------
#
# Misma regla biunívoca y misma graduación por fecha que Transfermarkt, con una
# diferencia clave: SofaScore no crea identidades. Cuando un par es seguro, el id
# de SofaScore se añade al ``canonical_id`` que Biwenger ya obtuvo en la pasada de
# Transfermarkt; si Biwenger aún no tiene canónico (su Transfermarkt sigue en
# revisión), no hay a qué colgarlo y se deja pendiente para una pasada posterior.


def _so_record(row) -> dict:
    """Registro neutral de un jugador de SofaScore (``club_name`` = nombre del equipo)."""
    return _source_record(row, id_col="sofascore_player_id", club_col="team_name")


def _biwenger_ids_with_source(df: pd.DataFrame, source: str) -> set[str]:
    """Ids de Biwenger cuyo canónico ya tiene un mapping a ``source``.

    ``df`` es ``store.players`` o ``store.teams``: la misma consulta sirve a ambos.
    """
    with_source = set(df.loc[df["fuente"] == source, "canonical_id"])
    biw = df[df["fuente"] == BIWENGER]
    return set(biw.loc[biw["canonical_id"].isin(with_source), "id_en_fuente"])


def _biwenger_ids_skipped(df: pd.DataFrame, skips: pd.DataFrame) -> set[str]:
    """Ids de Biwenger cuyo canónico está marcado como sin contraparte en SofaScore.

    El registro negativo (ADR 0011) los da por resueltos igual que un mapping: sin
    esto reaparecerían en revisión cada pasada (#94). El fichero de skips es único
    para jugadores y equipos; ``isin`` sobre ``canonical_id`` filtra por tipo solo
    (``p…`` no cruza con equipos ni ``t…`` con jugadores).
    """
    skipped = set(skips["canonical_id"])
    biw = df[df["fuente"] == BIWENGER]
    return set(biw.loc[biw["canonical_id"].isin(skipped), "id_en_fuente"])


def _map_teams_sofascore(
    store: MappingStore, biw_teams: pd.DataFrame, so_teams: pd.DataFrame, today: str
) -> list[UnappliedDecision]:
    """Cuelga cada equipo de SofaScore del canónico del equipo de Biwenger."""
    clubs = _clubs(so_teams, id_col="team_id", name_col="team_name")
    biw_canonical = store.canonical_by_source(store.teams, BIWENGER)
    resolved = _biwenger_ids_with_source(store.teams, SOFASCORE) | _biwenger_ids_skipped(
        store.teams, store.sofascore_skips
    )
    taken = store.approved_ids(store.teams, SOFASCORE)
    old_review = store.teams_review_sofascore

    unapplied = _apply_source_decisions(
        old_review,
        resolved,
        taken,
        biw_canonical,
        store.add_team,
        store.add_sofascore_skip,
        "equipo",
        "sofascore_team_id",
        today,
    )

    pending: list[tuple[str, object, str, list[dict]]] = []
    club_suitors: dict[str, set[str]] = defaultdict(set)
    for team in biw_teams.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(team.id))
        if biw_id in resolved or biw_id not in biw_canonical:
            continue
        competition = str(team.competition)
        scope = [c for c in clubs if c["competition"] == competition and c["club_id"] not in taken]
        cands = team_candidates(str(team.name), scope)
        pending.append((biw_id, team, competition, cands))
        for club in cands:
            club_suitors[club["club_id"]].add(biw_id)

    review_rows: list[dict] = []
    for biw_id, team, competition, cands in pending:
        if len(cands) == 1 and len(club_suitors[cands[0]["club_id"]]) == 1:
            store.add_team(
                biw_canonical[biw_id], [(SOFASCORE, cands[0]["club_id"])], method="auto", date=today
            )
            resolved.add(biw_id)
        elif cands:
            # Sin candidatos no hay dudoso: SofaScore solo cubre lo backfilleado, así
            # que la ausencia es lo esperado y el equipo queda pendiente, no en revisión.
            review_rows += _so_team_review_rows(biw_id, team, competition, cands, club_suitors)

    store.teams_review_sofascore = _preserve_decisions(
        review_rows,
        old_review,
        resolved,
        SOFASCORE_TEAM_REVIEW_COLUMNS,
        ("biwenger_id", "sofascore_team_id"),
    )
    return unapplied


def _so_team_review_rows(
    biw_id: str, team, competition: str, cands: list[dict], club_suitors: dict[str, set[str]]
) -> list[dict]:
    shared = any(len(club_suitors[club["club_id"]]) > 1 for club in cands)
    motivo = "candidato-compartido" if shared else "varios-candidatos"
    return [
        {
            "biwenger_id": biw_id,
            "biwenger_name": str(team.name),
            "competition": competition,
            "sofascore_team_id": club["club_id"],
            "sofascore_team_name": club["club_name"],
            "motivo": motivo,
            "decision": "",
        }
        for club in cands
    ]


def _map_players_sofascore(
    store: MappingStore,
    biw_players: pd.DataFrame,
    so_season: pd.DataFrame,
    so_all: pd.DataFrame,
    today: str,
) -> list[UnappliedDecision]:
    """Cuelga cada jugador de SofaScore del canónico del jugador de Biwenger."""
    so_club_to_canonical = store.canonical_by_source(store.teams, SOFASCORE)
    biw_team_to_canonical = store.canonical_by_source(store.teams, BIWENGER)
    biw_canonical = store.canonical_by_source(store.players, BIWENGER)

    so_by_canonical: dict[str, list[dict]] = defaultdict(list)
    for row in so_season.dropna(subset=["sofascore_player_id"]).itertuples():
        if not pd.isna(row.team_id):
            canonical = so_club_to_canonical.get(str(int(row.team_id)))
            if canonical:
                so_by_canonical[canonical].append(_so_record(row))
    pool = _global_pool(so_all, id_col="sofascore_player_id", record=_so_record)

    resolved = _biwenger_ids_with_source(store.players, SOFASCORE) | _biwenger_ids_skipped(
        store.players, store.sofascore_skips
    )
    taken = store.approved_ids(store.players, SOFASCORE)
    old_review = store.players_review_sofascore

    # ``y`` cuelga el id de SofaScore del canónico de Biwenger; ``skip`` registra en
    # sofascore-skips.csv que el jugador no tiene contraparte (persistente, ADR 0011).
    # Un ``y``/``skip`` sobre un Biwenger todavía sin canónico no se aplica
    # (biwenger-sin-canonico).
    unapplied = _apply_source_decisions(
        old_review,
        resolved,
        taken,
        biw_canonical,
        store.add_player,
        store.add_sofascore_skip,
        "jugador",
        "sofascore_id",
        today,
    )

    free_pool = [c for c in pool if c["id"] not in taken]
    proposals: list[_Proposal] = []
    players_by_id: dict[str, object] = {}
    for player in biw_players.sort_values(["competition", "id"]).itertuples():
        biw_id = str(int(player.id))
        # Solo entran los que ya tienen canónico y aún no tienen SofaScore.
        if biw_id in resolved or biw_id not in biw_canonical:
            continue
        canonical_team = (
            biw_team_to_canonical.get(str(int(player.team_id)))
            if not pd.isna(player.team_id)
            else None
        )
        biw_birth = "" if pd.isna(player.birth_date) else str(player.birth_date)[:10]
        in_club = [
            c
            for c in (so_by_canonical.get(canonical_team, []) if canonical_team else [])
            if c["id"] not in taken
        ]
        cands, scope = _candidates(str(player.name), biw_birth, in_club, free_pool)
        proposals.append(_Proposal(biw_id, biw_birth, cands, scope))
        players_by_id[biw_id] = player

    review_rows: list[dict] = []
    for proposal, match, cands, motivo in _resolve_graph(proposals):
        biw_id, biw_birth = proposal.left_id, proposal.birth_date
        player = players_by_id[biw_id]
        if match is not None:
            store.add_player(
                biw_canonical[biw_id], [(SOFASCORE, match["id"])], method="auto", date=today
            )
            resolved.add(biw_id)
            continue
        if not cands:
            # Sin candidatos no es un dudoso: SofaScore solo cubre lo backfilleado,
            # así que la ausencia es lo esperado y el jugador queda pendiente. Su id
            # de SofaScore sin resolver ya lo cuenta la métrica del informe.
            continue
        review_rows += [_so_player_row(biw_id, player, c, motivo, biw_birth) for c in cands]

    store.players_review_sofascore = _preserve_decisions(
        review_rows,
        old_review,
        resolved,
        SOFASCORE_PLAYER_REVIEW_COLUMNS,
        ("biwenger_id", "sofascore_id"),
    )
    return unapplied


def _so_player_row(biw_id: str, player, cand: dict | None, motivo: str, biw_birth: str) -> dict:
    return {
        "biwenger_id": biw_id,
        "biwenger_name": str(player.name),
        "biwenger_team": "" if pd.isna(player.team_id) else str(int(player.team_id)),
        "biwenger_birth_date": biw_birth,
        "sofascore_id": cand["id"] if cand else "",
        "sofascore_name": cand["name"] if cand else "",
        "sofascore_team": cand["club_name"] if cand else "",
        "sofascore_birth_date": cand["birth_date"] if cand else "",
        "motivo": motivo,
        "decision": "",
    }


def _apply_source_decisions(
    review_df: pd.DataFrame,
    resolved: set[str],
    taken: set[str],
    biw_canonical: dict[str, str],
    add_fn,
    skip_fn,
    kind: str,
    source_id_attr: str,
    today: str,
) -> list[UnappliedDecision]:
    """Aplica decisiones de una fuente que se cuelga de un canónico existente."""
    unapplied: list[UnappliedDecision] = []
    for biw_id, rows in review_df.groupby("biwenger_id"):
        biw_id = str(biw_id)
        if biw_id in resolved:
            continue
        marked = [
            (row, _decision(row.decision)) for row in rows.itertuples() if str(row.decision).strip()
        ]
        if not marked:
            continue

        action, problems = _classify_group(marked, source_id_attr, taken)
        unapplied += [
            UnappliedDecision(
                kind=kind,
                biwenger_id=biw_id,
                biwenger_name=str(row.biwenger_name),
                tm_id=str(getattr(row, source_id_attr) or ""),
                decision=str(row.decision),
                motivo=motivo,
            )
            for row, motivo in problems
        ]
        if action is None:
            continue

        verb, source_id = action
        if verb == "yes":
            canonical = biw_canonical.get(biw_id)
            if not canonical:
                # Biwenger aún no tiene canónico (su Transfermarkt sigue pendiente):
                # no hay de qué colgar SofaScore. Se conserva la decisión y se reporta.
                unapplied.append(
                    UnappliedDecision(
                        kind=kind,
                        biwenger_id=biw_id,
                        biwenger_name=str(marked[0][0].biwenger_name),
                        tm_id=source_id,
                        decision="y",
                        motivo="biwenger-sin-canonico",
                    )
                )
                continue
            add_fn(canonical, [(SOFASCORE, source_id)], method="manual", date=today)
            resolved.add(biw_id)
            taken.add(source_id)
        else:  # skip: no tiene contraparte en SofaScore — se registra por canónico
            name = str(marked[0][0].biwenger_name)
            canonical = biw_canonical.get(biw_id)
            if not canonical:
                # Sin canónico no hay dónde anclar el hecho negativo (su Transfermarkt
                # sigue en revisión): se conserva la decisión y se reporta, como el ``y``.
                unapplied.append(
                    UnappliedDecision(
                        kind=kind,
                        biwenger_id=biw_id,
                        biwenger_name=name,
                        tm_id="",
                        decision="skip",
                        motivo="biwenger-sin-canonico",
                    )
                )
                continue
            # Persistente (ADR 0011): sin esto, ``resolved`` se recalcula cada pasada
            # desde los mapeos existentes y el jugador volvería a proponerse (#94).
            skip_fn(canonical, name, today)
            resolved.add(biw_id)
    return unapplied


# --- informe y verificación --------------------------------------------------


def _report(
    store: MappingStore,
    biw_teams: pd.DataFrame,
    biw_players: pd.DataFrame,
    so_players_all: pd.DataFrame,
) -> MapReport:
    team_ids = {str(int(v)) for v in biw_teams["id"].dropna()}
    player_ids = {str(int(v)) for v in biw_players["id"].dropna()}
    approved_teams = store.approved_ids(store.teams, BIWENGER) & team_ids
    approved_players = store.players[store.players["fuente"] == BIWENGER]
    approved_players = approved_players[approved_players["id_en_fuente"].isin(player_ids)]

    so_ids = {str(int(v)) for v in so_players_all["sofascore_player_id"].dropna()}
    so_mapped = store.approved_ids(store.players, SOFASCORE)
    so_present = bool(so_ids) or not store.players[store.players["fuente"] == SOFASCORE].empty

    return MapReport(
        teams_total=len(team_ids),
        teams_mapped=len(approved_teams),
        teams_review=store.teams_review["biwenger_id"].nunique(),
        players_total=len(player_ids),
        players_auto=int((approved_players["metodo"] == "auto").sum()),
        players_manual=int((approved_players["metodo"] == "manual").sum()),
        players_review=store.players_review["biwenger_id"].nunique(),
        sofascore_present=so_present,
        sofascore_teams_mapped=int((store.teams["fuente"] == SOFASCORE).sum()),
        sofascore_teams_review=store.teams_review_sofascore["biwenger_id"].nunique(),
        sofascore_players_mapped=int((store.players["fuente"] == SOFASCORE).sum()),
        sofascore_players_review=store.players_review_sofascore["biwenger_id"].nunique(),
        sofascore_skipped=len(store.sofascore_skips),
        sofascore_unresolved=len(so_ids - so_mapped),
    )


def check_mappings(storage: Storage, mappings_dir) -> list[str]:
    """Devuelve los problemas de cobertura; lista vacía = todo mapeado.

    Falla (para CI y pipeline) si algún jugador o equipo de Biwenger presente en
    las tablas curadas no tiene un ID canónico aprobado, o si algún
    ``sofascore_player_id`` presente en el eventing curado no tiene canónico: sin
    ese cruce el eventing queda huérfano y no se puede unir a la identidad. Sin
    datos curados (p. ej. en CI, donde ``data/`` está en .gitignore) no hay nada
    que verificar y pasa.
    """
    biw_players = _read_curated(storage, "biwenger_players", ["id", "name", "competition"])
    biw_teams = _read_curated(storage, "biwenger_teams", ["id", "name", "competition"])

    store = MappingStore(mappings_dir)
    store.load()

    problems: list[str] = []
    problems += _missing(biw_teams, store.approved_ids(store.teams, BIWENGER), "equipo")
    problems += _missing(biw_players, store.approved_ids(store.players, BIWENGER), "jugador")
    problems += _missing_sofascore(storage, store.approved_ids(store.players, SOFASCORE))
    return problems


def _missing_sofascore(storage: Storage, approved: set[str]) -> list[str]:
    """IDs de SofaScore en el eventing curado que aún no tienen canónico aprobado."""
    seen: set[str] = set()
    for table in ("player_match_stats", "player_season_stats"):
        df = _read_curated(storage, table, ["sofascore_player_id"])
        for value in df["sofascore_player_id"].dropna():
            seen.add(str(int(value)) if isinstance(value, float) else str(value))
    return [
        f"sofascore sin canonical: id {source_id} presente en el eventing curado"
        for source_id in sorted(seen - approved)
        if source_id
    ]


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
