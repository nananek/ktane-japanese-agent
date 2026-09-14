import numpy as np
from silero_vad import VADIterator, load_silero_vad

SAMPLE_RATE = 16000
# silero-vadの推奨チャンクサイズ (16kHz)
FRAME_SAMPLES = 512


class SpeechSegmenter:
    """16kHz mono float32のフレームを逐次投入し、発話区間が確定したら音声全体を返す。

    silero-vad公式の`VADIterator`はstart/endイベントのみを返すため、発話中の音声本体は
    ここで別途バッファリングして連結する。min_silence_duration_msは、KTANEはDefuserの説明が
    長文になりがちなため、短い言い淀みで発話を打ち切らないよう既定値を少し長め(700ms)にしてある。
    """

    def __init__(self, silence_ms: int = 700, threshold: float = 0.5) -> None:
        model = load_silero_vad()
        self._vad_iterator = VADIterator(
            model,
            threshold=threshold,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=silence_ms,
        )
        self._buffer: list[np.ndarray] = []
        self._speaking = False

    def push(self, pcm_frame: np.ndarray) -> np.ndarray | None:
        """pcm_frame: float32, 16kHz, mono, FRAME_SAMPLES長。発話確定時はfloat32配列、それ以外はNone。"""
        if self._speaking:
            self._buffer.append(pcm_frame)

        event = self._vad_iterator(pcm_frame, return_seconds=False)
        if event is None:
            return None

        if "start" in event:
            self._speaking = True
            self._buffer = [pcm_frame]
            return None

        if "end" in event:
            self._speaking = False
            result = np.concatenate(self._buffer) if self._buffer else None
            self._buffer = []
            return result

        return None

    def reset(self) -> None:
        self._vad_iterator.reset_states()
        self._buffer = []
        self._speaking = False
