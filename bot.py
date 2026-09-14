import asyncio
import io
import logging
import os

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv

from llm.llm_client import LLMClient
from llm.manual import find_manual_version, module_name
from llm.prompts import build_system_prompt
from session import SessionManager
from stt.whisper_client import WhisperClient
from tts.voicevox_client import VoicevoxClient
from voice import dave
from voice.receiver import TranscribingSink

load_dotenv()
dave.install()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ktane-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = SessionManager()
# 応答の生成〜読み上げはギルドごとに1つずつ (開始時の読み上げとも重ならないように)
utterance_locks: dict[int, asyncio.Lock] = {}
whisper_lock = asyncio.Lock()
# 文字起こし済みで、まだ応答していない発話
pending_texts: dict[int, list[str]] = {}
responder_tasks: dict[int, asyncio.Task] = {}
# 応答生成中に新しい発話が届いたときに作り直す上限回数 (話し続けられても応答が返らなくならないように)
MAX_REGENERATIONS = 2
LLM_FAILURE_REPLY = "すみません、応答が取れませんでした。もう一度言ってください。"
whisper = WhisperClient()
llm = LLMClient()
voicevox = VoicevoxClient()
system_prompt = build_system_prompt()
manual_version, manual_code = find_manual_version()
# 開始時にマニュアルの版を伝え、Defuserがゲーム側の認証コードと一致しているか確認できるようにする
opening_line = (
    f"マニュアル、バージョン{manual_version or '不明'}、認証コード{manual_code}。"
    if manual_code
    else None
)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s", bot.user)


@bot.command()
async def join(ctx: commands.Context) -> None:
    if ctx.author.voice is None:
        await ctx.send("先にボイスチャンネルに参加してください。")
        return

    channel = ctx.author.voice.channel
    vc = ctx.voice_client
    if vc is None:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
    else:
        # 接続済みなら再接続せず、チャンネル移動と聞き取り対象の付け替えだけ行う
        if vc.channel != channel:
            await vc.move_to(channel)
        if vc.is_listening():
            vc.stop_listening()

    def on_utterance(user_id: int, pcm_16k) -> None:
        future = asyncio.run_coroutine_threadsafe(handle_utterance(ctx, user_id, pcm_16k), bot.loop)
        future.add_done_callback(_log_utterance_error)

    sink = TranscribingSink(on_utterance, target_user_id=ctx.author.id)
    vc.listen(sink)
    await ctx.send(f"{channel.name} に接続しました。{ctx.author.display_name} さんの発話を待ち受けます。")
    await announce_opening(ctx)


@bot.command()
async def leave(ctx: commands.Context) -> None:
    if ctx.voice_client is not None:
        await ctx.voice_client.disconnect()
        await ctx.send("退出しました。")


@bot.command()
async def newbomb(ctx: commands.Context) -> None:
    sessions.reset(ctx.guild.id)
    await ctx.send("新しい爆弾用にセッションをリセットしました。")
    await announce_opening(ctx)


async def announce_opening(ctx: commands.Context) -> None:
    """マニュアルの版と認証コードを読み上げ、会話履歴にもExpertの発言として残す。"""
    if opening_line is None:
        return
    lock = utterance_locks.setdefault(ctx.guild.id, asyncio.Lock())
    async with lock:
        sessions.get_or_create(ctx.guild.id).add_assistant_message(opening_line)
        await ctx.send(f"🤖 {opening_line}")
        if ctx.voice_client is not None:
            await wait_voice_encryption_ready(ctx.voice_client)
            wav_bytes = await asyncio.to_thread(voicevox.synthesize, opening_line)
            await play_wav(ctx.voice_client, wav_bytes)


async def wait_voice_encryption_ready(vc: discord.VoiceClient, timeout: float = 5.0) -> None:
    """接続直後はDAVEの鍵交換が終わっておらず、その間に送った音声は相手に届かないため待つ。"""
    state = vc._connection
    deadline = asyncio.get_running_loop().time() + timeout
    while state.dave_protocol_version != 0 and not state.can_encrypt:
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning("DAVEの準備が%s秒以内に完了しませんでした", timeout)
            return
        await asyncio.sleep(0.1)


def _log_utterance_error(future) -> None:
    if not future.cancelled() and future.exception() is not None:
        logger.error("発話の処理に失敗しました", exc_info=future.exception())


async def handle_utterance(ctx: commands.Context, user_id: int, pcm_16k) -> None:
    """発話を文字起こしして未処理の発話に積み、応答生成はギルドごとの応答係にまとめて任せる。

    1発話ずつ応答すると、応答生成や読み上げの間に話した内容が後から1件ずつ遅れて処理されてしまう。
    そこで待っている間に溜まった発話は1つの発言にまとめて応答する。
    """
    # 文字起こしは発話順に1件ずつ (GPUを取り合わず、順番も入れ替わらないように)
    async with whisper_lock:
        text = await asyncio.to_thread(whisper.transcribe, pcm_16k)
        if not text:
            return
        pending_texts.setdefault(ctx.guild.id, []).append(text)
    logger.info("transcript: %s", text)
    await ctx.send(f"🎙️ {text}")

    task = responder_tasks.get(ctx.guild.id)
    if task is None or task.done():
        task = asyncio.create_task(respond_to_pending(ctx))
        task.add_done_callback(_log_utterance_error)
        responder_tasks[ctx.guild.id] = task


async def respond_to_pending(ctx: commands.Context) -> None:
    pending = pending_texts.setdefault(ctx.guild.id, [])
    lock = utterance_locks.setdefault(ctx.guild.id, asyncio.Lock())
    async with lock:
        while pending:
            session = sessions.get_or_create(ctx.guild.id)
            texts: list[str] = []
            for attempt in range(MAX_REGENERATIONS + 1):
                texts.extend(pending)
                pending.clear()
                user_message = {"role": "user", "content": "\n".join(texts)}
                try:
                    result = await asyncio.to_thread(
                        llm.reply, system_prompt, [*session.history, user_message], session.session_id
                    )
                except Exception:
                    # タイムアウト等。無言で止まるとDefuserが待ち続けてしまうので、言い直しを促す
                    logger.exception("LLMの応答取得に失敗しました")
                    result = None
                # 応答を待つ間に続きを話していたら、その応答は古いので捨てて全部まとめて作り直す
                if pending and attempt < MAX_REGENERATIONS:
                    logger.info("応答生成中に新しい発話が届いたため、まとめて再生成します")
                    continue
                break

            session.history.append(user_message)
            if result is None:
                reply = LLM_FAILURE_REPLY
            else:
                session.history.extend(result.messages)
                reply = result.text
                if result.consulted_modules:
                    logger.info("manual lookup: %s", result.consulted_modules)
                    names = "、".join(module_name(module_id) for module_id in result.consulted_modules)
                    await ctx.send(f"📖 マニュアル参照: {names}")
                for output in result.solver_outputs:
                    logger.info("solver: %s", output)
                    await ctx.send(f"🧮 {output}")
            await ctx.send(f"🤖 {reply}")

            if ctx.voice_client is not None and reply:
                wav_bytes = await asyncio.to_thread(voicevox.synthesize, reply)
                await play_wav(ctx.voice_client, wav_bytes)


async def play_wav(vc: discord.VoiceClient, wav_bytes: bytes) -> None:
    """読み上げを再生し、再生が終わるまで待つ。"""
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def after(error: Exception | None) -> None:
        if error is not None:
            logger.error("読み上げの再生に失敗しました", exc_info=error)
        loop.call_soon_threadsafe(finished.set)

    source = discord.FFmpegPCMAudio(io.BytesIO(wav_bytes), pipe=True)
    vc.play(source, after=after)
    await finished.wait()


def main() -> None:
    token = os.environ["DISCORD_TOKEN"]
    bot.run(token)


if __name__ == "__main__":
    main()
