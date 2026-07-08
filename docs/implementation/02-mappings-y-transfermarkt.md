# Paso 2 — Ingesta de Transfermarkt y capa de mappings

**Objetivo:** cada jugador y equipo tiene un ID canónico propio, y Transfermarkt queda ingerido (valores de mercado, traspasos, datos biográficos) y mapeado.

## Decisiones ya tomadas que aplican aquí

- ID canónico propio + matching automático con revisión manual versionada en git (ADR 0001).
- Transfermarkt se accede por sus endpoints JSON internos (`ceapi`) para valores y traspasos; HTML solo para búsqueda y perfil (verificado en `docs/experiments/2026-07-07-alex-fores.md`).

## Componentes

### `lfdata.sources.transfermarkt`

- Búsqueda: `GET /schnellsuche/ergebnis/schnellsuche?query={nombre}` (HTML, extraer `/{slug}/profil/spieler/{id}`).
- Perfil: `GET /{slug}/profil/spieler/{id}` (HTML: fecha de nacimiento, posición, pie, altura, nacionalidad).
- Valores: `GET /ceapi/marketValueDevelopment/graph/{id}` (JSON: fecha, valor, club en esa fecha).
- Traspasos: `GET /ceapi/transferHistory/list/{id}` (JSON: fecha, origen, destino, tipo — cesión, fin de cesión, traspaso —, coste, valor).
- Espera entre peticiones: 4 s. Sin impersonación especial (basta User-Agent de navegador), pero pasa por el transporte común igualmente.

Alcance inicial: las plantillas de La Liga y Segunda División (por página de club, `/verein/{id}/saison_id/{año}`), ~1.100 jugadores.

### `lfdata.mappings`

Regla de identidad, en orden:

1. **Match automático seguro**: misma fecha de nacimiento + mismo club (mapeado) + nombre normalizado compatible (sin tildes, minúsculas, apellido contenido). Se aprueba solo.
2. **Candidato dudoso**: coincide fecha de nacimiento pero no club, o nombre muy similar sin fecha que lo confirme. Va al fichero de revisión.
3. **Sin candidato**: jugador queda pendiente; las tablas curadas con ID canónico no lo incluyen hasta resolverse.

Ficheros versionados en git, bajo `mappings/`:

- `mappings/players.csv` — aprobados: `canonical_id, fuente, id_en_fuente, metodo (auto|manual), fecha`
- `mappings/players-review.csv` — dudosos pendientes: candidatos con sus evidencias, columna `decision` vacía que se rellena a mano
- `mappings/teams.csv` y `mappings/teams-review.csv` — igual para equipos (volumen pequeño, casi todo manual la primera vez)

El comando `lfdata map` regenera candidatos y aplica decisiones; `lfdata map --check` falla si hay filas de datos sin mapping (para CI y pipeline).

Lección del experimento Forés: el club del perfil es "dueño actual", no "dónde jugó" — el matching por club usa el club en la fecha del dato (los traspasos de Transfermarkt dan esa línea temporal), no el club actual.

### Tablas curadas que produce este paso

| Tabla | Grano | Notas |
|---|---|---|
| `players` | jugador canónico | canonical_id, nombre, fecha de nacimiento, posición, nacionalidad |
| `teams` | equipo canónico | canonical_id, nombre, país |
| `player_mappings` / `team_mappings` | mapping | fuente, id en fuente, canonical_id |
| `market_values_tm` | jugador-fecha | valor Transfermarkt y club en esa fecha |
| `transfers` | movimiento | fecha, origen, destino, tipo (cesión/fin de cesión/traspaso), coste |

## Orden de trabajo

1. Cliente Transfermarkt con fixtures del experimento; ingesta de plantillas de La Liga y Segunda.
2. Normalizador de nombres + generador de candidatos + tests con casos reales (Forés incluido).
3. Primer ciclo completo de revisión manual (el grueso del trabajo humano de todo el proyecto, una vez).
4. Publicar tablas canónicas y re-publicar `fantasy_points` y `biwenger_prices` con canonical_id.

## Hecho cuando

- Todos los jugadores de La Liga y Segunda 2025-26 en Biwenger tienen canonical_id y mapping a Transfermarkt (o una fila justificada en revisión).
- `lfdata map --check` pasa en limpio.
- El caso Forés (cesiones encadenadas) se resuelve correctamente con tests que lo fijan.
