"""Shared Asian-line arithmetic. Profits are per one unit staked."""
import math

OUTCOMES = ("won", "half_won", "push", "half_lost", "lost")

def components(line):
    if not math.isfinite(line) or abs(line * 4 - round(line * 4)) > 1e-7:
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
