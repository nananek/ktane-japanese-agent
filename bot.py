import asyncio
import copy
import io
import logging
import os
import re
import time

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
from voice.chime import make_ack_blip, make_turn_chime
from voice.receiver import TranscribingSink

load_dotenv()
dave.install()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("ktane-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = SessionManager()
# 応答の生成〜読み上げはギルドごとに1つずつ (開始時の読み上げとも重ならないように)
utterance_locks: dict[int, asyncio.Lock] = {}
whisper_lock = asyncio.Lock()
# 文字起こし済みで、まだ応答していない発話と、そのうち最初の発話が終わった時刻 (応答までの遅延計測用)
pending_texts: dict[int, list[str]] = {}
pending_since: dict[int, float] = {}
responder_tasks: dict[int, asyncio.Task] = {}
text_post_tasks: dict[int, asyncio.Task] = {}
# 応答生成中に新しい発話が届いたときに作り直す上限回数 (話し続けられても応答が返らなくならないように)
MAX_REGENERATIONS = 2
FILLER_WORDS = {"", "ん", "んー", "んん", "あ", "あー", "あっ", "え", "えー", "えっと", "えーと", "あの", "あのー", "うーん", "ふむ"}
LLM_FAILURE_REPLY = "すみません、応答が取れませんでした。もう一度言ってください。"
# 読み上げ終了後も聞き取りを止めておく秒数 (スピーカーからの残響をbot自身の声として拾わないように)。
# 最後に鳴るのは短く減衰の速いチャイムなので短めでよく、長いとチャイム直後の話し始めが削られる
ECHO_TAIL_SECONDS = 0.3
TURN_CHIME = make_turn_chime()
ACK_BLIP = make_ack_blip()
# 受け付け音の再生中か (受け付け音は短く、鳴らしている間も聞き取りを止めない)
ack_playing: dict[int, bool] = {}
playback_ended_at: dict[int, float] = {}
whisper = WhisperClient()
llm = LLMClient()
voicevox = VoicevoxClient()
manual_version, manual_code = find_manual_version()
# 開始時にマニュアルの版を伝え、Defuserがゲーム側の認証コードと一致しているか確認できるようにする。
# 爆弾の情報はここでは聞かない (最初に全部確認すると時間を使い切るため、判定に要るときだけ聞く)
opening_line = (
    f"マニュアル、バージョン{manual_version or '不明'}、認証コード{manual_code}。" if manual_code else ""
) + "どのモジュールから？"


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
        # 文字起こしより先に鳴らし、話し終わりを検出したことをすぐ伝える
        asyncio.run_coroutine_threadsafe(play_ack(vc), bot.loop).add_done_callback(_log_utterance_error)
        future = asyncio.run_coroutine_threadsafe(handle_utterance(ctx, user_id, pcm_16k), bot.loop)
        future.add_done_callback(_log_utterance_error)

    guild_id = ctx.guild.id

    def is_muted() -> bool:
        # 読み上げ中と、読み上げ終了直後の残響が消えるまでは聞かない (半二重)
        # 受け付け音は短く小さいので、鳴らしている間も聞き続ける (続けて話した分を削らないため)
        ended_at = playback_ended_at.get(guild_id, 0.0)
        speaking = vc.is_playing() and not ack_playing.get(guild_id, False)
        return speaking or time.monotonic() - ended_at < ECHO_TAIL_SECONDS

    sink = TranscribingSink(on_utterance, target_user_id=ctx.author.id, is_muted=is_muted)
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
    """マニュアルの版と認証コードを伝えて爆弾の情報を聞き、会話履歴にもExpertの発言として残す。"""
    lock = utterance_locks.setdefault(ctx.guild.id, asyncio.Lock())
    async with lock:
        sessions.get_or_create(ctx.guild.id).add_assistant_message(opening_line)
        await ctx.send(f"🤖 {opening_line}")
        if ctx.voice_client is not None:
            await wait_voice_encryption_ready(ctx.voice_client)
            await speak(ctx.voice_client, opening_line)


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


def post_in_background(ctx: commands.Context, content: str) -> None:
    """テキストチャンネルへの記録を待たずに投稿する。

    投稿は1回0.3〜0.4秒かかり、待つとその分だけ応答の読み上げが遅れるため裏で送る。
    投稿順が前後しないよう、直前の投稿が終わってから送る。
    """
    previous = text_post_tasks.get(ctx.guild.id)

    async def send() -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        await ctx.send(content)

    task = asyncio.create_task(send())
    task.add_done_callback(_log_post_error)
    text_post_tasks[ctx.guild.id] = task


def _log_post_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("テキストチャンネルへの投稿に失敗しました", exc_info=task.exception())


def is_filler(text: str) -> bool:
    """意味を持たない言いよどみだけの発話か。「はい」「うん」は質問への答えになりうるので含めない。"""
    normalized = re.sub(r"[\s。、,.!?！？〜…]", "", text)
    return normalized in FILLER_WORDS


async def handle_utterance(ctx: commands.Context, user_id: int, pcm_16k) -> None:
    """発話を文字起こしして未処理の発話に積み、応答生成はギルドごとの応答係にまとめて任せる。

    1発話ずつ応答すると、応答生成や読み上げの間に話した内容が後から1件ずつ遅れて処理されてしまう。
    そこで待っている間に溜まった発話は1つの発言にまとめて応答する。
    """
    # 文字起こしは発話順に1件ずつ (GPUを取り合わず、順番も入れ替わらないように)
    utterance_ended_at = time.monotonic()
    async with whisper_lock:
        stt_started_at = time.monotonic()
        text = await asyncio.to_thread(whisper.transcribe, pcm_16k)
        logger.info(
            "timing: stt %.2fs (audio %.1fs, waited %.2fs)",
            time.monotonic() - stt_started_at, len(pcm_16k) / 16000, stt_started_at - utterance_ended_at,
        )
        if not text:
            return
        if is_filler(text):
            # 「ん」「えーと」だけの発話に応答すると、直前の指示を繰り返すなど往復が無駄に増える
            logger.info("filler ignored: %s", text)
            return
        if not pending_texts.get(ctx.guild.id):
            pending_since[ctx.guild.id] = utterance_ended_at
        pending_texts.setdefault(ctx.guild.id, []).append(text)
    logger.info("transcript: %s", text)
    post_in_background(ctx, f"🎙️ {text}")

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
                # ツールが書き換える状態はコピーに対して試し、応答を採用したときだけ反映する
                # (作り直しで捨てた応答のソルバー記録が残ると、記憶や順番ワイヤの数え方が狂うため)
                attempt_state = copy.deepcopy(session.state)
                llm_started_at = time.monotonic()
                try:
                    result = await asyncio.to_thread(
                        llm.reply,
                        build_system_prompt(attempt_state),
                        [*session.history, user_message],
                        session.session_id,
                        attempt_state,
                    )
                except Exception:
                    # タイムアウト等。無言で止まるとDefuserが待ち続けてしまうので、言い直しを促す
                    logger.exception("LLMの応答取得に失敗しました")
                    result = None
                logger.info("timing: llm %.2fs", time.monotonic() - llm_started_at)
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
                session.state = attempt_state
                reply = result.text
                if result.tool_log.consulted_modules:
                    logger.info("manual lookup: %s", result.tool_log.consulted_modules)
                    names = "、".join(module_name(module_id) for module_id in result.tool_log.consulted_modules)
                    post_in_background(ctx, f"📖 マニュアル参照: {names}")
                for output in result.tool_log.solver_outputs:
                    logger.info("solver: %s", output)
                    post_in_background(ctx, f"🧮 {output}")
            post_in_background(ctx, f"🤖 {reply}")

            if ctx.voice_client is not None and reply:
                await speak(ctx.voice_client, reply, since=pending_since.pop(ctx.guild.id, None))


async def speak(vc: discord.VoiceClient, text: str, since: float | None = None) -> None:
    """読み上げに続けてターン交代のチャイムを鳴らし、鳴り終わるまで待つ。

    チャイムが鳴り終わると聞き取りが再開するので、Defuserはチャイムを話し始めの合図にできる。
    """
    tts_started_at = time.monotonic()
    wav_bytes = await asyncio.to_thread(voicevox.synthesize, text)
    now = time.monotonic()
    if since is None:
        logger.info("timing: tts %.2fs", now - tts_started_at)
    else:
        logger.info("timing: tts %.2fs, 発話終了から読み上げ開始まで %.2fs", now - tts_started_at, now - since)
    await play_source(vc, discord.FFmpegPCMAudio(io.BytesIO(wav_bytes), pipe=True))
    await play_source(vc, discord.PCMAudio(io.BytesIO(TURN_CHIME)))


async def play_ack(vc: discord.VoiceClient) -> None:
    """話し終わりの受け付け音を鳴らす。bot自身の読み上げ中なら邪魔しないよう鳴らさない。"""
    if not vc.is_connected() or vc.is_playing():
        return
    ack_playing[vc.guild.id] = True
    try:
        await play_source(vc, discord.PCMAudio(io.BytesIO(ACK_BLIP)), is_ack=True)
    finally:
        ack_playing[vc.guild.id] = False


async def play_source(vc: discord.VoiceClient, source: discord.AudioSource, is_ack: bool = False) -> None:
    """音声を再生し、再生が終わるまで待つ。切断済みなら何もしない。"""
    if not is_ack:
        # 受け付け音が鳴っている最中なら、鳴り終わるのを待ってから読み上げる
        while vc.is_connected() and vc.is_playing() and ack_playing.get(vc.guild.id, False):
            await asyncio.sleep(0.02)
    if not vc.is_connected():
        # 読み上げ中にVCから外された場合など。応答処理全体を例外で止めないよう読み上げだけ諦める
        logger.warning("ボイスチャンネルに接続していないため再生をスキップしました")
        return
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def after(error: Exception | None) -> None:
        if not is_ack:
            # 残響待ちはbot自身の読み上げ・ターン交代音の後だけ (受け付け音の後は聞き取りを止めない)
            playback_ended_at[vc.guild.id] = time.monotonic()
        if error is not None:
            logger.error("音声の再生に失敗しました", exc_info=error)
        loop.call_soon_threadsafe(finished.set)

    vc.play(source, after=after)
    await finished.wait()


def main() -> None:
    token = os.environ["DISCORD_TOKEN"]
    bot.run(token)


if __name__ == "__main__":
    main()
