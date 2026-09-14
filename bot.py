import asyncio
import io
import logging
import os

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv

from llm.llm_client import LLMClient
from llm.prompts import build_system_prompt
from session import SessionManager
from stt.whisper_client import WhisperClient
from tts.voicevox_client import VoicevoxClient
from voice.receiver import TranscribingSink

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ktane-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = SessionManager()
whisper = WhisperClient()
llm = LLMClient()
voicevox = VoicevoxClient()
system_prompt = build_system_prompt()


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s", bot.user)


@bot.command()
async def join(ctx: commands.Context) -> None:
    if ctx.author.voice is None:
        await ctx.send("先にボイスチャンネルに参加してください。")
        return

    channel = ctx.author.voice.channel
    vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

    def on_utterance(user_id: int, pcm_16k) -> None:
        asyncio.run_coroutine_threadsafe(handle_utterance(ctx, user_id, pcm_16k), bot.loop)

    sink = TranscribingSink(on_utterance, target_user_id=ctx.author.id)
    vc.listen(sink)
    await ctx.send(f"{channel.name} に接続しました。{ctx.author.display_name} さんの発話を待ち受けます。")


@bot.command()
async def leave(ctx: commands.Context) -> None:
    if ctx.voice_client is not None:
        await ctx.voice_client.disconnect()
        await ctx.send("退出しました。")


@bot.command()
async def newbomb(ctx: commands.Context) -> None:
    sessions.reset(ctx.guild.id)
    await ctx.send("新しい爆弾用にセッションをリセットしました。")


async def handle_utterance(ctx: commands.Context, user_id: int, pcm_16k) -> None:
    session = sessions.get_or_create(ctx.guild.id)

    text = await asyncio.to_thread(whisper.transcribe, pcm_16k)
    if not text:
        return
    await ctx.send(f"🎙️ {text}")

    session.add_user_message(text)
    reply = await asyncio.to_thread(llm.reply, system_prompt, session.history, session.session_id)
    session.add_assistant_message(reply)
    await ctx.send(f"🤖 {reply}")

    if ctx.voice_client is not None and reply:
        wav_bytes = await asyncio.to_thread(voicevox.synthesize, reply)
        play_wav(ctx.voice_client, wav_bytes)


def play_wav(vc: discord.VoiceClient, wav_bytes: bytes) -> None:
    source = discord.FFmpegPCMAudio(io.BytesIO(wav_bytes), pipe=True)
    if vc.is_playing():
        vc.stop()
    vc.play(source)


def main() -> None:
    token = os.environ["DISCORD_TOKEN"]
    bot.run(token)


if __name__ == "__main__":
    main()
