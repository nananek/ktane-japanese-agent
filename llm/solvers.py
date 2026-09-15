"""LLMに任せると取り違えやすい照合や状態管理をコードで確定させるソルバー。

Defuserの説明を構造化 (記号ID、座標、色など) するのはLLMに任せ、
表の照合・経路探索・ステージをまたぐ記録のような機械的な処理だけをここで行う。
判定データはコードに持たず、knowledge/modules/ の書き起こしから読み取る。
"""

import re
from collections import deque
from dataclasses import dataclass

from .bomb_state import BombState
from .manual import KNOWLEDGE_DIR

KANJI_ORDINALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class SolverResult:
    """ソルバーの結果。speech があれば、LLMに文章化させずそのまま読み上げてよい確定した発話。

    答えが決まった後にLLMへもう1往復させると応答が1〜2秒遅れるため、定型で言える結果はここで作る。
    """

    text: str
    speech: str | None = None


def _read(file_name: str) -> str | None:
    path = KNOWLEDGE_DIR / file_name
    return path.read_text(encoding="utf-8") if path.exists() else None


# ---------------------------------------------------------------- キーパッド

KEYPAD_COLUMN_RE = re.compile(r"^- 列(\d+) \(上から押す順\): (.+)$", re.M)
KEYPAD_SYMBOL_RE = re.compile(r"^(\S)\((.*), (\d+-[a-z]+)\)$")


def load_keypad_columns() -> list[list[tuple[str, str, str]]]:
    """keypads.md の列一覧を [(記号ID, 文字, 見た目), ...] の列リストとして返す。未配置なら空。"""
    text = _read("keypads.md")
    if text is None:
        return []
    columns = []
    for _, body in KEYPAD_COLUMN_RE.findall(text):
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
        # 1記号だけ食い違う列があれば、その記号の特定ミスの可能性が高いので聞き返す的を示す
        appearances = {symbol_id: appearance for column in columns for symbol_id, _, appearance in column}
        hints = []
        for index, column in enumerate(columns, 1):
            column_ids = {symbol_id for symbol_id, _, _ in column}
            missing = [symbol_id for symbol_id in symbol_ids if symbol_id not in column_ids]
            if len(missing) == 1 and len(symbol_ids) > 1:
                alternatives = "、".join(
                    f"{char}({appearance})" for symbol_id, char, appearance in column if symbol_id not in wanted
                )
                hints.append(
                    f"列{index}なら「{appearances.get(missing[0], missing[0])}」({missing[0]})以外の3つが一致する。"
                    f"その記号は実は次のどれかではないか: {alternatives}"
                )
        message = "指定した記号をすべて含む列はありません。記号の特定を誤っている可能性が高いです。"
        if hints:
            return message + "\n" + "\n".join(hints) + "\n食い違っている記号の見た目だけを聞き返してください。"
        return message + "自信のない記号の見た目を聞き返してください。"
    if len(candidates) > 1:
        numbers = "、".join(f"列{index}" for index, _ in candidates)
        return f"候補の列が複数あります ({numbers})。残りの記号も特定してから再度呼んでください。"

    index, column = candidates[0]
    order = [(char, name) for symbol_id, char, name in column if symbol_id in wanted]
    if len(order) < 4:
        return f"列{index}に絞れたが記号が{len(order)}つしかない。残りの記号も聞いてから再度呼んでください。"
    return SolverResult(
        f"列{index}が該当。押す順番: " + " → ".join(f"{char}({name})" for char, name in order),
        "、".join(name for _, name in order) + "の順に押して。",
    )


# ---------------------------------------------------------------- 記憶

@dataclass(frozen=True)
class MemoryRule:
    kind: str  # "position" | "label" | "same_position" | "same_label"
    value: int  # 位置・ラベルの数字、または参照するステージ番号


MEMORY_STAGE_RE = re.compile(r"^## ステージ(\d)\n(.*?)(?=^## |\Z)", re.M | re.S)
MEMORY_ROW_RE = re.compile(r"^\| (\d) \| (.+?) \|$", re.M)


def _parse_memory_action(action: str) -> MemoryRule:
    if match := re.fullmatch(r"([一二三四])番目のボタンを押す", action):
        return MemoryRule("position", KANJI_ORDINALS[match.group(1)])
    if match := re.fullmatch(r"「(\d)」と書かれたボタンを押す", action):
        return MemoryRule("label", int(match.group(1)))
    if match := re.fullmatch(r"ステージ(\d)で押したのと同じ位置のボタンを押す", action):
        return MemoryRule("same_position", int(match.group(1)))
    if match := re.fullmatch(r"ステージ(\d)で押したのと同じラベルのボタンを押す", action):
        return MemoryRule("same_label", int(match.group(1)))
    raise ValueError(f"memory.md の操作を解釈できません: {action}")


def load_memory_rules() -> dict[int, dict[int, MemoryRule]]:
    """memory.md を {ステージ: {ディスプレー: ルール}} として返す。未配置なら空。"""
    text = _read("memory.md")
    if text is None:
        return {}
    return {
        int(stage): {int(display): _parse_memory_action(action) for display, action in MEMORY_ROW_RE.findall(body)}
        for stage, body in MEMORY_STAGE_RE.findall(text)
    }


def solve_memory(state: BombState, display: int, buttons: list[int], stage: int | None = None) -> str:
    rules = load_memory_rules()
    if stage is None:
        stage = len(state.memory_presses) + 1
    if not 1 <= stage <= len(rules):
        return f"ステージは1〜{len(rules)}で指定してください。"
    if stage - 1 > len(state.memory_presses):
        return f"ステージ{len(state.memory_presses) + 1}以前の記録がありません。そのステージから入力し直してください。"
    if len(buttons) != 4 or sorted(buttons) != [1, 2, 3, 4]:
        return "ボタンのラベルは左から順に1〜4の数字を1つずつ、4つ指定してください。"
    if display not in rules[stage]:
        return "ディスプレーの数字は1〜4で指定してください。"

    # 誤答でステージ1に戻った場合などに備え、指定ステージ以降の記録は捨てる
    del state.memory_presses[stage - 1:]
    rule = rules[stage][display]
    if rule.kind == "position":
        position = rule.value
    elif rule.kind == "label":
        position = buttons.index(rule.value) + 1
    elif rule.kind == "same_position":
        position = state.memory_presses[rule.value - 1][0]
    else:
        position = buttons.index(state.memory_presses[rule.value - 1][1]) + 1
    label = buttons[position - 1]
    state.memory_presses.append((position, label))

    done = "これで解除。" if stage == len(rules) else f"次はステージ{stage + 1}。"
    return SolverResult(
        f"ステージ{stage}: 左から{position}番目、ラベル「{label}」のボタンを押す。{done}",
        f"左から{position}番目、「{label}」を押して。",
    )


# ---------------------------------------------------------------- 順番ワイヤ

WIRE_COLORS = {"red": "赤", "blue": "青", "black": "黒"}
WIRE_SECTION_RE = re.compile(r"^## (赤|青|黒)いワイヤの出現回数\n(.*?)(?=^## |\Z)", re.M | re.S)
WIRE_ROW_RE = re.compile(r"^\| ([一二三四五六七八九])番目 \| ([ABC](?:か[ABC])*) \|$", re.M)


def load_wire_sequence_rules() -> dict[str, dict[int, set[str]]]:
    """wire-sequences.md を {色: {出現回数: 切るべき接続先}} として返す。未配置なら空。"""
    text = _read("wire-sequences.md")
    if text is None:
        return {}
    color_ids = {name: color for color, name in WIRE_COLORS.items()}
    return {
        color_ids[color_name]: {
            KANJI_ORDINALS[ordinal]: set(targets.split("か")) for ordinal, targets in WIRE_ROW_RE.findall(body)
        }
        for color_name, body in WIRE_SECTION_RE.findall(text)
    }


def solve_wire_sequence(state: BombState, panel: int, wires: list[dict]) -> str:
    rules = load_wire_sequence_rules()
    parsed = []
    for wire in wires:
        color, target = wire.get("color"), str(wire.get("target", "")).upper()
        if color not in WIRE_COLORS or target not in ("A", "B", "C"):
            return "各ワイヤは color (red/blue/black) と target (A/B/C) で指定してください。"
        parsed.append((color, target))

    # 同じパネルを言い直した場合も二重に数えないよう、パネル単位で置き換えてから数え直す
    state.wire_sequence_panels[panel] = parsed
    for later_panel in [n for n in state.wire_sequence_panels if n > panel]:
        del state.wire_sequence_panels[later_panel]
    counts = {color: 0 for color in WIRE_COLORS}
    for earlier_panel in sorted(n for n in state.wire_sequence_panels if n < panel):
        for color, _ in state.wire_sequence_panels[earlier_panel]:
            counts[color] += 1

    results = []
    counts_at = []
    for index, (color, target) in enumerate(parsed, 1):
        counts[color] += 1
        cut_targets = rules[color].get(counts[color])
        if cut_targets is None:
            return f"{WIRE_COLORS[color]}のワイヤが{counts[color]}本目で、表の範囲を超えています。入力を確認してください。"
        verdict = "切る" if target in cut_targets else "切らない"
        counts_at.append(counts[color])
        results.append(f"{index}本目({WIRE_COLORS[color]}→{target}, {WIRE_COLORS[color]}{counts[color]}本目): {verdict}")
    cut = [str(i) for i, (color, target) in enumerate(parsed, 1) if target in rules[color].get(counts_at[i - 1], set())]
    speech = f"{'、'.join(cut)}本目を切って、次のパネルへ。" if cut else "どれも切らずに次のパネルへ。"
    return SolverResult(f"パネル{panel}: " + "、".join(results), speech)


# ---------------------------------------------------------------- 迷路

MAZE_SECTION_RE = re.compile(r"^### (maze\d) \(丸印の位置: (.+?)\)\n```\n(.*?)\n```", re.M | re.S)
MAZE_SIZE = 6
DIRECTIONS = {"上": (-1, 0), "下": (1, 0), "左": (0, -1), "右": (0, 1)}


@dataclass(frozen=True)
class Maze:
    name: str
    circles: frozenset[tuple[int, int]]  # (行, 列) 0始まり
    lines: tuple[str, ...]

    def can_move(self, row: int, col: int, direction: str) -> bool:
        d_row, d_col = DIRECTIONS[direction]
        new_row, new_col = row + d_row, col + d_col
        if not (0 <= new_row < MAZE_SIZE and 0 <= new_col < MAZE_SIZE):
            return False
        if d_col:  # 左右: セル行の縦壁を見る
            wall_x = 3 * max(col, new_col)
            return self.lines[2 * row + 1][wall_x] != "|"
        wall_row = 2 * max(row, new_row)  # 上下: 横壁の行を見る
        return self.lines[wall_row][3 * col + 1:3 * col + 3] != "--"


def load_mazes() -> list[Maze]:
    text = _read("mazes.md")
    if text is None:
        return []
    mazes = []
    for name, circles, grid in MAZE_SECTION_RE.findall(text):
        positions = frozenset((int(r), int(c)) for r, c in re.findall(r"\((\d),(\d)\)", circles))
        lines = tuple(line.ljust(3 * MAZE_SIZE + 1) for line in grid.splitlines())
        if len(lines) != 2 * MAZE_SIZE + 1:
            raise ValueError(f"mazes.md の {name} の迷路図を解釈できません")
        mazes.append(Maze(name, positions, lines))
    return mazes


def _to_cell(position: dict) -> tuple[int, int] | None:
    try:
        row, col = int(position["row"]), int(position["column"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (1 <= row <= MAZE_SIZE and 1 <= col <= MAZE_SIZE):
        return None
    return row - 1, col - 1


def solve_maze(circles: list[dict], start: dict, goal: dict) -> str:
    cells = [_to_cell(c) for c in circles]
    start_cell, goal_cell = _to_cell(start), _to_cell(goal)
    if None in cells or not cells or start_cell is None or goal_cell is None:
        return "位置は左から数えた列 column と上から数えた行 row (どちらも1〜6) で指定してください。"

    candidates = [maze for maze in load_mazes() if set(cells) <= maze.circles]
    if not candidates:
        return "その丸印の位置に一致する迷路がありません。丸印の位置を聞き直してください。"
    if len(candidates) > 1:
        return "丸印の位置に一致する迷路が複数あります。もう1つの丸印の位置も聞いてください。"
    maze = candidates[0]

    previous: dict[tuple[int, int], tuple[tuple[int, int], str] | None] = {start_cell: None}
    queue = deque([start_cell])
    while queue:
        cell = queue.popleft()
        if cell == goal_cell:
            break
        for direction, (d_row, d_col) in DIRECTIONS.items():
            if maze.can_move(*cell, direction):
                neighbor = (cell[0] + d_row, cell[1] + d_col)
                if neighbor not in previous:
                    previous[neighbor] = (cell, direction)
                    queue.append(neighbor)
    if goal_cell not in previous:
        return "経路が見つかりません。白い点と赤い三角の位置を聞き直してください。"

    moves = []
    cell = goal_cell
    while previous[cell] is not None:
        cell, direction = previous[cell]
        moves.append(direction)
    moves.reverse()
    if not moves:
        return "白い点はすでにゴールにいます。"

    # 「右、右、下」→「右2、下1」のように連続する同方向をまとめる
    grouped = []
    for direction in moves:
        if grouped and grouped[-1][0] == direction:
            grouped[-1][1] += 1
        else:
            grouped.append([direction, 1])
    moves_text = "、".join(f"{d}{n}" for d, n in grouped)
    return SolverResult(f"{maze.name}が該当。押す順番: {moves_text}", f"{moves_text}。")


# ---------------------------------------------------------------- ワイヤ・ボタン共通 (条件付きルールの評価)
#
# wires.md / the-button.md は「条件 (かつ 条件) の場合、操作」の優先順位付きルールで書かれている。
# 条件の文言を原子条件に分解し、爆弾の情報が未判明な変数は取りうる値を総当たりして、
# 答えが変わる変数だけを「聞くべき情報」として返す (答えに影響しない質問で時間を使わないため)。

RULE_LINE_RE = re.compile(r"^\d+\. (.+?)。?$", re.M)
COLOR_NAMES = {"赤": "red", "青": "blue", "黄色": "yellow", "白": "white", "黒": "black"}
COLOR_RE = "(赤|青|黄色|白|黒)"
UNKNOWN_VARIABLE_LABELS = {
    "serial_odd": "シリアルナンバーの最後の数字が奇数か",
    "batteries": "バッテリーの本数",
}
# 読み上げ用の短い聞き方
QUESTION_LABELS = {"serial_odd": "シリアルの末尾は奇数", "batteries": "電池は何本"}


def _split_rule(sentence: str) -> tuple[list[str], str]:
    sentence = sentence.removeprefix("そうでない場合、")
    condition, _, action = sentence.rpartition("、")
    atoms = [a for a in re.split(r"かつ|、", condition) if a] if condition else []
    return atoms, action


def _enumerate(unknowns: dict[str, list]) -> list[dict]:
    assignments = [{}]
    for name, values in unknowns.items():
        assignments = [{**a, name: v} for a in assignments for v in values]
    return assignments


def _decide(rules, evaluate, known: dict, unknowns: dict[str, list], resolve=lambda action, env: action):
    """全ての未判明変数の組み合わせで先頭一致ルールを評価し、(一意な結果 or None, 影響する変数名) を返す。

    resolve は操作文を実際の結果に変換する (「最後の赤いワイヤ」と「二本目のワイヤ」が同じワイヤなら同じ答え)。
    """
    outcomes = []
    for assignment in _enumerate(unknowns):
        env = {**known, **assignment}
        action = next((action for atoms, action in rules if all(evaluate(atom, env) for atom in atoms)), None)
        outcome = None if action is None else resolve(action, env)
        outcomes.append((assignment, outcome))
    distinct = {outcome for _, outcome in outcomes}
    if len(distinct) == 1:
        return distinct.pop(), []
    relevant = []
    for name in unknowns:
        for assignment, outcome in outcomes:
            for other_assignment, other_outcome in outcomes:
                differs_only_here = all(
                    assignment[k] == other_assignment[k] for k in unknowns if k != name
                ) and assignment[name] != other_assignment[name]
                if differs_only_here and outcome != other_outcome and name not in relevant:
                    relevant.append(name)
    return None, relevant


def _serial_odd(state: BombState) -> bool | None:
    if state.serial_last_digit_odd is not None:
        return state.serial_last_digit_odd
    digits = [c for c in state.serial_number or "" if c.isdigit()]
    return int(digits[-1]) % 2 == 1 if digits else None


def _missing_message(names: list[str]) -> SolverResult:
    items = "、".join(UNKNOWN_VARIABLE_LABELS.get(n, n) for n in names)
    return SolverResult(
        f"判定には次の情報が必要です: {items}。これらをまとめて1回で聞き、update_bomb_info で記録してから再度呼んでください。",
        "、".join(QUESTION_LABELS.get(n, UNKNOWN_VARIABLE_LABELS.get(n, n)) for n in names) + "？",
    )


# ---------------------------------------------------------------- ワイヤ

WIRE_SECTION_COUNT_RE = re.compile(r"^## (\d)本のワイヤ\n(.*?)(?=^## |\Z)", re.M | re.S)


def load_wire_rules() -> dict[int, list[tuple[list[str], str]]]:
    text = _read("wires.md")
    if text is None:
        return {}
    return {int(n): [_split_rule(s) for s in RULE_LINE_RE.findall(body)] for n, body in WIRE_SECTION_COUNT_RE.findall(text)}


def _wire_atom(atom: str, env: dict) -> bool:
    colors = env["colors"]
    if match := re.search(f"最後のワイヤが{COLOR_RE}", atom):
        return colors[-1] == COLOR_NAMES[match.group(1)]
    if match := re.search(f"{COLOR_RE}いワイヤがな[けく]", atom):
        return colors.count(COLOR_NAMES[match.group(1)]) == 0
    if match := re.search(f"{COLOR_RE}いワイヤが一本よりも多", atom):
        return colors.count(COLOR_NAMES[match.group(1)]) > 1
    if match := re.search(f"{COLOR_RE}いワイヤが一本しかな", atom):
        return colors.count(COLOR_NAMES[match.group(1)]) == 1
    if "シリアルナンバーの最後の数字が奇数" in atom:
        return env["serial_odd"]
    raise ValueError(f"wires.md の条件を解釈できません: {atom}")


def _wire_action(action: str, colors: list[str]) -> str:
    match = re.fullmatch(f"(最初|最後|[一二三四五六]本目)の(?:{COLOR_RE}い)?ワイヤを切る", action)
    if match is None:
        raise ValueError(f"wires.md の操作を解釈できません: {action}")
    position, color_name = match.groups()
    if color_name:
        color = COLOR_NAMES[color_name]
        index = max(i for i, c in enumerate(colors) if c == color) + 1
    elif position == "最初":
        index = 1
    elif position == "最後":
        index = len(colors)
    else:
        index = KANJI_ORDINALS[position[0]]
    color_label = {v: k for k, v in COLOR_NAMES.items()}[colors[index - 1]]
    return f"上から{index}本目({color_label})のワイヤを切る。"


def solve_wires(state: BombState, colors: list[str]) -> str:
    rules = load_wire_rules()
    if len(colors) not in rules or any(c not in COLOR_NAMES.values() for c in colors):
        return "ワイヤは3〜6本、色は red/blue/yellow/white/black を上から順に指定してください。"
    serial_odd = _serial_odd(state)
    unknowns = {} if serial_odd is not None else {"serial_odd": [False, True]}
    answer, missing = _decide(
        rules[len(colors)], _wire_atom, {"colors": colors, "serial_odd": serial_odd}, unknowns,
        resolve=lambda action, env: _wire_action(action, env["colors"]),
    )
    if answer is None:
        return _missing_message(missing)
    return SolverResult(answer, answer.removesuffix("のワイヤを切る。").replace("(", "、").replace(")", "の") + "ワイヤを切って。")


# ---------------------------------------------------------------- ボタン

BUTTON_RELEASE_RE = re.compile(r"^- \*\*(.+?)場合：\*\* カウントダウンタイマーに(\d)が表示されているときに離す", re.M)


def load_button_rules() -> tuple[list[tuple[list[str], str]], dict[str, int]]:
    text = _read("the-button.md")
    if text is None:
        return [], {}
    rules_part, _, release_part = text.partition("## ボタンを離すタイミング")
    rules = [_split_rule(s) for s in RULE_LINE_RE.findall(rules_part)]
    release = {}
    for color_text, digit in BUTTON_RELEASE_RE.findall(release_part):
        key = next((COLOR_NAMES[name] for name in COLOR_NAMES if color_text.startswith(name)), "other")
        release[key] = int(digit)
    return rules, release


def _button_atom(atom: str, env: dict) -> bool:
    if "上記のいずれもが該当しない" in atom:
        return True
    if match := re.search(f"ボタンが{COLOR_RE}", atom):
        return env["color"] == COLOR_NAMES[match.group(1)]
    if match := re.search(r"ボタンに?「(.+?)」と書", atom) or re.search(r"「(.+?)」と書かれている", atom):
        return env["label"] == match.group(1)
    if match := re.search(r"バッテリーが(\d)本よりも多", atom):
        return env["batteries"] > int(match.group(1))
    if match := re.search(r"「([A-Z]+)」という点灯したインジケーターがある", atom):
        return env[f"lit_{match.group(1)}"]
    raise ValueError(f"the-button.md の条件を解釈できません: {atom}")


def solve_button(state: BombState, color: str, label: str, strip_color: str | None = None) -> str:
    rules, release = load_button_rules()
    if color not in COLOR_NAMES.values():
        return "ボタンの色は red/blue/yellow/white/black で指定してください。"

    known = {"color": color, "label": label}
    unknowns: dict[str, list] = {}
    if state.batteries is None:
        thresholds = [int(n) for atoms, _ in rules for a in atoms for n in re.findall(r"バッテリーが(\d)本よりも多", a)]
        unknowns["batteries"] = list(range(max(thresholds, default=0) + 2))
    else:
        known["batteries"] = state.batteries
    for atoms, _ in rules:
        for atom in atoms:
            for indicator in re.findall(r"「([A-Z]+)」という点灯したインジケーター", atom):
                key = f"lit_{indicator}"
                if state.lit_indicators is None:
                    unknowns[key] = [False, True]
                    UNKNOWN_VARIABLE_LABELS.setdefault(key, f"点灯した{indicator}インジケーターがあるか")
                    QUESTION_LABELS.setdefault(key, f"点灯した{indicator}はある")
                else:
                    known[key] = indicator in state.lit_indicators

    action, missing = _decide(
        rules, _button_atom, known, unknowns, resolve=lambda action, env: "tap" if "すぐに離す" in action else "hold"
    )
    if action is None:
        return _missing_message(missing)
    if action == "tap":
        return SolverResult("ボタンを押してすぐに離す。", "押してすぐ離して。")
    if strip_color is None:
        return SolverResult(
            "ボタンを押したままにして、右側に光る帯の色を聞く。帯の色を strip_color に指定して再度呼ぶ。",
            "押したまま、帯の色は？",
        )
    digit = release.get(strip_color, release.get("other"))
    return SolverResult(f"帯が{strip_color}なので、タイマーのどこかの桁に{digit}が表示されたときに離す。", f"タイマーに{digit}が出たら離して。")
