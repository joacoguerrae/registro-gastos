Acá van los extractos que el pipeline no puede bajar solo. Los procesa el `/cierre-mes`.

## Movimientos de cuentas de débito (CSV)

Export del web de Santander (Movimientos → exportar CSV), uno por cuenta y período:
**Santander UYU (9071)**, **Cuenta Casa (8261)**, **Santander USD**.

Los procesa `scripts/conciliar_debito.py` (identifica la cuenta por el número del
encabezado; el mapeo vive en `config/cuentas_bancarias.csv`). Son la **única fuente** de:

- **Ingresos** (sueldo, transferencias recibidas): no generan aviso por mail.
- Débitos sin aviso (comisiones, débitos automáticos).
- El **saldo real de cierre** de cada cuenta.

## Estados de cuenta de tarjetas de crédito (PDF)

**BBVA Crédito** (día 1-7) y **Santander Mastercard** (día 5-8) **no mandan avisos por
transacción** → sus consumos entran *solo* por acá. **Santander Platinum (6427)** sí
manda avisos; su estado sirve para cerrar al peso y corregir el tipo de cambio real
de los cargos en USD.

Los procesa `scripts/conciliar_estado.py` (identifica el emisor por contenido;
contraseña = CI, en `config/secrets.local.json`). Los extractores de PDF están
**pendientes de calibrar con el primer PDF real**.
