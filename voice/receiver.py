import threading
import time

import numpy as np
from discord.ext import voice_recv

from .vad import FRAME_SAMPLES, SpeechSegmenter

DISCORD_CHANNELS = 2
DOWNSAMPLE_RATIO = 3  # 48kHz -> 16kHz (簡易間引き)
# 発話中にこの秒数パケットが届かなければ、Discord側が送信を止めた (=話し終わった) とみなす。
# 通常の受信間隔は20msなので、ネットワークの揺らぎで誤判定しない程度に長く、体感で遅れない程度に短くする
PACKET_GAP_SECONDS = 0.3
WATCHDOG_INTERVAL_SECONDS = 0.05


class TranscribingSink(voice_recv.AudioSink):
    """ユーザー(SSRC)ごとにPCMをVADへ投入し、発話区間が確定したらon_utteranceを呼ぶSink。"""

    def __init__(self, on_utterance, target_user_id: int | None = None, is_muted=lambda: False) -> None:
        super().__init__()
        self._on_utterance = on_utterance
        self._target_user_id = target_user_id
        # botの読み上げ中に真を返す。スピーカーから回り込んだbot自身の声を発話として拾わないよう、その間は聞かない
        self._is_muted = is_muted
        self._muted = False
        self._segmenters: dict[int, SpeechSegmenter] = {}
        self._frame_buffers: dict[int, np.ndarray] = {}
        self._last_audio_at: dict[int, float] = {}
        # write (パケットルーターのスレッド) と送信途切れの見張り (専用スレッド) が同じVAD状態を触るため
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        threading.Thread(target=self._watch_packet_gaps, daemon=True, name="utterance-gap-watchdog").start()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        if self._target_user_id is not None and user.id != self._target_user_id:
            return

        with self._lock:
            if self._is_muted():
                if not self._muted:
                    # 読み上げ開始時点で途中まで溜まっていた発話も、回り込みが混ざりうるので捨てる
                    self._muted = True
                    for segmenter in self._segmenters.values():
                        segmenter.reset()
                    self._frame_buffers.clear()
                return
            self._muted = False
            self._last_audio_at[user.id] = time.monotonic()

            pcm_16k = self._to_mono_16k(data.pcm)
            buffer = np.concatenate(
                [self._frame_buffers.get(user.id, np.empty(0, dtype=np.float32)), pcm_16k]
            )

            segmenter = self._segmenters.setdefault(user.id, SpeechSegmenter())

            offset = 0
            while offset + FRAME_SAMPLES <= len(buffer):
                frame = buffer[offset : offset + FRAME_SAMPLES]
                offset += FRAME_SAMPLES
                utterance = segmenter.push(frame)
                if utterance is not None:
                    self._on_utterance(user.id, utterance)

            self._frame_buffers[user.id] = buffer[offset:]

    def _watch_packet_gaps(self) -> None:
        """発話中に送信が途切れたユーザーの発話を、無音を補って確定させる。"""
        while not self._stopped.wait(WATCHDOG_INTERVAL_SECONDS):
            now = time.monotonic()
            with self._lock:
                for user_id, segmenter in self._segmenters.items():
                    if not segmenter.speaking or now - self._last_audio_at.get(user_id, now) < PACKET_GAP_SECONDS:
                        continue
                    self._frame_buffers.pop(user_id, None)
                    utterance = segmenter.flush()
                    if utterance is not None:
                        self._on_utterance(user_id, utterance)

    @staticmethod
    def _to_mono_16k(pcm_bytes: bytes) -> np.ndarray:
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        mono = pcm.reshape(-1, DISCORD_CHANNELS).mean(axis=1)
        return mono[::DOWNSAMPLE_RATIO]

    def cleanup(self) -> None:
        self._stopped.set()
        with self._lock:
            self._segmenters.clear()
            self._frame_buffers.clear()
