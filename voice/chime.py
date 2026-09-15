import numpy as np

SAMPLE_RATE = 48000
CHANNELS = 2
# Discordの送信フレーム (20ms, 16bit stereo) のバイト数
FRAME_BYTES = SAMPLE_RATE // 50 * CHANNELS * 2


def _render(tones: tuple[tuple[float, float], ...], volume: float, decay: float) -> bytes:
    """(周波数, 秒数) の音を順に鳴らす、discord.PCMAudio にそのまま渡せる 48kHz/16bit/stereo の生PCMを返す。"""
    parts = []
    for frequency, seconds in tones:
        t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        # 立ち上がり5msのフェードインと指数減衰で、プチノイズの出ない柔らかい音にする
        envelope = np.minimum(t / 0.005, 1.0) * np.exp(-t * decay)
        parts.append(np.sin(2 * np.pi * frequency * t) * envelope)
    mono = (np.concatenate(parts) * volume * 32767).astype(np.int16)
    pcm = np.repeat(mono, CHANNELS).tobytes()
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


def make_turn_chime() -> bytes:
    """botの発話が終わり、Defuserが話してよい番になったことを知らせる上がり調子の「ピポン」。"""
    return _render(((880.0, 0.09), (1318.5, 0.14)), volume=0.35, decay=18)


def make_ack_blip() -> bytes:
    """Defuserの発話の終わりを検出したことを知らせる、低く短い「ポッ」。ターン交代音と聞き分けられるようにする。"""
    return _render(((587.3, 0.08),), volume=0.3, decay=40)
