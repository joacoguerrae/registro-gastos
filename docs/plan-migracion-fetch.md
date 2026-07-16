# Plan de migración: fetch determinístico + GitHub Actions

**Fecha:** 15/07/2026 · **Estado:** en ejecución (Fases 0-3 hechas, semana shadow por arrancar)

## Estado de ejecución

- [x] **Fase 0** — repo privado ✓; auditoría de secretos en historial (limpio) ✓; OAuth Gmail en producción + refresh token ✓; 5 secrets en GitHub ✓; inventario (MyFinance.db y casa.xlsx ya en repo) ✓.
- [x] **Fase 1** — `scripts/fetch_mails.py` (Gmail API, enumeración por mensaje, `html_a_texto` determinístico, idempotente, fail-safe) + `config/reglas_fetch.csv` + `scripts/get_gmail_token.py`.
- [x] **Fase 2** — parsers de `procesar_mails.py` tolerantes al formato nuevo. **GATE pasado:** pipeline completo sobre los 64 mails re-bajados por API → **57/57 filas `mail` idénticas, 0 diffs reales** (único delta: padding de espacios en el `comercio` crudo, cosmético; descripción/categoría/monto/ámbito byte-idénticos). Portabilidad de los 5 scripts (BASE computado + secretos por env var), `requirements.txt`, `.gitattributes`.
- [x] **Fase 3** — `.github/workflows/sync.yml` (cron 2×/día, modo shadow/live, gate de validación, timeout 15min). Pre-flight de los 3 code-paths por env var: verde. Corrida shadow de prueba: **en verificación**.
- [ ] **Fase 4** — semana shadow (≥12 corridas limpias, 0 diffs).
- [ ] **Fase 5** — cutover (flip a live, deshabilitar tareas de Windows, `git pull` en CLAUDE.md ✓ ya documentado).

---


## Motivación — los 4 incidentes que origina el fetch claudizado

| Incidente (julio 2026) | Causa raíz | Decisión de diseño que lo elimina |
|---|---|---|
| Mails perdidos (cargo RedTickets $1.560 y 9 más) | `search_threads` no devuelve todos los mensajes de un thread | Enumerar por **mensaje** (`messages.list`), no por thread |
| Parser crasheó y abortó el pipeline (Yova $1.700) | Claude renderiza el mismo mail en 2 layouts distintos entre corridas | HTML crudo + conversión HTML→texto **determinística y versionada** |
| Corrida AM 15/7 muerta por timeout (fetch >1h) | Decenas de round-trips conversacionales de Claude contra Gmail | Fetch por API directa: segundos, `timeout-minutes: 15` |
| Sistema atado a la PC prendida | Conector Gmail autenticado interactivamente en la sesión local | OAuth con refresh token → corre headless en GitHub Actions |

Claude queda **fuera del camino crítico** y dedicado a lo que requiere criterio: `/revisar-pendientes`, `/cierre-mes`, splits, duplicados, análisis.

## Decisiones tomadas (respuestas de Joaco, 15/7)

- **Cutover:** paralelo 1 semana (Actions en modo shadow, la PC sigue siendo dueña).
- **Frecuencia:** 2x/día como hoy (cron `23 12 * * *` y `23 0 * * *` UTC ≈ 9:23/21:23 UYT; GitHub puede demorar hasta ~15 min, aceptable).
- **Alertas de fallo:** email automático de GitHub al fallar el workflow.
- **Formatos nuevos:** sin Claude en la nube — se bajan igual y quedan pendientes `formato_nuevo` para revisión semanal.

---

## Fase 0 — Prerrequisitos (bloqueantes)

- **0.1 Repo privado** (hace Joaco: Settings → Danger Zone → Change visibility). Detectado 15/7: el repo estaba **público** con datos financieros. GATE: la URL sin autenticación devuelve 404.
- **0.2 Auditoría de secretos en el historial git.** El repo fue público: verificar que la key de Splitwise, el JSON de la service account y cualquier token NUNCA se hayan commiteado (`git log -p` sobre config/, scripts/). Si alguno aparece en el historial → **rotarlo** (no alcanza con borrarlo del HEAD).
- **0.3 OAuth de Gmail** en el proyecto GCP existente (el de la service account de Sheets):
  - Habilitar Gmail API. Crear OAuth client tipo "Desktop app".
  - Pantalla de consentimiento en modo **Producción** (en modo "Testing" el refresh token vence a los 7 días — gotcha conocido). La advertencia de "app no verificada" es aceptable: la app es propia y el único usuario es Joaco.
  - Scope mínimo: `gmail.readonly`.
  - Script one-shot `scripts/get_gmail_token.py`: flujo de consentimiento en el navegador (lo corre Joaco una vez) → imprime el refresh token.
  - *Plan B si el OAuth da problemas:* IMAP + app password (requiere 2FA); Gmail IMAP soporta `X-GM-RAW` para las mismas queries. Menos privilegio-mínimo, pero cero fricción.
- **0.4 Secretos en GitHub** (Settings → Secrets and variables → Actions): `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `SPLITWISE_API_KEY`, `GCP_SA_JSON`.
- **0.5 Inventario de insumos.** Todo lo que lee la fase 2 debe estar en el repo o venir de API: MyFinance.db (validación 2a), snapshot de la planilla histórica (2b/2c), `config/`, presupuestos. GATE: clonar el repo en una carpeta limpia y correr `correr_pipeline` (sin fetch) — debe funcionar sin tocar nada local externo.

## Fase 1 — `scripts/fetch_mails.py` (reemplaza a Claude headless)

- **1.1 Enumeración por mensaje:** `users.messages.list` con las 4 búsquedas del contrato (docs/pipeline-mails.md) + `after:` = watermark − 2 días, paginando hasta agotar. Al enumerar mensajes individuales, la clase entera del bug de `search_threads` (agrupación por thread) desaparece.
- **1.2 Descarga:** por cada message_id que no esté en `inbox_raw/` ni en `procesados.csv` → `messages.get format=full`, extraer la parte `text/html` (fallback `text/plain`).
- **1.3 `html_a_texto()`:** conversión determinística con configuración fija y versionada en el repo. Mismo HTML → mismo texto, en toda corrida, para siempre. (Elimina la clase del bug de los 2 layouts.)
- **1.4 Clasificación por reglas explícitas** (nuevo `config/reglas_fetch.csv`):
  - *Denylist* (no-transacciones, hoy criterio implícito de Claude): "Aviso de monitoreo de umbral", "Aviso: rechazo de tarjeta", marketing/promos (Meli+, etc.).
  - *Allowlist* de tipos conocidos: aviso de tarjeta débito/crédito, transferencia en plaza, recibos, facturas.
  - **Fail-safe:** remitente financiero conocido + ningún match de regla → **se baja igual** marcado `formato_nuevo` → `procesar_mails.py` lo deja como pendiente `formato_nuevo`. Hoy Claude puede descartar en silencio; con esto lo desconocido *siempre* aflora a revisión.
- **1.5 Compatibilidad total:** mismo esquema JSON `{message_id, thread_id, fecha_mail, from, subject, body_text}`, mismo watermark (`config/estado_sync.json`), misma idempotencia (`data/staging/procesados.csv`). Los 52 JSONs históricos de `inbox_raw/` **no se tocan**.
- **1.6 Salida:** resumen de 1 línea (mails nuevos por tipo) para el log y el mensaje de commit.

## Fase 2 — Adaptar `procesar_mails.py`

- **2.1** Los parsers toleran los formatos viejos (layouts A/B de Claude — necesarios para `--reprocesar` el corpus histórico) **más** el formato de `html_a_texto()`.
- **2.2 Regresión offline (GATE):** `--reprocesar` sobre los 52 mails actuales → ledger idéntico bit a bit (`git diff` vacío).
- **2.3 Regresión de paridad de fetch (GATE):** re-fetch por API de los mismos 52 message_ids a un directorio scratch (sin tocar el repo) → `html_a_texto()` → parsear → comparar campo a campo (fecha, tipo, monto, moneda, tarjeta, comercio) contra las filas actuales del ledger. Los IDs son estables porque `fuente_id` = message_id. Criterio: **52/52 OK**.
- **2.4 Suite de casos borde fijos** (todo lo que vimos en julio, como tests):
  - Transferencia layout A y layout B (Yova $1.700).
  - Avisos de tarjeta 8261 / 9071 / 6427 (RedTickets, PedidosYa, Guasones).
  - Rechazo ANTHROPIC USD 5 → **ignorar** (no se concretó).
  - Monitoreo de umbral → ignorar. Marketing Meli+ → ignorar.
  - Duplicados "Proyecto PAS" en el mismo segundo → **entran ambos** y caen a pendiente `posible_duplicado` (el fetch nunca deduplica; decide el sistema de pendientes + Joaco).
  - Factura Antel.

## Fase 3 — Workflow de GitHub Actions

- **3.1 Portabilidad:** `requirements.txt` pinneado; auditar rutas hardcodeadas `C:\`, encoding (cp1252 → UTF-8), y lógica que hoy vive en `.cmd`. Los pasos del pipeline se invocan directo desde el workflow; `correr_pipeline.cmd` queda solo para corridas manuales locales.
- **3.2 `.github/workflows/sync.yml`:** cron 2x/día + `workflow_dispatch` (corridas manuales); `ubuntu-latest`; `timeout-minutes: 15`; `concurrency` para que no se solapen corridas.
- **3.3 Pasos:** checkout → setup-python → pip install → `fetch_mails.py` → splitwise sync → `procesar_mails.py` → `merge_y_validar.py` → `generar_dashboard_data.py` → `actualizar_sheets.py` → `git pull --rebase` + commit + push (solo si hay cambios).
- **3.4 Fallos:** exit ≠ 0 en cualquier paso (incluido ERROR de validación fuera del baseline 2c documentado) → workflow falla → email automático de GitHub a Joaco.
- **3.5 Modo shadow** (para la semana paralela): flag `shadow=true` → **no pushea**. Re-fetch con lookback fijo de 3 días, reconstruye las filas y compara **bidireccional** contra el ledger commiteado por el sistema viejo:
  - Fila que shadow genera y no está en el ledger → el sistema viejo la perdió (o shadow sobre-incluye).
  - Fila `origen=mail` del ledger en la ventana que shadow no genera → shadow pierde mails.
  - Diff → artifact descargable + el workflow falla si hay ≥1 diferencia.

## Fase 4 — Semana de validación paralela (7 días)

- **4.1** La PC sigue siendo la dueña: tareas de Windows activas (límite ya subido a 3h el 15/7).
- **4.2** Actions corre en shadow 2x/día, ~1h después de cada corrida local.
- **4.3 GATE de aceptación:** ≥12 corridas shadow consecutivas con **0 diferencias** (ledger, pendientes, sumas de validación con el mismo baseline 2c), 0 mails perdidos en ambas direcciones, duración <5 min por corrida.
- **4.4** Cualquier diff → se corrige la causa y el contador de corridas limpias arranca de nuevo.

## Fase 5 — Cutover

- **5.1** Quitar el flag shadow → Actions commitea y pushea (es el nuevo dueño).
- **5.2** **Deshabilitar** (no borrar) las tareas de Windows AM/PM (`Disable-ScheduledTask`). Se borran recién tras 1 mes estable.
- **5.3** CLAUDE.md: al inicio de toda sesión de análisis → `git pull` (los commits ahora nacen en GitHub). Documentar la corrida manual de respaldo (`.env` local gitignored con los mismos secretos + scripts).
- **5.4** Primera corrida real supervisada: verificar commit remoto, Sheets refrescados, Looker al día.
- **5.5** `sync_gastos_headless.cmd` queda como referencia histórica; el log pasa a ser los run logs de Actions + los mensajes de commit.

## Rollback

En cualquier punto: re-habilitar las tareas de Windows (el sistema viejo queda intacto hasta 5.5) y desactivar el cron de Actions. El watermark + la idempotencia por message_id garantizan que alternar entre sistemas no duplica ni pierde mails.

## Qué NO cambia

Esquema del ledger, `resoluciones.csv`, `pendientes.csv`, `reglas_comercio.csv`, toda la fase 2 de procesamiento y validación, `/revisar-pendientes`, `/cierre-mes`, los dashboards, y la interacción con Claude para preguntas y análisis (solo se agrega el `git pull` inicial).
