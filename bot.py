import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import voice_recv
from dotenv import load_dotenv

from conversation import LLM_FAILURE_REPLY, attempt_reply, build_opening_line, commit_reply, is_filler, is_repeat_request
from llm.llm_client import LLMClient
from llm.manual import module_name
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

# スラッシュコマンドだけで操作するので、メッセージ本文を読む特権インテントは不要
intents = discord.Intents.default()
intents.voice_states = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

sessions = SessionManager()
# サーバーごとの聞き取り対象のユーザーID
listening_to: dict[int, int] = {}
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
# 直前にbotが読み上げた内容 (聞き返し用)
last_replies: dict[int, str] = {}
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
opening_line = build_opening_line()


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s", bot.user)
    # グローバル登録は反映に時間がかかるため、参加中の各サーバーに直接登録してすぐ使えるようにする
    for guild in bot.guilds:
        await sync_commands(guild)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await sync_commands(guild)


async def sync_commands(guild: discord.Guild) -> None:
    tree.copy_global_to(guild=guild)
    try:
        synced = await tree.sync(guild=guild)
        logger.info("スラッシュコマンドを登録しました: %s (%s)", guild.name, ", ".join(c.name for c in synced))
    except discord.HTTPException:
        logger.exception("スラッシュコマンドを登録できませんでした: %s", guild.name)


@dataclass
class Conversation:
    """1サーバー分の会話先。応答を投稿するテキストチャンネルと、ボイス接続を持つサーバー。"""

    guild: discord.Guild
    channel: discord.abc.Messageable

    @property
    def voice_client(self) -> voice_recv.VoiceRecvClient | None:
        return self.guild.voice_client  # type: ignore[return-value]


def listen_to(conv: Conversation, vc: voice_recv.VoiceRecvClient, member: discord.Member) -> None:
    """聞き取り対象を member ひとりに設定する (他の参加者の声やbotの声は拾わない)。"""
    if vc.is_listening():
        vc.stop_listening()

    def on_utterance(user_id: int, pcm_16k) -> None:
        # ゲーム終了 (end_game) の記録後は /ktane-newbomb まで、受け付け音・文字起こし・応答をすべて止める
        if is_game_over(guild_id):
            return
        # 文字起こしより先に鳴らし、話し終わりを検出したことをすぐ伝える
        asyncio.run_coroutine_threadsafe(play_ack(vc), bot.loop).add_done_callback(_log_utterance_error)
        future = asyncio.run_coroutine_threadsafe(handle_utterance(conv, user_id, pcm_16k), bot.loop)
        future.add_done_callback(_log_utterance_error)

    guild_id = conv.guild.id

    def is_muted() -> bool:
        # 読み上げ中と、読み上げ終了直後の残響が消えるまでは聞かない (半二重)
        # 受け付け音は短く小さいので、鳴らしている間も聞き続ける (続けて話した分を削らないため)
        ended_at = playback_ended_at.get(guild_id, 0.0)
        speaking = vc.is_playing() and not ack_playing.get(guild_id, False)
        return speaking or time.monotonic() - ended_at < ECHO_TAIL_SECONDS

    vc.listen(TranscribingSink(on_utterance, target_user_id=member.id, is_muted=is_muted))
    listening_to[guild_id] = member.id


@tree.command(name="ktane-join", description="ボイスチャンネルに参加し、実行した人の発話を聞き取る")
@app_commands.guild_only()
async def ktane_join(interaction: discord.Interaction) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        await interaction.response.send_message("先にボイスチャンネルに参加してください。", ephemeral=True)
        return
    # 接続に3秒以上かかると応答期限を過ぎるため、先に受け付けておく
    await interaction.response.defer()

    channel = member.voice.channel
    conv = Conversation(member.guild, interaction.channel)
    vc = conv.voice_client
    if vc is None:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
    elif vc.channel != channel:
        # 接続済みなら再接続せず、チャンネル移動と聞き取り対象の付け替えだけ行う
        await vc.move_to(channel)
    listen_to(conv, vc, member)
    if is_in_game(member.guild.id):
        # ゲーム中の再実行は聞き取り対象の付け替えだけにして、会話や爆弾の記録は残す
        await interaction.followup.send(f"{member.display_name} さんの発話を聞き取ります。ゲームはそのまま続けます。")
        return
    # 前のゲームが終わっている (終了記録が残っていると聞き取りが止まったまま) か、まだ始まっていなければ新しい爆弾にする
    sessions.reset(member.guild.id)
    await interaction.followup.send(f"{channel.name} に接続しました。{member.display_name} さんの発話を聞き取ります。")
    await announce_opening(conv)


@tree.command(name="ktane-newbomb", description="ゲーム終了後、新しい爆弾用に会話をリセットし、実行した人の発話を聞き取る")
@app_commands.describe(force="ゲームの終了が記録されていなくても強制的にリセットする")
@app_commands.guild_only()
async def ktane_newbomb(interaction: discord.Interaction, force: bool = False) -> None:
    member = interaction.user
    assert isinstance(member, discord.Member)
    # 解除中に誤って実行して会話や爆弾の記録を失う事故を防ぐため、終了 (解除/爆発/時間切れ) が記録されるまでは断る。
    # 開始の読み上げしかしていない (まだ何も話していない) 場合はゲーム前とみなして許可する
    if is_in_game(member.guild.id) and not force:
        await interaction.response.send_message(
            "まだゲーム中です。「解除できました」「爆発しました」「時間切れです」のように伝えて終了を記録してから"
            "実行するか、force を True にして実行してください。",
            ephemeral=True,
        )
        return

    conv = Conversation(member.guild, interaction.channel)
    sessions.reset(member.guild.id)
    message = "新しい爆弾用にセッションをリセットしました。"
    vc = conv.voice_client
    # 実行した人がbotと同じボイスチャンネルにいれば、聞き取り対象をその人に切り替える
    if vc is not None and member.voice is not None and member.voice.channel == vc.channel:
        if listening_to.get(member.guild.id) != member.id:
            listen_to(conv, vc, member)
            message += f"{member.display_name} さんの発話を聞き取ります。"
    await interaction.response.send_message(message)
    await announce_opening(conv)


@tree.command(name="ktane-leave", description="ボイスチャンネルから退出する")
@app_commands.guild_only()
async def ktane_leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc is None:
        await interaction.response.send_message("ボイスチャンネルに参加していません。", ephemeral=True)
        return
    await vc.disconnect()
    listening_to.pop(interaction.guild.id, None)
    await interaction.response.send_message("退出しました。")


async def announce_opening(conv: Conversation) -> None:
    """マニュアルの版と認証コードを伝えて爆弾の情報を聞き、会話履歴にもExpertの発言として残す。"""
    lock = utterance_locks.setdefault(conv.guild.id, asyncio.Lock())
    async with lock:
        sessions.get_or_create(conv.guild.id).add_assistant_message(opening_line)
        last_replies[conv.guild.id] = opening_line
        logger.info("reply (opening): %s", opening_line)
        await conv.channel.send(f"🤖 {opening_line}")
        if conv.voice_client is not None:
            await wait_voice_encryption_ready(conv.voice_client)
            await speak(conv.voice_client, opening_line)


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


def post_in_background(conv: Conversation, content: str) -> None:
    """テキストチャンネルへの記録を待たずに投稿する。

    投稿は1回0.3〜0.4秒かかり、待つとその分だけ応答の読み上げが遅れるため裏で送る。
    投稿順が前後しないよう、直前の投稿が終わってから送る。
    """
    previous = text_post_tasks.get(conv.guild.id)

    async def send() -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        await conv.channel.send(content)

    task = asyncio.create_task(send())
    task.add_done_callback(_log_post_error)
    text_post_tasks[conv.guild.id] = task


def _log_post_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("テキストチャンネルへの投稿に失敗しました", exc_info=task.exception())


def is_in_game(guild_id: int) -> bool:
    return sessions.get_or_create(guild_id).is_in_game


def is_game_over(guild_id: int) -> bool:
    return sessions.get_or_create(guild_id).is_game_over


async def repeat_last_reply(conv: Conversation) -> None:
    """直前の読み上げを繰り返す。応答中なら、その応答の読み上げが終わってから繰り返す。"""
    lock = utterance_locks.setdefault(conv.guild.id, asyncio.Lock())
    async with lock:
        reply = last_replies[conv.guild.id]
        logger.info("reply (repeat): %s", reply)
        post_in_background(conv, f"🤖 {reply}")
        if conv.voice_client is not None:
            await speak(conv.voice_client, reply)


async def handle_utterance(conv: Conversation, user_id: int, pcm_16k) -> None:
    """発話を文字起こしして未処理の発話に積み、応答生成はギルドごとの応答係にまとめて任せる。

    1発話ずつ応答すると、応答生成や読み上げの間に話した内容が後から1件ずつ遅れて処理されてしまう。
    そこで待っている間に溜まった発話は1つの発言にまとめて応答する。
    """
    # 文字起こしは発話順に1件ずつ (GPUを取り合わず、順番も入れ替わらないように)
    utterance_ended_at = time.monotonic()
    async with whisper_lock:
        if is_game_over(conv.guild.id):
            # 文字起こしを待つ間にゲーム終了が記録された発話も捨てる
            return
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
        if is_repeat_request(text) and conv.guild.id in last_replies:
            logger.info("repeat requested: %s", text)
            post_in_background(conv, f"🎙️ {text}")
            task = asyncio.create_task(repeat_last_reply(conv))
            task.add_done_callback(_log_utterance_error)
            return
        if not pending_texts.get(conv.guild.id):
            pending_since[conv.guild.id] = utterance_ended_at
        pending_texts.setdefault(conv.guild.id, []).append(text)
    logger.info("transcript: %s", text)
    post_in_background(conv, f"🎙️ {text}")

    task = responder_tasks.get(conv.guild.id)
    if task is None or task.done():
        task = asyncio.create_task(respond_to_pending(conv))
        task.add_done_callback(_log_utterance_error)
        responder_tasks[conv.guild.id] = task


async def respond_to_pending(conv: Conversation) -> None:
    pending = pending_texts.setdefault(conv.guild.id, [])
    lock = utterance_locks.setdefault(conv.guild.id, asyncio.Lock())
    async with lock:
        while pending:
            session = sessions.get_or_create(conv.guild.id)
            texts: list[str] = []
            for regeneration in range(MAX_REGENERATIONS + 1):
                texts.extend(pending)
                pending.clear()
                llm_started_at = time.monotonic()
                attempt = await asyncio.to_thread(attempt_reply, llm, session, "\n".join(texts))
                logger.info("timing: llm %.2fs", time.monotonic() - llm_started_at)
                # 応答を待つ間に続きを話していたら、その応答は古いので捨てて全部まとめて作り直す
                if pending and regeneration < MAX_REGENERATIONS:
                    logger.info("応答生成中に新しい発話が届いたため、まとめて再生成します")
                    continue
                break

            reply = commit_reply(session, attempt)
            result = attempt.result
            if result is not None:
                if session.state.game_result is not None:
                    # 終了の読み上げより後に届いた発話には応答しない
                    pending.clear()
                if result.tool_log.consulted_modules:
                    logger.info("manual lookup: %s", result.tool_log.consulted_modules)
                    names = "、".join(module_name(module_id) for module_id in result.tool_log.consulted_modules)
                    post_in_background(conv, f"📖 マニュアル参照: {names}")
                for output in result.tool_log.solver_outputs:
                    logger.info("solver: %s", output)
                    post_in_background(conv, f"🧮 {output}")
            # 監視中に答えの正否を確かめられるよう、読み上げる内容をログにも残す
            logger.info("reply: %s", reply)
            post_in_background(conv, f"🤖 {reply}")
            if reply and reply != LLM_FAILURE_REPLY:
                last_replies[conv.guild.id] = reply

            if conv.voice_client is not None and reply:
                await speak(conv.voice_client, reply, since=pending_since.pop(conv.guild.id, None))


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
