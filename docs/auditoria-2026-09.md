# Auditoría 2026-09 — robustez y automatización

Auditoría e2e del 19/9/2026, dos meses después de la de julio ([auditoria-2026-07.md](auditoria-2026-07.md), cerrada 5/5). Aquella cerró el **pipeline automático**. Esta mira lo que quedó afuera: las **ceremonias manuales**, que es donde hoy se va el tiempo y donde nacen los errores.

Método: tres relevamientos independientes del código (pipeline, ceremonias, infra/datos) + la evidencia de dos cierres reales (julio y agosto) y siete revisiones de pendientes. Todo hallazgo tiene referencia a archivo y línea, verificada el 19/9 sobre el commit `bf9af9e`.

## Estado de las fases

| Fase | Qué | Estado |
|---|---|---|
| 0 | Red de seguridad: tests en CI, invariantes nuevos, respaldo, quick wins | **CERRADA 20/9/2026**; respaldo activo desde el 27/9 |
| 1 | Fuente de verdad para lo manual: `gastos.py`, fuentes reproducibles, planilla de carga | pendiente |
| 2 | Conciliación que entiende splits e importes finales + `cierre.py` de un comando | pendiente |
| 3 | Reglas con lint, gastos fijos operativos, tipo de cambio real | pendiente |
| 4 | Avisos que llegan: resumen semanal por mail, panel de atención, cero fallas mudas | pendiente |
| 5 | Capacidades: presupuesto generado, alerta de mitad de mes, patrimonio, vista para Lu | pendiente |
| 6 | Higiene: docs, scripts muertos, chuleta reducida a lo que no tiene guard | pendiente |

Actualizar esta tabla al cerrar cada fase (fecha + commit), como se hizo en julio.

## Veredicto

**Los datos están sanos y el pipeline automático es sólido.** 0 ids duplicados, 0 splits huérfanos, 0 gastos sin `monto_uyu` en 4.959 filas; dependencias y actions pinneadas; `salud_pasos` escala y se cierra solo; el ruleset de master frenó un force-push mío; los gates de `merge_y_validar` cazaron tres errores míos en estos dos meses. El fallback de Splitwise a exports se verificó en Actions el mismo día que se escribió.

**El sistema es robusto en lo automático y artesanal en lo manual.** Tres síntomas medibles:

1. **No existe ninguna herramienta para escribir en el ledger.** Las 47 filas `manual` y las 15 hermanas de split se crearon con Python descartable pegado en la terminal — unas 15 veces solo entre agosto y setiembre. El invariante de `CLAUDE.md` ("nunca editar `ledger.csv` a mano") lo contradice el propio procedimiento documentado, que manda crear hermanas y borrar filas a mano.
2. **Los conciliadores no modelan cómo funciona la plata en la realidad**: no entienden splits (el banco trae una línea, el ledger dos), ni que el importe final difiere del aviso (Uber, recargo del 3% en USD). Cada cierre fabrica duplicados garantizados que se limpian a mano. El cierre de agosto llevó horas, e incluyó un incidente propio: 53 filas reinsertadas sobre meses cerrados.
3. **La chuleta del cierre tiene 14 lecciones; 12 no tienen ningún guard en el código.** Cada lección es una trampa que sigue armada y que depende de que el asistente de turno la recuerde.

Y uno de fondo: **el canal de avisos está roto de hecho.** El sistema avisa por Issues de GitHub y Joaco no los recibe ni los mira (D5). Se acumularon 24 sin cerrar. Toda alerta que el sistema emite hoy no le llega a nadie.

## Decisiones de Joaco (19/9) — son requisitos del plan

| # | Tema | Decisión |
|---|---|---|
| D1 | Cierre de mes | **Un comando + excepciones.** Deja los archivos en la carpeta del mes, un script concilia todo en seco y reporta; la IA o él intervienen solo en lo que el script no pudo resolver. Objetivo 15-20 min. |
| D2 | Extractos bancarios | Dispuesto a exportarlos **semanal o quincenalmente** → la conciliación de débito tiene que ser incremental e idempotente. |
| D3 | Lo que el banco no ve | **Planilla de Google compartida** que llenan él o Lu desde el celular (gastos de Lu con su tarjeta, aportes, efectivo). El pipeline la lee. |
| D4 | Capacidades que usaría | Las cuatro: alerta de presupuesto a mitad de mes, presupuesto generado solo, patrimonio y ahorro, vista de la casa para Lu. |
| D5 | Avisos | No le llegan / no mira los Issues → **mail semanal de resumen** enviado por el pipeline + panel en el dashboard. Issues solo para fallas técnicas. |
| D6 | Respaldo | **Sí, semanal a Google Drive**, zip cifrado. |
| D7 | Fixtures de test | Datos **reales** (el repo es privado y `inbox_raw/` ya está commiteado). |
| D8 | Ejecución | **Por fases con gate**, como julio. Primero lo que evita pérdida de datos, después lo que ahorra tiempo, al final capacidades. |

## Hallazgos

IDs estables. `SA` alta · `SM` media · `SB` baja. No se re-reporta lo cerrado en julio.

### Alta

**SA1 — 150 filas irreproducibles sin red.** *(CERRADO 20/9: `data/staging/ledger_stats.json` + el chequeo `1.conteo_origen`. Verificado: borrar `ledger.csv` ahora da exit 1 con el detalle por origen; antes daba `validacion OK`.)* `merge()` reescribe `data/ledger.csv` entero en cada corrida (`merge_y_validar.py:137-140`) y conserva las filas `mail`/`estado_cuenta`/`manual`/`splitwise` **releyéndolas de su propia salida**, dentro de `if os.path.exists(OUT_LEDGER)` (`:103-107`). Si el archivo falta o se trunca, el merge produce un ledger con solo las 4.525 filas de migración y **la validación pasa**: no hay invariante de conteo ni de mezcla de orígenes. `mail` (284) se regenera desde `inbox_raw/`; `estado_cuenta` (71), `manual` (47) y `splitwise` (32) no tienen fuente.

**SA2 — Sin herramienta para altas manuales.** Solo escriben el ledger los scripts del pipeline y los conciliadores. La hermana de split se describe en prosa (`revisar-pendientes.md:33`) y se ejecuta con Python ad-hoc; ídem gastos pagados por Lu (tres artefactos a mano: fila + `cierre_exclusiones.csv` + `aportes_lu.csv`), aportes entre cuentas propias, cobros de `A cobrar`, borrado de duplicados. Errores reales de ese método en esta sesión: gasto sin `monto_uyu` al convertir una transferencia (dos veces), heredoc truncado por largo, borrado por línea física que no coincidía con filas CSV.

**SA3 — Los conciliadores no entienden splits ni importes finales.**
- *Split-unaware:* `conciliar_debito.conciliar_cuenta` compara fila a fila (`:154-168`); `conciliar_estado` excluye la hermana de las candidatas (`:477-488`). El total bancario de un split siempre se inserta como "nuevo" y hay que borrarlo (agosto: Alfredo 18.000, Movistar 1.847,21, Pantagruel 4.300; Platinum: MercadoLibre 2.607,79, Namaste 6.947,57, Indicabeer 870, Apple 24,72).
- *"El estado manda" es código muerto:* `conciliar_estado.py:517-526` corrige `monto_uyu` solo si la línea trae `monto_uyu`, y ningún extractor real lo produce (solo el test lo inyecta). En agosto fueron 11 resoluciones a mano (6 Uber, Anthropic, Google, ShareTheMeal, Apple, Namaste).
- *Tolerancia UYU 0,5%* (`:90`): la tarifa final de Uber difiere ~6% del aviso → duplicado + `sin_respaldo_estado`.
- *Ventana por rango de movimientos*, no por `desde/hasta` del extracto (`conciliar_debito.py:137-138`), aunque el parser ya los extrae (`:108-109`): el pago de Rodrigo (sábado 1/8, posteado lunes 3/8) y el aporte de 59.000 quedaron fuera y se proponían como nuevos.

**SA4 — `conciliar_estado.py`: sin dry-run, con barrido, y saltea líneas.** Escribe ledger + pendientes + resoluciones en la misma corrida (`:652-659`). Sin argumentos barre todo `data/estados/*.pdf` no logueado (`:621`) → incidente del 15/9: 53 filas sobre meses cerrados, una con id repetido. Un PDF no reconocible lanza `ValueError` fuera del `try` (`:408` vs `:592-597`) y aborta todo. `RE_MC_LINEA` (`:312`) exige fecha y saltea `I.V.A. 22%` (352,44 el 4/9); el desvío solo aparece como `diferencia_cierre` impreso, no corta. **Carpetas por mes invisibles:** `glob("estados/*.pdf")` y `os.listdir` no son recursivos (`conciliar_debito.py:290`), el log guarda solo `basename` (colisiona entre meses), y las rutas explícitas se resuelven distinto en cada script (`:288` relativa a `data/estados/`, `conciliar_estado.py:618` relativa al repo).

**SA5 — Fallas que quedan mudas en el pipeline.**
- Mails descartados **sin pendiente**: `procesar_mails.py:438/447` (router → ignorado), `:469-470` y `:552` (tarjeta desconocida), `:721-722` (MP sin monto), `:790` (recibo no parseable). Quedan en `procesados.csv` y no se reintentan. Una tarjeta nueva o un cambio de subject del banco vacía el flujo sin una señal. Contradice `fetch_mails.py:14-16`.
- Los pendientes de `sync_splitwise` **nunca notifican**: `sync.yml:185` cuenta solo la salida de `procesar_mails`.
- Cloudflare sin secrets: `exit 0` con `::notice::`, sin pasar por `salud_pasos` (`sync.yml:140-143`) → dashboard congelado para siempre.
- `gh issue create … || echo` (`sync.yml:190-191`): si falla, ese aviso se pierde.
- `generar_dashboard_data.py` sin envoltorio (`sync.yml:109`): si falla, se saltean `escalar` y el commit de una corrida ya validada.

**SA6 — El canal de avisos no llega a nadie.** Un Issue por corrida con pendientes, sin dedupe y sin cierre automático (24 acumulados al 19/9). Joaco no los recibe ni los mira (D5).

**SA7 — Motor de reglas: un catch-all a mitad de archivo mata 39 reglas.** *(PARCHE 20/9: `DESBORDE` movido antes del catch-all; el arreglo estructural sigue siendo de la Fase 3.1. Relevamiento sobre los 2.018 textos reales: las únicas sombreadas son `DESBORDE` (resuelta) y `DISCO N` (redundante, misma categoría, inocua). Quedan dos conflictos de especificidad que necesitan la columna `prioridad`: `CINE|CINEMA|MOVIE|TEATRO` pierde contra `PedidosYa` en «DLO*PedidosYa Movie M» (2 filas), y `GARAGE`/casa pierde contra `ANCAP` en «ANCAP GARAGE AMERICAN» (1 fila, conflicto de ámbito).)* `HANDY|MERPAGO\*` (L15 de `reglas_comercio.csv`) tiene categoría vacía a propósito y está antes que 39 reglas; "primera que matchea gana" (`procesar_mails.py:138-143`). Seis víctimas en dos meses, cinco arregladas a mano una por una (PedidosYa Plus, MERPAGO\*MERCADOLIBRE, MERPAGO\*MELI, MACROMERCADO, HANDY Wanderers). **Sexta, viva hoy:** `DESBORDE` (L50) nunca gana — verificado contra los 1.978 textos del ledger. Además `CINE|MOVIE` (L35) pierde contra PedidosYa (L4) en "PedidosYa Movie"; `GARAGE/casa` pierde contra `ANCAP`; `DISCO N` es regla muerta.

**SA8 — Sin tests en CI.** *(CERRADO 20/9: `tests/` con 18 tests sobre datos reales, `tests.yml` en cada push y `-m smoke` dentro de `sync.yml`.)* No hay `tests/` ni paso de test. `scripts/test_conciliacion.py` existe y no lo invoca nadie. La paridad de migración es manual desde el 17/7. Todos los bugs de estos dos meses se encontraron en datos de producción.

**SA9 — Sin respaldo fuera de GitHub + la PC.** *(IMPLEMENTADO 20/9, ACTIVO 27/9 — primer respaldo subido: zip AES-256 con `pyzipper` — la stdlib no escribe cifrado —, 461 archivos / 12,77 MB → 5,50 MB, subida con el OAuth de usuario (scope `drive.file`), retención 26, workflow propio `respaldo.yml`. Gate de restauración verde: restaurado en un directorio limpio, 461/461 sha256 OK y `merge_y_validar` exit 0. Falta re-autorizar Google y crear el secret `BACKUP_ZIP_PASSWORD`: ver [respaldo.md](respaldo.md).)* `resoluciones.csv` son 756 decisiones humanas irrecuperables.

### Media

**SM1 — `config/gastos_fijos.csv` no lo lee ningún script.** El control que habría cazado el ANTEL de julio es una instrucción en prosa. Y es la llave de una automatización grande: "Proyecto PAS" + monto conocido (ANTEL 2.102,95 exacto; pago de tarjeta = total del último estado; Tigo; UTE en rango) se puede clasificar solo — hoy son 4-6 pendientes por mes.

**SM2 — Presupuesto artesanal.** No hay generador; agosto y setiembre se armaron con scripts descartables. Un mes sin filas desaparece del reporte en silencio (`generar_dashboard_data.py:46-56`).

**SM3 — Tipo de cambio estático desde el 11/7** (`parametros.json:2-3`). Y `tc_estimado` es ruido puro: nada lo resuelve en automático (el único camino, `conciliar_estado.py:576-582`, depende del código muerto de SA3); transferencias y Splitwise en USD no lo resuelven nunca. Se cierran a mano (~12 en setiembre).

**SM4 — `resoluciones.csv` sin lint.** *(CERRADO 20/9. Corrección de lo que decía esta auditoría, medido al implementar: hoy hay **cero** resoluciones con `campo` desconocido, y las claves repetidas con valores distintos son **2**, no 35 — las otras 33 son filas `motivo_resuelto`, que son un set de (id, motivo) y no un override. Los 59 ids huérfanos quedan como INFO: no pueden frenar el pipeline.)* Un `campo` desconocido se ignora en silencio (`procesar_mails.py:877`, `merge_y_validar.py:128`); 59 entradas apuntan a ids inexistentes; 35 claves `(id,campo)` repetidas, gana la última sin aviso (hoy benigno: `348b2e5d8083` 8,00→8,24 es una corrección legítima). Mismo riesgo de id huérfano en `cierre_exclusiones.csv` y `participaciones.csv`.

**SM5 — Cuentas sin validar.** *(CERRADO 20/9: el catálogo pasa de 12 a 31 cuentas y las violaciones de 211 apariciones a 0. Además un guard `1.cuentas_catalogo` que rechaza con FALLO cualquier nombre `<cuenta base> <moneda>`: catalogar `Santander Platinum USD` dejaba la tarjeta con 2 movimientos de 20, medido.)* 19 cuentas fuera de `config/cuentas.csv` en 167 filas (mayoría legado). Un typo entra callado: en esta sesión aparecieron `Lu pesos`, `Lu`, `Gletir`.

**SM6 — `sync_splitwise`.** *(PARCIALMENTE CERRADO 20/9.)* **Corrección: el caso testigo del asado que usaba esta auditoría es falso.** `sw:4650212111` (6.951 del 25/8) se pagó en DOS familias de cargo de la Mastercard (1.608,25 + hermana 4.825,16, y 129,50 + hermana 388,50), y el matcher compara UN cargo + su hermana contra el total con ±1%: incluir `estado_cuenta` NO lo resuelve — hace falta matching por familia agregada (Fase 2.1). Y la versión de este plan, sin guard, estaba **medida como dañina**: reabría el split `Burger` (`sw:4544350903`), resuelto el 12/7, sobre una fila con `en_analisis=0`. Se mergeó con el guard `en_analisis == \"1\"`, verificado no-op sobre los datos de hoy. Lo que sí quedó cerrado es el orden: corría **antes** que `procesar_mails` → un gasto del mismo día genera dos pendientes (primero sobre `rid`, después sobre `match["id"]`; `:517-522`). `cargos` excluye `origen=estado_cuenta` (`:416`) → un split pagado con la Mastercard (caso asado) nunca se cierra solo. `transferencia_a_clasificar` incondicional en pagos (`:540,:565`). Referencia de saldo parcial. `DATED_AFTER` y `YO_EXPORT` fijos.

**SM7 — `estado_sync.json` ensucia el árbol local.** *(CERRADO 20/9: `ultimo_sync` solo lo escribe Actions y el archivo se escribe con `newline="\n"`; verificado byte a byte que una corrida local lo deja idéntico al commiteado.)* `ultimo_sync` se reescribe en toda corrida de `procesar_mails` (`:914-917`), también en las locales → `git pull` aborta (pasó 3 veces). El único campo necesario es `ultima_fecha_mail`.

**SM8 — Fallback silencioso de coeficientes.** Si el mes no tiene fila en `ingresos.csv` se usa el del mes anterior sin avisar (`merge_y_validar.py:381-382`, ídem `cierre_casa`). El presupuesto de setiembre se armó con el coeficiente equivocado por esto.

**SM9 — Saldos.** `conciliar_saldos` pisa las filas de la misma `fecha_corte` (`:247-248`) y preserva solo el comentario; semilla de fecha única editada a mano; sin dry-run.

**SM10 — Docs desactualizados.** `fase3.md:8` (API de Splitwise como fuente única), `README.md:68` ("semana shadow", extractores "stubs"), `pipeline-mails.md:6` (Gmail MCP), `plan-migracion-fetch.md:8-13` (fases sin tildar), `esquema.md:10` (intérprete). Solo `CLAUDE.md` está al día.

**SM11 — `actualizar_sheets.py:81-93`**: `clear()` + `update` sin retry; un 429 entre ambos deja la hoja vacía.

**SM12 — `aportes_lu.csv` sin clave de dedupe**: re-correr la ceremonia duplica el aporte.

### Baja

**SB1** CRLF *(CERRADO 20/9; y eran más de los que decía esta auditoría: **14 puntos de escritura CSV en 9 scripts**, no 3, más 5 `open()` de texto commiteado)*. **SB2** Scripts muertos: `bajar_inbox_raw_jul2026.py`, `guardar_inbox_raw.py`, `inbox_raw_desde_threads.py`, `verificar_presupuesto.py`, `verificar_backfill_jul2026.py`, `sync_gastos_headless.cmd`, migradores; `registro_app.mmbackup` (2,3 MB). **SB3** Constantes que se pudren: `DEFAULT_AFTER`, `BACKFILL_DESDE`, ventana `DEDUPE_1M_*`, flag `ambito_8261_confirmado` (código muerto), rutas de anaconda en los `.cmd`. **SB4** `salud_pasos`: el Issue dice "exit 1" sin stderr (`:96`); `escalar` mudo si falla `gh` (`:109-113`). **SB5** Permisos del workflow a nivel repo; scopes de los secretos sin documentar; `.gitignore` por ruta exacta. **SB6** `inbox_raw/` sin retención (~2 MB/año — aceptar). **SB7** 2 transferencias sin `cuenta_destino`; 56 pendientes huérfanos.

## Plan por fases

**Reglas para el agente que ejecute** (valen para todas las fases):

1. Leer `CLAUDE.md` y este documento entero antes de tocar nada. `git pull` al empezar.
2. **El pipeline sigue vivo 2×/día y commitea `data/` a master.** Los cambios de código van en ramas cortas que tocan solo `scripts/`, `config/`, `tests/`, `.github/`, `docs/` — nunca `data/` en una rama. Integrar rápido (rebase + push el mismo día).
3. **Todo gate se prueba sobre una COPIA**, no sobre el repo vivo: los scripts respetan `GASTOS_BASE` (patrón usado el 19/9 para validar el adaptador de Splitwise). Copiar `data/` + `config/` a un directorio temporal y correr ahí.
4. Verificar el plan contra el código antes de ejecutarlo: las líneas citadas son del commit `bf9af9e` y pueden haberse movido. Si una premisa no se cumple, parar y anotarlo acá antes de improvisar.
5. Cada fase cierra con: tests verdes en CI, `merge_y_validar.py` OK, **una corrida manual del workflow en verde** (`gh workflow run "Sync gastos"`), tabla de estado actualizada, `CLAUDE.md` al día.
6. No cambiar una decisión D1-D8 sin preguntarle a Joaco. Los montos de este documento son evidencia, no fixtures: usar los datos reales del repo.

### Fase 0 — Red de seguridad *(antes de refactorizar nada)*

Cierra SA8, SA9, parte de SA1, SM4, SM5 y los quick wins. Es condición de las demás: sin tests, refactorizar los conciliadores es jugar a ciegas.

- **0.1 Tests en CI.** `tests/` con pytest (agregar `pytest` pinneado a `requirements.txt`), workflow `tests.yml` en push y como primer paso de `sync.yml`. Arrancar con **tests de caracterización**: copiar el repo a un temporal, correr `procesar_mails.py --reprocesar` y exigir que las filas `origen=mail` salgan idénticas a las actuales; ídem `sync_splitwise.py --fuente export` (hoy es idempotente byte a byte). Migrar `scripts/test_conciliacion.py` a pytest. Fixtures = datos reales (D7).
- **0.2 Invariantes nuevos en `merge_y_validar.py`:** conteo por origen persistido en `data/staging/ledger_stats.json` — si baja un origen no regenerable sin un borrado registrado, DESVIO; `fuente_id` único; `cuenta`/`cuenta_destino` ∈ `config/cuentas.csv` (dar de alta las 19 cuentas legado como inactivas, y `Lu tarjeta`, `Lu`, `Gletir`, `OCA` con su tipo); dominio de `tipo`/`ambito`/`en_analisis`/`moneda`; transferencia con destino. **Lint de `resoluciones.csv`:** `campo` desconocido = error; ids huérfanos y claves repetidas = warning con conteo (ídem `cierre_exclusiones` y `participaciones`).
- **0.3 Respaldo semanal a Drive (D6).** Zip cifrado de `data/` + `config/` (sin secretos). Subirlo con el OAuth del usuario y scope `drive.file`, no con la service account (las creadas desde 2025 no tienen cuota propia). **Acción de Joaco, una sola vez:** re-autorizar con `get_gmail_token.py` ampliado a `gmail.readonly` + `gmail.send` (lo necesita la fase 4) + `drive.file`, y actualizar el secret `GMAIL_REFRESH_TOKEN`. La clave del zip va como secret nuevo.
- **0.4 Quick wins** (≤10 líneas cada uno): `lineterminator="\n"` (SB1) · `sync_splitwise` **después** de `procesar_mails` en `sync.yml` y en los `.cmd`, y `cargos` incluye `estado_cuenta` (SM6) · `ultimo_sync` solo se escribe si `GITHUB_ACTIONS` está definido (SM7) · mover `DESBORDE` antes del catch-all (SA7, provisorio hasta la fase 3).

**Gate:** tests verdes en CI · sobre una copia, truncar `ledger.csv` a la mitad y correr `merge_y_validar.py` → tiene que salir ≠ 0 · un zip restaurado en un directorio limpio pasa la validación · un `git pull` después de una corrida local del pipeline no aborta.

### Fase 1 — Fuente de verdad para lo manual

Cierra SA1, SA2, SM12 y D3. Idea rectora: **`ledger.csv` pasa a ser un artefacto de build**; todo lo que hoy vive solo adentro de él pasa a tener una fuente.

- **1.1 Fuentes reproducibles.** `data/fuentes/manual.csv` y `data/fuentes/estado_cuenta.csv` con las 47 + 71 filas actuales migradas. `merge()` las lee de ahí, no de su propia salida. Los conciliadores escriben en la fuente. Borrar una fila = sacarla de la fuente con registro en `data/fuentes/borrados.csv` (id, motivo, fecha). Las filas `splitwise` ya son reproducibles desde `data/splitwise.csv` + reglas: verificarlo con un test.
- **1.2 `scripts/gastos.py` — la única puerta de escritura manual.** Subcomandos, todos con `--dry-run` por defecto y `--aplicar` explícito, todos validando categoría/cuenta/montos y aplicando los invariantes que hoy están en prosa:
  - `split <id> --mi-parte X [--participacion 0.5]` → ajusta la original vía resoluciones, crea la hermana, verifica original + hermana = cargo, cierra el pendiente, y si la cuenta es de la casa avisa que genera retiro.
  - `pago-lu` → atómico: fila `Lu tarjeta` + exclusión del aporte implícito + aporte de Lu (hoy tres archivos a mano).
  - `aporte`, `transferencia`, `gasto-manual`, `cobro` (contra `A cobrar`).
  - `resolver <id> campo=valor … --cerrar <motivo>` → reemplaza las filas de resoluciones escritas a mano; si `tipo` pasa de transferencia a gasto exige/estima `monto_uyu`.
  - `borrar <id> --motivo` → solo orígenes no-mail; escribe en `borrados.csv`.
  Actualizar `revisar-pendientes.md` y `cierre-mes.md` para usarlo, y reformular el invariante de `CLAUDE.md`: *toda escritura manual pasa por `gastos.py`*.
- **1.3 Planilla "Carga manual" (D3).** **Joaco la crea y la comparte con la service account** (que ya tiene scope de Sheets). Pestañas: `Gastos` (fecha, monto, moneda, qué, quién pagó: *Joaco efectivo / Lu tarjeta / Lu efectivo*, ámbito, categoría opcional, nota), `Aportes` (fecha, quién, monto), `Saldos` (fecha, cuenta, saldo — la usa la fase 5). Paso nuevo `leer_carga_manual.py`: baja un snapshot a `data/fuentes/carga_manual.csv` (commiteado → reproducible), genera filas con id estable por contenido, valida y manda lo inválido a pendientes. Los aportes de Lu y las exclusiones de sus gastos **se derivan** de la planilla: `aportes_lu.csv` y las entradas de `cierre_exclusiones.csv` de ese tipo dejan de editarse a mano.

**Gate:** sobre una copia, **borrar `data/ledger.csv` y reconstruirlo** → idéntico al actual · recrear con `gastos.py` tres casos reales ya cargados (split Pantagruel, los tres gastos de Lu de agosto, la compensación del Dilema) y obtener las mismas filas · una fila inválida en la planilla genera un pendiente, no una fila rota.

### Fase 2 — Conciliación que entiende la realidad + cierre de un comando

Cierra SA3, SA4, SM9 y D1/D2. Es la fase que más tiempo devuelve.

- **2.1 Módulo común `scripts/conciliacion.py`** usado por débito y estados: (a) candidatas agrupadas por **familia de split** (original + `-split` = una candidata por el total); (b) dos pasadas — exacta, y luego *mismo comercio ±3 días, importe dentro de ~10%* marcada `importe_ajustado`, que **emite la resolución `monto` y `monto_uyu` := importe del estado** (el estado manda, para UYU y para moneda extranjera; repartiendo proporcional si es un split) — esto reemplaza el código muerto de `correcciones_tc`; (c) ventana = `desde/hasta` del extracto ±3 días.
- **2.2 `conciliar_estado.py`:** `--dry-run` por defecto; nunca barre — recibe archivos o una carpeta de mes, y rechaza un PDF cuyo período ≤ último mes cerrado salvo `--forzar`; PDF no reconocible = se saltea con aviso y sigue; parsear líneas sin fecha (`I.V.A.`, intereses) y **cortar si Σ líneas ≠ total declarado**; reconocer la línea de PAGOS y aparearla con la transferencia. El layout del portal queda fuera de alcance (documentarlo).
- **2.3 `conciliar_debito.py` incremental (D2):** usar la columna **`Referencia`** del CSV del Santander como `fuente_id` → importar extractos solapados nunca duplica. `config/reglas_extracto.csv` para lo que solo se ve en el extracto: `SUELDOS`→Sueldo · `PAHORRO`→par interno de compra de dólares · `PAGO ELECTRONICO TARJETA`→transferencia a la tarjeta · `COMISION`→Otros · recibida de `DECAUX`→aporte de Lu · devoluciones de EONE→gasto negativo de Auto. Hoy todo crédito cae en `ingreso_a_clasificar`.
- **2.4 Carpetas por mes:** descubrimiento recursivo en `data/estados/<MM-AA>/`, log por ruta relativa, una sola convención de rutas; el fetch guarda los PDF del mail directo en la carpeta del mes.
- **2.5 `scripts/cierre.py <AAAA-MM>` (D1).** Orquesta en seco: descubre insumos → débitos → estados → export de Splitwise → control de fijos (fase 3) → saldos (los de extracto salen solos; efectivo y otros de la pestaña `Saldos`) → `cierre_casa` → `conciliar_saldos`. Emite `out/cierre_<mes>.md` con: lo resuelto solo, **EXCEPCIONES que requieren decisión**, saldos, posición, presupuesto. `--aplicar` escribe. Al cerrar con todas las cuentas OK **re-siembra solo**; `conciliar_saldos` agrega historia en vez de pisar. `cierre-mes.md` se reduce a: correr, leer excepciones, resolverlas con `gastos.py`, aplicar.

**Gate — reproducir el cierre de agosto.** Sobre un worktree en el commit anterior a `2e2fccc` con los insumos de agosto, `cierre.py 2026-08` tiene que llegar a la **misma posición (Lu −3.446,37 / Joaco −7.825,94) y a los mismos saldos, con cero ediciones manuales de filas**, y las excepciones tienen que ser solo las genuinamente humanas (los cuatro créditos sin identificar, la letra de cambio, la participación 50/50 del asado). Además: importar dos veces el mismo extracto, y un extracto parcial seguido del completo, no cambia nada.

### Fase 3 — Reglas, gastos fijos y tipo de cambio

Cierra SA7, SM1, SM3.

- **3.1 Reglas con prioridad y lint.** Columna `prioridad` (o bloque final) para los catch-all: se evalúan **al final**. Test de CI: para cada regla tiene que existir al menos un texto real del ledger donde **gane**; si no, está muerta o sombreada → falla. Ese test habría cazado los seis incidentes.
- **3.2 `gastos_fijos.csv` operativo.** (a) `verificar_fijos.py`: fijo esperado y no visto a `dia_aprox + 7` → ítem del resumen; (b) clasificación por monto conocido de "Proyecto PAS" y transferencias recurrentes (ANTEL exacto, UTE en rango, pago de tarjeta = total del último estado, Tigo, psicóloga, cuota del auto vía Lu) → categoría + descripción sin pendiente; (c) `cuotas_restantes` se descuenta solo y avisa "última cuota este mes".
- **3.3 Tipo de cambio.** `config/tc_historico.csv` alimentado a diario por el pipeline desde una fuente pública (BCU), con fallback al último conocido y aviso si lleva >7 días sin actualizar. `tc_estimado` deja de ser pendiente: pasa a marca informativa que el estado corrige solo.

**Gate:** lint verde · reprocesando agosto-setiembre sobre una copia, contar cuántos `sin_categoria` y `transferencia_a_clasificar` históricos se habrían resuelto solos (reportar antes/después) · cero pendientes `tc_estimado` abiertos.

### Fase 4 — Avisos que llegan

Cierra SA5, SA6, SB4 y D5.

- **4.1 Ningún descarte mudo:** todo camino `ignorado` de `procesar_mails.py` para remitentes bancarios genera un pendiente `mail_ignorado` (tarjeta desconocida, monto no parseable), salvo denylist explícita.
- **4.2 `scripts/resumen.py`, un solo armador de avisos:** pendientes de **todas** las fuentes, fijos faltantes, proyección de presupuesto, posición de casa y cuánto girar, salud de pasos, antigüedad del export de Splitwise **y del último extracto** (D2: "hace 9 días que no hay extracto"). Salidas: panel **"Requiere tu atención"** en el dashboard + **mail semanal** (lunes) por Gmail API con `gmail.send`; mail inmediato solo por falla técnica ≥3 corridas. Se elimina el Issue por corrida de pendientes; los Issues quedan para `salud_pasos`.
- **4.3 Workflow:** `generar_dashboard_data` bajo `salud_pasos`; `escalar` y commit corren aunque falle un paso no crítico posterior a la validación; Cloudflare sin secrets = falla registrada; `salud_pasos` guarda las últimas 20 líneas de stderr.

**Gate:** fixture de mail con tarjeta desconocida → pendiente · el resumen de prueba le llega a Joaco y él lo confirma · forzar 3 fallas de un paso en una copia → escala con el stderr adentro.

### Fase 5 — Capacidades (D4)

- **5.1 `generar_presupuesto.py <mes>`:** fijos vigentes (cuotas restantes, bimestrales por paridad de mes) × coeficiente **del mes** + variable = mediana de 3 meses por categoría, excluyendo extraordinarios. Comentario trazable por fila. Si faltan los ingresos del mes: aviso explícito en el resumen, nunca fallback silencioso (SM8).
- **5.2 Alerta de mitad de mes:** proyección = fijos que faltan cobrar + ritmo del variable, por categoría y total. Va al resumen y al panel.
- **5.3 Patrimonio y ahorro:** saldos bancarios (de extractos) + inversiones y efectivo (pestaña `Saldos`) × tc → `out/patrimonio.csv` y pestaña del dashboard: patrimonio neto, posición en USD, tasa de ahorro mensual, con los extraordinarios (auto) aparte.
- **5.4 Vista para Lu:** página `casa.html` con posición, cuánto girar cada uno, gasto de casa por categoría y sus aportes. **Sin ningún dato `ambito=personal` de Joaco** (test que lo verifique). **Acción de Joaco:** agregar el mail de Lu a la política de Cloudflare Access, restringida a esa ruta.

**Gate:** el generador reproduce el presupuesto de setiembre armado a mano dentro de ±3% con los mismos insumos · test de no-fuga de datos personales en `casa.html` · Lu entra y ve su página.

### Fase 6 — Higiene

Cierra SM10, SM11, SB2, SB3, SB5.

Reescribir `fase3.md`, `README.md`, `pipeline-mails.md`; cerrar `plan-migracion-fetch.md`. Borrar o archivar scripts muertos y `registro_app.mmbackup`. Retry con backoff en `actualizar_sheets.py` y escribir antes de limpiar. Permisos del workflow por job; documentar scopes de cada secreto. Limpiar constantes muertas. **Reducir la chuleta:** cada lección que ahora tiene guard se borra; lo que quede tiene que ser solo lo que de verdad requiere criterio humano.

**Gate:** ningún doc describe algo que el código ya no hace (revisión cruzada) · la chuleta de `cierre-mes.md` baja de 14 lecciones a las que no tengan guard, con la lista explícita de por qué cada una sigue ahí.

## Lo que necesita Joaco (acciones que ningún agente puede hacer)

1. ~~**Re-autorizar Google una vez** (fase 0.3): `gmail.readonly` + `gmail.send` + `drive.file`, y actualizar el secret.~~ **Hecho el 27/9.**
2. **Crear la planilla "Carga manual"** y compartirla con la service account (fase 1.3).
3. **Agregar el mail de Lu** a Cloudflare Access (fase 5.4).
4. **Exportar los 3 CSV del Santander** semanal o quincenalmente a la carpeta del mes (D2), y los grupos de Splitwise al cierre.
5. Confirmar que el primer resumen semanal le llega (gate de la fase 4).

## Orden y esfuerzo

0 → 1 → 2 son secuenciales (cada una apoya a la siguiente). 3 y 4 pueden ir en paralelo después de la 1. 5 necesita 1 (pestaña `Saldos`), 3 (fijos) y 4 (resumen). 6 al final. Estimación en sesiones de trabajo de un agente: F0 1 · F1 2 · F2 3 · F3 1-2 · F4 1-2 · F5 2-3 · F6 1.

**Si hubiera que cortar:** las fases 0-2 solas ya eliminan el riesgo de pérdida de datos y devuelven la mayor parte del tiempo del cierre. La 4 es la que hace que el resto sirva de algo: un sistema que avisa a un canal que nadie mira no avisa.

## Cierre de la Fase 0 — 20/9/2026

Qué quedó, medido:

- **El gate.** Antes: borrar `data/ledger.csv` entero daba `validacion OK` y exit 0, y el pipeline lo habría commiteado — se perdían 434 filas, 150 de ellas sin ninguna fuente para regenerarlas. Ahora da exit 1 con el detalle por origen (`mail: 286 -> 0`, etc.) y no se commitea. Una baja deliberada se acepta explícitamente con `--aceptar-bajas`.
- **18 tests** sobre datos reales, ninguno escribe en el repo. `tests.yml` corre todo en cada push; `sync.yml` corre `-m smoke` (~4 s) antes de tocar datos.
- **14 chequeos** en `merge_y_validar` contra los 6 de antes: conteo por origen, catálogo de cuentas + guard anti-sufijo, transferencias sin destino con allowlist auto-caducante, y lint de resoluciones/exclusiones/participaciones.
- **Respaldo** implementado y probado end-to-end salvo la subida (bloqueada por la acción de Joaco; **destrabada el 27/9**: primer respaldo en Drive, 485 archivos, 5,5 MB), con tres capas anti-filtración de secretos verificadas por separado.

Lo que la ejecución **corrigió de esta misma auditoría**: el caso testigo de SM6 era falso, SB1 subestimaba el alcance (14 puntos, no 3), SM4 sobreestimaba el problema (2 claves repetidas reales, no 35), y QW3 tal como estaba escrito era dañino. Queda anotado en cada hallazgo.

**Deuda que la Fase 0 deja anotada:** `merge_y_validar.py --migracion` sale exit 1 desde antes de esta fase, por `2e.presupuesto` (21 meses contra los 19 "congelados"). No afecta al pipeline, que nunca corre `--migracion`, pero es el mundo congelado pudriéndose: va a la Fase 6.

### Incidente post-cierre: el sync estuvo caído 4 días (21/9 → 25/9)

Un test smoke de la propia Fase 0 (`test_no_se_siembra_el_gate_desde_un_ledger_roto`) asumía que `ledger_stats.json` no existía. Era cierto al escribirlo; dejó de serlo esa misma noche, cuando Actions sembró el archivo y lo commiteó (`da91709`). Desde la corrida siguiente, `sync.yml` frenaba en el paso de tests y no commiteaba: **9 corridas en rojo seguidas**, del 21/9 05:03 al 25/9 05:03 UTC. Se detectó recién al pushear otro cambio el 25/9, y se arregló en `87def19` (el test borra el archivo a propósito y exige que el fallo venga de `1.conteo_origen`).

- **Datos:** no se perdió nada. `fetch_mails` arranca en `ultima_fecha_mail − OVERLAP_DAYS`, y la corrida de recuperación bajó desde el 18/9 (15 mails nuevos).
- **Lo que prueba:** es SA5/D5 en vivo. El pipeline se cortó bien —no commiteó datos con un test rojo— pero **nadie se enteró en 4 días**: `sync.yml` no tiene ningún paso `if: failure()`, así que un paso rojo corta el job antes de cualquier aviso propio (ni Issue). El único aviso fue el mail genérico "Run failed" de GitHub, que es justo el canal que D5 dice que no se mira. Refuerza el mail inmediato por falla técnica ≥3 corridas de la 4.2: acá habrían sido 3 avisos el primer día.
- **Lección para el agente:** un test que corre dentro del pipeline de datos no puede depender de archivos que ese mismo pipeline escribe. Y nunca `pytest ... | tail && git commit`: el pipe se come el exit code de pytest.
