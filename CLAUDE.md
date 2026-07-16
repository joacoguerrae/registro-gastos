# Guía de sesión — sistema de gastos

Ledger único de gastos (personal + casa con Lu + Splitwise). Diseño: [docs/esquema.md](docs/esquema.md) · README: [README.md](README.md).

## Al iniciar cada sesión

**Hacé `git pull` antes de cualquier análisis o edición.** El sync automático corre en la nube (GitHub Actions) y commitea los datos nuevos ahí, así que el repo local puede estar atrás. Si respondés preguntas de gastos con datos sin actualizar, van a estar incompletos.

## Modelo operativo

- **Fetch + pipeline automáticos:** GitHub Actions 2×/día (`.github/workflows/sync.yml`) → `fetch_mails.py` (Gmail API determinístico) → `sync_splitwise` → `procesar_mails` → `merge_y_validar` → `cierre_casa` (refresca la posición) → dashboards (`out/dashboard.html`) → Sheets → commit. Si quedan pendientes nuevos, abre un Issue (te llega mail).
- **Estado (2026-07-16):** migración en **semana shadow** — Actions corre en `shadow` (valida, no commitea) y la PC sigue siendo la dueña vía las tareas de Windows "Gastos Sync AM/PM" (9:23 y 21:23), que desde el 16/7 corren `scripts\sync_local.cmd`: **100% determinístico** (pull → `fetch_mails.py` → pipeline → commit → **push**). Claude ya no participa del fetch ni localmente. Al cutover se flipa Actions a `live` y se deshabilitan las tareas. Plan: [docs/plan-migracion-fetch.md](docs/plan-migracion-fetch.md).
- **Corrida manual local:** `scripts\sync_local.cmd` (todo: pull, fetch, pipeline, push). Solo el pipeline sin fetch: `scripts\correr_pipeline.cmd`. ⚠️ **`cmd /c` desde la tool Bash NO ejecuta los .cmd** (abre shell interactiva y cuelga): correrlos con la tool PowerShell → `& "scripts\sync_local.cmd"`.
- **Claude NO está en el camino crítico del fetch** (era la causa de los 4 incidentes de julio: mails perdidos, crash por layouts, timeout, dependencia de la PC). Claude es para lo que requiere criterio: `/revisar-pendientes`, `/cierre-mes`, análisis.

## Invariantes (no romper)

- **Correcciones persistentes → `data/resoluciones.csv`** (campo/valor por `id_ledger`), nunca editar `ledger.csv` a mano: se regenera. Sobreviven a `--reprocesar`.
- **`en_analisis`** = anti-doble-conteo. Filas `en_analisis=0` no cuentan en ningún agregado.
- **Paridad del fetch:** `fetch_mails.py` debe producir el mismo `body_text` que consumen los parsers de `procesar_mails.py`. Cambios ahí se validan re-bajando por API y comparando filas `mail` (test en [docs/validacion-migracion.md](docs/validacion-migracion.md)).
- **Split (gasto compartido):** fila original ajustada a la fracción propia + fila hermana `transferencia` a Deudas por el resto; la suma = cargo real de la tarjeta.
- **Secretos:** nunca commitear `config/secrets.local.json`, `config/gcp_service_account.json`, `config/gmail_token.json`, `config/oauth_client.json` (todos gitignored). En Actions van como repository secrets.

## Comandos

- `/sync-gastos` — trae mails y procesa (interactivo; el automático lo hace Actions).
- `/revisar-pendientes` — revisión semanal de la bandeja.
- `/cierre-mes` — ceremonia mensual (ingresos, aportes de Lu, saldos, cierre de casa).
