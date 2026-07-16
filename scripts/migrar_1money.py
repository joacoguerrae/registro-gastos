# -*- coding: utf-8 -*-
"""
Migra la base de 1Money (data/raw/MyFinance.db, SQLite) al formato ledger
definido en docs/esquema.md. Genera data/staging/ledger_1money.csv.

Reglas aplicadas (ver docs/esquema.md):
- Solo entidades activas (isRemoved=0) en transaction, transfer, account,
  category y sync_link.
- Montos INTEGER en centesimos -> /100.
- Transaccion: si amountInAccountCurrency no es NULL -> ese monto +
  accountCurrencyCode; si no -> amountInDefaultCurrency + UYU.
  monto_uyu = amountInDefaultCurrency/100 siempre.
- Transferencia: monto = fromAmount/100 en fromCurrencyCode; si la moneda
  destino difiere se anota "-> {toAmount/100} {toCurrencyCode}" en descripcion.
  monto_uyu solo si fromCurrencyCode == UYU; si no, vacio.
- Cuentas canonicas via config/cuentas.csv (alias_1money); si no hay alias se
  conserva el titulo original.
- Categorias via config/mapeo_categorias.csv (case-insensitive, sin espacios
  sobrantes). subcategoria = titulo original. La categoria "Otros" de 1Money
  se desambigua por tipo: Expense -> "Otros (gasto)", Income -> "Otros (ingreso)".
- Ambito: cuenta "Casa" -> casa; cuenta "Deudas" -> split; categoria
  "Gasto de Tercerl" -> split; transferencias y ajustes -> interno;
  resto -> personal.
- Ajustes de saldo (transacciones activas sin categoria): ambito=interno,
  categoria vacia, subcategoria "Ajuste saldo", en_analisis=0.
- Anti doble conteo CASA: desde 2023-05-01 toda transaccion de cuenta "Casa"
  o categoria "Casa" queda en_analisis=0 (la planilla es la fuente canonica
  en las ventanas 2023-05 -> 2024-08 y 2024-09 -> hoy). Antes de 2023-05
  1Money es canonico -> en_analisis=1.
- Transferencias: ambito=interno, en_analisis=0.
- id = sha1('1money' + uid)[:12]; origen=1money; estado=confirmado;
  fuente_id=uid; comercio vacio.
"""
import os
import csv
import hashlib
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(os.environ.get("GASTOS_BASE") or Path(__file__).resolve().parent.parent)
DB = BASE / "data" / "raw" / "MyFinance.db"
OUT = BASE / "data" / "staging" / "ledger_1money.csv"
CUENTAS_CSV = BASE / "config" / "cuentas.csv"
MAPEO_CSV = BASE / "config" / "mapeo_categorias.csv"

CASA_CUTOFF = "2023-05-01"   # desde aca 1Money duplica a la planilla
CASA_W2_START = "2024-09-01"  # inicio segunda ventana (solo para reporte)

COLUMNS = [
    "id", "fecha", "mes", "tipo", "monto", "moneda", "monto_uyu",
    "cuenta", "cuenta_destino", "categoria", "subcategoria", "comercio",
    "descripcion", "ambito", "origen", "estado", "en_analisis", "fuente_id",
]


def norm(s):
    return (s or "").strip().casefold()


def fmt2(cents):
    """centesimos (int) -> string decimal positivo con 2 decimales"""
    v = abs(int(cents))
    return f"{v // 100}.{v % 100:02d}"


def make_id(uid):
    return hashlib.sha1(("1money" + uid).encode("utf-8")).hexdigest()[:12]


def load_alias_map():
    alias = {}
    with open(CUENTAS_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            a = (row.get("alias_1money") or "").strip()
            if a:
                alias[norm(a)] = row["cuenta"].strip()
    return alias


def load_cat_map():
    m = {}
    with open(MAPEO_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            m[norm(row["original"])] = row["categoria"].strip()
    return m


def main():
    alias_map = load_alias_map()
    cat_map = load_cat_map()

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    accounts = {r["uid"]: r["title"].strip()
                for r in cur.execute("SELECT uid, title FROM account WHERE isRemoved=0")}
    # titulo de categoria: se toma aunque la categoria este removida, siempre
    # que el link este activo (para no perder el detalle historico)
    categories = {r["uid"]: (r["title"].strip(), r["type"])
                  for r in cur.execute("SELECT uid, title, type FROM category")}
    cats_removed = {r["uid"] for r in cur.execute(
        "SELECT uid FROM category WHERE isRemoved=1")}

    def links(entity_type, other_type):
        q = ("SELECT entityUid, otherUid FROM sync_link "
             "WHERE entityType=? AND otherType=? AND isRemoved=0")
        return {r["entityUid"]: r["otherUid"] for r in cur.execute(q, (entity_type, other_type))}

    tx_account = links("Transaction", "Account")
    tx_category = links("Transaction", "Category")
    tr_from = links("Transfer", "FromAccount")
    tr_to = links("Transfer", "ToAccount")

    def canonical(title):
        return alias_map.get(norm(title), title)

    rows = []
    unmatched_cats = Counter()
    en0_reasons = Counter()
    linked_removed_cat = 0
    warnings = []

    # ---------------- transacciones ----------------
    for t in cur.execute(
        "SELECT uid, type, amountInDefaultCurrency, amountInAccountCurrency, "
        "accountCurrencyCode, date, comment FROM [transaction] WHERE isRemoved=0 "
        "ORDER BY date, uid"
    ):
        uid = t["uid"]
        fecha = (t["date"] or "").strip()[:10]
        if len(fecha) != 10:
            warnings.append(f"fecha rara en transaccion {uid}: {t['date']!r}")
            continue
        tipo = "gasto" if t["type"] == "Expense" else "ingreso"

        if t["amountInAccountCurrency"] is not None:
            monto = fmt2(t["amountInAccountCurrency"])
            moneda = (t["accountCurrencyCode"] or "UYU").strip()
        else:
            monto = fmt2(t["amountInDefaultCurrency"])
            moneda = "UYU"
        monto_uyu = fmt2(t["amountInDefaultCurrency"])

        acc_uid = tx_account.get(uid)
        acc_title = accounts.get(acc_uid, "")
        if not acc_title:
            warnings.append(f"transaccion {uid} sin cuenta activa")
        cuenta = canonical(acc_title)

        cat_uid = tx_category.get(uid)
        if cat_uid is None:
            # ajuste de saldo
            categoria, subcategoria = "", "Ajuste saldo"
            ambito, en_analisis = "interno", "0"
            en0_reasons["ajuste_saldo (sin categoria)"] += 1
            is_adjust = True
            cat_title = None
        else:
            is_adjust = False
            cat_title, cat_type = categories.get(cat_uid, ("", None))
            if cat_uid in cats_removed:
                linked_removed_cat += 1
            key = cat_title
            if norm(cat_title) == "otros":
                key = "Otros (gasto)" if cat_type == "Expense" else "Otros (ingreso)"
            categoria = cat_map.get(norm(key))
            if categoria is None:
                unmatched_cats[cat_title] += 1
                categoria = "Otros"
            subcategoria = cat_title

            # ambito
            if norm(cat_title) == "gasto de tercerl":
                ambito = "split"
            elif norm(acc_title) == "casa":
                ambito = "casa"
            elif norm(acc_title) == "deudas":
                ambito = "split"
            else:
                ambito = "personal"

            # anti doble conteo CASA
            es_casa = norm(acc_title) == "casa" or norm(cat_title) == "casa"
            if es_casa and fecha >= CASA_CUTOFF:
                en_analisis = "0"
                if fecha >= CASA_W2_START:
                    en0_reasons["casa duplicado ventana 2024-09->hoy"] += 1
                else:
                    en0_reasons["casa duplicado ventana 2023-05->2024-08"] += 1
            else:
                en_analisis = "1"

        rows.append({
            "id": make_id(uid), "fecha": fecha, "mes": fecha[:7], "tipo": tipo,
            "monto": monto, "moneda": moneda, "monto_uyu": monto_uyu,
            "cuenta": cuenta, "cuenta_destino": "",
            "categoria": categoria, "subcategoria": subcategoria,
            "comercio": "", "descripcion": (t["comment"] or "").strip(),
            "ambito": ambito if not is_adjust else "interno",
            "origen": "1money", "estado": "confirmado",
            "en_analisis": en_analisis, "fuente_id": uid,
        })

    # ---------------- transferencias ----------------
    for t in cur.execute(
        "SELECT uid, fromAmount, fromCurrencyCode, toAmount, toCurrencyCode, "
        "date, comment FROM transfer WHERE isRemoved=0 ORDER BY date, uid"
    ):
        uid = t["uid"]
        fecha = (t["date"] or "").strip()[:10]
        if len(fecha) != 10:
            warnings.append(f"fecha rara en transfer {uid}: {t['date']!r}")
            continue
        from_cur = (t["fromCurrencyCode"] or "UYU").strip()
        to_cur = (t["toCurrencyCode"] or "").strip()
        monto = fmt2(t["fromAmount"])
        monto_uyu = monto if from_cur == "UYU" else ""

        descripcion = (t["comment"] or "").strip()
        if to_cur and to_cur != from_cur:
            nota = f"→ {fmt2(t['toAmount'])} {to_cur}"
            descripcion = f"{descripcion} {nota}".strip()

        cuenta = canonical(accounts.get(tr_from.get(uid), ""))
        cuenta_destino = canonical(accounts.get(tr_to.get(uid), ""))
        if not cuenta or not cuenta_destino:
            warnings.append(f"transfer {uid} sin cuenta origen/destino activa")

        en0_reasons["transferencia (ambito interno)"] += 1
        rows.append({
            "id": make_id(uid), "fecha": fecha, "mes": fecha[:7],
            "tipo": "transferencia", "monto": monto, "moneda": from_cur,
            "monto_uyu": monto_uyu, "cuenta": cuenta,
            "cuenta_destino": cuenta_destino, "categoria": "",
            "subcategoria": "", "comercio": "", "descripcion": descripcion,
            "ambito": "interno", "origen": "1money", "estado": "confirmado",
            "en_analisis": "0", "fuente_id": uid,
        })

    con.close()

    rows.sort(key=lambda r: (r["fecha"], r["fuente_id"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # ---------------- reporte ----------------
    out = sys.stdout
    print(f"OK -> {OUT}", file=out)
    print(f"filas totales: {len(rows)}", file=out)

    por_tipo = Counter(r["tipo"] for r in rows)
    print("\nfilas por tipo:", dict(por_tipo), file=out)

    print("\nsuma monto_uyu de gastos en_analisis=1 por anio:", file=out)
    por_anio = defaultdict(float)
    for r in rows:
        if r["tipo"] == "gasto" and r["en_analisis"] == "1":
            por_anio[r["fecha"][:4]] += float(r["monto_uyu"])
    for y in sorted(por_anio):
        print(f"  {y}: {por_anio[y]:.2f}", file=out)

    print("\ncategorias originales SIN match en mapeo:",
          dict(unmatched_cats) if unmatched_cats else "ninguna (0)", file=out)

    print("\nfilas en_analisis=0 por regla:", file=out)
    for k, v in sorted(en0_reasons.items()):
        print(f"  {k}: {v}", file=out)
    print(f"  TOTAL en_analisis=0: {sum(en0_reasons.values())}", file=out)
    print(f"  TOTAL en_analisis=1: {len(rows) - sum(en0_reasons.values())}", file=out)

    en1_por_ambito = Counter(r["ambito"] for r in rows)
    print("\nfilas por ambito:", dict(en1_por_ambito), file=out)

    if linked_removed_cat:
        print(f"\nAVISO: {linked_removed_cat} transacciones activas apuntan a "
              "categoria con isRemoved=1 (se uso el titulo igual)", file=out)
    if warnings:
        print("\nWARNINGS:", file=out)
        for wmsg in warnings:
            print(" ", wmsg, file=out)


if __name__ == "__main__":
    main()
