"""LLMに渡すツールの定義と実行。"""

import json
import logging
from dataclasses import dataclass, field

from .bomb_state import INDICATORS, PORTS, BombState
from .manual import MODULE_IDS, load_module_manual
from .solvers import (
    SolverResult,
    keypad_symbol_ids,
    solve_button,
    solve_keypad,
    solve_maze,
    solve_memory,
    solve_wire_sequence,
    solve_wires,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolLog:
    """1回の応答中に使われたツールの記録。テキストチャンネルへの表示用。"""

    consulted_modules: list[str] = field(default_factory=list)
    solver_outputs: list[str] = field(default_factory=list)
    # 直近のソルバーが返した、LLMを通さずそのまま読み上げてよい発話
    final_speech: str | None = None


MAZE_POSITION = {
    "type": "object",
    "properties": {
        "column": {"type": "integer", "minimum": 1, "maximum": 6, "description": "左から数えた列"},
        "row": {"type": "integer", "minimum": 1, "maximum": 6, "description": "上から数えた行"},
    },
    "required": ["column", "row"],
}


def build_tools() -> list[dict]:
    tools = [
        {
            "name": "get_module_manual",
            "description": "指定したモジュールのマニュアル (解除手順) を取得する。専用ソルバーのないモジュールは指示を出す前に必ず取得すること。",
            "parameters": {
                "type": "object",
                "properties": {"module": {"type": "string", "enum": list(MODULE_IDS), "description": "モジュールID"}},
                "required": ["module"],
            },
        },
        {
            "name": "update_bomb_info",
            "description": (
                "判明した爆弾の情報を記録する。Defuserが爆弾の情報を言ったら必ず呼ぶこと。"
                "Defuserが言及していない項目は必ず null にすること (推測で空配列や0を入れない)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serial_number": {"type": ["string", "null"], "description": "シリアルナンバー (英数字)。未言及なら null"},
                    "serial_last_digit_odd": {"type": ["boolean", "null"],
                                              "description": "シリアル全体ではなく末尾の数字の偶奇だけ判明したとき、奇数なら true・偶数なら false。それ以外は null"},
                    "batteries": {"type": ["integer", "null"], "minimum": 0, "description": "バッテリーの合計本数。未言及なら null"},
                    "lit_indicators": {"type": ["array", "null"], "items": {"type": "string", "enum": list(INDICATORS)},
                                       "description": "点灯しているインジケーターすべて。「インジケーターはない」などと言われたら空配列 []、触れていなければ null"},
                    "unlit_indicators": {"type": ["array", "null"], "items": {"type": "string", "enum": list(INDICATORS)},
                                         "description": "点灯していないインジケーターすべて。「インジケーターはない」などと言われたら空配列 []、触れていなければ null"},
                    "ports": {"type": ["array", "null"], "items": {"type": "string", "enum": list(PORTS)},
                              "description": "ついているポートすべて。「ポートはない」などと言われたら空配列 []、ポートに触れていなければ null"},
                    "strikes": {"type": ["integer", "null"], "minimum": 0, "description": "現在のミス回数。未言及なら null"},
                },
                "required": ["serial_number", "serial_last_digit_odd", "batteries", "lit_indicators",
                             "unlit_indicators", "ports", "strikes"],
            },
        },
        {
            "name": "solve_wires",
            "description": (
                "ワイヤモジュール (3〜6本の単色ワイヤ) で切るワイヤを求める。自分で判定せず必ずこのツールを使うこと。"
                "Defuserから全ワイヤの色を聞いてから呼ぶこと (推測した色で呼ばない)。"
                "判定に足りない爆弾の情報があれば、答えに影響するものだけを返す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "colors": {"type": "array", "items": {"type": "string", "enum": ["red", "blue", "yellow", "white", "black"]},
                               "minItems": 3, "maxItems": 6, "description": "ワイヤの色を上から順に"},
                },
                "required": ["colors"],
            },
        },
        {
            "name": "solve_button",
            "description": (
                "ボタンモジュールで押してすぐ離すか押し続けるかを求め、押し続ける場合は帯の色から離すタイミングを求める。"
                "自分で判定せず必ずこのツールを使うこと。判定に足りない爆弾の情報があれば、答えに影響するものだけを返す。"
                "Defuserからボタンの色と文字を聞いてから呼ぶこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "color": {"type": "string", "enum": ["red", "blue", "yellow", "white", "black"], "description": "ボタンの色"},
                    "label": {"type": "string", "description": "ボタンに書かれた文字 (例: 中止、起爆、長押し、押す)"},
                    "strip_color": {"type": ["string", "null"], "enum": ["red", "blue", "yellow", "white", "black", None],
                                    "description": "押し続けたときに右側に光る帯の色。まだ分からなければ null"},
                },
                "required": ["color", "label", "strip_color"],
            },
        },
        {
            "name": "solve_memory",
            "description": "記憶モジュールで押すボタンを求め、押したボタンをステージごとに記録する。自分で判定せず必ずこのツールを使うこと。",
            "parameters": {
                "type": "object",
                "properties": {
                    "display": {"type": "integer", "minimum": 1, "maximum": 4, "description": "ディスプレーの数字"},
                    "buttons": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 4},
                                "minItems": 4, "maxItems": 4, "description": "4つのボタンのラベルを左から順に"},
                    "stage": {"type": "integer", "minimum": 1, "maximum": 5,
                              "description": "現在のステージ。省略時は記録から自動判定。誤答でステージ1に戻ったときは1を指定"},
                },
                "required": ["display", "buttons"],
            },
        },
        {
            "name": "solve_wire_sequence",
            "description": "順番ワイヤで、パネルの各ワイヤを切るかどうかを求める。色ごとの出現回数はパネルをまたいでツール側で数える。自分で判定せず必ずこのツールを使うこと。",
            "parameters": {
                "type": "object",
                "properties": {
                    "panel": {"type": "integer", "minimum": 1, "description": "何枚目のパネルか (1から)"},
                    "wires": {
                        "type": "array",
                        "description": "パネルのワイヤを左側の番号順に",
                        "items": {
                            "type": "object",
                            "properties": {
                                "color": {"type": "string", "enum": ["red", "blue", "black"]},
                                "target": {"type": "string", "enum": ["A", "B", "C"], "description": "右側の接続先"},
                            },
                            "required": ["color", "target"],
                        },
                    },
                },
                "required": ["panel", "wires"],
            },
        },
        {
            "name": "solve_maze",
            "description": "迷路モジュールの丸印の位置から迷路を特定し、白い点から赤い三角までの押す順番を求める。自分で経路を考えず必ずこのツールを使うこと。",
            "parameters": {
                "type": "object",
                "properties": {
                    "circles": {"type": "array", "items": MAZE_POSITION, "minItems": 1, "maxItems": 2,
                                "description": "緑の丸印の位置"},
                    "start": {**MAZE_POSITION, "description": "白い点 (現在地) の位置"},
                    "goal": {**MAZE_POSITION, "description": "赤い三角 (ゴール) の位置"},
                },
                "required": ["circles", "start", "goal"],
            },
        },
    ]
    symbol_ids = keypad_symbol_ids()
    if symbol_ids:
        tools.append({
            "name": "solve_keypad",
            "description": (
                "キーパッドの記号IDから該当する列と押す順番を求める。記号IDはsystem promptのキーパッドの記号表で特定する。"
                "押す順番は自分で判定せず、必ずこのツールの結果に従うこと。"
                "Defuserが4つの記号をすべて説明してから呼ぶこと。1つの記号の説明 (例: キリル文字のZH) を複数の記号に分けないこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string", "enum": symbol_ids},
                                "minItems": 1, "maxItems": 4, "description": "モジュールについているキーの記号ID"},
                },
                "required": ["symbols"],
            },
        })
    return tools


def run_tool(name: str, arguments: str, state: BombState, log: ToolLog) -> str:
    # LLMが発話をどう構造化したかは誤判定の原因調査に要るので、引数を必ず残す
    logger.info("tool call: %s %s", name, arguments)
    try:
        args = json.loads(arguments or "{}")
        if name == "get_module_manual":
            log.consulted_modules.append(args["module"])
            return load_module_manual(args["module"])
        if name == "update_bomb_info":
            output = _update_bomb_info(state, args)
        elif name == "solve_memory":
            stage = None if args.get("stage") is None else int(args["stage"])
            output = solve_memory(state, int(args["display"]), [int(b) for b in args["buttons"]], stage)
        elif name == "solve_wires":
            output = solve_wires(state, list(args["colors"]))
        elif name == "solve_button":
            output = solve_button(state, args["color"], str(args["label"]), args.get("strip_color"))
        elif name == "solve_wire_sequence":
            output = solve_wire_sequence(state, int(args["panel"]), list(args["wires"]))
        elif name == "solve_maze":
            output = solve_maze(list(args["circles"]), args["start"], args["goal"])
        elif name == "solve_keypad":
            output = solve_keypad(list(args["symbols"]))
        else:
            return f"不明なツールです: {name}"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        return f"引数が不正です ({e}): {arguments}"
    if isinstance(output, SolverResult):
        log.final_speech = output.speech
        output = output.text
    else:
        log.final_speech = None
    log.solver_outputs.append(output)
    return output


def _update_bomb_info(state: BombState, args: dict) -> str:
    # null (未言及) の項目は既存の記録を残す
    if args.get("serial_number"):
        state.serial_number = str(args["serial_number"]).upper().replace(" ", "")
    if args.get("serial_last_digit_odd") is not None:
        state.serial_last_digit_odd = bool(args["serial_last_digit_odd"])
    if args.get("batteries") is not None:
        state.batteries = int(args["batteries"])
    for key in ("lit_indicators", "unlit_indicators", "ports"):
        if args.get(key) is not None:
            setattr(state, key, set(args[key]))
    if args.get("strikes") is not None:
        state.strikes = int(args["strikes"])
    return "記録しました。現在の爆弾情報:\n" + state.summary()
