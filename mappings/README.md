# Mappings de identidad canónica

La correspondencia entre los IDs de cada fuente y nuestro **jugador/equipo
canónico** (ADR 0001). Vive en git, no en S3: el trabajo manual de revisión es
código y se revisa en pull request.

Anclamos la identidad en **Biwenger** (el universo que la plataforma necesita) y
buscamos su contraparte en **Transfermarkt** primero y en **SofaScore** después.
Transfermarkt crea el ID canónico; **SofaScore se cuelga del canónico que Biwenger
ya obtuvo de Transfermarkt** (no crea identidades nuevas), con la misma regla
biunívoca. Por eso Transfermarkt va primero: sin canónico no hay de qué colgar
SofaScore, y si el Transfermarkt de un jugador sigue en revisión, su SofaScore
espera a la siguiente pasada.

## Cómo se busca la contraparte

El **club es una pista, no un filtro**: acota el pool para que un homónimo único
baste, pero quien no esté en él se busca igual en todas las temporadas
descargadas. La identidad de una persona no tiene temporada; la pertenencia a una
plantilla, sí. Por eso `--season` decide *de qué plantillas salen los clubes* (año
de inicio; la actual por defecto) y no *a quién se puede mapear*: Biwenger conserva
la ficha de quien ya dejó la liga, y su contraparte vive en la temporada en la que
jugó.

La **fecha de nacimiento** (que la ingesta de reports rellena en `biwenger_players`
desde el detalle por jugador, issue #37) es la que gradúa la confianza según de
dónde salga el candidato:

| De dónde sale el candidato | Qué se exige para aprobarlo en `auto` |
|---|---|
| Del club ya mapeado | Un único homónimo. La fecha solo **descarta**: si discrepa, va a revisión. |
| Del club, sin homónimo, por fecha | Un único jugador del club nacido ese mismo día. Rescata al que el apodo escondía: `Ez Abde` ↔ `Abde Ezzalzouli`, `Yusi` ↔ `Youssef Enríquez`. |
| Del pool global (sin club, o sin nadie compatible en él) | Ahí el pool son miles y un apellido suelto no identifica a nadie: la fecha tiene que **confirmar**. Sin fecha que verificar, a revisión. |

Los **entrenadores** no entran: Biwenger los publica en la misma lista que a los
jugadores, con ficha, precio y puntos, pero no existen en la plantilla de
Transfermarkt. La ingesta los deja fuera de `biwenger_players` (`position` 5), y
con ello desaparece un conflicto que bloqueaba a dos: el entrenador *Simeone*
competía con su hijo *Giuliano* por la misma ficha de Transfermarkt.

## Ficheros

| Fichero | Qué contiene |
|---|---|
| `players.csv` / `teams.csv` | Mappings **aprobados** (todas las fuentes). Formato largo: una fila por `(fuente, id_en_fuente)`, todas con el mismo `canonical_id`. `metodo` es `auto` o `manual`. |
| `players-review.csv` / `teams-review.csv` | **Candidatos dudosos** de Transfermarkt, con sus evidencias y una columna `decision` vacía. |
| `sofascore-review.csv` / `sofascore-teams-review.csv` | Igual, para SofaScore: la evidencia trae los dos lados (nombre, equipo y fecha de nacimiento de Biwenger y de SofaScore). Solo se escriben cuando hay catálogo de SofaScore que revisar. |
| `sofascore-skips.csv` | Registro negativo de SofaScore ([ADR 0011](../docs/adr/0011-registro-negativo-de-sofascore.md)): canónicos que **no tienen contraparte** en SofaScore. Uno por `canonical_id` (el prefijo `p…`/`t…` distingue jugador de equipo). Reabrir un skip = borrar su fila. |

## SofaScore

La evidencia del matcher de SofaScore (nombre, **fecha de nacimiento** y club) no
está en `player_match_stats` ni en `search/all`; solo viene entera en las
alineaciones. Se publica aparte, desde raw/ y sin peticiones, con
`lfdata curate sofascore-catalog`, que construye dos tablas curadas —
`sofascore_players` y `sofascore_teams`— con la cobertura de lo backfilleado
(La Liga/Segunda). `lfdata map` las lee como lee las de Transfermarkt.

En `sofascore-review.csv`, `y` cuelga el `sofascore_id` del canónico. El `skip`, en
cambio, **no** puede crear un canónico solo-Biwenger como en Transfermarkt (aquí el
canónico ya existe): registra el hecho negativo en `sofascore-skips.csv`, por
`canonical_id` ([ADR 0011](../docs/adr/0011-registro-negativo-de-sofascore.md)), y así
`map` no vuelve a proponer al jugador entre ejecuciones. Motivo extra posible en ambos:
`biwenger-sin-canonico` (marcaste `y`/`skip` pero el jugador de Biwenger aún no tiene
canónico porque su Transfermarkt sigue en revisión; resuélvelo primero). Tras aprobar
mappings nuevos, `lfdata curate sofascore-canonical` rellena el `canonical_id` de
`player_match_stats`/`player_season_stats` cruzándolas con los mappings (sin releer
raw/).

## Jugadores fuera de plantilla

Un jugador de Biwenger enlazado a mano a su `spieler_id` (o un fichaje que aún no
sale en ningún kader descargado) tiene **identidad** en Transfermarkt pero no
**historial** curado: la ingesta por competición solo recorre plantillas. Su
historial (valor de mercado, traspasos, disponibilidad, lesiones) se trae con
`lfdata ingest transfermarkt-player --player <spieler_id|url|canonical_id>`, que
cura las cuatro tablas de historial —de carrera completa— y **nunca**
`transfermarkt_players`. Cae en la partición centinela `competition=bajo-demanda`
hasta que el jugador aparezca en un kader de La Liga, y una red de seguridad
contrasta la fecha de nacimiento del perfil con la de Biwenger antes de curar
(`--force` la salta). El porqué de todo esto, en
[ADR 0013](../docs/adr/0013-historial-transfermarkt-carrera-completa-particion-por-procedencia.md).

## Flujo

1. `lfdata map` regenera candidatos: aprueba los seguros (`auto`) y deja los
   dudosos en los ficheros de revisión, cada uno con el motivo por el que no se
   pudo aprobar solo:

   - `varios-en-club` — más de un homónimo dentro del club.
   - `fecha-discrepante` — candidato único, pero las dos fuentes no coinciden en
     la fecha de nacimiento (ambas se muestran como evidencia del desempate).
   - `sin-fecha-que-verificar` — único homónimo fuera de su club, pero a alguna de
     las dos fuentes le falta la fecha: nada confirma que sea él.
   - `varios-candidatos` / `varios-misma-fecha` — la evidencia no distingue entre
     varios.
   - `candidato-compartido` — dos entidades de Biwenger se disputan el mismo
     candidato; ninguna se aprueba y ambas se muestran juntas.
   - `sin-candidato` — nadie compatible en ninguna temporada descargada.
2. Rellena a mano la columna `decision` de los dudosos:
   - `y` en la fila del candidato correcto de Transfermarkt.
   - `skip` si el jugador no tiene contraparte en Transfermarkt (se le da ID
     canónico solo con Biwenger).
   - Un **`spieler_id`** (o una URL `.../profil/spieler/NNN`) en la fila de un
     jugador `sin-candidato`: lo mapea a ese `tm_id` aunque no hubiera candidato
     que ofrecer (Segunda/filiales y extranjeros que no salen en ninguna plantilla
     descargada). Solo en la revisión de **jugadores de Transfermarkt**; pegarlo en
     una fila que ya trae candidato es ambiguo (`id-en-fila-con-candidato`).
   - En blanco = sigue pendiente.
3. `lfdata map` de nuevo: promueve las decisiones a `players.csv`/`teams.csv`
   (como `manual`) y vuelve a proponer solo lo que siga sin resolver. Es
   idempotente: lo aprobado no se vuelve a tocar.
4. `lfdata map --check` falla (CI y pipeline) si algún jugador o equipo de
   Biwenger presente en las tablas curadas se quedó sin `canonical_id`, si algún
   `sofascore_player_id` del eventing curado no tiene canónico aprobado, o si los
   ficheros de aprobados violan la integridad (ver abajo).

## Decisiones que no se pueden aplicar

`lfdata map` nunca borra una `decision` escrita a mano. Si una decisión no se
puede aplicar, se **conserva** en el fichero de revisión (para que la corrijas en
vez de reescribirla) y se lista en el informe con su motivo:

- `varios-y` — más de un `y` en el mismo jugador/equipo.
- `y-sin-candidato` — `y` en una fila sin candidato de Transfermarkt.
- `y-con-skip` — `y` y `skip` mezclados.
- `token-no-reconocido` — la `decision` no es `y`, `skip` ni un sinónimo válido
  (ni un `spieler_id`/URL en la revisión de jugadores de Transfermarkt).
- `id-en-fila-con-candidato` — pegaste un `spieler_id` en una fila que ya trae
  candidato; usa `y` para ese candidato, o pega el id en la fila `sin-candidato`.
- `tm-id-ya-tomado` — el candidato elegido ya está mapeado a otra identidad
  canónica.

## Integridad de los aprobados

`players.csv`/`teams.csv` se editan a mano, así que `lfdata map` (y `--check`)
valida su integridad al cargarlos y falla señalando las filas si se viola alguna
de estas reglas (relación del dominio, ADR 0001 — un canónico tiene como máximo
un mapping por fuente):

- `(fuente, id_en_fuente)` único en todo el fichero.
- cada `canonical_id` con como máximo una fila por fuente.
- `canonical_id` con formato reconocible (`p…` para jugadores, `t…` para equipos).
