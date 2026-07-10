# Paso 3 — SofaScore, FotMob y backfill de eventing

**Objetivo:** estadísticas por jugador-partido de SofaScore para La Liga, Segunda y las 5 grandes ligas europeas (5 temporadas), más descarga bajo demanda del historial de cualquier fichaje que venga de otra liga.

## Decisiones ya tomadas que aplican aquí

- SofaScore primaria, FotMob redundancia (ADR 0002, ambas verificadas).
- Cobertura: top-5 (Premier, Serie A, Bundesliga, Ligue 1 + La Liga) completas; resto de ligas **bajo demanda** por jugador cuando aparece un fichaje.
- ScrapeOps con plan gratuito integrado en el transporte desde el paso 1.

## Uso de ScrapeOps (aclaración operativa)

- Por defecto **apagado**: el volumen normal (actualización diaria) va directo con `curl-cffi` + esperas de 3-5 s, que el experimento demostró suficiente. El proxy actúa como desbordamiento automático ante bloqueo persistente (ADR 0004).
- El plan gratuito (~1.000 créditos/mes) sirve para validar la integración y absorber bloqueos puntuales del incremental.
- **El backfill masivo se lanza dentro de un mes de plan de pago puntual, sincronizado con el resto del backfill de Biwenger** (decidido el 2026-07-10): se contrata cuando este paso esté implementado y ambos backfills corren en paralelo por proxy ese mismo mes (~23.000 créditos SofaScore + ~4.500 Biwenger caben en un plan de ~100k). La paralelización solo se hace en modo proxy (rotación de IPs); en directo aceleraría el bloqueo.

## Componentes

### `lfdata.sources.sofascore`

Endpoints (verificados):

- Búsqueda: `api/v1/search/all?q=` — para resolver jugadores bajo demanda.
- Temporadas de un jugador: `api/v1/player/{id}/statistics/seasons`.
- Agregado por temporada: `api/v1/player/{id}/unique-tournament/{ut}/season/{sid}/statistics/overall` (115 campos).
- Nota por partido: `api/v1/player/{id}/unique-tournament/{ut}/season/{sid}/ratings`.
- Por liga-temporada (backfill masivo): calendario de eventos del torneo → alineaciones por partido (`api/v1/event/{id}/lineups`) → estadísticas de cada jugador en ese partido. Es la vía que da el jugador-partido completo, no solo la nota.

IDs de torneo relevantes: La Liga 8, LaLiga2 54, Premier 17, Serie A 23, Bundesliga 35, Ligue 1 34.

### `lfdata.sources.fotmob` (redundancia, mínima)

- Búsqueda: `apigw.fotmob.com/searchapi/suggest?term=`.
- Jugador: `fotmob.com/api/data/playerData?id=` (temporadas, partidos recientes con nota propia, valores de mercado).
- Solo se usa si SofaScore falla para un dato concreto; su nota se guarda en columna separada (`rating_fotmob`), nunca mezclada con la de SofaScore (calibraciones distintas: 6.15 vs 6.48 para el mismo Forés 25/26).

### Modo bajo demanda

`lfdata ingest sofascore --player {canonical_id|nombre}`: busca al jugador, descarga todas sus temporadas disponibles (cualquier liga, incluida Primera Federación o Brasileirão) y las cura. Se lanza automáticamente cuando el pipeline detecta en Biwenger un jugador nuevo sin historial curado.

### Tabla curada principal

`player_match_stats` — grano jugador-partido, todas las ligas: canonical_id (si ya está mapeado; si no, id SofaScore pendiente), torneo, temporada, fecha, club del partido, rival, local/visitante, minutos, nota, goles, asistencias, tiros, pases clave, duelos, xG (nulo donde SofaScore no lo da, p. ej. LaLiga2), fuente.

## Volumen y ritmo del backfill

~380 partidos × 6 ligas × 5 temporadas ≈ 11.400 partidos; a 2 peticiones por partido y 3 s de espera ≈ 19 h por temporada completa de las 6 ligas. Se ejecuta por liga-temporada en tandas nocturnas durante ~1-2 semanas, reanudable (lo ya presente en `raw/` no se re-descarga).

## Orden de trabajo

1. Cliente SofaScore + fixtures del experimento; primero el modo bajo demanda (es el más simple y desbloquea el paso 5).
2. Backfill La Liga y Segunda (cruce de validación contra los minutos/notas de Biwenger).
3. Backfill top-5.
4. Cliente FotMob mínimo (búsqueda + jugador) como reserva.
5. Matching de IDs SofaScore → canonical (reutiliza el paso 2; la fecha de nacimiento viene en la API).

## Hecho cuando

- `player_match_stats` tiene 5 temporadas de las 6 ligas y pasa el cruce contra Biwenger (minutos ±10% en ≥95% de filas comunes; las discrepancias conocidas están documentadas).
- Un fichaje inventado ("jugador X del Feyenoord") obtiene su historial completo con un solo comando.
