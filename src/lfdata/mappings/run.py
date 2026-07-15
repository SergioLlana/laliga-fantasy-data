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
    TEAM_REVIEW_COLUMNS,
    TRANSFERMARKT,
    MappingStore,
)
from lfdata.sources.transfermarkt import DEFAULT_SEASON
from lfdata.storage import Storage

_YES = frozenset({"y", "yes", "si", "sí", "x", "1", "true", "ok"})
_SKIP = frozenset({"skip", "none", "no-tm", "biwenger-only", "solo-biwenger"})


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
        if self.unapplied:
            lines.append("")
            lines.append(
                f"Decisiones no aplicadas ({len(self.unapplied)}) — "
                "se conservan en el fichero de revisión; corrígelas y re-ejecuta:"
            )
            lines += [u.render() for u in self.unapplied]
        return "\n".join(lines)


def _decision(value: str) -> str | None:
    token = str(value).strip().lower()
    if token in _YES:
        return "yes"
    if token in _SKIP:
        return "skip"
    return None


def _classify_group(marked: list[tuple], tm_id_attr: str, taken: set[str]):
    """Clasifica las decisiones marcadas de un ``biwenger_id``.

    Devuelve ``(accion, problemas)`` donde ``accion`` es ``("yes", tm_id)``,
    ``("skip", None)`` o ``None`` si no se puede aplicar; ``problemas`` es la
    lista de ``(fila, motivo)`` de las decisiones que no se aplican.
    """
    yes = [row for row, d in marked if d == "yes"]
    skip = [row for row, d in marked if d == "skip"]
    unknown = [row for row, d in marked if d is None]

    if unknown:
        return None, [(row, "token-no-reconocido") for row, _ in marked]
    if yes and skip:
        return None, [(row, "y-con-skip") for row, _ in marked]
    if len(yes) > 1:
        return None, [(row, "varios-y") for row in yes]
    if len(yes) == 1:
        tm_id = str(getattr(yes[0], tm_id_attr) or "")
        if not tm_id:
            return None, [(yes[0], "y-sin-candidato")]
        if tm_id in taken:
            return None, [(yes[0], "tm-id-ya-tomado")]
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
) -> list[UnappliedDecision]:
    """Promueve las decisiones válidas a aprobados; reporta las que no lo son.

    Las filas de un ``biwenger_id`` con ``decision`` no vacía se clasifican en
    conjunto: o promueven la identidad (un único ``y`` con candidato libre, o uno
    o varios ``skip``), o quedan sin aplicar con su motivo.
    """
    unapplied: list[UnappliedDecision] = []
    for biw_id, rows in review_df.groupby("biwenger_id"):
        biw_id = str(biw_id)
        if biw_id in approved:
            continue
        marked = [
            (row, _decision(row.decision)) for row in rows.itertuples() if str(row.decision).strip()
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

    store = MappingStore(mappings_dir)
    store.load()

    unapplied = _map_teams(store, biw_teams, tm_season, today)
    unapplied += _map_players(store, biw_players, tm_season, tm_all, today)

    store.save()
    report = _report(store, biw_teams, biw_players)
    report.unapplied = unapplied
    return report


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
) -> list[UnappliedDecision]:
    clubs = _tm_clubs(tm_players)
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


def _tm_record(row) -> dict:
    return {
        "id": str(int(row.id)),
        "name": str(row.name),
        "club_name": "" if pd.isna(row.club_name) else str(row.club_name),
        "birth_date": "" if pd.isna(row.birth_date) else str(row.birth_date)[:10],
        "position": "" if pd.isna(row.position) else str(row.position),
        "season": "" if pd.isna(row.season) else str(row.season),
    }


def _global_pool(tm_all: pd.DataFrame) -> list[dict]:
    """Un registro por jugador de Transfermarkt, con su club más reciente.

    El mismo jugador aparece en una fila por temporada en la que estuvo en una
    plantilla; para el matching solo importa su identidad, así que nos quedamos
    con la fila de la temporada más alta (el club que mostramos al revisor es
    entonces el último que se le conoce).
    """
    rows = tm_all.dropna(subset=["id"])
    if rows.empty:
        return []
    latest = rows.sort_values("season").drop_duplicates(subset=["id"], keep="last")
    return [_tm_record(row) for row in latest.itertuples()]


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


def _reserved_by_birthdate(proposals: list[tuple]) -> dict[str, str]:
    """Candidatos que la fecha de nacimiento adjudica a un único jugador de Biwenger.

    Quien tiene su identidad probada por la fecha se lleva a su candidato, y los
    demás lo pierden de su lista: si no, una ficha huérfana de nombre genérico
    —Biwenger conserva un ``Thomas`` y un ``Adrián`` sin equipo— reclama a todos
    sus homónimos y bloquea a quien está identificado sin ninguna duda. Lemar nació
    el día exacto de Thomas Lemar, pero ``Thomas`` le disputaba el candidato y
    ninguno de los dos se aprobaba. Un candidato confirmado por dos no lo adjudica
    nadie: esa disputa es real y va a revisión.
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for biw_id, _, _, _, _, confirmed in proposals:
        if len(confirmed) == 1:
            claims[confirmed[0]["id"]].add(biw_id)
    return {tm_id: next(iter(owners)) for tm_id, owners in claims.items() if len(owners) == 1}


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
    pool = _global_pool(tm_all)

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
    )

    # Grafo bipartito sobre un scope fijo (los no tomados). Se calcula primero
    # para todos y solo después se auto-aprueba, de modo que dos jugadores de
    # Biwenger que se disputen el mismo candidato de Transfermarkt vayan ambos a
    # revisión, sin que el orden de los ids decida por nosotros.
    free_pool = [c for c in pool if c["id"] not in taken]
    proposals: list[tuple[str, object, str, list[dict], str, list[dict]]] = []
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
        confirmed = [c for c in cands if birthdate_matches(biw_birth, c["birth_date"])]
        proposals.append((biw_id, player, biw_birth, cands, scope, confirmed))

    reserved = _reserved_by_birthdate(proposals)

    pending: list[tuple[str, object, str, list[dict], str]] = []
    tm_suitors: dict[str, set[str]] = defaultdict(set)
    for biw_id, player, biw_birth, cands, scope, confirmed in proposals:
        if len(confirmed) == 1 and reserved.get(confirmed[0]["id"]) == biw_id:
            cands = confirmed
        else:
            cands = [c for c in cands if reserved.get(c["id"], biw_id) == biw_id]
        pending.append((biw_id, player, biw_birth, cands, scope))
        for c in cands:
            tm_suitors[c["id"]].add(biw_id)

    review_rows: list[dict] = []
    for biw_id, player, biw_birth, cands, scope in pending:
        if any(len(tm_suitors[c["id"]]) > 1 for c in cands):
            # Algún candidato lo reclama también otro jugador de Biwenger: nadie
            # se auto-aprueba; todos a revisión con el cuadro completo.
            review_rows += [
                _player_row(biw_id, player, c, "candidato-compartido", biw_birth) for c in cands
            ]
            continue

        match, motivo = _resolve(biw_birth, cands, scope)
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
