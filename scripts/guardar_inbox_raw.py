# -*- coding: utf-8 -*-
"""Materializa el dump crudo de mails en data/inbox_raw/<message_id>.json (UTF-8).

Uso: python scripts/guardar_inbox_raw.py <batch.json> [<batch2.json> ...]

Cada <batch.json> es una lista de objetos con el contrato de docs/pipeline-mails.md:
  {"message_id","thread_id","fecha_mail","from","subject","body_text"}

La descarga desde Gmail la hace Claude via MCP (search_threads + get_thread
FULL_CONTENT, un JSON por MENSAJE, no por thread). Este script solo valida el
contrato y escribe los archivos; es idempotente: no pisa archivos existentes.
"""
import json
import sys
from pathlib import Path

CAMPOS = ["message_id", "thread_id", "fecha_mail", "from", "subject", "body_text"]
RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "data" / "inbox_raw"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("uso: guardar_inbox_raw.py <batch.json> [...]")
    DESTINO.mkdir(parents=True, exist_ok=True)
    nuevos = existentes = 0
    for arg in sys.argv[1:]:
        batch = json.loads(Path(arg).read_text(encoding="utf-8"))
        for m in batch:
            faltan = [c for c in CAMPOS if not m.get(c)]
            if faltan:
                raise SystemExit(f"mensaje {m.get('message_id', '?')}: faltan campos {faltan}")
            out = DESTINO / f"{m['message_id']}.json"
            if out.exists():
                existentes += 1
                continue
            payload = {c: m[c] for c in CAMPOS}
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            nuevos += 1
    total = len(list(DESTINO.glob("*.json")))
    print(f"nuevos={nuevos} ya_existian={existentes} total_en_carpeta={total}")


if __name__ == "__main__":
    main()
