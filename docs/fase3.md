# Fase 3 — Conciliación, Splitwise y cierre de casa

Contratos de la fase 3. Complementa esquema.md y pipeline-mails.md.

## 1. Splitwise (cuenta Deudas) — solo lectura

- `scripts/sync_splitwise.py`: API v3.0 (`https://secure.splitwise.com/api/v3.0/...`), auth `Authorization: Bearer <api key>` leída de `config/secrets.local.json` (clave `splitwise_api_key`). **NUNCA imprimir ni loguear la key.**
- Endpoints: `get_current_user` (id propio), `get_friends` (saldos por persona/moneda), `get_expenses?dated_after=2026-07-01T00:00:00Z&limit=100` (paginar con offset).
- Salidas:
  - `data/splitwise.csv`: crudo por gasto: id, fecha, descripcion, grupo, moneda, costo_total, mi_paid_share, mi_owed_share, creado_por, es_pago (payment=true).
  - Filas al ledger (origen=splitwise, fuente_id=`sw:<expense_id>`, dedupe por fuente_id):
    - **Gastos donde otro pagó y yo debo** (`owed_share>0`, `paid_share==0`, `payment=false`) → tipo=gasto, monto=mi owed_share, cuenta=Deudas, ambito=split, categoria por reglas sobre descripción (sin match → pendiente `sin_categoria`).
    - **Gastos que YO pagué** (`paid_share>0`): NO se insertan — el cargo ya entró por el aviso de tarjeta; la parte prestada se maneja con el mecanismo de splits de pipeline-mails.md. Se registran solo en splitwise.csv y se marca pendiente `split_a_cargar` si no existe ya una fila hermana `<id>-split` para un cargo que matchee (monto total ±1%, fecha ±3 días).
    - **Pagos** (`payment=true`): si me pagaron → transferencia Deudas → Santander UYU con pendiente `transferencia_a_clasificar` (confirmar a qué cuenta entró); si pagué yo → transferencia Santander UYU → Deudas (dedupe contra transferencias mail/1money existentes ±1 moneda ±2 días).
- Validación de saldo: al final compara el saldo neto de `get_friends` contra el saldo calculado de la cuenta Deudas y lo reporta (por moneda).
- Integración: paso nuevo en `scripts/correr_pipeline.cmd` ANTES de procesar_mails (tolerante: si no hay red o falla la API, sigue sin Splitwise y lo anota en la salida).

## 2. Estados de cuenta (carpeta data/estados/)

- El conector de Gmail no baja adjuntos → **Joaco suelta los PDF en `data/estados/`** cuando llegan (BBVA día 1-7, Santander MC día 5-8). Nombres libres; el parser identifica el emisor por contenido.
- `scripts/conciliar_estado.py <archivo.pdf>` (o sin argumento: procesa todos los PDF nuevos de la carpeta; log en `data/staging/estados_procesados.csv`):
  1. Si el PDF está protegido → contraseña = `ci_pdf_password` de secrets (pikepdf/PyPDF2; **nunca imprimirla**).
  2. Extrae: emisor, período, líneas (fecha, descripción, monto, moneda), total del período, vencimiento, tipo de cambio si figura.
  3. **Matching contra el ledger** (filas de la cuenta correspondiente en el período): monto en moneda original ±0,5%, fecha ±3 días, descripción fuzzy.
     - Línea matcheada → fila del ledger pasa a `estado=conciliado`; si la fila tenía `tc_estimado`, corrige `monto_uyu` con el TC real (vía data/resoluciones.csv para que persista) y cierra ese pendiente.
     - Línea sin match → se INSERTA (origen=mail? no: origen=`estado_cuenta`, agregar al vocabulario de origen; para BBVA y Mastercard es la fuente primaria), con categoría por reglas y ambito por cuenta (Mastercard → casa).
     - Fila del ledger sin línea en el estado → pendiente `sin_respaldo_estado` (posible reversa/duplicado).
  4. **Cierre al peso**: suma de filas conciliadas+insertadas del período == total del estado (por moneda). Diferencia ≠ 0 → reporte con el detalle.
- Como todavía no hay PDF reales en el repo, el motor de matching se desarrolla y testea con un estado SINTÉTICO generado desde los avisos capturados (script de test); el parser de PDF real se ajusta con el primer archivo que Joaco suelte.

### 2.1 Caso real: reconciliación manual con extracto de Cuenta Casa (jul-2026)

El 12/7 Joaco proveyó `data/estados/cuenta_casa_127.csv` (extracto real de movimientos, formato Santander con Débito/Crédito/Saldos). Se hizo un `conciliar_estado.py`-equivalente manual (comparación línea a línea por monto+fecha) contra el ledger. Hallazgos y correcciones aplicadas (todas en `data/ledger.csv` con `origen=manual`, `fuente_id` prefijo `mp-`, salvo las exclusiones):
- **Aporte real de $37.000 (1/7, "CREDITO OPERACION EN BANCA DIGITAL")**: no estaba capturado (ningún mail lo generó) — agregado como transferencia Santander UYU → Cuenta Casa.
- **2 comisiones de transferencia ($77,81 c/u)** y **1 comisión de transferencia a Mercado Pago ($78)**: gastos de casa que no generan aviso — agregados manualmente.
- **2 Propinas de PedidosYa ($35 y $25, tarjeta 8261)**: sin aviso por mail — agregadas manualmente.
- **Gasto invisible durante el parqueo en Mercado Pago ($3.061,72)**: de los $20.000 enviados a MP el 8/7 solo volvieron $16.938,28 el 12/7; la diferencia es gasto real hecho con el saldo de MP, que no genera mail. Se registró como gasto único `cuenta=Mercado Pago` con la nota de origen.
- **Exclusiones confirmadas por Joaco**: la transferencia "Regalo agus" $2.224 (1/7) y el "Aporte" $12.000 (1/7, 1Money) NO aparecen en el extracto real de julio — son eventos de JUNIO que ya están dentro de la semilla (`config/cierre_exclusiones.csv`).
- **Residual sin explicar tras todo esto: ~$589** (de un total mensual ~$68.000 de movimientos) — atribuible casi en su totalidad a una transacción ("Helado" $558,36, splitwise 30/6) que nuestro sistema fecha por el aviso (30/6, dentro de la semilla) pero el banco procesó el 1/7 (fuera de la semilla): un artefacto de borde de mes, no un error sistémico. No se persigue más allá de este punto.

**Lección para `conciliar_estado.py` (Fase 3 automatizada)**: el motor de matching debe tolerar esta clase de borde de mes (transacción con fecha de aviso en el último día del mes anterior pero posteada por el banco al día siguiente) sin generar alarma — o al menos documentarlo como categoría de discrepancia esperada, no un error a resolver.

## 3. Cierre mensual de casa (cuenta continua)

- Config semilla: `config/posicion_casa.json`: `{"fecha_corte": "2026-06-30", "lu": 11244.07, "joaco": -9692.04}` (Saldos Tarjeta de la planilla congelada; positivo = a favor).
- `scripts/cierre_casa.py [YYYY-MM]`:
  - Parte del mes de cada uno: suma de gastos ambito=casa en_analisis=1 del mes × coeficiente (config/ingresos.csv; mes faltante → último disponible, marcado "a confirmar").
  - Aportes de Joaco del mes: transferencias del ledger con cuenta_destino=Cuenta Casa + gastos ambito=casa pagados desde cuentas ≠ Cuenta Casa (aporte implícito, ej. seguro BSE en la Platinum).
  - Aportes de Lu del mes: de `data/aportes_lu.csv` (`mes,fecha,monto,nota`) — input manual (las transferencias entrantes no generan mail).
  - Posición nueva = posición anterior + aportes − parte. Append a `data/cierre_casa.csv`: `mes,parte_lu,parte_joaco,aportes_lu,aportes_joaco,posicion_lu,posicion_joaco,nota`.
  - Sugerencia de convergencia: dado el gasto casa promedio de los últimos 3 meses, cuánto conviene que aporte cada uno el mes próximo para que la posición tienda a 0 (sin transferencias entre ellos).
- Comando `/cierre-mes` (`.claude/commands/cierre-mes.md`) orquesta la ceremonia mensual: pide ingresos del mes (default: repetir el anterior), aportes de Lu, saldos reales por cuenta; corre conciliar_estado (si hay PDF nuevos), cierre_casa y conciliar_saldos; presenta el reporte (posición casa, presupuesto vs real, saldos, pendientes) y commitea.

## 4. Saldos mensuales

- `scripts/conciliar_saldos.py`: saldo_calculado por cuenta = saldo de `config/saldos_iniciales.csv` (corte 2026-07-12) + movimientos del ledger POSTERIORES al corte (gastos restan de su cuenta, ingresos suman, transferencias mueven; tarjetas de crédito: gastos suman deuda, pagos la bajan). Compara contra saldos reales que Joaco reporta en el cierre y appendea a `data/saldos.csv` con diferencia y sospechosos (movimientos grandes del período) si ≠ 0.
- Nota vigente: los gastos de la 8261/Cuenta Casa se registran contra Cuenta Casa (= cuenta corriente física); los aportes de Lu entran ahí sin mail → el saldo calculado de Cuenta Casa SIEMPRE va a requerir los aportes de Lu cargados antes de conciliar.
