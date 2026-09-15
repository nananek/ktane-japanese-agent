import re
from dataclasses import dataclass
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "modules"
GENERAL_RULES_FILE = "00_bomb_features.md"


@dataclass(frozen=True)
class ModuleInfo:
    id: str
    name: str
    # Defuserは正式名称ではなく見た目で呼ぶことが多い (例: キーパッドを「キーボード」) ため、特定の手がかりを添える
    appearance: str


MODULES: tuple[ModuleInfo, ...] = (
    ModuleInfo("wires", "ワイヤ", "3〜6本の色付きワイヤが横に並ぶ。「線」「コード」とも呼ばれる"),
    ModuleInfo("the-button", "ボタン", "ラベル付きの大きな色付きボタンが1つ。押し続けると右に色の帯が光る"),
    ModuleInfo("keypads", "キーパッド", "見慣れない記号が書かれた4つのキー。「キーボード」「記号」とも呼ばれる"),
    ModuleInfo("simon-says", "サイモンゲーム", "赤・青・緑・黄の4色のボタンが順に光る"),
    ModuleInfo("whos-on-first", "表比較 (Who's on First)", "上の表示窓に単語、下に単語の書かれた6つのボタン"),
    ModuleInfo("memory", "記憶", "大きな数字の表示と、数字の書かれた4つのボタン。ステージが5段階"),
    ModuleInfo("morse-code", "モールス信号", "点滅するランプ、周波数の表示と左右の矢印、TXボタン"),
    ModuleInfo("complicated-wires", "複雑ワイヤ", "ワイヤの上にLED、下に★印があることがある。縞模様のワイヤもある"),
    ModuleInfo("wire-sequences", "順番ワイヤ", "左の数字と右のアルファベットを結ぶワイヤが数ページに分かれている"),
    ModuleInfo("mazes", "迷路", "6×6のマス目、緑の丸印2つ、白い点(現在地)と赤い三角(ゴール)"),
    ModuleInfo("passwords", "パスワード", "5文字の表示窓で、各文字を上下のボタンで切り替える"),
    ModuleInfo("needy-vent-gas", "ガス放出 (Needy)", "「VENT GAS?」などの質問とY/Nボタン、タイマー付き"),
    ModuleInfo("needy-capacitor-discharge", "コンデンサー (Needy)", "レバーとメーター、タイマー付き"),
    ModuleInfo("needy-knobs", "ダイヤル (Needy)", "回すつまみと、その横に並ぶLED、タイマー付き"),
)
MODULE_IDS = tuple(module.id for module in MODULES)


def _read(file_name: str) -> str | None:
    path = KNOWLEDGE_DIR / file_name
    return path.read_text(encoding="utf-8") if path.exists() else None


def load_general_rules() -> str:
    return _read(GENERAL_RULES_FILE) or (
        "(マニュアル未配置。knowledge/modules/ にPhase 0で書き起こしたMarkdownを配置してください)"
    )


def load_module_manual(module_id: str) -> str:
    if module_id not in MODULE_IDS:
        return f"不明なモジュールIDです: {module_id}。使えるID: {', '.join(MODULE_IDS)}"
    return _read(f"{module_id}.md") or f"{module_id} のマニュアルは未配置です。"


def keypad_symbol_table() -> str:
    """keypads.md の記号表 (見た目と呼び方) だけを返す。

    キーパッドは記号の特定にこの表が毎回必要で、マニュアル取得に1往復 (約3秒) かかるためsystem promptに常駐させる。
    """
    text = _read("keypads.md") or ""
    match = re.search(r"^## 記号の見た目と呼び方\n(.*?)(?=^## )", text, re.M | re.S)
    return match.group(1).strip() if match else "(キーパッドの記号表が未配置)"


def module_catalog() -> str:
    return "\n".join(f"- {module.id}: {module.name} — {module.appearance}" for module in MODULES)


def module_name(module_id: str) -> str:
    return next((module.name for module in MODULES if module.id == module_id), module_id)


def find_manual_version() -> tuple[str | None, str | None]:
    """マニュアル表紙のバージョンと認証コードを返す。ゲーム側と版が一致しているか開始時に確認するため。"""
    rules = load_general_rules()
    version = re.search(r"バージョン[:：]\s*(\S+)", rules)
    code = re.search(r"認証コード[:：]\s*(\d+)", rules)
    return (version.group(1) if version else None, code.group(1) if code else None)
