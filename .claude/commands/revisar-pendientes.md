---
description: Revisión semanal de la bandeja de pendientes del ledger de gastos (categorías, ámbitos, duplicados, splits)
---

Revisión semanal de pendientes del sistema de gastos.

**REGLA DE ORO (invariante de CLAUDE.md): jamás edites `data/ledger.csv` ni `data/pendientes.csv` a mano.** El ledger se REGENERA (`--reprocesar`, re-merge): una edición directa *parece* guardada y se borra en la próxima regeneración. Toda decisión se materializa en `data/resoluciones.csv` y el pipeline la re-aplica en cada corrida, para siempre. (La versión anterior de este comando instruía editar el ledger directamente — fue el hallazgo A2 de [docs/auditoria-2026-07.md](../../docs/auditoria-2026-07.md).)

## Mecánica de `data/resoluciones.csv`

Columnas: `id_ledger,campo,valor,nota,fecha`. Una fila por corrección:

| Decisión | Fila(s) a agregar |
|---|---|
| Corregir categoría | `<id>,categoria,<valor>,<por qué>,<hoy>` |
| Corregir ámbito | `<id>,ambito,casa\|personal,<por qué>,<hoy>` |
| Duplicado confirmado | `<id>,en_analisis,0,duplicado de <otro id>,<hoy>` |
| Cerrar un pendiente sin tocar datos | `<id>,motivo_resuelto,<motivo>,<decisión>,<hoy>` |

- `motivo_resuelto` es especial: cierra el pendiente `(id, motivo)` y **suprime su regeneración** en toda corrida futura. Usalo SIEMPRE que resuelvas un pendiente cuya causa no desaparece sola con el override (p.ej. `split_a_cargar`, `posible_duplicado` descartado, `transferencia_a_clasificar`). Para `sin_categoria` alcanza el override de `categoria` (la causa desaparece); no hace falta la fila extra.
- No marques `resuelto=1` en `pendientes.csv` a mano: lo hace `procesar_mails.py` al ver el `motivo_resuelto`.
- El `estado` de la fila (`pendiente`→`confirmado`) también lo recalcula el pipeline solo.

## Pasos

1. `git pull` (regla de CLAUDE.md). Leé `data/pendientes.csv` (filas con `resuelto` vacío) y las filas correspondientes de `data/ledger.csv`.
2. Presentale a Joaco los pendientes agrupados por motivo, en tandas cortas y con contexto (fecha, monto, comercio, cuenta). Para cada uno proponé una resolución concreta (la más probable primero). Usá AskUserQuestion cuando haya opciones claras.
3. Con las respuestas, escribí las filas en `data/resoluciones.csv` según la tabla de arriba. La `nota` registra la decisión de Joaco con sus palabras — es la memoria del porqué.
4. REGLAS NUEVAS: si una decisión es generalizable ("Handy del kiosco X siempre es Super"), agregá el patrón a `config/reglas_comercio.csv` para que no vuelva a caer en pendientes. Ídem transferencias recurrentes.
5. Si Joaco confirma que la casa ya volvió a la 8261 (o da la fecha del cambio), registrá la fecha en `config/parametros.json` (clave `fecha_8261_casa`) y resolvé en bloque los `ambito_a_confirmar`: posteriores a la fecha → `ambito,casa`; anteriores → preguntar si eran casa o personal.
6. SPLITS (`split_a_cargar`) — si Joaco confirma el split con fracción propia f:
   - Fila original: `<id>,monto,<monto×f>` y `<id>,monto_uyu,<monto_uyu×f>` en resoluciones (persistente).
   - Fila hermana (el resto va a Deudas): es una fila NUEVA — resoluciones no puede crearla. Es el **único** caso en que se agrega una fila al ledger, y va con `origen=manual` (las filas manual sobreviven al re-merge y a `--reprocesar`; ver `merge()` en `merge_y_validar.py`): `tipo=transferencia`, misma cuenta y fecha → `cuenta_destino=Deudas`, `monto` = resto, `en_analisis=0`, `estado=confirmado`, `fuente_id=<id original>-split`, `id` = primeros 12 hex de sha1(`"manual"+fuente_id`).
   - Cerrá el pendiente: `<id>,motivo_resuelto,split_a_cargar,<detalle>,<hoy>`.
   - **Invariante:** original×f + hermana = cargo real de la tarjeta. Verificalo sumando antes de dar por hecho.
   - Si es recurrente, registrá patrón y fracción en `config/splits_recurrentes.csv`. Recordale a Joaco cargarlo en Splitwise.
7. `formato_nuevo` (mail que el parser no supo leer): el dato NO está en el ledger — el mail quedó en `data/inbox_raw/` y el pendiente apunta al id que la fila habría tenido. Lee el mail, mostráselo a Joaco, y o bien se arregla el parser (y se corre `procesar_mails.py --reprocesar`), o se cierra con `motivo_resuelto` si el mail no era una transacción.
8. Aplicá y verificá: corré `C:/Users/joaco/anaconda3/python.exe scripts/procesar_mails.py` (aplica resoluciones y cierra pendientes) y después `scripts/merge_y_validar.py` (invariantes; sale ≠0 si algo está roto), `scripts/cierre_casa.py` (si cambió algo de ámbito casa) y `scripts/generar_dashboard_data.py` + `scripts/generar_dashboard_html.py`. Confirmá en la salida que los pendientes resueltos ya no aparecen y que el override quedó reflejado.
9. Commit: `git add data/resoluciones.csv data/ config/ out/` con mensaje `revision pendientes <fecha>: N resueltos` y `git push`.
10. Cerrá con el estado: pendientes restantes, gasto del mes vs presupuesto, y cualquier cosa rara detectada.
