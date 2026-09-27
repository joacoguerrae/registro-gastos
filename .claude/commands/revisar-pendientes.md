---
description: Revisión semanal de la bandeja de pendientes del ledger de gastos (categorías, ámbitos, duplicados, splits)
---

Revisión semanal de pendientes del sistema de gastos.

**REGLA DE ORO (invariante de CLAUDE.md): toda escritura manual pasa por `scripts/gastos.py`.** Nunca edites `data/ledger.csv`, `data/pendientes.csv`, `data/resoluciones.csv` ni `data/fuentes/*` a mano ni con Python descartable. El ledger es un artefacto de build (se reconstruye desde `data/fuentes/`, los staging, `inbox_raw/` y las resoluciones): una edición directa *parece* guardada y la próxima corrida la pisa. `gastos.py` valida contra los catálogos, aplica los invariantes (el split suma el cargo, un gasto tiene `monto_uyu`, un aporte no se duplica) y al final corre el pipeline de verificación. **Siempre primero en seco** (sin `--aplicar`), mostrale a Joaco lo que va a escribir si hay dudas, y después `--aplicar`.

## Qué comando para cada decisión

| Decisión | Comando |
|---|---|
| Corregir categoría / ámbito / descripción | `gastos.py resolver <id> categoria=Super ambito=casa --cerrar sin_categoria --nota "<palabras de Joaco>"` |
| Duplicado confirmado (fila mail o de planilla) | `gastos.py resolver <id> en_analisis=0 --cerrar posible_duplicado --nota "duplicado de <otro id>"` |
| Duplicado que INSERTÓ un conciliador (`estado_cuenta`, `manual`) | `gastos.py borrar <id> --motivo "duplicado de <otro id>"` (queda en `data/fuentes/borrados.csv`) |
| Cerrar un pendiente sin tocar datos | `gastos.py resolver <id> --cerrar <motivo> --nota "<decisión>"` |
| Transferencia que en realidad es gasto | `gastos.py resolver <id> tipo=gasto categoria=X ambito=personal --cerrar transferencia_a_clasificar --nota "..."` (completa `monto_uyu` solo si es UYU; si no, exige `monto_uyu=`) |
| Split (`split_a_cargar`) | `gastos.py split <id> --mia 1/3` (resto a Deudas) · `--mia 2/6 --a Deudas=3/6 --a "A cobrar"` (varios destinos) · `--participacion-lu 0.5` si tu parte es de casa y se reparte fijo |
| Gasto de casa 100% de uno / reparto fijo | `gastos.py resolver <id> --participacion-lu 1.0 --nota "..."` (1.0 = todo de Lu, 0.5 = mitades) |
| Gasto de casa que pagó Lu | `gastos.py pago-lu --fecha F --monto M --categoria K --desc "..."` (`--efectivo` si fue en efectivo). Cuenta solo como aporte de ella |
| Aporte a la casa | `gastos.py aporte --quien lu --monto M --mes AAAA-MM` · `--quien joaco --monto M --fecha F [--desde "Santander UYU"]` |
| Te pagan algo de `A cobrar` | `gastos.py cobro --monto M --fecha F --a "Santander UYU" --desc "..."` |
| Gasto sin rastro bancario | `gastos.py gasto --fecha F --monto M --cuenta Efectivo --categoria K --ambito personal --desc "..."` |

- La `--nota` registra la decisión de Joaco **con sus palabras** — es la memoria del porqué. `resolver` no deja escribir sin nota.
- `--cerrar <motivo>` escribe el `motivo_resuelto`, que cierra el pendiente `(id, motivo)` y **suprime su regeneración** en toda corrida futura. **Va SIEMPRE, para todo motivo — incluido `sin_categoria`.** Los handlers crean los pendientes ANTES de que se apliquen las resoluciones (`procesar_mails.py`: handlers en los pasos 1-4, overrides en el 5), así que un override de `categoria` NO evita que el pendiente vuelva a nacer en un `--reprocesar`. La única fila que suprime es `motivo_resuelto`. (Verificado el 16/7 fabricando el caso.)
- `resuelto=1` en `pendientes.csv` y el `estado` de la fila los recalcula el pipeline solo (`gastos.py --aplicar` ya lo corre).

## Pasos

1. `git pull` (regla de CLAUDE.md). Leé `data/pendientes.csv` (filas con `resuelto` vacío) y las filas correspondientes de `data/ledger.csv`.
2. Presentale a Joaco los pendientes agrupados por motivo, en tandas cortas y con contexto (fecha, monto, comercio, cuenta). Para cada uno proponé una resolución concreta (la más probable primero). Usá AskUserQuestion cuando haya opciones claras.
3. Con las respuestas, corré el comando de la tabla — en seco, y después con `--aplicar`. Cada `--aplicar` corre `procesar_mails` + `merge_y_validar` (+ `cierre_casa` si tocó la casa) y muestra el resultado: si algo sale `[FALLO]`/`[DESVIO]`, pará y resolvelo antes de seguir. Para una tanda larga podés usar `--sin-pipeline` en cada comando y correr el paso 8 una vez al final.
4. REGLAS NUEVAS: si una decisión es generalizable ("Handy del kiosco X siempre es Super"), agregá el patrón a `config/reglas_comercio.csv` **antes** del catch-all `HANDY|MERPAGO\*` (gana la primera que matchea). Ídem transferencias recurrentes.
5. Si Joaco confirma que la casa ya volvió a la 8261 (o da la fecha del cambio), registrá la fecha en `config/parametros.json` (clave `fecha_8261_casa`) y resolvé en bloque los `ambito_a_confirmar`: posteriores a la fecha → `ambito=casa`; anteriores → preguntar si eran casa o personal.
6. SPLITS (`split_a_cargar`) — `gastos.py split` hace todo lo que antes era prosa: ajusta la original a tu parte (monto y `monto_uyu`, el redondeo lo absorbe la original para que la suma sea exacta en las dos monedas), crea la(s) hermana(s) `transferencia` en `data/fuentes/manual.csv`, verifica original + hermanas = cargo real, cierra el pendiente, y avisa si el cargo salió de una cuenta de la casa. Si es recurrente, registrá patrón y fracción en `config/splits_recurrentes.csv`. Recordale a Joaco cargarlo en Splitwise.

   - **CASO ESPECIAL — split pagado con una cuenta/tarjeta DE LA CASA** (planteado por Joaco el 25/8/2026 con un asado de 6.951 en la Mastercard). El cobro de Splitwise entra a la cuenta PERSONAL de Joaco, pero la plata salió del fondo de la casa: si no se registra, Lu termina financiando la parte de los amigos. **No hay que hacer nada especial: el sistema ya lo modela.** La hermana va a `Deudas` desde la MISMA cuenta de la casa, y como `Cuenta Casa`, `Santander Mastercard` y `Mercado Pago` están en `CUENTAS_CASA`, `cierre_casa.py` la toma como **retiro** y se la resta al aporte de Joaco. Cuando Joaco cobre y transfiera esa plata a la Cuenta Casa, esa transferencia cuenta como aporte y **cancela el retiro exacto**. Dos cosas a no olvidar:
     1. La fracción que queda en la familia se reparte entre TODAS las líneas del mismo consumo (un asado puede venir en 2 o 3 renglones de tarjeta): `gastos.py split` una vez por línea, con la misma fracción.
     2. Si esa parte no se divide por el coeficiente del mes sino explícito (típico 50/50): `--participacion-lu 0.5` en cada línea.
   - **Timing:** el BBVA no manda aviso por transacción (la Mastercard tampoco, y se dio de baja el 27/9/2026). Un split pagado con esas tarjetas deja el pendiente de Splitwise SIN cargo que matchee hasta que llega el estado de cuenta (día ~5-8). No es un error: es la espera normal.
7. `formato_nuevo` (mail que el parser no supo leer): el dato NO está en el ledger — el mail quedó en `data/inbox_raw/` y el pendiente apunta al id que la fila habría tenido. Leé el mail, mostráselo a Joaco, y o bien se arregla el parser (y se corre `procesar_mails.py --reprocesar`), o se cierra con `gastos.py resolver <id> --cerrar formato_nuevo` si el mail no era una transacción.
8. **Pendientes `[planilla]`** (vienen de la planilla "Carga manual"): `carga_manual_invalida` = la fila NO entró (falta la fecha, categoría inexistente...): **se corrige en la planilla**, no acá, y el pendiente se va solo en la corrida siguiente. `carga_manual_duplicado` = ya estaba cargado por otra vía (mismo monto, ±3 días): si Joaco confirma que es OTRO movimiento, `gastos.py resolver <id> --cerrar carga_manual_duplicado --nota "..."` y entra solo. `sin_categoria` de la planilla: se resuelve igual que uno de mail (o poniendo la categoría en la planilla).
9. Verificá (si usaste `--sin-pipeline`): `C:/Users/joaco/anaconda3/python.exe scripts/procesar_mails.py`, `scripts/merge_y_validar.py` (sale ≠0 si algo está roto), `scripts/cierre_casa.py` (si cambió algo de ámbito casa) y siempre `scripts/generar_dashboard_data.py` + `scripts/generar_dashboard_html.py`. Confirmá en la salida que los pendientes resueltos ya no aparecen y que el override quedó reflejado.
10. Commit: `git add data/ config/ out/` con mensaje `revision pendientes <fecha>: N resueltos` y `git push`.
11. **Cerrá los Issues del bot que quedaron obsoletos.** El workflow abre un Issue "🔎 N pendiente(s) nuevo(s)" por cada corrida que deja pendientes, pero nada los cierra. Si la bandeja quedó en 0: `gh issue list -R joacoguerrae/registro-gastos --state open` y cerrá los de pendientes con `gh issue close <n> -c "resueltos en la revisión de <fecha> (commit <hash>)"`.
12. Cerrá con el estado: pendientes restantes, gasto del mes vs presupuesto, y cualquier cosa rara detectada.
