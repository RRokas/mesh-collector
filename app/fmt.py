"""Display helpers shared by templates and alert messages."""


def fmt_duration(sec: float) -> str:
    """Human-sized duration, sign only when negative: 0.4 s, 12 s, -12 min, 3.2 h, -365 d."""
    sign = "-" if sec < 0 else ""
    a = abs(sec)
    if a < 0.05:
        return "0 s"
    if a < 10:
        return f"{sign}{a:.1f} s"
    if a < 90:
        return f"{sign}{a:.0f} s"
    if a < 90 * 60:
        return f"{sign}{a / 60:.0f} min"
    if a < 48 * 3600:
        return f"{sign}{a / 3600:.1f} h"
    return f"{sign}{a / 86400:.0f} d"
