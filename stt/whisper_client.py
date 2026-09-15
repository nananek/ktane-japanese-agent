import ctypes
import importlib.util
import os
from pathlib import Path

import numpy as np


def _preload_cuda12_libs() -> None:
    """CTranslate2はCUDA 12版のcuBLAS/cuDNNを要求するが、torchがCUDA 13版を入れるため
    pip版の nvidia-cublas-cu12 / nvidia-cudnn-cu12 を先にロードしておく (LD_LIBRARY_PATH不要にする)。"""
    for package, lib_names in (
        ("nvidia.cublas", ("libcublasLt.so.12", "libcublas.so.12")),
        ("nvidia.cudnn", ("libcudnn.so.9",)),
    ):
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        lib_dir = Path(next(iter(spec.submodule_search_locations))) / "lib"
        for lib_name in lib_names:
            if (lib_dir / lib_name).exists():
                ctypes.CDLL(str(lib_dir / lib_name), mode=ctypes.RTLD_GLOBAL)


_preload_cuda12_libs()

from faster_whisper import WhisperModel  # noqa: E402

# ゲームで使う語彙を先に与えて認識を寄せる。数の答え (「にほん」→「日本」「ニコン」) や色・ラベルの誤認識が大きく減る
INITIAL_PROMPT = "電池は2本、3本。ワイヤは赤、青、白、黒、黄色。奇数、偶数。キーパッド、ボタン、起爆、中止、長押し。"
# initial_prompt を与えると無音・雑音に対してプロンプトの語句を出力することがある。
# 実測で実際の発話は no_speech_prob 0.16以下、プロンプト由来の幻聴は0.79以上だったため、その間で切る
NO_SPEECH_PROB_THRESHOLD = 0.5

# 日本語Whisperが雑音や無音に対して出しがちな定型の誤認識 (動画字幕の学習データ由来)
HALLUCINATION_PHRASES = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "ご清聴ありがとうございました",
    "ご清聴ありがとうございました。",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録よろしくお願いします。",
    "おやすみなさい",
    "おやすみなさい。",
}


class WhisperClient:
    def __init__(self) -> None:
        model_size = os.environ.get("WHISPER_MODEL_SIZE", "medium")
        device = os.environ.get("WHISPER_DEVICE", "cuda")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm_16k: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            pcm_16k, language="ja", beam_size=5, condition_on_previous_text=False, initial_prompt=INITIAL_PROMPT
        )
        texts = []
        for segment in segments:
            # 雑音・無音をWhisperが発話と取り違えた (幻聴) とみられる区間は捨てる
            if segment.no_speech_prob > NO_SPEECH_PROB_THRESHOLD:
                continue
            text = segment.text.strip()
            if text in HALLUCINATION_PHRASES:
                continue
            texts.append(text)
        return "".join(texts).strip()
