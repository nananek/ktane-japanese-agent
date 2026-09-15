import os

import httpx


class VoicevoxClient:
    """VOICEVOX ENGINE (ローカルHTTPサーバー) のクライアント。"""

    def __init__(self) -> None:
        base_url = os.environ.get("VOICEVOX_BASE_URL", "http://127.0.0.1:50021")
        self._speaker = int(os.environ.get("VOICEVOX_SPEAKER_ID", "3"))
        # 話速 (1.0が標準)。爆弾解除中は指示を早く聞き取りたいので既定を速めにしてある
        self._speed_scale = float(os.environ.get("VOICEVOX_SPEED_SCALE", "1.3"))
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def synthesize(self, text: str, pause_scale: float | None = None) -> bytes:
        """pause_scale は句点などの区切りの間の倍率。迷路の経路のように1手ずつ聞き取らせたい読み上げで長くする。"""
        query_res = self._client.post("/audio_query", params={"text": text, "speaker": self._speaker})
        query_res.raise_for_status()
        query = query_res.json()
        query["speedScale"] = self._speed_scale
        if pause_scale is not None:
            query["pauseLengthScale"] = pause_scale

        synth_res = self._client.post(
            "/synthesis",
            params={"speaker": self._speaker},
            json=query,
        )
        synth_res.raise_for_status()
        return synth_res.content
