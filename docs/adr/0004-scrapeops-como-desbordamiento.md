# ScrapeOps como desbordamiento, no como vía principal

Biwenger corta con 429 sostenido a las ~200 peticiones por ventana e IP (comprobado el 2026-07-10; la ventana dura horas, ir más lento no la evita). ScrapeOps la sortea rotando IPs, pero cada petición cuesta 1 crédito y el plan gratuito (~1.000/mes) no cubre ni una temporada de Biwenger. Decidimos que el proxy sea **desbordamiento**: toda petición va directa (gratis) y solo se conmuta a proxy cuando la fuente confirma un bloqueo persistente (429/403 tras el primer reintento — no se queman los 3 reintentos contra una IP cuya ventana tarda horas en reponerse).

## Considered Options

- **Proxy siempre activo por fuente** (lo que hacía `PROXY_ENABLED = True`): simple, pero ~1.300+ créditos/mes solo con el incremental de La Liga → plan de pago permanente, y añade 5-37 s de latencia a cada petición sin necesidad.
- **Solo tandas directas espaciadas** bajo la cuota: coste 0 pero exige caracterizar la duración de la ventana y retrasa los datos una ventana entera; queda como optimización (si la sonda post-429 demuestra ventana corta, el desbordamiento tiende a 0 créditos solo). La sonda existe: `lfdata probe biwenger-quota` (issue #54, ver docs/handoff-scraping.md §6.3) mide la ventana lanzando una petición ligera por hora, siempre directa, hasta el primer 200. **Conclusión tras la primera ejecución real: _(pendiente de rellenar con la duración medida y la decisión resultante)_.**

## Consequences

- El incremental de temporada cabe en el free tier (~300-400 créditos/mes en el peor caso: ~80 detalles de desbordamiento × 4-5 jornadas).
- Los backfills grandes (Biwenger histórico + SofaScore) no caben en el free tier: se contrata **un mes de plan de pago puntual**, sincronizado para lanzar ambos backfills en paralelo dentro del mismo mes.
- Paralelizar peticiones solo tiene sentido en modo proxy (rotación de IP); en directo aceleraría el agotamiento de la ventana.
