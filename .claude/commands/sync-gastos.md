---
description: Trae los mails financieros nuevos de Gmail, los procesa al ledger y regenera los agregados del dashboard
---

Sincronizá el ledger de gastos con Gmail. Contratos: docs/pipeline-mails.md y docs/esquema.md.

1. Leé `config/estado_sync.json` → `ultima_fecha_mail`. La fecha de búsqueda es esa menos 2 días (formato Gmail `after:YYYY/MM/DD`). Si el archivo no existe, usá `after:2026/06/30`.
2. Cargá las herramientas Gmail con ToolSearch (`select:mcp__98094892-569b-4b30-a137-22234d347a0f__search_threads,mcp__98094892-569b-4b30-a137-22234d347a0f__get_thread`). Si el servidor Gmail no está disponible (sesión headless), reportá "Gmail no disponible — se recupera en la próxima sesión interactiva" y terminá sin error: el watermark garantiza que no se pierde nada.
3. Corré las 4 búsquedas del contrato (sección "Fetch" de docs/pipeline-mails.md) con el `after:` calculado, iterando MENSAJES (no threads) y paginando hasta agotar. Guardá cada mensaje nuevo como `data/inbox_raw/<message_id>.json` (los ya existentes en disco, salteálos). Sin marketing.
4. Ejecutá `C:\Users\joaco\anaconda3\python.exe scripts\procesar_mails.py` y después `scripts\generar_dashboard_data.py`. Si hay filas nuevas, corré también `scripts\merge_y_validar.py` y confirmá que las validaciones den OK.
5. Actualizá los Google Sheets del dashboard SOLO si hubo filas nuevas: recreá el contenido de `gastos_resumen_mensual` y `gastos_presupuesto_vs_real` (el MCP de Drive no edita archivos: creá archivos nuevos con sufijo de fecha SOLO si el usuario lo pidió; por defecto, avisá que los Sheets quedaron desactualizados y que el refresh es manual o esperá a que exista la vía de escritura de Fase 3).
6. Commit de los cambios en data/ con mensaje "sync mails <fecha>: N filas nuevas, M pendientes".
7. Reportá: mails nuevos por tipo, filas insertadas por cuenta/categoría, pendientes nuevos por motivo, y gasto acumulado del mes en curso vs presupuesto (data/presupuesto.csv del mes).
