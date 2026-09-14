from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "modules"

SYSTEM_PROMPT_TEMPLATE = """あなたはKeep Talking and Nobody Explodesという爆弾解除協力ゲームのExpert役です。
あなたはこの後に続くマニュアルを読むことができますが、爆弾そのものを見ることはできません。
Defuser役のプレイヤーが爆弾の状態を音声で説明するので、以下のマニュアルの内容だけに基づいて、
次に取るべき操作を具体的かつ簡潔に指示してください。

# 振る舞いのルール
- マニュアルに書かれていない情報を推測や一般知識で補わないこと。情報が不足している場合は、
  必要な情報(シリアルナンバー、バッテリー数、インジケーターの有無など)を聞き返すこと。
- 1回の応答は次の1アクションに絞り、簡潔に話すこと。長い説明を一度にまとめて話さないこと。
- 応答はそのまま音声合成されて読み上げられる。箇条書き記号や見出し記号を使わず、自然な話し言葉で答えること。
- 複数モジュールの話が混在しうる。今どのモジュールについて話しているか不明なら先に確認すること。

# マニュアル (無印/標準モジュールのみ)

{manual_text}
"""


def _load_manual_text() -> str:
    if not KNOWLEDGE_DIR.exists() or not any(KNOWLEDGE_DIR.glob("*.md")):
        return "(マニュアル未配置。knowledge/modules/ にPhase 0で書き起こしたMarkdownを配置してください)"
    parts = [path.read_text(encoding="utf-8") for path in sorted(KNOWLEDGE_DIR.glob("*.md"))]
    return "\n\n---\n\n".join(parts)


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(manual_text=_load_manual_text())
