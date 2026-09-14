import os

import numpy as np
from faster_whisper import WhisperModel


class WhisperClient:
    def __init__(self) -> None:
        model_size = os.environ.get("WHISPER_MODEL_SIZE", "medium")
        device = os.environ.get("WHISPER_DEVICE", "cuda")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm_16k: np.ndarray) -> str:
        segments, _ = self._model.transcribe(pcm_16k, language="ja", beam_size=5)
        return "".join(segment.text for segment in segments).strip()
