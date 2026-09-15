import uuid
from dataclasses import dataclass, field

from llm.bomb_state import BombState


@dataclass
class BombSession:
    guild_id: int
    defuser_user_id: int | None = None
    history: list[dict] = field(default_factory=list)
    state: BombState = field(default_factory=BombState)
    # LLM APIへ会話単位のIDとして渡す。!newbombでセッションごと作り直されるため爆弾1個につき1ID
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def add_user_message(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant_message(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    @property
    def is_in_game(self) -> bool:
        """ゲーム中か。開始の読み上げしかしていない (まだ誰も話していない) 場合はゲーム前とみなす。"""
        return self.state.game_result is None and any(m.get("role") == "user" for m in self.history)

    @property
    def is_game_over(self) -> bool:
        return self.state.game_result is not None


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[int, BombSession] = {}

    def get_or_create(self, guild_id: int) -> BombSession:
        if guild_id not in self._sessions:
            self._sessions[guild_id] = BombSession(guild_id=guild_id)
        return self._sessions[guild_id]

    def reset(self, guild_id: int) -> BombSession:
        self._sessions[guild_id] = BombSession(guild_id=guild_id)
        return self._sessions[guild_id]
