"""テキストだけでExpertとやり取りを試すWeb UI。

Discord・Whisper・VOICEVOXを通さずに、LLMとソルバーの応答を確かめるためのもの。botとは別プロセスで動き、
会話と爆弾の状態はブラウザのタブごとに持つため、通話中のセッションには影響しない。
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from conversation import LLM_FAILURE_REPLY, attempt_reply, build_opening_line, commit_reply, is_filler, is_repeat_request
from llm.llm_client import LLMClient
from llm.manual import module_name
from session import BombSession

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("ktane-web")

PAGE = Path(__file__).resolve().parent / "static" / "chat.html"


@dataclass
class ChatSession:
    """ブラウザのタブ1つ分の会話。画面に出す記録 (entries) はリロードしても表示し直せるようサーバー側で持つ。"""

    id: str
    bomb: BombSession = field(default_factory=lambda: BombSession(guild_id=0))
    entries: list[dict] = field(default_factory=list)
    last_reply: str | None = None
    # 応答生成は1つずつ (同じセッションに複数タブから送られても履歴が入れ替わらないように)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def start_bomb(self) -> None:
        self.bomb = BombSession(guild_id=0)
        self.entries.append({"kind": "note", "text": f"新しい爆弾 (session_id: {self.bomb.session_id})"})
        self.bomb.add_assistant_message(opening_line)
        self.add_reply(opening_line)

    def add_reply(self, text: str, **meta) -> None:
        self.entries.append({"kind": "assistant", "text": text, **meta})
        if text and text != LLM_FAILURE_REPLY:
            self.last_reply = text

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "entries": self.entries,
            "state": self.bomb.state.summary(),
            "in_game": self.bomb.is_in_game,
            "game_over": self.bomb.is_game_over,
        }


def tool_calls(messages: list[dict]) -> list[dict]:
    """応答で追記されたメッセージから、ツール呼び出しの引数と結果を組にして取り出す (Chat/Responses両形式)。"""
    calls: dict[str, dict] = {}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                calls[call["id"]] = {"name": call["function"]["name"], "arguments": call["function"]["arguments"]}
        elif message.get("role") == "tool":
            calls[message["tool_call_id"]]["output"] = message["content"]
        elif message.get("type") == "function_call":
            calls[message["call_id"]] = {"name": message["name"], "arguments": message["arguments"]}
        elif message.get("type") == "function_call_output":
            calls[message["call_id"]]["output"] = message["output"]
    return list(calls.values())


def tool_entry(call: dict) -> dict:
    try:
        args = json.loads(call["arguments"] or "{}")
        arguments = json.dumps(args, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        args, arguments = {}, call["arguments"]
    if call["name"] == "get_module_manual":
        label = f"📖 マニュアル参照: {module_name(args.get('module', '?'))}"
    else:
        label = f"🧮 {call['name']}"
    return {"kind": "tool", "label": label, "arguments": arguments, "output": call.get("output", "")}


llm = LLMClient()
opening_line = build_opening_line()
chats: dict[str, ChatSession] = {}
routes = web.RouteTableDef()


def get_chat(request: web.Request) -> ChatSession:
    chat = chats.get(request.match_info["chat_id"])
    if chat is None:
        # サーバーを再起動するとセッションは消える。画面側で新しく作り直す
        raise web.HTTPNotFound(text="セッションが見つかりません")
    return chat


@routes.get("/")
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(PAGE)


@routes.post("/api/sessions")
async def create_session(request: web.Request) -> web.Response:
    chat = ChatSession(id=uuid.uuid4().hex)
    chat.start_bomb()
    chats[chat.id] = chat
    return web.json_response(chat.snapshot())


@routes.get("/api/sessions/{chat_id}")
async def show_session(request: web.Request) -> web.Response:
    return web.json_response(get_chat(request).snapshot())


@routes.post("/api/sessions/{chat_id}/newbomb")
async def new_bomb(request: web.Request) -> web.Response:
    chat = get_chat(request)
    body = await request.json()
    async with chat.lock:
        # botの /ktane-newbomb と同じく、解除中の誤操作で会話を失わないよう終了の記録までは断る
        if chat.bomb.is_in_game and not body.get("force"):
            raise web.HTTPConflict(text="まだゲーム中です。終了を伝えて記録するか、強制的にリセットしてください。")
        chat.start_bomb()
    return web.json_response(chat.snapshot())


@routes.post("/api/sessions/{chat_id}/messages")
async def post_message(request: web.Request) -> web.Response:
    chat = get_chat(request)
    text = str((await request.json()).get("text", "")).strip()
    if not text:
        raise web.HTTPBadRequest(text="発言が空です")
    async with chat.lock:
        if chat.bomb.is_game_over:
            raise web.HTTPConflict(text="ゲーム終了が記録されています。新しい爆弾を始めてください。")
        chat.entries.append({"kind": "user", "text": text})
        if is_filler(text):
            chat.entries.append({"kind": "note", "text": "言いよどみだけの発言として無視しました (botと同じ扱い)"})
        elif is_repeat_request(text) and chat.last_reply is not None:
            chat.add_reply(chat.last_reply, source="聞き返し: 直前の応答を繰り返し")
        else:
            started_at = time.monotonic()
            attempt = await asyncio.to_thread(attempt_reply, llm, chat.bomb, text)
            seconds = time.monotonic() - started_at
            logger.info("timing: llm %.2fs", seconds)
            reply = commit_reply(chat.bomb, attempt)
            result = attempt.result
            if result is None:
                chat.add_reply(reply, source=f"LLMの応答取得に失敗 ({seconds:.1f}秒)")
            else:
                chat.entries.extend(tool_entry(call) for call in tool_calls(result.messages))
                via_solver = result.tool_log.final_speech is not None and reply == result.tool_log.final_speech
                chat.add_reply(reply, source=f"{'ソルバーの定型文' if via_solver else 'LLM'} ({seconds:.1f}秒)")
    return web.json_response(chat.snapshot())


def main() -> None:
    app = web.Application()
    app.add_routes(routes)
    # 認証がないため既定ではローカルからだけ受け付ける (コンテナ内では WEB_HOST=0.0.0.0 にする)
    web.run_app(app, host=os.environ.get("WEB_HOST", "127.0.0.1"), port=int(os.environ.get("WEB_PORT", "8080")))


if __name__ == "__main__":
    main()
