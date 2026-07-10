# Handoff: scraping de Biwenger, Transfermarkt y SofaScore

Documento para un agente que va a **optimizar el scraping**. Describe cómo se
descarga cada fuente hoy, por qué usamos ScrapeOps y cuántas peticiones cuesta
cada cosa. El objetivo del lector es reducir tiempo y consumo de créditos sin
romper el contrato de datos (raw/ reproducible, tablas curadas estables).

Estado a 2026-07-10: Biwenger y Transfermarkt están implementados; SofaScore/
FotMob están **diseñados pero no codificados** (solo se consume la nota de
SofaScore que Biwenger ya trae). Este documento marca claramente qué es real y
qué es plan.

---

## 1. Transporte HTTP común (`src/lfdata/sources/http.py`)

Todas las fuentes comparten `HttpTransport`. Una instancia por fuente, con su
propio ritmo. Comportamiento:

- **Sesión `curl-cffi` impersonando Chrome** (huella TLS + User-Agent realistas;
  necesario para SofaScore/FotMob y usado por coherencia en todas).
- **Espera configurable entre peticiones** (`wait_seconds`), aplicada como
  intervalo mínimo entre llamadas consecutivas de esa fuente.
- **Reintentos con espera creciente** ante estados `429, 500, 502, 503, 504`:
  backoff `5·2^intento` (5 s, 10 s, 20 s), hasta `max_retries=3`.
- **Reintento ante errores de red** (timeout, corte de conexión): mismo backoff.
  Si persiste tras los reintentos, se traduce a un `SourceHTTPError` 504 para que
  la fuente lo trate como fallo saltable (saltar el jugador), no como crash.
- **Timeout de petición: 90 s** (`REQUEST_TIMEOUT_SECONDS`). Subido desde los 30 s
  por defecto de curl porque a través del proxy las respuestas grandes tardan
  ~37 s (ver §2).
- **Proxy ScrapeOps opcional**, con doble compuerta:
  1. la fuente debe estar marcada `PROXY_ENABLED = True`, y
  2. debe existir `LFDATA_SCRAPEOPS_KEY` en el entorno.
  Sin cualquiera de las dos, va directo. La petición se reenvía como
  `https://proxy.scrapeops.io/v1/?api_key=…&url=<destino url-encoded>`.

> **Importante para el consumo:** cada intento cuenta como 1 crédito ScrapeOps.
> Los reintentos multiplican el gasto: un jugador que agota 3 reintentos consume
> 4 créditos. Bajar la tasa de 429/timeout no solo acelera, también ahorra.

---

## 2. Biwenger — IMPLEMENTADO

`src/lfdata/sources/biwenger/{client,ingest}.py`. Base: `https://cf.biwenger.com/api/v2`.

API JSON. Dos endpoints:

| Endpoint | Qué da | Peticiones |
|---|---|---|
| `competitions/{la-liga\|segunda-division}/data?lang=es&score=1` | Plantilla entera: **todos** los jugadores, equipos y jornadas de la competición | **1** por competición |
| `players/{competición}/{slug}?fields=…&season=YYYY` | Por jugador y temporada: un report por partido (puntos en los 5 sistemas, minutos, nota SofaScore), precios diarios, fecha de nacimiento | **1 por jugador** |

Config actual: `WAIT_SECONDS = 2.0`, `PROXY_ENABLED = True`.

### Conteo de peticiones (Biwenger)

- **Plantilla** (`lfdata ingest biwenger --competition la-liga`): **1 petición**
  (devuelve los 634 jugadores de La Liga de golpe).
- **Temporada con reports** (`… --season 2025`): 1 (plantilla) + **1 por jugador**.
  La Liga 2025 = **1 + 634 = 635 peticiones**. Segunda ronda cifras similares
  (~600 jugadores).
- **Backfill 5 temporadas × 2 competiciones**: la plantilla es de la temporada
  actual, pero los reports son por jugador-temporada → ~634×5 + ~600×5 ≈
  **~6.200 peticiones**.

### Por qué ScrapeOps en Biwenger (el hallazgo clave)

Al principio se asumió que Biwenger "no bloquea con esperas educadas" y se
descargaba directo. **Falso, comprobado el 2026-07-10 ingiriendo La Liga 2025:**
Biwenger corta con **429 sostenido a partir de la petición ~200 por ventana e
IP**, aun con 2 s entre peticiones. Es una cuota por ventana de tiempo, no un
problema de ritmo: ir más lento no la evita, y un re-run entero vuelve a chocar
al llegar a ~200. Por eso se activó el proxy (`PROXY_ENABLED = True`): ScrapeOps
rota IPs y permite pasar de las ~200 en un solo run.

**Coste del proxy:** latencia. La plantilla (payload de ~239 KB) tarda **~37 s**
por ScrapeOps (vs <1 s directo). Los reports por jugador tardan ~5–25 s. Una
temporada completa de La Liga por proxy ≈ **2–3 horas** de reloj (dominadas por
la latencia del proxy, no por el `wait_seconds` de 2 s).

---

## 3. Transfermarkt — IMPLEMENTADO

`src/lfdata/sources/transfermarkt/{client,ingest,parse}.py`. Base:
`https://www.transfermarkt.com` (host .com a propósito: respuestas en inglés).

Mezcla HTML (competición, plantilla, perfil, lesiones) y JSON interno `ceapi`
(valor de mercado, traspasos, rendimiento). **5 peticiones por jugador:**

| Endpoint | Tipo | Qué da |
|---|---|---|
| `…/startseite/wettbewerb/{code}/saison_id/{s}` | HTML | Clubes de la competición (1 por competición) |
| `x/kader/verein/{club_id}/saison_id/{s}` | HTML | Plantilla del club (1 por club) |
| `…/profil/spieler/{id}` | HTML | Perfil: nombre, nacimiento, posición |
| `ceapi/marketValueDevelopment/graph/{id}` | JSON | Histórico de valor de mercado |
| `ceapi/transferHistory/list/{id}` | JSON | Traspasos y cesiones |
| `ceapi/performance-game/{id}` | JSON | Rendimiento partido a partido (disponibilidad) |
| `…/verletzungen/spieler/{id}` | HTML | Historial de lesiones |

Config actual: `WAIT_SECONDS = 4.0`, `PROXY_ENABLED = False` (Transfermarkt no
bloquea con UA de navegador + espera de 4 s; verificado).

### Conteo de peticiones (Transfermarkt)

Por competición-temporada: 1 (clubes) + 20 (kader, uno por club) + **5 × jugadores**.
Con ~560 jugadores (20 clubes × ~28): 1 + 20 + 2.800 ≈ **~2.820 peticiones**, a
4 s ≈ **~3 horas** en directo. El backfill de 5 temporadas multiplica por 5.

Tiene ya un mecanismo de reanudación que Biwenger **no** tiene:
`--since-days N` salta a los jugadores cuya última descarga en `raw/` sea más
reciente que N días (usa la petición de lesiones —la última por jugador— como
marca de "descarga completa"). Permite backfill por tandas sin re-scrapear.

---

## 4. SofaScore + FotMob — DISEÑADO, NO IMPLEMENTADO

No hay código todavía (`src/lfdata/sources/` no tiene `sofascore/` ni `fotmob/`).
Diseño en `docs/implementation/03-sofascore-fotmob-y-backfill.md`. Resumen para
que el optimizador lo tenga en cuenta:

- **SofaScore primaria, FotMob redundancia** (ADR 0002). API JSON
  (`api/v1/...`). Endpoints previstos: búsqueda, temporadas del jugador,
  agregado por temporada (115 campos), nota por partido, y para backfill masivo
  calendario del torneo → alineaciones (`event/{id}/lineups`) → stats por jugador.
- **Volumen del backfill previsto:** ~380 partidos × 6 ligas × 5 temporadas ≈
  11.400 partidos, a ~2 peticiones/partido y 3 s ≈ **~19 h por temporada de las 6
  ligas**. Reanudable (lo que ya está en `raw/` no se re-descarga).
- **ScrapeOps aquí:** apagado por defecto; se enciende si SofaScore empieza a
  devolver 403/429 sostenidos o para paralelizar un backfill grande.

---

## 5. ScrapeOps: por qué, y límites

- **Qué resuelve:** rotación de IPs (y resolución de retos Cloudflare) para pasar
  cuotas por IP. Hoy es imprescindible en Biwenger (cuota ~200/ventana);
  previsto como escape para SofaScore.
- **Plan gratuito:** ~**1.000 créditos/mes**. 1 petición = 1 crédito (los
  reintentos también cuentan).
- **Implicación directa:** una sola temporada de Biwenger (~635) casi agota el
  plan free. El backfill completo (Biwenger ~6.200 + eventual SofaScore ~decenas
  de miles) **no cabe** en el plan gratuito. Opciones: plan de pago, o reducir
  peticiones, o combinar proxy (solo cuando hace falta) con vía directa.

---

## 6. Dónde está el margen de optimización (el encargo)

Ordenado por impacto estimado:

1. **Biwenger no tiene reanudación tipo `--since-days`.** Un run que se corta (o
   la cuota) obliga a repetir los 634 desde cero, malgastando créditos y tiempo.
   Portar el patrón de Transfermarkt (`_scraped_within` sobre `raw/`) a Biwenger
   permitiría backfill por tandas de ~180 y reanudar sin re-scrapear. **Alto
   impacto, bajo riesgo.**
2. **La latencia del proxy domina el tiempo** (~5–37 s/petición), no el
   `wait_seconds`. Con proxy, el ritmo de 2 s casi no importa. Paralelizar las
   peticiones de reports (que son independientes por jugador) recortaría horas —
   ojo a no disparar el consumo ni provocar bloqueos.
3. **¿Se puede evitar el proxy en Biwenger?** El bloqueo es a las ~200/ventana.
   Un run directo en tandas de ~180 espaciadas (esperando a que la ventana se
   reponga) evitaría gastar créditos ScrapeOps por completo. Compensa medir la
   duración real de la ventana (¿por hora?, ¿por día?) — no está caracterizada.
4. **Reducir campos / payload.** El `fields=*,reports(...),prices,seasons` de
   Biwenger trae mucho; si la webapp/modelos no usan todo, pedir menos aligera la
   respuesta (y la latencia por proxy, que escala con el tamaño).
5. **Reintentos y timeout como coste.** Cada 429/timeout cuesta hasta 4 créditos.
   Afinar `max_retries`/backoff y detectar bloqueo persistente antes (para no
   reintentar contra una IP ya quemada) ahorra créditos.

### Contrato que NO se puede romper

- Toda respuesta se escribe en `raw/` **antes** de interpretarla (fuente de
  verdad reproducible; ADR 0003). Optimizar no debe saltarse esto.
- Validación Pydantic: si la fuente cambia de forma, se falla explícito y no se
  escribe una tabla curada a medias.
- Idempotencia: la ingesta usa `upsert_table` por jugador; reprocesar el mismo
  lote deja las tablas igual. Cualquier paralelización debe preservarlo.

---

## 7. Estado del run actual (contexto)

- La Liga 2025 por proxy en curso el 2026-07-10. Antes de arreglar el timeout
  fallaba en la 1ª petición; ahora avanza limpio, 0 saltados, ~2–3 h estimadas.
- Datos ya en S3 (`s3://lfdata-data-593760774245`): plantilla completa (634
  jugadores, 20 equipos) y reports parciales de intentos previos que el upsert
  irá completando.

Archivos de referencia: `src/lfdata/sources/http.py`,
`src/lfdata/sources/biwenger/`, `src/lfdata/sources/transfermarkt/`,
`docs/implementation/01`, `02`, `03`, `docs/adr/0002`, `0003`.
