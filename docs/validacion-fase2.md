# Validación independiente — backfill de mails julio 2026 (Fase 2)

Fecha: 2026-07-12. Verificación adversarial contra Gmail (MCP) + re-chequeo local.
Script reproducible: `scripts/verificar_backfill_jul2026.py` (exit 0 = todo OK; corre sobre ledger, procesados, pendientes, inbox_raw, resumen_mensual).

## Veredicto

**APROBADO.** 0 discrepancias críticas o altas. 1 hallazgo medio (clasificación de ámbito ya capturado en pendientes), 2 bajos. Sin faltantes, sin dobles conteos, sin errores de parsing de montos/fechas.

## 1. Cobertura (Gmail vs ledger)

- `from:notificaciones@santander.com.uy after:2026/06/30 subject:"uso de tarjeta"`: **35 mensajes** (13 threads, sin paginación pendiente; `in:anywhere` da el mismo conjunto → nada en spam/trash).
  - 31 insertados (ledger origen=mail tipo=gasto) + 4 `ignorado_pre_backfill` (fecha del CUERPO 30/06/2026, correcto aunque el header sea 07-01) = 35. **1:1 exacto.**
- Transferencias del mismo remitente: **6 mensajes** en Gmail = 6 insertadas. 1:1.
- Recibos (Apple/Google/Meli/MP): 2 reales en Gmail → 1 matcheado (enriquece `a6ef3e758f12`), 1 `recibo_sin_matchear`. Facturas Antel/DirecTV: 0 en Gmail → `data/facturas.csv` vacío, correcto.
- Estado de cuenta BBVA: 1 → logueado `estado_cuenta_pendiente_f3`, no toca ledger. Correcto.
- ids de `data/inbox_raw/` (44) == ids de `procesados.csv` (44), diff vacío.
- Watermark `ultima_fecha_mail=2026-07-10T16:03:24Z` = último aviso real; no hay mails financieros posteriores al corte.
- Conteo del build confirmado: 44 procesados = 31 avisos + 4 pre-backfill + 6 transferencias + 2 recibos + 1 estado de cuenta; 38 filas nuevas (31+6+1); ledger 4525→4563.

## 2. Exactitud

- **31/31 avisos** reparseados independientemente desde `inbox_raw` (regex propio) y comparados contra el ledger: monto, moneda, fecha del cuerpo, cuenta/ámbito por tarjeta, tipo, en_analisis, monto_uyu (USD × 40.33 redondeado a 2) e id = sha1("mail"+message_id)[:12] — **0 discrepancias**.
- Muestra profunda con `get_message` (cuerpo completo de Gmail): `19f2003eeee734dc` (9071 USD 530,47 → 530.47 / 21393.86 UYU ✓), `19f44033457195fa` (6427 UYU 28.170,00 → 28170.00, Educación ✓), `19f2e5e5c9fc5eb5` (transferencia UYU 23768,00 sin sep. de miles → 23768.00, Fecha valor Jul 04 ✓). Los snippets del cuerpo de los 41 mensajes restantes también cotejados contra el ledger.
- Fecha del cuerpo vs header: 5 mensajes cruzan de día (ej. `19f2563d431a6f60` header 07-03 / cuerpo 02/07 → ledger 2026-07-02 ✓; `19f4438ae31bede9` header 07-09 / cuerpo 08/07 → 2026-07-08 ✓). Todos usan la fecha del cuerpo.
- Categorías conforme a `reglas_comercio.csv` incl. orden de reglas (EL TIMON *HANDY* → Comer afuera porque la regla EL TIMON precede a HANDY; PedidosYa Market → Super; MERPAGO* → sin categoría a propósito).
- Redondeo/formato: 0 montos sospechosos de ×100 o ÷100 (ningún monto mail ≥100000 ni <1 salvo USD 1.99, verificado contra el mail). Punto de miles y coma decimal parseados bien en ambos formatos (aviso `28.170,00` y transferencia `23768,00`).

## 3. Transferencias y dedupe cruzado (CRÍTICO)

- 6 avisos en Gmail → 2 internas (`Lu pesos` 4715 pendiente; `Joaco Mercado Pago 'Casa'` 20000 → Santander UYU→Cuenta Casa, interno, en_analisis=0) + 4 a terceros como gasto (Garage 3600 casa/Auto; Dan de Amilivia 90; Gastos Comunes VL 23768 y Bea Itau 14278 pendientes). Conforme a reglas.
- Dedupe cruzado: las 5 transferencias origen=1money de julio (2224, 768, 5840, 12000, 4256) vs las 2 mail (4715 el 07-03, 20000 el 07-08): **ningún match ±1 UYU / ±2 días** → `0 duplicado_1money` es correcto, no hay aporte contado dos veces. Aportes julio a Cuenta Casa: 1money 2224 + 12000 (07-01, sin aviso Santander porque la cuenta casa era MP) y mail 20000 (07-08) — tres operaciones distintas, todas en_analisis=0.

## 4. Pendientes (21, todos sin resolver)

- 12 `ambito_a_confirmar` = exactamente las 12 filas gasto 8261. ✓
- 4 `tc_estimado` = exactamente las 4 filas USD (1.99, 530.47, 97.47, 150.37). ✓
- 1 `sin_categoria` (MERPAGO*DONALBERTOUY — comercio genérico, razonable). 3 `transferencia_a_clasificar`. 1 `recibo_sin_matchear`.
- estado=pendiente en ledger (21 filas mail) == pendientes.csv sin resolver. Falsos pendientes: 0. Todos los id_ledger existen.

## 5. Agregados y re-corrida

- Gasto 2026-07 en_analisis=1: **122092.33 UYU** (36 filas, todas origen=mail; 38 − 2 transferencias) == suma `out/resumen_mensual.csv` 2026-07 (7 categorías, incl. "(sin categoria)" 38923.69 = 877.69+23768+14278). ✓
- `merge_y_validar.py` re-corrido: exit 0, todas las validaciones OK; **md5 de data/ledger.csv idéntico antes/después** (b89b557d…), 4563 filas, 38 mail, 0 ids duplicados, 0 (origen,fuente_id) duplicados → no duplica ni pierde filas.
- 2c.doble_conteo DESVIO: 14 meses, todos 2023-05→2025-10, ninguno de 2026 → baseline preexistente, no atribuible al backfill. Confirmado.
- 2d.categorias excluye estado=pendiente por diseño (las 3 filas mail sin categoría están pendientes) — consistente, no tapa errores.

## Hallazgos por severidad

| Sev | Hallazgo | Detalle |
|---|---|---|
| MEDIA | `41e0c57ed0b2` (Gastos Comunes VL, 23768 UYU, 07-04) quedó **ambito=personal** y cuenta en el gasto personal | "VL" = Vazquez Ledesma: son casi seguro los gastos comunes del apto (concepto '801') → debería ser `casa` y entrar en la división. La regla solo matchea "Vazquez Ledesma" en el concepto, no el titular. Mitigado: está en pendientes (`transferencia_a_clasificar`); resolver en la revisión semanal y agregar regla `Gastos Comunes VL → casa`. |
| BAJA | 2 mails de remitentes ruteados no logueados | `19f4c93109b05ae2` (Google Play, cambio de TOS) y `19f47dc85cbe48d9` (Meli+ promo) son marketing y no se bajaron a inbox_raw ni quedaron como `ignorado` en procesados.csv; el contrato dice "resto → ignorado (log)". Sin impacto en el ledger. |
| BAJA | Recibo Meli+ `a7ad2ee2689b` (199 UYU, "Medio de pago Visa") | Insertado como gasto en Santander UYU sin aviso de tarjeta que lo respalde. Si el aviso llega tarde en una corrida futura, se insertaría como fila nueva (el matching recibo→aviso es unidireccional) → posible doble conteo futuro de 199. Vigilar en conciliación F3. |
