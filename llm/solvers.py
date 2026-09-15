"""LLMに任せると取り違えやすい照合や状態管理をコードで確定させるソルバー。

Defuserの説明を構造化 (記号ID、座標、色など) するのはLLMに任せ、
表の照合・経路探索・ステージをまたぐ記録のような機械的な処理だけをここで行う。
判定データはコードに持たず、knowledge/modules/ の書き起こしから読み取る。
"""

import itertools
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
    # 判定に足りない爆弾の情報の名前。記録されたら同じ呼び出しで判定し直せるようにする
    missing: tuple[str, ...] = ()


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


def solve_keypad(symbol_candidates: list[list[str]]) -> SolverResult | str:
    """記号ごとの候補IDのリストから、該当する列と押す順番を求める。

    聞き取りで2択に絞れたが決めきれない記号は候補を複数渡せる。候補の組み合わせのうち
    ほかの記号と同じ列に入るものが1通りに決まれば、聞き返さずに答えを確定させる。
    """
    columns = load_keypad_columns()
    names = {symbol_id: name for column in columns for symbol_id, _, name in column}
    slots = [list(dict.fromkeys(candidates)) for candidates in symbol_candidates if candidates]

    solutions: dict[tuple[int, tuple[str, ...]], list] = {}
    for combo in itertools.product(*slots):
        if len(set(combo)) != len(combo):
            continue
        for index, column in enumerate(columns, 1):
            if set(combo) <= {symbol_id for symbol_id, _, _ in column}:
                solutions[(index, combo)] = column

    if not solutions:
        if any(len(slot) > 1 for slot in slots):
            return "候補をどう組み合わせても同じ列に入りません。自信のない記号の見た目を聞き返してください。"
        return _keypad_near_miss([slot[0] for slot in slots], columns)

    column_indexes = {index for index, _ in solutions}
    if len(column_indexes) > 1 or len(solutions) > 1:
        # 候補の選び方で列や記号が変わる。答えを分ける記号だけを聞き返す
        differing = [
            i for i in range(len(slots)) if len({combo[i] for _, combo in solutions}) > 1
        ]
        if not differing:
            numbers = "、".join(f"列{index}" for index in sorted(column_indexes))
            return f"候補の列が複数あります ({numbers})。残りの記号も特定してから再度呼んでください。"
        choices = [
            "か".join(f"「{names[symbol_id]}」" for symbol_id in sorted({combo[i] for _, combo in solutions}))
            for i in differing
        ]
        return SolverResult(
            "候補の組み合わせが複数の答えに当てはまります: " + " / ".join(choices),
            "、".join(choices) + "、どれ？",
        )

    (index, combo), column = next(iter(solutions.items()))
    if len(combo) < 4:
        return f"列{index}に絞れたが記号が{len(combo)}つしかない。残りの記号も聞いてから再度呼んでください。"
    order = [(char, name) for symbol_id, char, name in column if symbol_id in combo]
    return SolverResult(
        f"列{index}が該当。押す順番: " + " → ".join(f"{char}({name})" for char, name in order),
        "、".join(name for _, name in order) + "の順に押して。",
    )


def _keypad_near_miss(symbol_ids: list[str], columns: list[list[tuple[str, str, str]]]) -> str:
    """該当する列がないとき、1記号だけ食い違う列があればその記号の特定ミスとして聞き返す的を示す。"""
    wanted = set(symbol_ids)
    names = {symbol_id: name for column in columns for symbol_id, _, name in column}
    hints = []
    for index, column in enumerate(columns, 1):
        column_ids = {symbol_id for symbol_id, _, _ in column}
        missing = [symbol_id for symbol_id in symbol_ids if symbol_id not in column_ids]
        if len(missing) == 1 and len(symbol_ids) > 1:
            alternatives = "、".join(
                f"{char}({name}, {symbol_id})" for symbol_id, char, name in column if symbol_id not in wanted
            )
            hints.append(
                f"列{index}なら「{names.get(missing[0], missing[0])}」({missing[0]})以外の3つが一致する。"
                f"その記号は実は次のどれかではないか: {alternatives}"
            )
    message = "指定した記号をすべて含む列はありません。記号の特定を誤っている可能性が高いです。"
    if hints:
        return message + "\n" + "\n".join(hints) + "\n食い違っている記号の見た目だけを聞き返してください。"
    return message + "自信のない記号の見た目を聞き返してください。"


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
# 順番ワイヤの1パネルあたりの左の番号の数
WIRE_SEQUENCE_PANEL_SIZE = 3
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


def solve_wire_sequence(state: BombState, panel: int | None, wires: list[dict]) -> str:
    rules = load_wire_sequence_rules()
    parsed = []
    sources = []
    for wire in wires:
        color, target = wire.get("color"), str(wire.get("target", "")).upper()
        if color not in WIRE_COLORS or target not in ("A", "B", "C"):
            return "各ワイヤは color (red/blue/black) と target (A/B/C) で指定してください。"
        parsed.append((color, target))
        sources.append(wire.get("source"))

    # パネル番号はDefuserに聞かない (聞き返しが伝わらず足踏みした)。左の番号はパネルをまたいで続き番号 (1〜3がパネル1、
    # 4〜6がパネル2…) なので、番号が分かればそこから決める。言い直しも同じパネルとして置き換わる
    source_panels = {(int(source) - 1) // WIRE_SEQUENCE_PANEL_SIZE + 1 for source in sources if source}
    if len(source_panels) > 1:
        return f"左の番号が複数のパネルにまたがっています: {[s for s in sources if s]}。1枚のパネル分ずつ指定してください。"
    if source_panels:
        panel = source_panels.pop()
    elif panel is None:
        # 番号が分からなければ、直前のパネルと同じ内容なら言い直し、違えば次のパネルとみなす
        last = max(state.wire_sequence_panels, default=0)
        panel = last if last and state.wire_sequence_panels[last] == parsed else last + 1
    # 同じパネルを言い直した場合も二重に数えないよう、パネル単位で置き換えてから数え直す。
    # 前のパネルを聞き返されただけのこともあるので、後のパネルの記録は残す
    state.wire_sequence_panels[panel] = parsed
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
    # 「2本目」はDefuserが左の番号と取り違えるため、左の番号 (なければ接続先) と色で伝える
    cut = [
        f"{source}から{target}の{WIRE_COLORS[color]}" if source else f"{target}につながる{WIRE_COLORS[color]}"
        for (color, target), source, count in zip(parsed, sources, counts_at)
        if target in rules[color].get(count, set())
    ]
    speech = f"{'と、'.join(cut)}を切って、次のパネルへ。" if cut else "どれも切らずに次のパネルへ。"
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
        f"判定には次の情報が必要です: {items}。これらをまとめて1回で聞き、答えを update_bomb_info で記録してください"
        " (記録するとこの判定を自動でやり直すので、呼び直さなくてよい)。",
        "、".join(QUESTION_LABELS.get(n, UNKNOWN_VARIABLE_LABELS.get(n, n)) for n in names) + "？",
        missing=tuple(names),
    )


def is_known(state: BombState, name: str) -> bool:
    """_missing_message で聞いた情報が記録済みか。"""
    if name == "serial_odd":
        return _serial_odd(state) is not None
    if name == "batteries":
        return state.batteries is not None
    if name == "parallel":
        return state.ports is not None
    if name.startswith("lit_"):
        return state.lit_indicators is not None or name.removeprefix("lit_") in state.not_lit_indicators
    return False


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
                if state.lit_indicators is not None:
                    known[key] = indicator in state.lit_indicators
                elif indicator in state.not_lit_indicators:
                    known[key] = False
                else:
                    unknowns[key] = [False, True]
                    UNKNOWN_VARIABLE_LABELS.setdefault(key, f"点灯した{indicator}インジケーターがあるか")
                    QUESTION_LABELS.setdefault(key, f"点灯した{indicator}はある")

    action, missing = _decide(
        rules, _button_atom, known, unknowns, resolve=lambda action, env: "tap" if "すぐに離す" in action else "hold"
    )
    if action is None:
        return _missing_message(missing)
    if action == "tap":
        return SolverResult("ボタンを押してすぐに離す。", "押してすぐ離して。")
    if strip_color is None:
        # 帯の色を聞き返すと1往復増えるうえ、ボタンの色と取り違えやすいので、離すタイミングを全色分まとめて伝える
        other = release.get("other")
        names = {v: k for k, v in COLOR_NAMES.items()}
        specific = "、".join(f"{names[color]}なら{digit}" for color, digit in release.items() if color != "other" and digit != other)
        timing = f"帯が{specific}、それ以外は{other}" if specific else f"帯の色によらず{other}"
        return SolverResult(
            f"ボタンを押したままにする。離すタイミング: {timing}が表示されたとき。",
            f"押したまま。{timing}が出たら離して。",
        )
    digit = release.get(strip_color, release.get("other"))
    return SolverResult(f"帯が{strip_color}なので、タイマーのどこかの桁に{digit}が表示されたときに離す。", f"タイマーに{digit}が出たら離して。")


# ---------------------------------------------------------------- 複雑ワイヤ

CW_ROW_RE = re.compile(r"^\| (あり|なし) \| (あり|なし) \| (あり|なし) \| (あり|なし) \| ([A-Z]) \|$", re.M)
CW_CODE_RE = re.compile(r"^\| ([A-Z]) \| (.+?) \|$", re.M)
CW_BATTERY_RE = re.compile(r"バッテリーが([一二三四五六七八九\d])本以上")
UNKNOWN_VARIABLE_LABELS["parallel"] = "パラレルポートがあるか"
QUESTION_LABELS["parallel"] = "パラレルポートはある"
ORDINALS = ("1本目", "2本目", "3本目", "4本目", "5本目", "6本目")


def load_complicated_wire_rules() -> tuple[dict[tuple[bool, bool, bool, bool], str], dict[str, str]]:
    """complicated-wires.md を ({(赤, 青, ★, LED): 指示コード}, {指示コード: 意味}) として返す。未配置なら空。"""
    text = _read("complicated-wires.md")
    if text is None:
        return {}, {}
    table = {
        tuple(cell == "あり" for cell in cells): code
        for *cells, code in CW_ROW_RE.findall(text)
    }
    return table, dict(CW_CODE_RE.findall(text))


def _cw_should_cut(meaning: str, env: dict) -> bool:
    if "切らない" in meaning:
        return False
    if "偶数" in meaning:
        return not env["serial_odd"]
    if "パラレル" in meaning:
        return env["parallel"]
    if battery := CW_BATTERY_RE.search(meaning):
        count = battery.group(1)
        return env["batteries"] >= KANJI_ORDINALS.get(count, int(count) if count.isdigit() else 0)
    return "切る" in meaning


def _record_complicated_wires(
    state: BombState, wire_count: int | None, wires: list[dict], marks: dict[str, list[int] | None]
) -> str | None:
    """聞き取れた分だけを状態に積み上げる。入力が不正ならその説明を返す。"""
    if wire_count is not None:
        if state.complicated_wire_count not in (None, wire_count):
            # 本数が変わったら別のモジュールか言い直しなので、それまでの記録は捨てる
            state.complicated_wires.clear()
            state.complicated_marks.clear()
        state.complicated_wire_count = wire_count
    for wire in wires:
        entry = state.complicated_wires.setdefault(int(wire["position"]), {})
        if wire.get("colors"):
            entry["colors"] = list(wire["colors"])
        for key in ("led", "star"):
            if wire.get(key) is not None:
                entry[key] = bool(wire[key])
    for key, positions in marks.items():
        if positions is None:
            continue
        # まとめて言い直された内容を優先する
        state.complicated_marks[key] = {int(p) for p in positions}
        for entry in state.complicated_wires.values():
            entry.pop(key, None)
    count = state.complicated_wire_count
    positions = set(state.complicated_wires) | {p for ps in state.complicated_marks.values() for p in ps}
    if count is not None and any(not 1 <= p <= count for p in positions):
        return f"{count}本の複雑ワイヤに対して範囲外の位置があります: {sorted(positions)}。位置を左から1〜{count}で指定し直してください。"
    return None


def _positions_label(positions: list[int], count: int) -> str:
    return "" if len(positions) == count else "と".join(ORDINALS[p - 1] for p in positions) + "の"


def solve_complicated_wires(
    state: BombState,
    wire_count: int | None = None,
    wires: list[dict] = (),
    lit_led_positions: list[int] | None = None,
    star_positions: list[int] | None = None,
) -> SolverResult | str:
    table, meanings = load_complicated_wire_rules()
    error = _record_complicated_wires(state, wire_count, list(wires), {"led": lit_led_positions, "star": star_positions})
    if error:
        return error
    count = state.complicated_wire_count
    if count is None:
        return SolverResult("複雑ワイヤの本数が分かりません。本数を聞いてください。", "ワイヤは何本？")

    current = {position: state.complicated_wire(position) for position in range(1, count + 1)}
    missing = []
    for key, label in (("colors", "色"), ("led", "LED"), ("star", "★")):
        positions = [position for position, wire in current.items() if key not in wire]
        if positions:
            missing.append(f"{_positions_label(positions, count)}{label}")
    if missing:
        # 分かった分は記録済みなので、足りない項目だけを聞く (最初から聞き直すと時間を失う)
        prefix = "左から順に、" if len(missing) == 3 and not state.complicated_wires and not state.complicated_marks else ""
        return SolverResult(
            f"記録しました。まだ分からない項目: {'、'.join(missing)}。聞き取れた分から続けてこのツールに入れてください。",
            f"{prefix}{'、'.join(missing)}は？",
        )

    codes = []
    for wire in current.values():
        colors = set(wire["colors"])
        codes.append(table[("red" in colors, "blue" in colors, wire["star"], wire["led"])])

    # 実際に出てきた指示コードの判定に要る情報だけを、未判明なら総当たりの対象にする
    needed = {name for code in codes for name, word in (("serial_odd", "偶数"), ("parallel", "パラレル"), ("batteries", "バッテリー")) if word in meanings[code]}
    known: dict = {}
    unknowns: dict[str, list] = {}
    if "serial_odd" in needed:
        if (odd := _serial_odd(state)) is None:
            unknowns["serial_odd"] = [False, True]
        known["serial_odd"] = odd
    if "parallel" in needed:
        if state.ports is None:
            unknowns["parallel"] = [False, True]
        known["parallel"] = state.ports is not None and "パラレル" in state.ports
    if "batteries" in needed:
        if state.batteries is None:
            thresholds = [KANJI_ORDINALS.get(n, int(n) if n.isdigit() else 0) for m in meanings.values() for n in CW_BATTERY_RE.findall(m)]
            unknowns["batteries"] = list(range(max(thresholds, default=0) + 1))
        known["batteries"] = state.batteries

    outcomes = []
    for assignment in _enumerate(unknowns):
        env = {**known, **assignment}
        outcomes.append((assignment, tuple(i for i, code in enumerate(codes) if _cw_should_cut(meanings[code], env))))
    if len({cut for _, cut in outcomes}) > 1:
        relevant = [
            name for name in unknowns
            if any(a[name] != b[name] and all(a[k] == b[k] for k in unknowns if k != name) and ca != cb
                   for a, ca in outcomes for b, cb in outcomes)
        ]
        return _missing_message(relevant)

    cut = outcomes[0][1]
    detail = "、".join(f"{ORDINALS[i]}={code}" for i, code in enumerate(codes))
    if not cut:
        return SolverResult(f"どのワイヤも切らない (左から {detail})。", "どのワイヤも切らなくていい。")
    targets = "と".join(ORDINALS[i] for i in cut)
    return SolverResult(f"左から{targets}のワイヤを切る (左から {detail})。", f"左から{targets}を切って。")


# ---------------------------------------------------------------- 表比較

WOF_STEP1_RE = re.compile(r"^## ステップ1.*?\n(.*?)(?=^## )", re.M | re.S)
WOF_STEP2_RE = re.compile(r"^## ステップ2.*?\n(.*)", re.M | re.S)
WOF_POSITION_RE = re.compile(r"^\| (.+?) \| ([123])行目-(左|右) \|$", re.M)
WOF_PRIORITY_RE = re.compile(r"^\| (.+?) \| (.+?) \|$", re.M)
WOF_EMPTY_DISPLAY = "空欄"
# ボタンの位置を読み上げる順番。ステップ1の「N行目-左/右」と対応させる
WOF_POSITIONS = ("左上", "右上", "左中", "右中", "左下", "右下")
# 聞き取れなかったボタンの文字
WOF_UNKNOWN_BUTTON = "不明"
# 表示語のうち読みが同じで、音声では区別できないもの (読み → {表示語: 聞き返すときの説明})。
# マニュアルの判定データではなく日本語の読みの知識なのでコードに持つ (書き起こしの表にない語は使わない)。
# Defuserが漢字を説明しても音声認識で崩れやすく (「開くの開」→「開かなってから」)、LLMが1つに決めつけてミスした
WOF_HOMOPHONES = {
    "かい": {"解": "解答の解", "回": "回数の回", "下位": "上位下位の下位", "快": "快適の快", "開": "開くの開"},
    "たいしょう": {"大正": "大正時代の大正", "対照": "対照的の対照", "対称": "左右対称の対称", "大賞": "グランプリの大賞"},
    "どう": {"導": "導くの導", "同": "同じの同", "動": "動くの動", "どう": "ひらがなのどう", "どう？": "はてな付きのどう"},
    "さい": {"才": "天才の才", "再": "再びの再", "最": "最高の最"},
}
# 不確かなボタンをこの数より多く総当たりせず、まとめて聞き返す
WOF_MAX_UNCERTAIN_BUTTONS = 2


def load_whos_on_first() -> tuple[dict[str, int], dict[str, list[str]]]:
    """whos-on-first.md を ({表示語: ボタン位置の添字}, {読んだ単語: 押す優先順位}) として返す。未配置なら空。"""
    text = _read("whos-on-first.md")
    if text is None:
        return {}, {}
    positions = {}
    for display, row, side in WOF_POSITION_RE.findall(WOF_STEP1_RE.search(text).group(1)):
        key = WOF_EMPTY_DISPLAY if "空欄" in display else display
        positions[key] = (int(row) - 1) * 2 + (0 if side == "左" else 1)
    priorities = {
        word: [w.strip() for w in order.split(",")]
        for word, order in WOF_PRIORITY_RE.findall(WOF_STEP2_RE.search(text).group(1))
        if "," in order
    }
    return positions, priorities


def wof_display_readings() -> list[str]:
    """表示語の代わりに読み (ひらがな) で渡してよい語。漢字が分からないまま候補を広げさせるため。"""
    return list(WOF_HOMOPHONES)


def _expand_homophones(candidates: list[str], positions: dict[str, int], kanji_confirmed: bool) -> list[str]:
    expanded = []
    for candidate in candidates:
        group = next(
            (words for reading, words in WOF_HOMOPHONES.items() if candidate == reading or candidate in words), None
        )
        # 読みで渡されたときは漢字が決まっていないので、確定済みでも広げる
        if group is not None and (not kanji_confirmed or candidate not in positions):
            expanded.extend(word for word in group if word in positions)
        else:
            expanded.append(candidate)
    return list(dict.fromkeys(expanded))


def solve_whos_on_first(
    display_candidates: list[str], buttons: list[str], kanji_confirmed: bool = False
) -> SolverResult | str:
    positions, priorities = load_whos_on_first()
    if len(buttons) != 6:
        return f"ボタンは {'・'.join(WOF_POSITIONS)} の順に6つ指定してください。"
    displays = _expand_homophones(display_candidates, positions, kanji_confirmed)
    unknown_displays = [display for display in displays if display not in positions]
    if unknown_displays:
        return f"表にない表示語です: {'、'.join(unknown_displays)}。表示の文字を聞き返してください。"

    # ボタンの文字は同じ語群 (優先順位リストに並ぶ14語) から出る。表にない文字や、ほかと別の語群の文字は聞き間違いとみなす
    groups = {word: frozenset(order) for word, order in priorities.items()}
    known_groups = [groups[word] for word in buttons if word in groups]
    if not known_groups:
        return "ボタンの文字が1つも表にありません。6つのボタンの文字を聞き返してください。"
    group = max(set(known_groups), key=known_groups.count)
    uncertain = [i for i, word in enumerate(buttons) if groups.get(word) != group]
    if len(uncertain) > WOF_MAX_UNCERTAIN_BUTTONS:
        return _wof_ask_buttons(buttons, uncertain)

    # 表示語の候補と、不確かなボタンに入りうる文字を総当たりし、押す位置が変わる要素だけを聞き返す
    fill_words = [word for word in sorted(group) if word not in buttons]
    outcomes: dict[tuple, int] = {}
    for display, *fills in itertools.product(displays, *[fill_words] * len(uncertain)):
        if len(set(fills)) != len(fills):
            continue
        filled = list(buttons)
        for i, word in zip(uncertain, fills):
            filled[i] = word
        read_word = filled[positions[display]]
        outcomes[(display, *fills)] = filled.index(next(word for word in priorities[read_word] if word in filled))

    if len(set(outcomes.values())) > 1:
        display_matters = _wof_varies(outcomes, 0)
        buttons_matter = [i for n, i in enumerate(uncertain, 1) if _wof_varies(outcomes, n)]
        if display_matters and not buttons_matter:
            return _wof_ask_display(displays, positions, buttons)
        if buttons_matter and not display_matters:
            return _wof_ask_buttons(buttons, buttons_matter)
        display_question = _wof_ask_display(displays, positions, buttons)
        buttons_question = _wof_ask_buttons(buttons, buttons_matter)
        return SolverResult(
            f"{display_question.text} / {buttons_question.text}",
            f"{display_question.speech}それと、{buttons_question.speech}",
        )

    press_index = next(iter(outcomes.values()))
    position = WOF_POSITIONS[press_index]
    notes = []
    if len(displays) > 1:
        notes.append(f"表示語の候補「{'/'.join(displays)}」のどれでも同じ")
    if uncertain:
        notes.append(f"不確かなボタン ({'、'.join(WOF_POSITIONS[i] for i in uncertain)}) は答えに影響しない")
    note = f" ({'、'.join(notes)})" if notes else ""
    if press_index in uncertain:
        # 押すボタンの文字自体が聞き取れていないので、位置だけを伝える
        return SolverResult(f"{position}のボタンを押す。{note}", f"{position}を押して。")
    press = buttons[press_index]
    return SolverResult(f"{position}の「{press}」を押す。{note}", f"{position}の「{press}」を押して。")


def _wof_varies(outcomes: dict[tuple, int], index: int) -> bool:
    """組み合わせの index 番目の要素だけを変えたときに、押す位置が変わることがあるか。"""
    seen: dict[tuple, int] = {}
    for key, outcome in outcomes.items():
        rest = key[:index] + key[index + 1:]
        if seen.setdefault(rest, outcome) != outcome:
            return True
    return False


def _wof_ask_display(displays: list[str], positions: dict[str, int], buttons: list[str]) -> SolverResult:
    descriptions = {word: text for words in WOF_HOMOPHONES.values() for word, text in words.items()}
    choices = "、".join(descriptions.get(display, f"「{display}」") for display in displays)
    return SolverResult(
        f"表示語の候補によって押すボタンが変わります: {'、'.join(displays)}。表示の漢字を聞き返し、"
        "答えを受けたら kanji_confirmed=true でその漢字だけを入れて呼び直してください。",
        f"表示の漢字は、{choices}、どれ？",
    )


def _wof_ask_buttons(buttons: list[str], indices: list[int]) -> SolverResult:
    names = "、".join(WOF_POSITIONS[i] for i in indices)
    heard = "、".join(f"{WOF_POSITIONS[i]}「{buttons[i]}」" for i in indices)
    return SolverResult(
        f"表にないか、ほかのボタンと別の語群の文字です: {heard}。その位置のボタンの文字だけを聞き返してください。",
        f"{names}のボタンの文字は？",
    )
