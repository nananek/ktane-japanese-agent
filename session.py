import uuid
from dataclasses import dataclass, field


@dataclass
class BombSession:
    guild_id: int
    defuser_user_id: int | None = None
    history: list[dict] = field(default_factory=list)
    # LLM APIへ会話単位のIDとして渡す。!newbombでセッションごと作り直されるため爆弾1個につき1ID
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def add_user_message(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant_message(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})


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
