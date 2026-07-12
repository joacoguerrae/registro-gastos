import csv
led = list(csv.DictReader(open(r"data\ledger.csv", encoding="utf-8")))
hits = [r for r in led if r["monto"] in ("402.79","402.00") or (r["mes"]=="2026-06" and "uber" in (r["descripcion"]+r["comercio"]).lower())]
for r in hits:
    print(r["fecha"], r["origen"], r["cuenta"], r["monto"], r["categoria"], r["descripcion"][:40])
print("hits:", len(hits))
import hashlib
print("id BSE:", hashlib.sha1(b"mail19f465b363be7949").hexdigest()[:12])
