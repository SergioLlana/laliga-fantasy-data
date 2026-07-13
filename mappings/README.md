# Mappings de identidad canónica

La correspondencia entre los IDs de cada fuente y nuestro **jugador/equipo
canónico** (ADR 0001). Vive en git, no en S3: el trabajo manual de revisión es
código y se revisa en pull request.

Anclamos la identidad en **Biwenger** (el universo que la plataforma necesita) y
buscamos su contraparte en **Transfermarkt**.

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
| `players.csv` / `teams.csv` | Mappings **aprobados**. Formato largo: una fila por `(fuente, id_en_fuente)`, todas con el mismo `canonical_id`. `metodo` es `auto` o `manual`. |
| `players-review.csv` / `teams-review.csv` | **Candidatos dudosos** con sus evidencias y una columna `decision` vacía. |

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
   - En blanco = sigue pendiente.
3. `lfdata map` de nuevo: promueve las decisiones a `players.csv`/`teams.csv`
   (como `manual`) y vuelve a proponer solo lo que siga sin resolver. Es
   idempotente: lo aprobado no se vuelve a tocar.
4. `lfdata map --check` falla (CI y pipeline) si algún jugador o equipo de
   Biwenger presente en las tablas curadas se quedó sin `canonical_id`, o si los
   ficheros de aprobados violan la integridad (ver abajo).

## Decisiones que no se pueden aplicar

`lfdata map` nunca borra una `decision` escrita a mano. Si una decisión no se
puede aplicar, se **conserva** en el fichero de revisión (para que la corrijas en
vez de reescribirla) y se lista en el informe con su motivo:

- `varios-y` — más de un `y` en el mismo jugador/equipo.
- `y-sin-candidato` — `y` en una fila sin candidato de Transfermarkt.
- `y-con-skip` — `y` y `skip` mezclados.
- `token-no-reconocido` — la `decision` no es `y`, `skip` ni un sinónimo válido.
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
