"""爆弾1個分の状態。会話履歴に埋もれると見落としや聞き直しが起きるため、判明した事実をここに集約する。"""

from dataclasses import dataclass, field

INDICATORS = ("SND", "CLR", "CAR", "IND", "FRQ", "SIG", "NSA", "MSA", "TRN", "BOB", "FRK")
PORTS = ("DVI-D", "パラレル", "PS/2", "RJ-45", "シリアル", "ステレオRCA")
VOWELS = set("AEIOU")


@dataclass
class BombState:
    serial_number: str | None = None
    # シリアル全体は聞かず「末尾は奇数」とだけ答えることもあるため、末尾の偶奇だけでも持てるようにする
    serial_last_digit_odd: bool | None = None
    batteries: int | None = None
    lit_indicators: set[str] | None = None
    unlit_indicators: set[str] | None = None
    ports: set[str] | None = None
    strikes: int | None = None
    # 記憶モジュール: ステージごとに押したボタンの (位置1〜4, ラベル)
    memory_presses: list[tuple[int, int]] = field(default_factory=list)
    # 順番ワイヤ: パネル番号 → [(色, 接続先)]。出現回数はパネルをまたいで累積するため全パネル分を保持する
    wire_sequence_panels: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    def summary(self) -> str:
        lines = []
        if self.serial_number is not None:
            serial = self.serial_number
            digits = [c for c in serial if c.isdigit()]
            last_digit = f"末尾の数字{digits[-1]}は{'偶数' if int(digits[-1]) % 2 == 0 else '奇数'}" if digits else "数字なし"
            vowel = "母音を含む" if VOWELS & set(serial.upper()) else "母音を含まない"
            lines.append(f"- シリアルナンバー: {serial} ({last_digit}、{vowel})")
        elif self.serial_last_digit_odd is not None:
            lines.append(f"- シリアルナンバーの最後の数字: {'奇数' if self.serial_last_digit_odd else '偶数'}")
        if self.batteries is not None:
            lines.append(f"- バッテリー: {self.batteries}本")
        if self.lit_indicators is not None:
            lines.append(f"- 点灯インジケーター: {'、'.join(sorted(self.lit_indicators)) or 'なし'}")
        if self.unlit_indicators is not None:
            lines.append(f"- 消灯インジケーター: {'、'.join(sorted(self.unlit_indicators)) or 'なし'}")
        if self.ports is not None:
            lines.append(f"- ポート: {'、'.join(sorted(self.ports)) or 'なし'}")
        if self.strikes is not None:
            lines.append(f"- ミス: {self.strikes}回")
        if self.memory_presses:
            stages = "、".join(
                f"ステージ{i}: {pos}番目(ラベル{label})" for i, (pos, label) in enumerate(self.memory_presses, 1)
            )
            lines.append(f"- 記憶モジュールで押したボタン: {stages}")
        if self.wire_sequence_panels:
            panels = "、".join(f"パネル{n}" for n in sorted(self.wire_sequence_panels))
            lines.append(f"- 順番ワイヤで入力済み: {panels}")
        return "\n".join(lines) if lines else "(まだ何も判明していない)"
