"""LLM 送信前のマスキング。complete() は自動でマスクしない。呼び出し側が明示的に
mask() → complete() → unmask() の順で呼ぶ。"""
import re
from typing import Any

from app import config

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)")


def mask(text: str, persons: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """個人情報をプレースホルダに置換し、復元表 {placeholder: original} を返す"""
    table: dict[str, str] = {}
    counters = {"EMAIL": 0, "PHONE": 0, "PERSON": 0}

    def _sub(kind: str, pattern):
        nonlocal text
        def repl(m):
            orig = m.group(0)
            for k, v in table.items():
                if v == orig:
                    return k
            counters[kind] += 1
            key = f"<{kind}_{counters[kind]}>"
            table[key] = orig
            return key
        text = pattern.sub(repl, text)

    _sub("EMAIL", _EMAIL)
    _sub("PHONE", _PHONE)
    # 人名は長い名前から順に置換（部分一致の誤爆を避ける）
    for name in sorted(persons or config.PERSONS, key=len, reverse=True):
        if name in text:
            _sub("PERSON", re.compile(re.escape(name)))
    return text, table


def unmask_text(s: str, table: dict[str, str]) -> str:
    for key, orig in table.items():
        s = s.replace(key, orig)
    return s


def unmask(obj: Any, table: dict[str, str]) -> Any:
    """LLM の出力（dict / list / str）に含まれるプレースホルダを元に戻す"""
    if isinstance(obj, str):
        return unmask_text(obj, table)
    if isinstance(obj, list):
        return [unmask(x, table) for x in obj]
    if isinstance(obj, dict):
        return {k: unmask(v, table) for k, v in obj.items()}
    return obj
