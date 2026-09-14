# keep-talking-agent

Keep Talking and Nobody Explodes (KTANE) のExpert役 (マニュアル読み役) をAIに代行させる
Discordボイスチャンネルbot。

- ローカルWhisper (faster-whisper) でDefuserの発話をリアルタイム文字起こし
- OpenAI互換APIのLLMで bombmanual.com/ja/ の標準モジュールマニュアルに基づく
  解除指示を生成
- VOICEVOXで指示を音声合成し、ボイスチャンネルで読み上げ

## セットアップ

### 1. マニュアルの配置 (Phase 0) — 完了済み

`knowledge/modules/*.md` に bombmanual.com/ja/ の標準モジュール15種 (無印11 + Needy 3 +
爆弾全般ルール) を書き起こし済み。このディレクトリは著作権配慮のため `.gitignore` 済みで
コミットされないため、リポジトリを別環境にcloneした場合は再度書き起こしが必要。

**要検証**: `complicated-wires.md` の判定表 (16パターン) は、原文中の表形式テキストではなく
インラインSVGのベン図を座標から幾何学的に解析して機械的に導出したもの。数学的な確度は高いが、
**実際にゲームをプレイして既知のパターンと突き合わせてから信頼すること**。
`morse-code.md` のアルファベット⇔符号対応表は、原文が画像のみだったため公知の国際モールス符号で
補完している (bombmanual.com独自コンテンツではないため著作権上の問題はない)。

### 2. 依存関係のインストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

GPU (CUDA) でfaster-whisperを動かす場合、環境のCUDA/cuDNNバージョンに合ったPyTorch/CTranslate2が
必要になることがある。詳細は faster-whisper の公式READMEを参照。

### 3. VOICEVOX ENGINEの起動

別プロセスとしてVOICEVOX ENGINEをローカルで起動しておく (Docker推奨)。
起動後、`http://127.0.0.1:50021` で待ち受けていることを確認する。

### 4. 環境変数の設定

```bash
cp .env.example .env
```

`.env` に以下を設定する:

- `DISCORD_TOKEN` — Discord Developer PortalでBotを作成し取得
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` — 利用するOpenAI互換APIのAPIキー、ベースURL、モデルID
- `LLM_HEADERS` (省略可) — 追加するHTTPヘッダーをJSONオブジェクトで指定。値中の `{session_id}` は
  爆弾セッション (`!newbomb` で更新) ごとのUUIDに置換される。
  例: `{"User-Agent": "ktane-japanese-agent/0.1", "x-session-id": "{session_id}"}`
- `VOICEVOX_SPEAKER_ID` — 使用する話者ID

### 5. 起動

```bash
python3 bot.py
```

Discord上で `!join` (ボイスチャンネル参加+聞き取り開始)、`!newbomb` (セッションリセット)、
`!leave` (退出) が使える。

## 既知の要検証・要調整ポイント (Phase 1〜3で実測しながら詰める)

- `discord-ext-voice-recv` は非公式拡張のため、`AudioSink.write()` に渡される `VoiceData` の
  正確なフィールド仕様はインストールされたバージョンのソースで確認すること。
- `voice/receiver.py` の48kHz→16kHzダウンサンプリングは単純間引きの簡易実装。
  文字起こし精度に問題が出る場合はリサンプリングライブラリ (例: `scipy.signal.resample_poly`) への
  差し替えを検討する。
- `voice/vad.py` のsilero-vadしきい値・無音判定時間 (`silence_ms`) は、実際の発話を試しながら
  チューニングが必要。
- `bot.py` の `!join` は発話者をコマンド実行者1人に固定している (`target_user_id`)。
  複数人のDefuserに対応する場合は `TranscribingSink` の対象ユーザー絞り込みを見直す。
- AI発話中にDefuserが話しかけてきた場合の割り込み (barge-in) は未対応。現状は
  「AIが喋り終わるまで新しい発話は個別に処理される」シンプルな挙動。
- マニュアル全文を毎リクエスト system prompt に含めるため、利用するAPIの料金・レート制限に対する
  消費量を運用しながら確認する。
