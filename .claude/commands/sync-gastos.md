---
description: Trae los mails financieros nuevos de Gmail, los procesa al ledger y regenera los agregados del dashboard
---

Sincronizá el ledger de gastos con Gmail. Contratos: docs/pipeline-mails.md y docs/esquema.md.

1. Leé `config/estado_sync.json` → `ultima_fecha_mail`. La fecha de búsqueda es esa menos 2 días (formato Gmail `after:YYYY/MM/DD`). Si el archivo no existe, usá `after:2026/06/30`.
2. Cargá las herramientas Gmail con ToolSearch usando la query `+gmail search_threads get_thread` (el nombre del servidor varía según el contexto: en el escritorio es `mcp__98094892-...`, en CLI headless es `mcp__claude_ai_Gmail__...` — usá las que aparezcan). Si el servidor Gmail no está disponible o el permiso es denegado, reportá "Gmail no disponible — se recupera en la próxima sesión interactiva" y terminá sin error: el watermark garantiza que no se pierde nada.
3. Corré las 4 búsquedas del contrato (sección "Fetch" de docs/pipeline-mails.md) con el `after:` calculado, iterando MENSAJES (no threads) y paginando hasta agotar. Guardá cada mensaje nuevo como `data/inbox_raw/<message_id>.json` (los ya existentes en disco, salteálos). Sin marketing.
4. Ejecutá EXACTAMENTE este comando (único punto de entrada — corre procesar_mails, datos de dashboard, validación y commit): `cmd /c scripts\correr_pipeline.cmd` — y leé su salida completa. No corras los scripts de Python por separado.
5. Si la validación reporta ERROR o DESVIO nuevo (distinto del baseline 2c documentado), decilo en el reporte.
6. Reportá: mails nuevos por tipo, filas insertadas por cuenta/categoría, pendientes nuevos por motivo, y gasto acumulado del mes en curso vs presupuesto (data/presupuesto.csv del mes). Los Google Sheets del dashboard NO se tocan desde acá (refresh manual hasta Fase 3).
