"""Decoding of the 16-byte tag payload.

Layout: 0-3 node (BE), 4-5 temp (signed, 0.01 C), 6-7 hum (0.01 %RH),
8 status, 9-12 seq (BE), 13-14 reserved, 15 hop count.

The byte order of temp/hum is unconfirmed, so it is a parameter.
"""
from __future__ import annotations

import struct
from typing import Optional


def decode_raw(raw: str, byte_order: str = "be") -> Optional[dict]:
    """Return {node, temp, hum, status, seq, hops} or None if raw is unusable."""
    try:
        b = bytes.fromhex(raw)
    except (ValueError, TypeError):
        return None
    if len(b) != 16:
        return None
    th = ">hH" if byte_order == "be" else "<hH"
    t, h = struct.unpack(th, b[4:8])
    return {
        "node": b[0:4].hex().upper(),
        "temp": round(t / 100, 2),
        "hum": round(h / 100, 2),
        "status": b[8],
        "seq": struct.unpack(">I", b[9:13])[0],
        "hops": b[15],
    }


def effective_values(
    mode: str, raw: Optional[str], router_temp: Optional[float], router_hum: Optional[float]
) -> tuple[Optional[float], Optional[float]]:
    """temp/hum to store as the 'effective' reading, per DECODE_MODE.

    router  -> router's values (falls back to big-endian raw decode if missing)
    raw_be  -> decode raw as big-endian (falls back to router values)
    raw_le  -> decode raw as little-endian (falls back to router values)
    """
    if mode in ("raw_be", "raw_le"):
        d = decode_raw(raw, "be" if mode == "raw_be" else "le") if raw else None
        if d is not None:
            return d["temp"], d["hum"]
        return router_temp, router_hum
    if router_temp is None or router_hum is None:
        d = decode_raw(raw, "be") if raw else None
        if d is not None:
            return (router_temp if router_temp is not None else d["temp"],
                    router_hum if router_hum is not None else d["hum"])
    return router_temp, router_hum
