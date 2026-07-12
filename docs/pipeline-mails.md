# Pipeline de mails (Fase 2)

Convierte los mails financieros de Gmail en filas del ledger. Complementa a [esquema.md](esquema.md) — el contrato de columnas del ledger no cambia.

## Flujo

```
Gmail (MCP) → data/inbox_raw/<message_id>.json → scripts/procesar_mails.py → data/ledger.csv
                                                        ├→ data/staging/procesados.csv (log por mail)
                                                        ├→ data/pendientes.csv (bandeja de revisión)
                                                        ├→ data/facturas.csv (facturas informativas)
                                                        └→ config/estado_sync.json (watermark)
```

- La descarga desde Gmail la hace Claude (MCP) — dump crudo a `data/inbox_raw/`, un JSON por MENSAJE (no por thread; Gmail agrupa avisos en threads):
  `{"message_id","thread_id","fecha_mail","from","subject","body_text"}`
- `procesar_mails.py` es Python puro sobre esos JSON: idempotente (un mail procesado no se reprocesa: `procesados.csv` por message_id), reprocesable (`--reprocesar` regenera desde cero las filas origen=mail).

## Backfill inicial

Desde **2026-07-01** (junio ya está en el ledger vía 1Money; los 1money transfers llegan hasta 2026-07-08 → ver dedupe cruzado).

## Router por remitente/asunto

| Remitente | Asunto | Handler |
|---|---|---|
| notificaciones@santander.com.uy | `Aviso: uso de tarjeta de débito/crédito` | aviso_tarjeta |
| notificaciones@santander.com.uy | `Aviso de Transferencia Instantánea…` / `…en plaza` / `…interna…` | transferencia |
| no_reply@email.apple.com, googleplay-noreply@google.com, info@mercadolibre.com.uy, info@mercadopago.com | recibos | recibo (matching) |
| factura@antel.com.uy, info_uy@email.directvla.com | facturas | factura (informativo) |
| resto (marketing, seguridad, estados de cuenta*) | — | ignorado (log) |

*Estados de cuenta PDF (BBVA, Santander MC, Itaú→cerrada) son Fase 3: se loguean como `estado_cuenta_pendiente_f3`.

## Handler aviso_tarjeta

Campos del cuerpo: `VISA terminada en NNNN`, `Importe` (`UYU 28.170,00` / `USD 97,47` — punto miles, coma decimal), `Establecimento` (sic), `Fecha y hora` (`DD/MM/YYYY HH:MM:SS`, hora UY — usar esta fecha, no el header).

| Tarjeta | cuenta | ambito |
|---|---|---|
| 9071 | Santander UYU | personal |
| 8261 | Cuenta Casa | casa |
| 6427 | Santander Platinum | personal |

- Fila ledger: tipo=gasto, monto/moneda del importe, comercio=establecimiento crudo, descripcion=establecimiento limpio (sin prefijos `MONTEVIDEO/`, `INTERNET/`, `FKD`, `9829089080/`, sufijo `VIS3`), origen=mail, fuente_id=message_id, id=sha1("mail"+message_id)[:12], estado según reglas.
- monto_uyu: UYU → monto; USD/EUR → monto × tc de `config/parametros.json` (redondeo 2), y motivo `tc_estimado` en pendientes (lo corrige la conciliación de Fase 3).
- categoria: por `config/reglas_comercio.csv` (regex case-insensitive sobre establecimiento crudo; primera que matchea gana; columna ambito_override opcional). Sin match → categoria vacía + pendiente `sin_categoria`.
- **Herencia PedidosYa**: `PedidosYa Propina` y `PedidosYa Envio` heredan categoría y ámbito del pedido que los originó (misma tarjeta, hasta 6 h antes): pedido Market → Super, pedido restaurante → Comer afuera / Delivery. Sin pedido previo en ventana → regla general (Delivery).
- **Proyecto PAS** es el canal de pagos web de Santander (el destinatario varía) → siempre queda pendiente `sin_categoria` para clasificar a mano.
- **Transición 8261 (jul-2026)**: hasta que Joaco confirme la vuelta de la casa a Santander, toda fila 8261 además va a pendientes con motivo `ambito_a_confirmar`.

## Handler transferencia

Campos: Titular destino, Concepto origen/destino, Monto (`UYU 20000,00` — SIN separador de miles), Fecha valor (`Mmm DD, YYYY`, meses en inglés), tipo de aviso.

Reglas de clasificación (en orden):
1. Titular contiene "Mercado Pago" y/o concepto ~ "Casa" → transferencia interna Santander UYU → Cuenta Casa (aporte).
2. Titular ~ "Lu" (Lu pesos, etc.) → transferencia interna → pendiente `transferencia_a_clasificar` (puede ser cierre de casa).
3. Resto (terceros: garage, personas) → tipo=gasto, ambito por concepto (concepto ~ "Garage"/"Vazquez Ledesma" → casa), categoria por reglas sobre titular+concepto; sin match → pendiente `transferencia_a_clasificar`.
- "en plaza" es una solicitud asincrónica: se registra igual (estado del aviso ≈ ejecución) — la conciliación mensual la confirma.

### Dedupe cruzado con 1Money (solo ventana 2026-07-01 → 2026-07-08)

El ledger ya tiene 5 transferencias de 1Money en julio. Si un mail de transferencia matchea una fila origen=1money tipo=transferencia (monto ±1 UYU, fecha ±2 días) → resultado=`duplicado_1money`, no se inserta.

## Handler recibo (Apple / Google / Meli+ / MP "Pago aprobado")

Extraer monto+moneda y comercio/producto. Buscar en el ledger un aviso de tarjeta origen=mail con mismo monto (misma moneda, ±0,5%) en ±3 días:
- Match → enriquecer esa fila: comercio=nombre real (ej. "Apple – YouTube Premium"), no crear fila nueva.
- Sin match → crear fila pendiente `recibo_sin_matchear` (posible pago con saldo MP), cuenta=Cuenta Casa si vino de MP, si no Santander UYU.

## Handler factura (Antel, DirecTV)

No tocan el ledger. Append a `data/facturas.csv`: `mes,emisor,monto,vencimiento,message_id`. Sirven de control cruzado en la conciliación.

## Dedupe de avisos

- Primario: message_id (idempotencia).
- Heurístico: mismo (tarjeta, monto, establecimiento) con message_id distinto en ±10 minutos → la segunda fila se inserta con pendiente `posible_duplicado` (hubo duplicados reales del banco Y cargos legítimos repetidos — decide Joaco en la revisión).
- PedidosYa: pedido + Propina + Envio son avisos separados legítimos → filas separadas, misma categoría.

## Splits (gastos compartidos con hermanos/amigos → Splitwise)

Problema: Joaco paga el 100% (ej. YouTube familiar, un asado) pero su gasto real es una fracción; el resto se lo deben (cuenta Deudas / Splitwise).

**Recordatorio (automático):** el pipeline marca pendiente `split_a_cargar` cuando:
1. el comercio/producto matchea `config/splits_recurrentes.csv` (patron, mi_fraccion, contraparte), o
2. heurística: categoría Comer afuera / Delivery o Salidas & ocio con monto_uyu ≥ `umbral_split_uyu` (config/parametros.json, hoy 1.500).

**Resolución (en /revisar-pendientes, con fracción f confirmada):**
- La fila original ajusta `monto` y `monto_uyu` a `× f` (mantiene categoría y fecha) → el gasto del mes queda solo con su parte.
- Se agrega una fila hermana `tipo=transferencia`, misma cuenta → `cuenta_destino=Deudas`, por el `× (1−f)` restante, `en_analisis=0`, `fuente_id=<original>-split`.
- **Invariante de conciliación: fila original ajustada + fila hermana = cargo real de la tarjeta**, así el estado de cuenta sigue cerrando al peso.
- Cuando pagan (Splitwise settle / transferencia recibida) → transferencia Deudas → Santander, y Deudas tiende a 0.
- Con la API de Splitwise (Fase 3): al resolver el split, Claude además CREA el gasto en Splitwise (con ok de Joaco), y a la inversa, los gastos que le cargan a él entran solos como gasto cuenta=Deudas por su parte.

## data/pendientes.csv — bandeja de revisión

`id_ledger,motivo,detalle,fecha_alta,resuelto` — motivos: `sin_categoria`, `ambito_a_confirmar`, `posible_duplicado`, `transferencia_a_clasificar`, `recibo_sin_matchear`, `tc_estimado`, `split_a_cargar`.
Revisión semanal: Claude presenta los no resueltos, Joaco decide, se actualizan ledger y reglas (los fallos de categoría alimentan `reglas_comercio.csv` para que no se repitan).

## Fase 3 — conciliación (diseño, para no duplicar ni descuadrar)

**Estados de cuenta vs avisos (ej. Santander Platinum 6427):** el estado de cuenta NUNCA inserta directo — hace MATCHING contra las filas ya capturadas por mail del período (monto en la moneda original ±0,5%, fecha ±3 días, comercio fuzzy). Línea matcheada → la fila pasa a `estado=conciliado` y se corrige `monto_uyu` con el tipo de cambio real (cierra los `tc_estimado`). Línea SIN match → se inserta (es lo que los mails no ven: débitos automáticos, ajustes, tarjetas sin avisos como BBVA y la Mastercard). Fila mail sin línea del estado → sospechoso (reversa o error) → pendiente. Cierre: suma de filas del período == total del estado, al peso.

**Saldos por cuenta:** `config/saldos_iniciales.csv` (sembrado desde 1Money al 2026-07-11, a calibrar) + corte mensual en `data/saldos.csv`: `fecha_corte,cuenta,saldo_calculado,saldo_real,diferencia,comentario`, donde `saldo_calculado = inicial + Σ movimientos del ledger` (gastos restan, ingresos suman, transferencias mueven entre cuentas) y `saldo_real` viene de: estados de cuenta (tarjetas), saldo del banco que Joaco reporta 1 vez/mes (2 minutos), export MP. Diferencia ≠ 0 → lista de movimientos sospechosos del período, NUNCA ajuste ciego. El primer corte calibra los iniciales.

## Validación post-corrida

`merge_y_validar.py` sigue siendo el guardián, con las comparaciones contra fuentes (2a/2b/2c) restringidas a `origen IN (1money, planilla_casa)` — las filas origen=mail se validan contra Gmail (conteo por día/tarjeta), no contra las fuentes viejas.

## config/estado_sync.json

`{"ultimo_sync": ISO, "ultima_fecha_mail": ISO}` — la próxima corrida busca en Gmail desde `ultima_fecha_mail` − 2 días (solapamiento seguro; la idempotencia absorbe repetidos).
