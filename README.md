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

```bash
docker build -t mcp-drift-monitor:local .
docker run -v mcp-data:/app/data --name drift-monitor-prod -d \
  mcp-drift-monitor:local sweep
```

## Uso

### API Python

```python
from mcp_drift_monitor.core.poller import Poller, PollConfig
from mcp_drift_monitor.core.sweep import run_sweep
from mcp_drift_monitor.core.state import StateStore
from mcp_drift_monitor.core.calibrate import replay

# Barrido en vivo sobre un feed MCP
poller = Poller(PollConfig(feed_url="https://registry.example/servers"))
state = StateStore("drift.sqlite")
report = run_sweep(
    poller, state,
    on_drift=lambda d: print(f"Cambio detectado: {d.server_id}")
)

# Replay offline contra el panel del paper
r = replay("data/mcp_registry_drift_panel_v1.jsonl")
print(f"Eventos de drift: {r.drift_events}")
print(f"Nuevas adiciones: {r.arrival_events}")
print(f"Eliminaciones: {r.removal_events}")
```

### CLI

```bash
# Barrido completo (control primario)
mcp-drift-monitor sweep

# Polling incremental
mcp-drift-monitor poll --feed-url https://registry.example/servers

# Replay de calibración
mcp-drift-monitor replay --panel data/mcp_registry_drift_panel_v1.jsonl
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
verbatim desde https://www.gnu.org/licenses/agpl-3.0.txt).

> **Nota sobre AGPL §13:** Al ser AGPL, si ejecutas esta herramienta como un
> servicio de red, debes poner a disposición del público el código fuente de
> tu versión modificada a quienes interactúan con ella de forma remota.

## Cita

Si utilizas este software en investigación, por favor cita el paper subyacente:

> arXiv:2608.00997 — *MCP Registry Drift: A 88.6-Day Measurement of 19,099 Servers*

```
@misc{sordo2026mcp-drift-monitor,
  title={mcp-drift-monitor: Continuous MCP Registry Drift Monitor},
  author={Sordo Mart{\\\\'i}nez, Pedro},
  year={2026},
  url={https://github.com/amurlaniakea/mcp-drift-monitor},
  license={AGPL-3.0-or-later}
}
```