# Guía de sesión — sistema de gastos

Ledger único de gastos (personal + casa con Lu + Splitwise). Diseño: [docs/esquema.md](docs/esquema.md) · README: [README.md](README.md).

## Al iniciar cada sesión

**Hacé `git pull` antes de cualquier análisis o edición.** El sync automático corre en la nube (GitHub Actions) y commitea los datos nuevos ahí, así que el repo local puede estar atrás. Si respondés preguntas de gastos con datos sin actualizar, van a estar incompletos.

## Modelo operativo

- **Fetch + pipeline automáticos:** GitHub Actions 2×/día (`.github/workflows/sync.yml`) → `fetch_mails.py` (Gmail API determinístico) → `sync_splitwise` → `procesar_mails` → `merge_y_validar` → `cierre_casa` (refresca la posición) → dashboards (`out/dashboard.html`) → Sheets → commit. Si quedan pendientes nuevos, abre un Issue (te llega mail).
- **Estado: migración CERRADA (cutover 2026-07-16).** Actions es el **único dueño** del pipeline y corre en `live` siempre por cron (no depende de ninguna variable de repo). Las tareas de Windows "Gastos Sync AM/PM" quedaron **deshabilitadas** (no borradas: son el rollback). La PC no participa más del sync automático. Plan y bitácora: [docs/plan-migracion-fetch.md](docs/plan-migracion-fetch.md).
- **Si el sync está caído:** revisá Actions → "Sync gastos". Para pausarlo, deshabilitá el workflow desde la UI (se ve); no hay switch por variable a propósito, porque dejaría de commitear en silencio. Rollback completo: `Enable-ScheduledTask -TaskName "Gastos Sync AM"` (y `PM`), que vuelven a correr `scripts\sync_local.cmd` desde la PC.
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
