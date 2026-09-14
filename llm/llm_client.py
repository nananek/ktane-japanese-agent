import json
import os

from openai import OpenAI

SESSION_ID_PLACEHOLDER = "{session_id}"


class LLMClient:
    """OpenAI互換APIのラッパー。base_urlを差し替えれば別プロバイダにも流用できる。

    LLM_HEADERS (JSONオブジェクト) で任意のHTTPヘッダーを追加できる。値に`{session_id}`を含めると
    リクエストごとに爆弾セッションのIDへ置換されるため、会話単位のセッションIDを要求するAPIにも対応できる。
    """

    def __init__(self) -> None:
        self._client = OpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ["LLM_BASE_URL"],
        )
        self._model = os.environ["LLM_MODEL"]
        self._headers = self._load_headers()

    @staticmethod
    def _load_headers() -> dict[str, str]:
        raw = os.environ.get("LLM_HEADERS", "").strip()
        if not raw:
            return {}
        headers = json.loads(raw)
        if not isinstance(headers, dict):
            raise ValueError("LLM_HEADERS はJSONオブジェクトで指定してください")
        return {str(name): str(value) for name, value in headers.items()}

    def reply(self, system_prompt: str, history: list[dict], session_id: str) -> str:
        messages = [{"role": "system", "content": system_prompt}, *history]
        headers = {
            name: value.replace(SESSION_ID_PLACEHOLDER, session_id)
            for name, value in self._headers.items()
        }
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.2,
            extra_headers=headers,
        )
        return response.choices[0].message.content or ""
