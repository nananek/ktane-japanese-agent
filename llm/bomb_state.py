"""爆弾1個分の状態。会話履歴に埋もれると見落としや聞き直しが起きるため、判明した事実をここに集約する。"""

from dataclasses import dataclass, field

INDICATORS = ("SND", "CLR", "CAR", "IND", "FRQ", "SIG", "NSA", "MSA", "TRN", "BOB", "FRK")
PORTS = ("DVI-D", "パラレル", "PS/2", "RJ-45", "シリアル", "ステレオRCA")
VOWELS = set("AEIOU")


@dataclass
class PendingSolver:
    """爆弾の情報が足りず判定を保留したソルバーの呼び出し。聞き返した情報が記録されたら同じ引数で判定し直す。"""

    name: str
    arguments: str
    # 判定に要る情報の名前 (solvers の UNKNOWN_VARIABLE_LABELS のキー)
    missing: list[str]
    # 保留した直後の発言でだけ判定し直す。それより後は別のモジュールの話に移ったとみなして捨てる
    fresh: bool = True


@dataclass
class BombState:
    serial_number: str | None = None
    # シリアル全体は聞かず「末尾は奇数」とだけ答えることもあるため、末尾の偶奇だけでも持てるようにする
    serial_last_digit_odd: bool | None = None
    batteries: int | None = None
    # 点灯インジケーターを全部答えてもらったときだけ設定する (None なら未判明)
    lit_indicators: set[str] | None = None
    # 「FRKは点灯していない」のように個別に点灯していないと分かったもの。全体が判明したとは扱わない
    not_lit_indicators: set[str] = field(default_factory=set)
    unlit_indicators: set[str] | None = None
    ports: set[str] | None = None
    strikes: int | None = None
    # ゲームの結果 (解除/爆発/時間切れ)。記録されるまではゲーム中とみなし、新しい爆弾への切り替えを防ぐ
    game_result: str | None = None
    # 記憶モジュール: ステージごとに押したボタンの (位置1〜4, ラベル)
    memory_presses: list[tuple[int, int]] = field(default_factory=list)
    # 順番ワイヤ: パネル番号 → [(色, 接続先)]。出現回数はパネルをまたいで累積するため全パネル分を保持する
    wire_sequence_panels: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    # 複雑ワイヤ: Defuserは色・LED・★を何回にも分けて言い、LLMに覚えさせると最初から聞き直してしまうため、ここに積み上げる
    complicated_wire_count: int | None = None
    # 左からの位置 → 判明した項目 ({"colors": [...], "led": bool, "star": bool} の一部)
    complicated_wires: dict[int, dict] = field(default_factory=dict)
    # 「LEDは右の2つが点灯」のようにまとめて言われた、LED点灯・★ありの位置 (ほかは消灯・なし)。個別の記録より優先度は低い
    complicated_marks: dict[str, set[int]] = field(default_factory=dict)
    pending_solver: PendingSolver | None = None

    def complicated_wire(self, position: int) -> dict:
        """複雑ワイヤの位置ごとの判明した項目。まとめて言われたLED・★の位置も反映する。"""
        wire = dict(self.complicated_wires.get(position, {}))
        for key, positions in self.complicated_marks.items():
            wire.setdefault(key, position in positions)
        return wire

    def age_pending_solver(self) -> None:
        """Defuserの発言1つごとに呼ぶ。保留した次の発言を過ぎたソルバーは判定し直さない。"""
        if self.pending_solver is None:
            return
        if self.pending_solver.fresh:
            self.pending_solver.fresh = False
        else:
            self.pending_solver = None

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
        if self.lit_indicators is None and self.not_lit_indicators:
            lines.append(f"- 点灯していないと分かったインジケーター: {'、'.join(sorted(self.not_lit_indicators))}")
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
        if self.complicated_wire_count is not None:
            color_names = {"red": "赤", "blue": "青", "white": "白"}
            wires = []
            for position in range(1, self.complicated_wire_count + 1):
                wire = self.complicated_wire(position)
                color = "と".join(color_names.get(c, c) for c in wire["colors"]) if "colors" in wire else "色?"
                led = {True: "LED点灯", False: "LED消灯"}.get(wire.get("led"), "LED?")
                star = {True: "★あり", False: "★なし"}.get(wire.get("star"), "★?")
                wires.append(f"{position}本目 {color}/{led}/{star}")
            lines.append(f"- 複雑ワイヤ ({self.complicated_wire_count}本): {'、'.join(wires)}")
        if self.wire_sequence_panels:
            panels = "、".join(f"パネル{n}" for n in sorted(self.wire_sequence_panels))
            lines.append(f"- 順番ワイヤで入力済み: {panels}")
        if self.game_result is not None:
            lines.append(f"- ゲーム終了: {self.game_result}")
        return "\n".join(lines) if lines else "(まだ何も判明していない)"
