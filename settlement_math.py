"""Shared Asian-line arithmetic. Profits are per one unit staked."""
import math
import re

OUTCOMES = ("won", "half_won", "push", "half_lost", "lost")

def parse_manual_line(value, label, *, allow_split=False):
    """Normalize notation, never division; leading minus applies to both legs."""
    raw = str(value).strip().translate(str.maketrans("＋－−／", "+--/"))
    if "/" in raw:
        if not allow_split:
            raise ValueError(f"{label}的分盤寫法僅支援足球")
        parts = [part.strip() for part in raw.split("/")]
        number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        if len(parts) != 2 or not all(re.fullmatch(number, part) for part in parts):
            raise ValueError(f"{label}分盤格式錯誤，例如 -0/0.5 或 2/2.5")
        left, right = map(float, parts)
        if parts[0].startswith("-") and not parts[1].startswith(("+", "-")):
            right = -right
        if (not all(math.isfinite(n * 4) and n * 2 == round(n * 2) for n in (left, right))
                or left * right < 0 or abs(left - right) != .5
                or (parts[0].startswith("-") and parts[1].startswith("+"))
                or (parts[0].startswith("+") and parts[1].startswith("-"))):
            raise ValueError(f"{label}分盤須為同方向、相差 0.5 的整數／半球線")
        line = (left + right) / 2
    else:
        try:
            line = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必須是數字，例如 -0.5、+0.5 或 2.5") from exc
    components(line)
    return line

def components(line):
    if not math.isfinite(line * 4) or abs(line * 4 - round(line * 4)) > 1e-7:
        raise ValueError("盤口必須是有限的 0.25 倍數")
    return (math.floor(line * 2) / 2, math.ceil(line * 2) / 2) if round(line * 4) % 2 else (line,)

def settle(value, line):
    signs = [1 if value + part > 0 else -1 if value + part < 0 else 0 for part in components(line)]
    return {1: "won", .5: "half_won", 0: "push", -.5: "half_lost", -1: "lost"}[sum(signs) / len(signs)]

def profit(outcome, price):
    if not math.isfinite(price) or price <= 1:
        raise ValueError("十進位賠率必須大於 1")
    return {"won": price - 1, "half_won": (price - 1) / 2,
            "push": 0, "half_lost": -.5, "lost": -1}[outcome]
