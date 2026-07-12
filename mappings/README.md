# Mappings de identidad canónica

La correspondencia entre los IDs de cada fuente y nuestro **jugador/equipo
canónico** (ADR 0001). Vive en git, no en S3: el trabajo manual de revisión es
código y se revisa en pull request.

Anclamos la identidad en **Biwenger** (el universo que la plataforma necesita) y
buscamos su contraparte en **Transfermarkt** por club mapeado + nombre
normalizado. La ingesta de reports rellena la fecha de nacimiento en
`biwenger_players` desde el detalle por jugador (issue #37): un homónimo único en
el club solo se aprueba en `auto` si su fecha coincide con la de Transfermarkt o
falta alguna de las dos; si discrepan, va a revisión con motivo
`fecha-discrepante` y ambas fechas (`biwenger_birth_date` y `tm_birth_date`) como
evidencia del desempate.

## Ficheros

| Fichero | Qué contiene |
|---|---|
| `players.csv` / `teams.csv` | Mappings **aprobados**. Formato largo: una fila por `(fuente, id_en_fuente)`, todas con el mismo `canonical_id`. `metodo` es `auto` o `manual`. |
| `players-review.csv` / `teams-review.csv` | **Candidatos dudosos** con sus evidencias y una columna `decision` vacía. |

## Flujo

`transfermarkt_players` está particionada por temporada, así que `lfdata map`
busca la contraparte en las plantillas de la temporada que se le pida
(`--season`, año de inicio; la actual por defecto).

1. `lfdata map` regenera candidatos: aprueba los seguros (`auto`) y deja los
   dudosos en los ficheros de revisión.
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
