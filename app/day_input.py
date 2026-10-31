"""Validate explicit day references before model calls or day-list indexing."""
import re

MAX_TRIP_DAYS = 30
DAY_REFERENCE = re.compile(r"第\s*([+＋\-−－]?\s*\d+|[负負零〇一二两三四五六七八九十百]+)\s*天")
CHINESE_DIGITS = {c: n for n, c in enumerate("零一二三四五六七八九")}
CHINESE_DIGITS.update({"〇": 0, "两": 2})


def day_references(text, limit=MAX_TRIP_DAYS):
    values = []
    for match in DAY_REFERENCE.finditer(text):
        raw = re.sub(r"\s+", "", match[1]).translate(str.maketrans("＋−－", "+--"))
        try:
            if re.fullmatch(r"[+-]?\d+", raw):
                value = int(raw)
            elif raw in CHINESE_DIGITS:
                value = CHINESE_DIGITS[raw]
            else:
                tens, units = raw.split("十")
                value = (CHINESE_DIGITS[tens] if tens else 1) * 10 + (CHINESE_DIGITS[units] if units else 0)
        except (ValueError, KeyError):
            raise ValueError(f"活动日序必须在第1天到第{limit}天之间") from None
        if not 1 <= value <= limit:
            raise ValueError(f"活动日序必须在第1天到第{limit}天之间")
        values.append(value)
    return values
