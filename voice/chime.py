import numpy as np

SAMPLE_RATE = 48000
CHANNELS = 2
# Discordの送信フレーム (20ms, 16bit stereo) のバイト数
FRAME_BYTES = SAMPLE_RATE // 50 * CHANNELS * 2


def make_turn_chime() -> bytes:
    """botの発話が終わり、Defuserが話してよい番になったことを知らせる「ピポン」。

    discord.PCMAudio にそのまま渡せる 48kHz/16bit/stereo の生PCMを返す。
    """
    tones = []
    for frequency, seconds in ((880.0, 0.09), (1318.5, 0.14)):
        t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        # 立ち上がり5msのフェードインと指数減衰で、プチノイズの出ない柔らかい音にする
        envelope = np.minimum(t / 0.005, 1.0) * np.exp(-t * 18)
        tones.append(np.sin(2 * np.pi * frequency * t) * envelope)
    mono = (np.concatenate(tones) * 0.35 * 32767).astype(np.int16)
    pcm = np.repeat(mono, CHANNELS).tobytes()
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)
