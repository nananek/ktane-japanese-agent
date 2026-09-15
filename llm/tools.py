"""LLMに渡すツールの定義と実行。"""

import json
import logging
from dataclasses import dataclass, field

from .bomb_state import INDICATORS, PORTS, BombState, PendingSolver
from .manual import MODULE_IDS, load_module_manual
from .solvers import (
    WOF_UNKNOWN_BUTTON,
    SolverResult,
    is_known,
    keypad_symbol_ids,
    load_complicated_wire_rules,
    load_simon_rules,
    solve_button,
    solve_complicated_wires,
    solve_simon_says,
    solve_keypad,
    solve_maze,
    solve_memory,
    load_whos_on_first,
    solve_whos_on_first,
    solve_wire_sequence,
    solve_wires,
    wof_display_readings,
)

logger = logging.getLogger(__name__)

GAME_RESULTS = ("解除", "爆発", "時間切れ", "中断")
# 爆発・時間切れで「お疲れさま」は合わないので、結果ごとに締めのセリフを変える
GAME_END_SPEECH = {
    "解除": "解除成功！",
    "爆発": "爆発しちゃった…次は解除しよう。",
    "時間切れ": "時間切れ…次は解除しよう。",
    "中断": "ゲームを中断したよ。",
}
BUTTON_LABELS = ("中止", "起爆", "長押し", "押す", "その他")
# 爆弾の情報が足りないと判定を保留するソルバー。聞き返した情報が記録されたらその場で判定し直す
PENDABLE_SOLVERS = ("solve_wires", "solve_button", "solve_complicated_wires", "solve_simon_says")


@dataclass
class ToolLog:
    """1回の応答中に使われたツールの記録。テキストチャンネルへの表示用。"""

    consulted_modules: list[str] = field(default_factory=list)
    solver_outputs: list[str] = field(default_factory=list)
    # 直近のソルバーが返した、LLMを通さずそのまま読み上げてよい発話
    final_speech: str | None = None
    # final_speech を読み上げるときの句点の間の倍率 (ソルバーが指定したときだけ)
    final_speech_pause_scale: float | None = None


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
                "ソルバーが聞き返した情報を記録すると、そのソルバーの判定を自動でやり直して結果を返すので、ソルバーを呼び直さなくてよい。"
                "Defuserが言及していない項目は必ず null にすること (推測で空配列や0を入れない)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serial_number": {"type": ["string", "null"], "description": "シリアルナンバー (英数字)。未言及なら null"},
                    "serial_has_vowel": {"type": ["boolean", "null"],
                                         "description": "シリアル全体ではなく母音 (A,E,I,O,U) の有無だけ判明したとき、含むなら true・含まないなら false。それ以外は null"},
                    "serial_last_digit_odd": {"type": ["boolean", "null"],
                                              "description": "シリアル全体ではなく末尾の数字の偶奇だけ判明したとき、奇数なら true・偶数なら false。それ以外は null"},
                    "batteries": {"type": ["integer", "null"], "minimum": 0, "description": "バッテリーの合計本数。未言及なら null"},
                    "lit_indicators": {"type": ["array", "null"], "items": {"type": "string", "enum": list(INDICATORS)},
                                       "description": "爆弾の点灯しているインジケーターを全部答えてもらったときだけ、その全部。「点灯はない」と言われたら空配列 []。特定のインジケーターだけの話なら null にして not_lit_indicators を使う"},
                    "not_lit_indicators": {"type": ["array", "null"], "items": {"type": "string", "enum": list(INDICATORS)},
                                           "description": "「FRKは点灯していない」「CARはない」のように、特定のインジケーターだけ点灯していないと分かったもの。なければ null"},
                    "unlit_indicators": {"type": ["array", "null"], "items": {"type": "string", "enum": list(INDICATORS)},
                                         "description": "点灯していないインジケーターすべて。「インジケーターはない」などと言われたら空配列 []、触れていなければ null"},
                    "ports": {"type": ["array", "null"], "items": {"type": "string", "enum": list(PORTS)},
                              "description": "ついているポートすべて。「ポートはない」などと言われたら空配列 []、ポートに触れていなければ null"},
                    "strikes": {"type": ["integer", "null"], "minimum": 0,
                                "description": "Defuserが「ミスは合計2回」「ストライクが2つ」のようにミス回数の合計を明言したときだけ、その数。それ以外は null"},
                    "strike_occurred": {"type": ["boolean", "null"],
                                        "description": "Defuserがミスの発生を報告したら true (「ミス」「ミス1回」「ミスった」「ストライク」など、合計と明言していないもの)。記録済みのミス数に1を足す。それ以外は null"},
                },
                "required": ["serial_number", "serial_has_vowel", "serial_last_digit_odd", "batteries", "lit_indicators",
                             "not_lit_indicators", "unlit_indicators", "ports", "strikes", "strike_occurred"],
            },
        },
        {
            "name": "end_game",
            "description": (
                "Defuserが爆弾の解除成功・爆発・時間切れ、またはゲームの中断を明言したときだけ呼び、ゲーム終了を記録する。"
                "「ミスした」「失敗です」などのミス (ストライク) はゲーム終了ではないので呼ばず、update_bomb_info でミス数を記録すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "enum": list(GAME_RESULTS), "description": "ゲームの結果"},
                },
                "required": ["result"],
            },
        },
        {
            "name": "solve_wires",
            "description": (
                "ワイヤモジュール (3〜6本の単色ワイヤ) で切るワイヤを求める。自分で判定せず必ずこのツールを使うこと。"
                "Defuserから全ワイヤの色を聞いてから呼ぶこと (推測した色で呼ばない)。"
                "判定に足りない爆弾の情報があれば、答えに影響するものだけを返す。"
                "その答えは update_bomb_info で記録すれば、この判定が自動でやり直される。"
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
                "ボタンモジュールで押してすぐ離すか押し続けるかを求める。押し続ける場合は帯の色ごとの離すタイミングをまとめて返すので、帯の色を聞き返す必要はない。"
                "自分で判定せず必ずこのツールを使うこと。判定に足りない爆弾の情報があれば、答えに影響するものだけを返す。"
                "ボタンの色と文字が分かったら、確認の聞き返しをせずにすぐ呼ぶこと。"
                "聞き返した爆弾の情報 (電池の本数・インジケーターなど) の答えは update_bomb_info で記録すれば、この判定が自動でやり直される。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "color": {"type": "string", "enum": ["red", "blue", "yellow", "white", "black"], "description": "ボタンの色"},
                    "label": {"type": "string", "enum": list(BUTTON_LABELS),
                              "description": "ボタンに書かれた文字。ボタンの説明に出てきた「長押し」「押す」は操作ではなく文字として扱う。音の近い誤認識は読み替える (例: 「無線中止」→中止)"},
                    "strip_color": {"type": ["string", "null"], "enum": ["red", "blue", "yellow", "white", "black", None],
                                    "description": "Defuserが自分から帯の色を言った場合だけその色。聞き返してまで埋めない。分からなければ null"},
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
            "description": (
                "順番ワイヤで、パネルの各ワイヤを切るかどうかを求める。色ごとの出現回数とパネルの枚数はツール側で数える。"
                "自分で判定せず必ずこのツールを使うこと。パネル番号はDefuserに聞かないこと。"
                "結果は「3からBの青を切って」のように左の番号で伝えるので、「N本目」と言い換えないこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "panel": {"type": ["integer", "null"], "minimum": 1,
                              "description": "通常は null (左の番号からツールがパネルを決める)。左の番号が言われず、記録済みのパネルを言い直されたときだけそのパネル番号"},
                    "wires": {
                        "type": "array",
                        "description": "パネルのワイヤを左側の番号順に",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": ["integer", "null"], "minimum": 1,
                                           "description": "左側の番号 (「2からAに赤」なら2)。パネルをまたいで続き番号になり、10以上もある。言われていなければ null"},
                                "color": {"type": "string", "enum": ["red", "blue", "black"]},
                                "target": {"type": "string", "enum": ["A", "B", "C"], "description": "右側の接続先"},
                            },
                            "required": ["source", "color", "target"],
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
    if load_simon_rules():
        tools.append({
            "name": "solve_simon_says",
            "description": (
                "サイモンゲーム (赤・青・緑・黄のボタンが順に光る) で押す色の順番を求める。自分で表を引かず必ずこのツールを使うこと。"
                "光った色の並びはツールが記録し、押す色はシリアルの母音と記録済みのミス数から決めるので、ミス数は聞かないこと。"
                "母音が分からず答えが変わる場合だけ母音を聞き返す文を返し、その答えを update_bomb_info で記録すれば判定が自動でやり直される。"
                "結果の「〜の順に押して」をそのまま伝えること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flashes": {"type": "array", "items": {"type": "string", "enum": ["red", "blue", "green", "yellow"]},
                                "description": "Defuserが言った光った色を順に"},
                    "only_new": {"type": "boolean",
                                 "description": "Defuserが前回から増えた色だけを言ったとき true (記録済みの並びの後ろに足す)。最初から全部言ったときは false"},
                },
                "required": ["flashes", "only_new"],
            },
        })
    if load_complicated_wire_rules()[0]:
        tools.append({
            "name": "solve_complicated_wires",
            "description": (
                "複雑ワイヤ (縦向きに並ぶワイヤで、上にLED・下に★印の場所があり、縞模様のワイヤもある) で切るワイヤを求める。"
                "自分で判定せず必ずこのツールを使い、マニュアルの条件 (シリアルが偶数なら、など) をDefuserにそのまま伝えないこと。"
                "Defuserは本数・色・LED・★を何回にも分けて言うので、聞き取れた分だけを入れてその都度呼ぶこと (言われていない項目は null)。"
                "ツールがそれまでの分と合わせて記録し、足りない項目だけを聞き返す文を返すので、自分で覚えたり最初から聞き直したりしないこと。"
                "記録済みの内容は「現在判明している爆弾の情報」に載る。"
                "判定に足りない爆弾の情報があれば答えに影響するものだけを返し、その答えを update_bomb_info で記録すれば判定が自動でやり直される。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wire_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 6,
                                   "description": "ワイヤの本数。この発言で言われていなければ null"},
                    "wires": {
                        "type": "array", "maxItems": 6,
                        "description": "この発言で分かったワイヤの項目だけ (なければ空配列)。色を左から順に並べて言われたら1本目から順に",
                        "items": {
                            "type": "object",
                            "properties": {
                                "position": {"type": "integer", "minimum": 1, "maximum": 6, "description": "左から何本目か"},
                                "colors": {"type": ["array", "null"], "minItems": 1, "maxItems": 2,
                                           "items": {"type": "string", "enum": ["red", "blue", "white"]},
                                           "description": "ワイヤの色。縞模様 (縒り線) なら2色とも。言われていなければ null"},
                                "led": {"type": ["boolean", "null"], "description": "このワイヤの上のLEDが点灯しているか。言われていなければ null"},
                                "star": {"type": ["boolean", "null"], "description": "このワイヤの下に★印があるか。言われていなければ null"},
                            },
                            "required": ["position", "colors", "led", "star"],
                        },
                    },
                    "lit_led_positions": {
                        "type": ["array", "null"], "items": {"type": "integer", "minimum": 1, "maximum": 6},
                        "description": (
                            "「LEDは右の2つが点灯」のように点灯しているLEDをまとめて言われたとき、点灯している位置すべて (ほかは消灯とみなす)。"
                            "位置は本数から数える (6本で「右の2つ」なら [5, 6])。「LEDは全部消えている」なら []。言われていなければ null"
                        ),
                    },
                    "star_positions": {
                        "type": ["array", "null"], "items": {"type": "integer", "minimum": 1, "maximum": 6},
                        "description": "★印のある位置をまとめて言われたとき、その位置すべて (ほかはなし)。「★はない」なら []。言われていなければ null",
                    },
                },
                "required": ["wire_count", "wires", "lit_led_positions", "star_positions"],
            },
        })
    positions, priorities = load_whos_on_first()
    if positions:
        tools.append({
            "name": "solve_whos_on_first",
            "description": (
                "表比較 (Who's on First) のステージごとに押すボタンを求める。自分で判定せず必ずこのツールを使うこと。"
                "表示語は同じ読みで漢字の違うもの (かい: 解/回/下位/快/開、たいしょう: 大正/対照/対称/大賞、"
                "どう: 導/同/動/どう/どう？、さい: 才/再/最) があり、音声認識では漢字の説明も崩れるので、"
                "漢字が確定していなければ (kanji_confirmed=false)、ツールが同じ読みの候補を自動で広げ、"
                "押すボタンが変わる場合だけ漢字を聞き返す文を返す。"
                "漢字の説明がはっきりしなくても自分で聞き返さず、聞こえた漢字か読みを入れてすぐ呼ぶこと。"
                "ボタンの文字は聞き取れたとおりに入れ、聞き取れない位置は「不明」にすること (答えに影響する位置だけツールが聞き返す)。"
                "ボタンの文字「残り」「えーと」「なし」なども普通の言葉ではなく文字として扱う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "display_candidates": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string", "enum": list(dict.fromkeys([*positions, *wof_display_readings()]))},
                        "description": "ディスプレーの表示語 (何も表示されていなければ「空欄」)。漢字が分からなければ読み (かい など)",
                    },
                    "buttons": {
                        "type": "array", "minItems": 6, "maxItems": 6,
                        "items": {"type": "string", "enum": [*priorities, WOF_UNKNOWN_BUTTON]},
                        "description": "6つのボタンの文字を 左上・右上・左中・右中・左下・右下 の順に",
                    },
                    "kanji_confirmed": {
                        "type": "boolean",
                        "description": (
                            "漢字が1つに決まっているときだけ true にして、その漢字だけを display_candidates に入れる。"
                            "true にしてよいのは、ツールの「表示の漢字は、〜、どれ？」にDefuserが答えたときと、"
                            "Defuserの漢字の説明が意味の通る言葉のまま聞き取れていて1つの漢字を指しているとき"
                            " (例: 「コントラストの対照」→対照、「下の位」→下位、「開くの開」→開)。"
                            "説明が崩れて意味の通らない言葉になっている (例: 「心領位の回」「開かなってから」) か、"
                            "説明がないときは false"
                        ),
                    },
                },
                "required": ["display_candidates", "buttons", "kanji_confirmed"],
            },
        })
    symbol_ids = keypad_symbol_ids()
    if symbol_ids:
        tools.append({
            "name": "solve_keypad",
            "description": (
                "キーパッドの記号IDから該当する列と押す順番を求める。記号IDはsystem promptのキーパッドの記号表で特定する。"
                "押す順番は自分で判定せず、必ずこのツールの結果に従うこと。"
                "Defuserが4つの記号をすべて説明してから呼ぶこと。1つの記号の説明 (例: キリル文字のZH) を複数の記号に分けないこと。"
                "説明が記号表の1つにぴったり当てはまらない記号は、1つに決めつけず形の近い候補をすべて入れること"
                " (例: 「アルファベットのB」→6の形と横棒つきのb、「斜めのN」→稲妻と帽子つきの逆N、「3に何か付いた形」→尻尾つきの3と飾りつきの3)。"
                "聞き返す前に必ず候補を入れて呼ぶこと。ほかの記号と同じ列に入る組み合わせが1つならツールが確定させ、"
                "複数あれば答えを分ける記号だけを聞き返す文を返す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "キーごとの候補の記号IDのリスト。特定できた記号は候補1つ、決めきれない記号は候補を複数入れる",
                        "items": {"type": "array", "items": {"type": "string", "enum": symbol_ids}, "minItems": 1},
                    },
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
        if name == "update_bomb_info":
            return _record_bomb_info(state, args, log)
        # 情報の記録以外のツールが呼ばれたら、保留していた判定の話からは離れたとみなす
        state.pending_solver = None
        if name == "get_module_manual":
            log.consulted_modules.append(args["module"])
            return load_module_manual(args["module"])
        if name == "solve_memory":
            stage = None if args.get("stage") is None else int(args["stage"])
            output = solve_memory(state, int(args["display"]), [int(b) for b in args["buttons"]], stage)
        elif name == "end_game":
            output = _end_game(state, str(args["result"]))
        elif name == "solve_whos_on_first":
            output = solve_whos_on_first(
                list(args["display_candidates"]), list(args["buttons"]), bool(args.get("kanji_confirmed"))
            )
        elif name == "solve_simon_says":
            output = solve_simon_says(state, list(args.get("flashes") or []), bool(args.get("only_new")))
        elif name == "solve_complicated_wires":
            output = solve_complicated_wires(
                state, args.get("wire_count"), list(args.get("wires") or []),
                args.get("lit_led_positions"), args.get("star_positions"),
            )
        elif name == "solve_wires":
            output = solve_wires(state, list(args["colors"]))
        elif name == "solve_button":
            output = solve_button(state, args["color"], str(args["label"]), args.get("strip_color"))
        elif name == "solve_wire_sequence":
            panel = args.get("panel")
            output = solve_wire_sequence(state, None if panel is None else int(panel), list(args["wires"]))
        elif name == "solve_maze":
            output = solve_maze(list(args["circles"]), args["start"], args["goal"])
        elif name == "solve_keypad":
            output = solve_keypad([[s] if isinstance(s, str) else list(s) for s in args["symbols"]])
        else:
            return f"不明なツールです: {name}"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        return f"引数が不正です ({e}): {arguments}"
    if isinstance(output, SolverResult):
        log.final_speech = output.speech
        log.final_speech_pause_scale = output.speech_pause_scale
        if output.missing and name in PENDABLE_SOLVERS:
            pending_arguments = arguments if output.rerun_arguments is None else json.dumps(output.rerun_arguments)
            state.pending_solver = PendingSolver(name, pending_arguments, list(output.missing))
        output = output.text
    else:
        log.final_speech = None
    log.solver_outputs.append(output)
    return output


def _end_game(state: BombState, result: str) -> SolverResult | str:
    if result not in GAME_RESULTS:
        return f"result は {', '.join(GAME_RESULTS)} のどれかで指定してください。"
    state.game_result = result
    return SolverResult(f"ゲーム終了を記録しました: {result}", GAME_END_SPEECH[result] + "次の爆弾は ktane-newbomb で。")


def _record_bomb_info(state: BombState, args: dict, log: ToolLog) -> str:
    output = _update_bomb_info(state, args)
    log.final_speech = None
    log.solver_outputs.append(output)
    pending = state.pending_solver
    if pending is None or not any(is_known(state, name) for name in pending.missing):
        return output
    # ソルバーが聞き返した情報の答えなら、LLMにソルバーを呼び直させる往復 (約2秒) を待たずにその場で判定し直す
    logger.info("pending solver rerun: %s %s", pending.name, pending.arguments)
    state.pending_solver = None
    result = run_tool(pending.name, pending.arguments, state, log)
    return f"{output}\n\n保留していた {pending.name} をこの情報で判定し直しました: {result}"


def _update_bomb_info(state: BombState, args: dict) -> str:
    # null (未言及) の項目は既存の記録を残す
    if args.get("serial_number"):
        state.serial_number = str(args["serial_number"]).upper().replace(" ", "")
    if args.get("serial_has_vowel") is not None:
        state.serial_has_vowel = bool(args["serial_has_vowel"])
    if args.get("serial_last_digit_odd") is not None:
        state.serial_last_digit_odd = bool(args["serial_last_digit_odd"])
    if args.get("batteries") is not None:
        state.batteries = int(args["batteries"])
    for key in ("lit_indicators", "unlit_indicators", "ports"):
        if args.get(key) is not None:
            setattr(state, key, set(args[key]))
    if args.get("not_lit_indicators"):
        state.not_lit_indicators |= set(args["not_lit_indicators"])
    if args.get("strikes") is not None:
        state.strikes = int(args["strikes"])
    elif args.get("strike_occurred"):
        # 「ミス1回」は合計なのか1回増えたのか区別がつかず、2回目も1回と記録していたため、ミスの報告は常に1回ずつ足す
        state.strikes = (state.strikes or 0) + 1
    return "記録しました。現在の爆弾情報:\n" + state.summary()
