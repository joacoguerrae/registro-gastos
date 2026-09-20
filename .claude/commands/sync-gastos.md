---
description: Trae los mails financieros nuevos de Gmail (fetch determinístico), los procesa al ledger y regenera los agregados del dashboard
---

Sincronizá el ledger de gastos con Gmail. Contratos: docs/pipeline-mails.md y docs/esquema.md.

> El sync automático corre solo, en GitHub Actions 2×/día (`.github/workflows/sync.yml`). Este comando es para cuando Joaco quiere el dato AHORA sin esperar al cron. Hace lo mismo que Actions, localmente y con reporte interactivo.

## Flujo (determinístico — sin Claude en el fetch)

1. `git pull` (regla de CLAUDE.md: los commits nacen en la nube).
2. **Fetch:** corré con la tool Bash `python scripts/fetch_mails.py` y leé su salida (usa `config/gmail_token.json`; enumera por MENSAJE con watermark − 2 días; también baja los PDF de estados de cuenta). No armes búsquedas de Gmail a mano: el script ES el contrato del fetch.
3. **Pipeline:** corré `scripts\correr_pipeline.cmd` **con la tool PowerShell**: `& "scripts\correr_pipeline.cmd"` — ⚠️ NUNCA `cmd /c` desde la tool Bash: abre una shell interactiva y cuelga hasta el timeout (gotcha documentado en CLAUDE.md). Leé la salida completa: corre procesar_mails → Splitwise → merge_y_validar → cierre_casa → dashboards → Sheets → commit.
4. Si `merge_y_validar` falla (sale ≠0), el pipeline se detiene y NO commitea: reportá el invariante roto tal cual lo imprime — no lo "arregles" tocando datos; las correcciones van por `data/resoluciones.csv` (ver `/revisar-pendientes`).
5. `git push` (el pipeline commitea local pero no pushea; sin push, GitHub y las corridas de Actions quedan atrás).
6. Reportá: mails nuevos por tipo, filas insertadas por cuenta/categoría, pendientes nuevos por motivo (los `formato_nuevo` son mails que el parser no supo leer — mencionalos explícitamente), y gasto acumulado del mes en curso vs presupuesto.

## Fallback — SOLO si `fetch_mails.py` falla

(API caída, token vencido — el error dice cuál.) Reportá primero el error: casi siempre conviene arreglar la causa (`scripts/get_gmail_token.py` regenera el token) antes que el fallback. Si Joaco quiere seguir igual con el conector Gmail de Claude:

- Buscá con las herramientas Gmail del contexto (ToolSearch `+gmail search_threads get_thread`) usando `after:` = `ultima_fecha_mail` de `config/estado_sync.json` menos 2 días.
- **CRÍTICO — enumerar por thread completo, NO por la lista de `search_threads`:** Gmail agrupa avisos en threads y la búsqueda solo trae un subconjunto (así se perdieron mails el 13-14/7). Por cada thread ID, `get_thread` (MINIMAL) para listar TODOS sus mensajes; recién sobre esa lista decidí qué bajar.
- Guardá cada mensaje financiero nuevo como `data/inbox_raw/<message_id>.json` con el esquema `{message_id, thread_id, fecha_mail, from, subject, body_text}`. IGNORÁ marketing, "Aviso de monitoreo de umbral" y "Aviso: rechazo de tarjeta" (rechazo = no se concretó).
- ⚠️ El `body_text` que renderiza el conector NO es idéntico al de `fetch_mails.py` (paridad = invariante de CLAUDE.md): los parsers pueden dejar esos mails en pendientes. Avisale a Joaco que la próxima corrida por API los reprocesa bien (la idempotencia por `message_id` evita duplicados... los archivos ya guardados NO se re-bajan). Si un mail del fallback quedó mal parseado, borrá su JSON de `inbox_raw/` para que la API lo re-baje limpio.
- Seguí desde el paso 3 del flujo normal.
