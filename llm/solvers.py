"""LLMに任せると取り違えやすい照合をコードで確定させるソルバー。

記号の特定 (Defuserの説明 → 記号ID) はLLMが得意なのでLLMに任せ、
列の照合や順番の決定のような機械的な処理だけをここで行う。
"""

import re

from .manual import KNOWLEDGE_DIR

KEYPAD_COLUMN_RE = re.compile(r"^- 列(\d+) \(上から押す順\): (.+)$", re.M)
KEYPAD_SYMBOL_RE = re.compile(r"^(\S)\((.*), (\d+-[a-z]+)\)$")


def load_keypad_columns() -> list[list[tuple[str, str, str]]]:
    """keypads.md の列一覧を [(記号ID, 文字, 見た目), ...] の列リストとして返す。未配置なら空。"""
    path = KNOWLEDGE_DIR / "keypads.md"
    if not path.exists():
        return []
    columns = []
    for _, body in KEYPAD_COLUMN_RE.findall(path.read_text(encoding="utf-8")):
        column = []
        for cell in body.split(" → "):
            match = KEYPAD_SYMBOL_RE.match(cell.strip())
            if match is None:
                raise ValueError(f"keypads.md の列一覧を解釈できません: {cell}")
            char, appearance, symbol_id = match.groups()
            column.append((symbol_id, char, appearance))
        columns.append(column)
    return columns


def keypad_symbol_ids() -> list[str]:
    return sorted({symbol_id for column in load_keypad_columns() for symbol_id, _, _ in column})


def solve_keypad(symbol_ids: list[str]) -> str:
    columns = load_keypad_columns()
    wanted = set(symbol_ids)
    if len(wanted) != len(symbol_ids):
        return "同じ記号が重複しています。4つのキーの記号をそれぞれ特定し直してください。"

    candidates = [
        (index, column) for index, column in enumerate(columns, 1)
        if wanted <= {symbol_id for symbol_id, _, _ in column}
    ]
    if not candidates:
        return (
            "指定した記号をすべて含む列はありません。記号の特定を誤っている可能性が高いので、"
            "自信のない記号の見た目を聞き返してください。"
        )
    if len(candidates) > 1:
        numbers = "、".join(f"列{index}" for index, _ in candidates)
        return f"候補の列が複数あります ({numbers})。残りの記号も特定してから再度呼んでください。"

    index, column = candidates[0]
    order = [f"{char}({appearance})" for symbol_id, char, appearance in column if symbol_id in wanted]
    return f"列{index}が該当。押す順番: " + " → ".join(order)
