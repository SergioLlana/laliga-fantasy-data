# La Liga Fantasy Data

Plataforma abierta de datos y proyecciones para fantasy de La Liga (Biwenger primero): proyecciones de puntos, recomendaciones de mercado y optimización de alineación.

## Instalación

Requiere [uv](https://docs.astral.sh/uv/) y Python ≥ 3.12.

```bash
git clone https://github.com/SergioLlana/laliga-fantasy-data.git
cd laliga-fantasy-data
uv sync --dev
```

## Uso

```bash
uv run lfdata --help
```

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

La documentación del proyecto vive en `docs/`: el plan general en `docs/plan.md`, los planes de implementación en `docs/implementation/` y las decisiones de arquitectura en `docs/adr/`. El lenguaje del dominio está en `CONTEXT.md`.
