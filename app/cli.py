"""Maintenance commands.

  python -m app.cli redecode --mode raw_le   # recompute temp/hum from raw
  python -m app.cli redecode --mode router   # restore router-decoded values
  python -m app.cli decode <32-hex raw>      # inspect one payload both ways
  python -m app.cli quarantine               # show rejected records
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select, update

from .config import Settings
from .db import init_db, make_engine, quarantine, records
from .decode import decode_raw, effective_values


def cmd_redecode(args) -> int:
    s = Settings.from_env()
    engine = make_engine(s.database_url)
    init_db(engine)
    n = 0
    with engine.begin() as conn:
        rows = conn.execute(
            select(records.c.id, records.c.raw, records.c.temp_router, records.c.hum_router)
        ).all()
        for rid, raw, tr, hr in rows:
            t, h = effective_values(args.mode, raw, tr, hr)
            conn.execute(update(records).where(records.c.id == rid).values(temp=t, hum=h))
            n += 1
    print(f"re-decoded {n} records with mode={args.mode}")
    if args.mode != s.decode_mode:
        print(f"NOTE: set DECODE_MODE={args.mode} so new records match", file=sys.stderr)
    return 0


def cmd_decode(args) -> int:
    for order in ("be", "le"):
        print(order, json.dumps(decode_raw(args.raw, order)))
    return 0


def cmd_quarantine(args) -> int:
    s = Settings.from_env()
    engine = make_engine(s.database_url)
    init_db(engine)
    with engine.connect() as conn:
        for r in conn.execute(select(quarantine).order_by(quarantine.c.pk.desc()).limit(args.limit)):
            print(json.dumps(dict(r._mapping)))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("redecode", help="recompute effective temp/hum for all stored records")
    r.add_argument("--mode", choices=["router", "raw_be", "raw_le"], required=True)
    r.set_defaults(fn=cmd_redecode)
    d = sub.add_parser("decode", help="decode one raw payload as BE and LE")
    d.add_argument("raw")
    d.set_defaults(fn=cmd_decode)
    q = sub.add_parser("quarantine", help="list quarantined records")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=cmd_quarantine)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
