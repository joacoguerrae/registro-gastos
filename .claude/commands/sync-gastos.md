---
description: Trae los mails financieros nuevos de Gmail, los procesa al ledger y regenera los agregados del dashboard
---

Sincronizá el ledger de gastos con Gmail. Contratos: docs/pipeline-mails.md y docs/esquema.md.

1. Leé `config/estado_sync.json` → `ultima_fecha_mail`. La fecha de búsqueda es esa menos 2 días (formato Gmail `after:YYYY/MM/DD`). Si el archivo no existe, usá `after:2026/06/30`.
2. Cargá las herramientas Gmail con ToolSearch usando la query `+gmail search_threads get_thread` (el nombre del servidor varía según el contexto: en el escritorio es `mcp__98094892-...`, en CLI headless es `mcp__claude_ai_Gmail__...` — usá las que aparezcan). Si el servidor Gmail no está disponible o el permiso es denegado, reportá "Gmail no disponible — se recupera en la próxima sesión interactiva" y terminá sin error: el watermark garantiza que no se pierde nada.
3. Corré las 4 búsquedas del contrato (sección "Fetch" de docs/pipeline-mails.md) con el `after:` calculado, paginando con pageToken hasta agotar. Juntá los **thread IDs** que devuelvan.
   **CRÍTICO — enumerar por thread completo, NO por la lista de search_threads:** `search_threads` NO devuelve todos los mensajes de un thread (Gmail agrupa muchos avisos "uso de tarjeta" en un mismo thread y la búsqueda solo trae un subconjunto → así se perdieron mails el 13-14/7, incluido un cargo de $1.560). Por cada thread ID, llamá `get_thread` (messageFormat MINIMAL) para listar TODOS sus mensajes. Recién sobre esa lista completa decidí qué bajar.
4. Para CADA mensaje financiero que NO esté ya en `data/inbox_raw/` (comparar por message_id): llamá `get_message` FULL_CONTENT y guardalo como `data/inbox_raw/<message_id>.json` con el esquema {message_id, thread_id, fecha_mail, from, subject, body_text}. Incluí avisos de tarjeta débito/crédito, transferencias, recibos y facturas. IGNORÁ (no son transacciones): "Aviso de monitoreo de umbral", "Aviso: rechazo de tarjeta" (rechazo = no se concretó), marketing.
5. Ejecutá EXACTAMENTE este comando (único punto de entrada — corre Splitwise, procesar_mails, validación, dashboard, actualiza Sheets y commit): `cmd /c scripts\correr_pipeline.cmd` — y leé su salida completa. No corras los scripts de Python por separado.
6. Si la validación reporta ERROR o DESVIO nuevo (distinto del baseline 2c documentado), decilo en el reporte.
7. Reportá: mails nuevos por tipo, filas insertadas por cuenta/categoría, pendientes nuevos por motivo, y gasto acumulado del mes en curso vs presupuesto. Los 3 Google Sheets se refrescan solos dentro de correr_pipeline.cmd (actualizar_sheets.py).
