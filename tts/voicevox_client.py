import os

import httpx


class VoicevoxClient:
    """VOICEVOX ENGINE (ローカルHTTPサーバー) のクライアント。"""

    def __init__(self) -> None:
        base_url = os.environ.get("VOICEVOX_BASE_URL", "http://127.0.0.1:50021")
        self._speaker = int(os.environ.get("VOICEVOX_SPEAKER_ID", "3"))
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def synthesize(self, text: str) -> bytes:
        query_res = self._client.post("/audio_query", params={"text": text, "speaker": self._speaker})
        query_res.raise_for_status()

        synth_res = self._client.post(
            "/synthesis",
            params={"speaker": self._speaker},
            json=query_res.json(),
        )
        synth_res.raise_for_status()
        return synth_res.content
