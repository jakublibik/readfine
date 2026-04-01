def safe_int(value, default=None) -> int | None:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def clamp(value: int | None, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    return max(lo, min(hi, value))
