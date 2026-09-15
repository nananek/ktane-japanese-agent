"""Discordや音声に依存しない、Defuserの発言への応答の進め方。botとWeb UIで同じ判定・状態の扱いにするため共有する。"""

import copy
import logging
import re
from dataclasses import dataclass

from llm.bomb_state import BombState
from llm.llm_client import LLMClient, LLMReply
from llm.manual import find_manual_version
from llm.prompts import build_system_prompt
from session import BombSession

logger = logging.getLogger(__name__)

# 聞き返し。LLMに回すと「最初からやり直す」と解釈されることがあるため、直前の読み上げをそのまま繰り返す
REPEAT_REQUEST_RE = re.compile(
    r"(もう(一|1|いっ)(度|回|かい)|繰り返して|リピート|聞こえなかった|なんて(言った)?|何て(言った)?|なんだって|何だって)"
    r"(お願い(します)?|言って(ください)?|ください)?"
)
FILLER_WORDS = {"", "ん", "んー", "んん", "あ", "あー", "あっ", "え", "えー", "えっと", "えーと", "あの", "あのー", "うーん", "ふむ"}
LLM_FAILURE_REPLY = "すみません、応答が取れませんでした。もう一度言ってください。"


def build_opening_line() -> str:
    """開始時にマニュアルの版を伝え、Defuserがゲーム側の認証コードと一致しているか確認できるようにする。

    爆弾の情報はここでは聞かない (最初に全部確認すると時間を使い切るため、判定に要るときだけ聞く)。
    """
    manual_version, manual_code = find_manual_version()
    return (
        f"マニュアル、バージョン{manual_version or '不明'}、認証コード{manual_code}。" if manual_code else ""
    ) + "どのモジュールから？"


def is_repeat_request(text: str) -> bool:
    return REPEAT_REQUEST_RE.fullmatch(re.sub(r"[\s。、,.!?！？〜…ー]", "", text)) is not None


def is_filler(text: str) -> bool:
    """意味を持たない言いよどみだけの発話か。「はい」「うん」は質問への答えになりうるので含めない。"""
    normalized = re.sub(r"[\s。、,.!?！？〜…]", "", text)
    return normalized in FILLER_WORDS


@dataclass
class ReplyAttempt:
    user_message: dict
    # ツールが書き換えた状態のコピー。応答を採用したときだけセッションに反映する
    state: BombState
    # LLMの応答取得に失敗した (タイムアウト等) 場合は None
    result: LLMReply | None


def attempt_reply(llm: LLMClient, session: BombSession, text: str) -> ReplyAttempt:
    """発言への応答を生成する (同期呼び出し)。セッションはまだ変更せず、採用するなら commit_reply に渡す。

    作り直しで捨てた応答のソルバー記録が残ると、記憶や順番ワイヤの数え方が狂うため、
    ツールが書き換える状態はコピーに対して試す。
    """
    user_message = {"role": "user", "content": text}
    state = copy.deepcopy(session.state)
    state.age_pending_solver()
    try:
        result = llm.reply(build_system_prompt(state), [*session.history, user_message], session.session_id, state)
    except Exception:
        # タイムアウト等。無言で止まるとDefuserが待ち続けてしまうので、呼び出し側で言い直しを促す
        logger.exception("LLMの応答取得に失敗しました")
        result = None
    return ReplyAttempt(user_message, state, result)


def commit_reply(session: BombSession, attempt: ReplyAttempt) -> str:
    """応答を会話履歴と爆弾の状態に反映し、Defuserに伝える文を返す。"""
    session.history.append(attempt.user_message)
    if attempt.result is None:
        return LLM_FAILURE_REPLY
    session.history.extend(attempt.result.messages)
    session.state = attempt.state
    return attempt.result.text
