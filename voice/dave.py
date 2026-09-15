"""discord-ext-voice-recv に DAVE (Discordの音声E2EE) の受信側復号を後付けするパッチ。

PyPI版の discord-ext-voice-recv はトランスポート層の暗号しか外さないため、DAVEが有効な通話では
E2EE暗号文のままopusデコーダーに渡され `OpusError: corrupted stream` でパケットルーターが停止する。
discord.py本体 (2.7+) は送信用にDAVEのMLSセッション (`davey.DaveSession`) を維持しているので、
それを使ってopusデコード直前に復号する。
"""

import logging

import davey
from discord.ext.voice_recv.opus import PacketDecoder
from discord.ext.voice_recv.rtp import OPUS_SILENCE
from discord.opus import OpusError

log = logging.getLogger(__name__)

# RTPPacketは__slots__で属性を追加できないため、復号済みのパケットはデコーダー側に覚えておく。
# 二重に復号しうるのはFEC用に先読みした直後のパケットだけなので、直近1個を保持すれば足りる
_LAST_DECRYPTED_ATTR = "_dave_last_decrypted_packet"
_failure_count = 0


def _dave_decrypt(decoder: PacketDecoder, packet) -> bool:
    """packet.decrypted_data をDAVE復号して置き換える。復号不要なら何もしない。成否を返す。"""
    if not packet or packet is getattr(decoder, _LAST_DECRYPTED_ATTR, None):
        return True

    voice_client = decoder.sink.voice_client
    state = voice_client._connection
    session = state.dave_session
    if state.dave_protocol_version == 0 or session is None or packet.decrypted_data == OPUS_SILENCE:
        return True

    user_id = voice_client._get_id_from_ssrc(decoder.ssrc)
    if user_id is None:
        return False

    try:
        packet.decrypted_data = session.decrypt(user_id, davey.MediaType.audio, bytes(packet.decrypted_data))
    except Exception as e:
        if "UnencryptedWhenPassthroughDisabled" in str(e):
            # E2EEをかけずに送ってくるクライアントもある。通信経路の暗号は外れているので、そのままopusとして扱う
            setattr(decoder, _LAST_DECRYPTED_ATTR, packet)
            return True
        # 鍵交換 (epoch切替) 直後などは一時的に失敗しうる。そのフレームは欠損扱いにする
        global _failure_count
        _failure_count += 1
        if _failure_count % 250 == 1:
            log.warning("DAVE decrypt failed (%d回目): ssrc=%s user=%s: %s", _failure_count, decoder.ssrc, user_id, e)
        return False

    setattr(decoder, _LAST_DECRYPTED_ATTR, packet)
    return True


def install() -> None:
    original_decode_packet = PacketDecoder._decode_packet

    def _decode_packet(self: PacketDecoder, packet):
        try:
            ok = _dave_decrypt(self, packet)
            if not packet:
                # FakePacket時は次パケットでFEC復元されるため、そちらも先に復号しておく
                next_packet = self._buffer.peek_next()
                if next_packet is not None:
                    _dave_decrypt(self, next_packet)
            if ok:
                return original_decode_packet(self, packet)
        except OpusError as e:
            log.debug("Opus decode failed: ssrc=%s: %s", self.ssrc, e)
        # 復号・デコードできなかったフレームはパケットロス補間で埋め、ルーターを止めない
        return packet, self._decoder.decode(None, fec=False)

    PacketDecoder._decode_packet = _decode_packet
