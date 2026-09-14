import json
import os
from dataclasses import dataclass, field

from openai import OpenAI

from .manual import MODULE_IDS, load_module_manual
from .solvers import keypad_symbol_ids, solve_keypad

SESSION_ID_PLACEHOLDER = "{session_id}"
# 1回の応答でツール呼び出しを繰り返せる上限。超えたらツールなしで回答させる
MAX_TOOL_ROUNDS = 3

MANUAL_TOOL = {
    "name": "get_module_manual",
    "description": "指定したモジュールのマニュアル (解除手順) を取得する。指示を出す前に必ず取得すること。",
    "parameters": {
        "type": "object",
        "properties": {
            "module": {"type": "string", "enum": list(MODULE_IDS), "description": "モジュールID"},
        },
        "required": ["module"],
    },
}


def _build_tools() -> list[dict]:
    tools = [MANUAL_TOOL]
    symbol_ids = keypad_symbol_ids()
    if symbol_ids:
        tools.append({
            "name": "solve_keypad",
            "description": (
                "キーパッドの記号IDから該当する列と押す順番を求める。記号IDはキーパッドのマニュアルの記号表で特定する。"
                "押す順番は自分で判定せず、必ずこのツールの結果に従うこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string", "enum": symbol_ids},
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "モジュールについているキーの記号ID",
                    },
                },
                "required": ["symbols"],
            },
        })
    return tools


TOOLS = _build_tools()
CHAT_TOOLS = [{"type": "function", "function": tool} for tool in TOOLS]
RESPONSES_TOOLS = [{"type": "function", **tool} for tool in TOOLS]


@dataclass
class LLMReply:
    text: str
    # 会話履歴に追記すべきメッセージ (ツール呼び出しと結果、最終応答)。取得済みマニュアルを次ターン以降も参照させるため
    messages: list[dict] = field(default_factory=list)
    consulted_modules: list[str] = field(default_factory=list)
    solver_outputs: list[str] = field(default_factory=list)


class LLMClient:
    """OpenAI互換APIのラッパー。base_urlを差し替えれば別プロバイダにも流用できる。

    LLM_HEADERS (JSONオブジェクト) で任意のHTTPヘッダーを追加できる。値に`{session_id}`を含めると
    リクエストごとに爆弾セッションのIDへ置換されるため、会話単位のセッションIDを要求するAPIにも対応できる。
    """

    def __init__(self) -> None:
        # 爆弾解除中は応答を待ち続けられないので、既定 (600秒+自動リトライ) より大幅に短く打ち切る
        self._client = OpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ["LLM_BASE_URL"],
            timeout=float(os.environ.get("LLM_TIMEOUT", "30")),
            max_retries=0,
        )
        self._model = os.environ["LLM_MODEL"]
        # "chat" (Chat Completions API) か "responses" (Responses API)。モデルによって対応エンドポイントが異なる
        self._api = os.environ.get("LLM_API", "chat").strip() or "chat"
        if self._api not in ("chat", "responses"):
            raise ValueError("LLM_API は chat か responses を指定してください")
        self._headers = self._load_headers()
        # 思考型モデルは曖昧な発話に対して長考しがちで、爆弾解除のテンポに合わないため浅くできるようにする
        self._reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "").strip() or None

    @staticmethod
    def _load_headers() -> dict[str, str]:
        raw = os.environ.get("LLM_HEADERS", "").strip()
        if not raw:
            return {}
        headers = json.loads(raw)
        if not isinstance(headers, dict):
            raise ValueError("LLM_HEADERS はJSONオブジェクトで指定してください")
        return {str(name): str(value) for name, value in headers.items()}

    def reply(self, system_prompt: str, history: list[dict], session_id: str) -> LLMReply:
        """history は呼び出し側で保持する会話履歴 (APIごとのネイティブ形式)。成功時に返す messages を追記すること。"""
        headers = {
            name: value.replace(SESSION_ID_PLACEHOLDER, session_id)
            for name, value in self._headers.items()
        }
        if self._api == "responses":
            return self._reply_responses(system_prompt, history, headers)
        return self._reply_chat(system_prompt, history, headers)

    def _reply_chat(self, system_prompt: str, history: list[dict], headers: dict[str, str]) -> LLMReply:
        result = LLMReply(text="")
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system_prompt}, *history, *result.messages],
                tools=CHAT_TOOLS,
                tool_choice="none" if round_index == MAX_TOOL_ROUNDS else "auto",
                temperature=0.2,
                extra_headers=headers,
                **({"reasoning_effort": self._reasoning_effort} if self._reasoning_effort else {}),
            )
            message = response.choices[0].message
            if not message.tool_calls:
                result.text = message.content or ""
                result.messages.append({"role": "assistant", "content": result.text})
                return result

            result.messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.function.name, "arguments": call.function.arguments}}
                    for call in message.tool_calls
                ],
            })
            for call in message.tool_calls:
                output = self._run_tool(call.function.name, call.function.arguments, result)
                result.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        raise AssertionError("unreachable")

    def _reply_responses(self, system_prompt: str, history: list[dict], headers: dict[str, str]) -> LLMReply:
        result = LLMReply(text="")
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            response = self._client.responses.create(
                model=self._model,
                instructions=system_prompt,
                input=[*history, *result.messages],
                tools=RESPONSES_TOOLS,
                tool_choice="none" if round_index == MAX_TOOL_ROUNDS else "auto",
                extra_headers=headers,
                **({"reasoning": {"effort": self._reasoning_effort}} if self._reasoning_effort else {}),
            )
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                result.text = response.output_text or ""
                result.messages.append({"role": "assistant", "content": result.text})
                return result

            for call in calls:
                output = self._run_tool(call.name, call.arguments, result)
                result.messages.append(
                    {"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                )
                result.messages.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
        raise AssertionError("unreachable")

    @staticmethod
    def _run_tool(name: str, arguments: str, result: LLMReply) -> str:
        try:
            args = json.loads(arguments or "{}")
            if name == "get_module_manual":
                module_id = args["module"]
                result.consulted_modules.append(module_id)
                return load_module_manual(module_id)
            if name == "solve_keypad":
                output = solve_keypad(list(args["symbols"]))
                result.solver_outputs.append(output)
                return output
        except (json.JSONDecodeError, KeyError, TypeError):
            return f"引数が不正です: {arguments}"
        return f"不明なツールです: {name}"
