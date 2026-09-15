import numpy as np
from discord.ext import voice_recv

from .vad import FRAME_SAMPLES, SpeechSegmenter

DISCORD_CHANNELS = 2
DOWNSAMPLE_RATIO = 3  # 48kHz -> 16kHz (簡易間引き)


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

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        if self._target_user_id is not None and user.id != self._target_user_id:
            return

        if self._is_muted():
            if not self._muted:
                # 読み上げ開始時点で途中まで溜まっていた発話も、回り込みが混ざりうるので捨てる
                self._muted = True
                for segmenter in self._segmenters.values():
                    segmenter.reset()
                self._frame_buffers.clear()
            return
        self._muted = False

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

    @staticmethod
    def _to_mono_16k(pcm_bytes: bytes) -> np.ndarray:
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        mono = pcm.reshape(-1, DISCORD_CHANNELS).mean(axis=1)
        return mono[::DOWNSAMPLE_RATIO]

    def cleanup(self) -> None:
        self._segmenters.clear()
        self._frame_buffers.clear()
