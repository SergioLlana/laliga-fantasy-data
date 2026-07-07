# Experimento: Álex Forés en todas las fuentes

**Fecha:** 2026-07-07 · **Objetivo:** comprobar, con un jugador real, qué información da cada fuente, cómo se accede a ella y qué discrepancias aparecen entre fuentes. Elegido Álex Forés (delantero, n. 12/04/2001) porque en 2024-25 y 2025-26 pasó por cesiones y categorías distintas: es exactamente el caso "baseline de fichaje" que la plataforma debe resolver.

## Su historia real (reconstruida cruzando fuentes)

- 2024-25 primera mitad: Villarreal B (Primera Federación).
- 2025-01-20: cedido al Levante UD (Segunda División).
- 2025-07-24: cedido al Real Oviedo (La Liga 2025-26).
- 2026-06-30: fin de cesión, vuelve al Villarreal.

## Qué da cada fuente

### Biwenger (API JSON, sin bloqueo)

Endpoints verificados, todos con `User-Agent` normal y sin autenticación:

- `GET cf.biwenger.com/api/v2/competitions/la-liga/data?lang=es&score=1` — los ~626 jugadores de la competición con precio, incremento diario, posición, estado y puntos agregados. Existe también `competitions/segunda-division/...`.
- `GET cf.biwenger.com/api/v2/players/la-liga/{slug}?fields=*,reports(...),prices,seasons&season=YYYY` — detalle por temporada (hasta ~2019): un report por partido con **puntos en los cinco sistemas de puntuación** (`points: {1: AS, 2: SofaScore, 5: Media, 3: Estadísticas, 6: Social}`), y `rawStats` con `minutesPlayed`, nota `sofascore`, `picas`, resultado, portería a cero. `prices` da el precio diario de la temporada (~366 puntos). `birthday` incluido.

Hallazgos:

- **Cubre Segunda División** con la misma API (19 reports de Forés en 24-25 con el Levante), pero en Segunda `rawStats` **no trae la nota SofaScore** (`picas: "SC"`), solo minutos y puntos.
- El equipo mostrado es el dueño actual del jugador (tras fin de cesión), no el club donde jugó cada partido.
- No cubre Primera Federación: su primera mitad de 24-25 no existe en Biwenger.

### Transfermarkt (mixto: JSON interno + HTML, sin bloqueo con UA de navegador)

- `GET transfermarkt.es/ceapi/marketValueDevelopment/graph/{id}` — **JSON limpio** del histórico de valor de mercado, con el club en cada fecha de tasación.
- `GET transfermarkt.es/ceapi/transferHistory/list/{id}` — **JSON limpio** de todos los traspasos y cesiones, con fechas, clubes, tipo (cesión/fin de cesión) y valor.
- Búsqueda (`/schnellsuche/ergebnis/schnellsuche?query=`) y perfil (`/{slug}/profil/spieler/{id}`): HTML a parsear; el perfil da fecha de nacimiento (`itemprop="birthDate"`), posición, pie, agente.

Hallazgo clave: los dos endpoints `ceapi` reducen muchísimo el parseo de HTML previsto — lo esencial (valores + traspasos) es JSON.

### SofaScore (API JSON, bloqueo por huella TLS)

- `curl` normal → **403**. Con `curl-cffi` e `impersonate='chrome'` → 200 en todo. Misma solución ya usada en world-cup-predictor.
- `GET api.sofascore.com/api/v1/search/all?q=` — búsqueda (Forés = id 1086128).
- `GET .../player/{id}/statistics/seasons` — qué torneos/temporadas tiene: LaLiga 25/26, **LaLiga2** 22/23-24/25, **Primera Federación** 24/25, Copa del Rey. La cobertura de categorías inferiores es la más profunda de las fuentes probadas.
- `GET .../player/{id}/unique-tournament/{ut}/season/{sid}/statistics/overall` — **115 campos agregados** por temporada (nota media, xG, pases clave, duelos...). Ojo: en LaLiga2 no hay xG.
- `GET .../player/{id}/unique-tournament/{ut}/season/{sid}/ratings` — nota partido a partido.

### FotMob (API JSON, bloqueo parcial)

- `GET apigw.fotmob.com/searchapi/suggest?term=` — búsqueda sin restricciones (Forés = id 1304120).
- `GET fotmob.com/api/data/playerData?id=` — 200 con `curl-cffi` (la ruta antigua `/api/playerData` ya no existe). Da temporadas desde 22/23, partidos recientes con **nota propia de FotMob** (no SofaScore), e incluso `marketValues`.
- Cobertura menor: para 24-25 solo lista LaLiga2, no Primera Federación.

## Discrepancias encontradas (material para la capa de mappings y validación)

| Dato | Biwenger | SofaScore | FotMob | Transfermarkt |
|---|---|---|---|---|
| Club "actual" (07-07-2026) | Villarreal | Villarreal | Villarreal | Villarreal (tras fin de cesión el 30-06) |
| Minutos 24-25 (Segunda) | 386 | 409 | — | — |
| Minutos 25-26 (La Liga) | (18 partidos) | 382 | 351 | — |
| Nota media 25-26 | vía puntos | 6.48 | 6.15 | — |
| Fecha de nacimiento | 20010412 | ✓ | 2001-04-12 | 12/04/2001 |

Conclusiones:

1. **La fecha de nacimiento coincide en las cuatro fuentes** → excelente clave de matching junto a nombre normalizado y club.
2. **Los minutos no cuadran entre fuentes** (386/409, 382/351): cada una cuenta distinto (descuentos, redondeos). Hay que declarar una fuente de verdad por campo: minutos de Biwenger para La Liga/Segunda (es lo que alimenta sus puntos), SofaScore para el resto de ligas.
3. **"Club" significa cosas distintas**: dueño actual (Biwenger) frente a club en la fecha del dato (Transfermarkt). Las cesiones hacen que el club de un jugador-partido deba salir del partido mismo, nunca del perfil del jugador.
4. **Las notas SofaScore y FotMob no son intercambiables** (6.48 vs 6.15): si FotMob sustituye a SofaScore como fuente, sus notas necesitan calibración propia.
5. **`curl-cffi` con impersonación de Chrome es requisito del transporte HTTP** para SofaScore y FotMob; Biwenger y Transfermarkt bastan con un User-Agent normal.

## Impacto en el plan

- FotMob queda **verificado** como fuente secundaria (ADR 0002 actualizado).
- Biwenger cubre también Segunda División → los recién ascendidos y los cedidos a Segunda tienen histórico de puntos sin scraping externo.
- Transfermarkt aporta además la semántica de cesiones (quién es el dueño vs dónde juega), necesaria para interpretar bien los movimientos de mercado.
