# mcp-drift-monitor

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.txt)
[![Docker](https://img.shields.io/badge/Docker-mcp--drift--monitor%3Alocal-blue?logo=docker)](https://github.com/amurlaniakea/mcp-drift-monitor)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://www.python.org/)

## ¿Qué es y para qué sirve?

**mcp-drift-monitor** es una herramienta de código abierto que detecta cambios no
autorizados en los servidores MCP (Model Context Protocol). Monitorea
continuamente un registro público de servidores MCP y alerta cuando alguno
cambia su descripción, hash de contenido, o desaparece de forma inesperada.

**¿Por qué es necesario?** Según el paper de investigación
[arXiv:2608.00997](https://arxiv.org/abs/2608.00997) (*"MCP Registry Drift:
A 88.6-Day Measurement of 19,099 Servers"*), los enfoques tradicionales de
detección de cambios fallan en identificar dos modos de fallo críticos:

1. **Cambios silenciosos** — un servidor cuyo hash de descripción cambia pero
   el monitor ya lo conocía y lo rankinga por historial pasado.
2. **Nuevas adiciones** — servidores que aparecen en el registro pero el
   monitor no tiene registro previo.

Este monitor implementa el **control primario faltante** descrito en el paper:
un barrido periódico completo del catálogo (**full-catalog sweep**) que
re-descarga todo el registro y recomputa todos los hashes en una sola pasada,
garantizando que ningún cambio pase desapercibido.

### Utilidad concreta

- **Seguridad**: Detecta si un servidor MCP ha sido modificado o reemplazado
  por una versión maliciosa sin que el operador lo note.
- **Integridad de cadenas de suministro**: Verifica que los servidores MCP
  que usas en producción no hayan sido modificados sin autorización.
- **Auditoría de cumplimiento**: Mantiene un historial completo de cambios
  para auditorías de seguridad y cumplimiento.
- **Validación científica**: Calibrado y verificado contra el panel real de
  19,099 servidores del paper (15,845 cambios, 19,877 adiciones, 911
  eliminaciones).

## Características

- **Motor de diferencias único** (`compute_events`) que sirve tanto para polling
  incremental como para barridos completos — sin lógica duplicada.
- **Ordenamiento determinista** (por `server_id`) entre procesos — verificado
  a gran escala contra el panel del paper (36,753 filas) con resultados
  idénticos byte a byte en dos ejecuciones (AC-3).
- **Revalidación de enlaces de contenido**: revalida cada vez que cualquier
  hash de descripción cambia — `len(drifts) > 0` es el único disparador.
- **Gestión de límites de velocidad**: backoff de 429 + `Retry-After`;
  agotamiento de reintentos → `FetchStatus.FAILED` (nunca una lista vacía
  silenciosa tratada como "OK").
- **Protección contra desviaciones de esquema**: sobres malformados lanzan
  `SchemaDriftError` y registran el payload ofensor a nivel ERROR (NFR-3).
- **Calibrado contra el paper** (AC-1/2/3): resultados verificados, no
  simples afirmaciones.
- **Licencia AGPL-3.0-or-later** (SPDX: `AGPL-3.0-or-later`).

## Instalación

### Como librería (recomendado)

```bash
git clone https://github.com/amurlaniakea/mcp-drift-monitor.git
cd mcp-drift-monitor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
```

### Con Docker

El panel de calibración (`data/`) viene **embebido en la imagen**, así que
`replay` funciona sin pasos extra. Si prefieres usar un panel más nuevo sin
reconstruir, monta tu propio directorio sobre `/app/data` al arrancar.

**Importante sobre la persistencia del estado:** el flag `--db` por defecto es
`drift.sqlite` (relativo → `/app/drift.sqlite`), que queda **FUERA** del volumen
`/app/data` y se pierde al reiniciar el contenedor. Para que el estado sobreviva,
pasa siempre `--db /app/data/drift.sqlite`.

```bash
# Construir la imagen local
docker build -t mcp-drift-monitor:local .

# Barrido completo (CONTROL PRIMARIO). --feed-url es OBLIGATORIO y el estado
# persiste en el volumen gracias a --db /app/data/drift.sqlite.
docker run -v mcp-data:/app/data --name drift-monitor-prod -d \
  mcp-drift-monitor:local sweep \
  --feed-url https://registry.example/servers \
  --db /app/data/drift.sqlite

# Replay de calibración (panel ya embebido en /app/data)
docker run --rm mcp-drift-monitor:local \
  replay --panel-path data/mcp_registry_drift_panel_v1.jsonl

# Para usar tu propio panel sin reconstruir la imagen, monta tu dir sobre /app/data:
docker run --rm -v /ruta/a/mi/panel:/app/data mcp-drift-monitor:local \
  replay --panel-path data/mcp_registry_drift_panel_v1.jsonl
```

## Uso

### API Python (bajo nivel)

`run_sweep` / `compute_events` requieren un `obs_index` y un `ts` reales (no
`0` / `""`): el índice de observación sale de `state.get_next_obs_index()` y el
timestamp de `datetime.now(UTC)`. Este es el MISMO patrón que usa `cli.py` — no
lo omitas en tu propio código, o los eventos quedarán sin `obs_index`/`ts`.

```python
from datetime import UTC, datetime

from mcp_drift_monitor.core.poller import Poller, PollConfig
from mcp_drift_monitor.core.sweep import run_sweep
from mcp_drift_monitor.core.state import StateStore
from mcp_drift_monitor.core.calibrate import replay

# Barrido en vivo sobre un feed MCP
poller = Poller(PollConfig(feed_url="https://registry.example/servers"))
state = StateStore("drift.sqlite")

# índice de observación MONÓTONO + timestamp UTC REALES (NFR-1, FR-5).
obs_index = state.get_next_obs_index()
ts = datetime.now(UTC).isoformat()
report = run_sweep(
    poller, state,
    on_drift=lambda d: print(f"Cambio detectado: {d.server_id}"),
    obs_index=obs_index, ts=ts,
)
print(f"fetch_status={report.fetch_status.value} "
      f"drifts={report.drift_count} arrivals={report.arrival_count} "
      f"removals={report.removal_count}")

# Replay offline contra el panel del paper
r = replay("data/mcp_registry_drift_panel_v1.jsonl")
print(f"Eventos de drift: {r.drift_events}")
print(f"Nuevas adiciones: {r.arrival_events}")
print(f"Eliminaciones: {r.removal_events}")
```

### CLI (entregable principal de T7)

El CLI es la interfaz recomendada. Todos los subcomandos aceptan `--sink`
(`stdout` por defecto, o `file:<ruta>` para volcar salida NDJSON a disco).

```bash
# Barrido completo del catálogo — CONTROL PRIMARIO (FR-4)
mcp-drift-monitor sweep --feed-url https://registry.example/servers

# Polling incremental (un único fetch contra el feed)
mcp-drift-monitor poll --feed-url https://registry.example/servers

# Replay de calibración contra el panel real del paper (FR-6, AC-1/2/3)
mcp-drift-monitor replay --panel-path data/mcp_registry_drift_panel_v1.jsonl

# Placeholder del watcher en vivo (aún no implementado; usa poll/sweep)
mcp-drift-monitor serve
```

Enrutar la salida a un archivo con `--sink` (la salida es NDJSON, una línea por evento):

```bash
# Volcar drift/arrival/removal del barrido a un log persistente
mcp-drift-monitor sweep --feed-url https://registry.example/servers \
  --sink file:drift-events.ndjson

# Lo mismo para polling incremental
mcp-drift-monitor poll --feed-url https://registry.example/servers \
  --sink file:poll-events.ndjson

# Replay a archivo
mcp-drift-monitor replay --panel-path data/mcp_registry_drift_panel_v1.jsonl \
  --sink file:replay-report.ndjson
```

**En Docker (usuario non-root `appuser`):** el destino de `--sink file:` debe ser
un path escribible por `appuser`. Usa el volumen de datos y apunta bajo `/app/data/`,
igual que con `--db`:

```bash
# En el contenedor, escribir el sink bajo /app/data (ya chown appuser):
docker run --rm -v mcp-data:/app/data mcp-drift-monitor:local \
  sweep --feed-url https://registry.example/servers \
  --db /app/data/drift.sqlite \
  --sink file:/app/data/drift-events.ndjson

docker run --rm -v mcp-data:/app/data mcp-drift-monitor:local \
  replay --panel-path data/mcp_registry_drift_panel_v1.jsonl \
  --sink file:/app/data/replay-report.ndjson
```

Cada evento emitido lleva `obs_index` y `ts` reales (UTC), p. ej.:

```json
{"event": "drift", "server_id": "srv_123", "old_desc_hash": "a1b2", "new_desc_hash": "c3d4", "obs_index": 7, "ts": "2026-08-23T10:15:32.481027+00:00"}
```

## Tests

```bash
ruff check mcp_drift_monitor tests && pytest -v
```

Suit de tests:
- `tests/test_diff.py` — motor de diferencias (determinismo, orden, disjunción de eventos).
- `tests/test_hasher.py` — normalización NFC + casos límite de hashing.
- `tests/test_poller.py` — backoff 429, agotamiento de reintentos, registro de desviaciones de esquema.
- `tests/test_state.py` — persistencia de StateStore, flag eliminado, dataclass fuente única.
- `tests/test_sweep.py` — control primario de barrido completo (nueva adición, cambio silencioso).
- `tests/test_replay_panel.py` — [EXTERNAL-VALIDITY] AC-1/2/3 contra el panel real del paper.

## Arquitectura

```
core/
  diff.py      — CatalogEntry, DriftEvent, NewArrivalEvent, RemovalEvent, compute_events
  hasher.py    — normalize_description (NFC), hash_description
  state.py     — StateStore (sqlite), FetchStatus, flag 'removed', get_all_hashes
  poller.py    — Poller.fetch_catalog, PollConfig, SchemaDriftError, backoff
  sweep.py     — run_sweep (control primario), SweepReport
  calibrate.py — replay (FR-6), ReplayReport, validación externa vs panel
```

## Calibration (FR-6)

`calibrate.replay(panel_path)` reconstruye el catálogo por snapshot de
observación a partir del flujo de adiciones/cambios/eliminaciones del panel y
ejecuta `compute_events` sobre cada transición adyacente. El campo `d` del
panel es un **token hash opaco** (no reinterpretado como sha256).

Resultados verificados:

| Evento del panel | Recuento | compute_events (neto) |
|-------------------|----------|-----------------------|
| `add`             | 19,877   | 16,367 + 3,510 seed = 19,877 ✓ |
| `chg`             | 15,845   | 2,514 drift netos (≤ chg) ✓ |
| `del`             | 911      | 911 eliminaciones ✓ |

`arrival_events == add_count` y `removal_events == del_count` se cumplen para
este panel porque **ningún servidor es eliminado y re-agregado dentro del
mismo intervalo inter-snapshot** (0 violaciones de precondición). Un panel
futuro que viole esto fallaría en la prueba de precondición de forma
explícita en lugar de producir silenciosamente KPIs incorrectos.

## Referencia del paper

- **Título**: *MCP Registry Drift: A 88.6-Day Measurement of 19,099 Servers*
- **arXiv**: [arXiv:2608.00997](https://arxiv.org/abs/2608.00997)
- **Autores**: Pedro Sordo Martínez (amurlaniakea)

## Licencia

Este programa es software libre: puedes redistribuirlo y/o modificarlo bajo
los términos de la Licencia GNU Affero General Public License tal como la
publica la Free Software Foundation, ya sea la versión 3 de la Licencia, o
(con tu opción) cualquier versión posterior.

SPDX-License-Identifier: AGPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>

Consulta [LICENSE](LICENSE) para el texto completo (661 líneas, texto
verbatim oficial extraído de <https://www.gnu.org/licenses/agpl-3.0.txt>).

> **Nota sobre AGPL §13:** Al ser AGPL, si ejecutas esta herramienta como un
> servicio de red, debes poner a disposición del público el código fuente de
> tu versión modificada a quienes interactúan con ella de forma remota.

## Cita

Si utilizas este software en investigación, por favor cita el paper subyacente:

> arXiv:2608.00997 — *MCP Registry Drift: A 88.6-Day Measurement of 19,099 Servers*

```
@misc{sordo2026mcp-drift-monitor,
  title={mcp-drift-monitor: Continuous MCP Registry Drift Monitor},
  author={Sordo Martínez, Pedro},
  year={2026},
  url={https://github.com/amurlaniakea/mcp-drift-monitor},
  license={AGPL-3.0-or-later}
}
```