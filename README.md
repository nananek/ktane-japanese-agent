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

コードが前提にしている書き起こしの形式:

- `00_bomb_features.md` — 爆弾全体のルール。毎回system promptに含める。表紙の
  `バージョン: 1-ja` / `認証コード: 122` を読み取り、`/ktane-join`・`/ktane-newbomb` 時に読み上げる
- `<モジュールID>.md` — 各モジュールのマニュアル。LLMが `get_module_manual` ツールで必要な分だけ取得する
  (モジュールIDと見た目の特徴の一覧は `llm/manual.py`)
- 以下は `llm/solvers.py` が解析し、表の照合・経路探索・ステージをまたぐ記録をコード側で確定させる
  (LLMは入力の聞き取りと構造化だけを担当する)
  - `wires.md` (`solve_wires`) / `the-button.md` (`solve_button`) — 番号付きの優先順位ルール
    「(そうでない場合、)条件(かつ条件)の場合/ければ、操作。」を解析する。未判明の爆弾情報は取りうる値を
    総当たりし、答えが変わる情報だけを聞き返す
  - `keypads.md` (`solve_keypad`) — 列一覧を `- 列N (上から押す順): Ϙ(見た目, 28-balloon) → ...` の形式で書く
  - `memory.md` (`solve_memory`) — `## ステージN` ごとに `| ディスプレー | 操作 |` の表。操作は
    「N番目のボタンを押す」「「N」と書かれたボタンを押す」「ステージNで押したのと同じ位置/ラベルのボタンを押す」
  - `wire-sequences.md` (`solve_wire_sequence`) — `## 赤/青/黒いワイヤの出現回数` ごとに
    `| N番目 | AかC |` の表
  - `mazes.md` (`solve_maze`) — `### mazeN (丸印の位置: (行,列), ...)` に続くコードブロックの迷路図
    (`+--+` と `|` で壁を表す6×6マス、座標は0始まり)

ソルバーの答えが定型で言えるもの (キーパッド以外) は、LLMに文章化させる往復を省いてそのまま読み上げる
(LLM 1往復あたり1.5〜2.5秒かかるため)。

シリアルナンバー・バッテリー・インジケーター・ポート・ミス数は、LLMが `update_bomb_info` ツールで
爆弾ごとの状態 (`llm/bomb_state.py`) に記録し、毎回system promptの末尾に載せる。

### 2. 依存関係のインストール

音声の送受信にffmpegとlibopusが必要 (Debian系: `sudo apt install ffmpeg libopus0`)。

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

### 4. Discord botの準備

[Discord Developer Portal](https://discord.com/developers/applications) でアプリケーションを作成し、以下を設定する。

**Bot** タブ

- **Reset Token** でトークンを発行し、`.env` の `DISCORD_TOKEN` に設定する
- **Privileged Gateway Intents** (PRESENCE / SERVER MEMBERS / MESSAGE CONTENT) はすべて不要 (OFFのままでよい)。
  スラッシュコマンドだけで操作し、メッセージ本文やメンバー一覧は読まない

**OAuth2 → URL Generator** で招待URLを作り、サーバーに招待する

- SCOPES: `bot`、`applications.commands` (スラッシュコマンドの登録に必要)
- BOT PERMISSIONS:

| 権限 | 用途 |
|------|------|
| View Channels (チャンネルを見る) | コマンドを実行したテキストチャンネル・ボイスチャンネルを参照する |
| Send Messages (メッセージを送信) | 文字起こし (🎙️)・応答 (🤖)・ソルバーの結果 (🧮) をテキストチャンネルに記録する |
| Connect (接続) | ボイスチャンネルに参加する |
| Speak (発言) | 指示の読み上げ・効果音を再生する |
| Use Voice Activity (音声検出を使用) | プッシュ・トゥ・トークが必須のチャンネルでも常時送信できるようにする |

権限の整数値は `36703232`。以下の `<CLIENT_ID>` をアプリケーションIDに置き換えても招待できる:

```
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot+applications.commands&permissions=36703232
```

ボイスチャンネルやテキストチャンネル側で権限を個別に拒否していると、その操作だけ失敗する
(例: Send Messages がないとテキストへの記録は失敗するが、音声でのやり取りは続く)。

### 5. 環境変数の設定

```bash
cp .env.example .env
```

`.env` に以下を設定する:

- `DISCORD_TOKEN` — 手順4で発行したbotのトークン
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` — 利用するOpenAI互換APIのAPIキー、ベースURL、モデルID
- `LLM_API` (省略可) — `chat` (Chat Completions API、既定) か `responses` (Responses API)。
  モデルが対応しているエンドポイントに合わせる
- `LLM_REASONING_EFFORT` (省略可) — 思考型モデルの思考量 (`low` など)。曖昧な発話で長考して応答が
  遅れる場合に下げる
- `LLM_TIMEOUT` (省略可) — 応答待ちのタイムアウト秒数 (既定30)。超えると言い直しを促す定型文を読み上げる
- `LLM_HEADERS` (省略可) — 追加するHTTPヘッダーをJSONオブジェクトで指定。値中の `{session_id}` は
  爆弾セッション (`/ktane-newbomb` で更新) ごとのUUIDに置換される。
  例: `{"User-Agent": "ktane-japanese-agent/0.1", "x-session-id": "{session_id}"}`
- `VOICEVOX_SPEAKER_ID` — 使用する話者ID
- `VOICEVOX_SPEED_SCALE` (省略可) — 読み上げの話速。1.0が標準で、既定は1.3

### 6. 起動

```bash
python3 bot.py
```

#### Docker Compose で起動する場合

GPU (NVIDIA Container Toolkit) が使えるホストで、`.env` と `knowledge/` を用意してから起動する。
VOICEVOX ENGINE (CPU版) も同じcomposeで起動するため、手順2・3は不要。

```bash
docker compose up -d
```

- イメージは `main` へのpushとタグ (`v*`) のpushで GitHub Actions がビルドし、
  `ghcr.io/nananek/ktane-japanese-agent` に公開する (`.github/workflows/docker.yml`)。
  ローカルでビルドする場合は `docker compose build`
- `knowledge/` は非公開のため、イメージには含めず `/app/knowledge` に読み取り専用でマウントする
- Whisperのモデルは `whisper-models` ボリュームに保存され、再作成時にダウンロードし直さない
- 同じ `DISCORD_TOKEN` でbotを複数起動すると応答が重複するため、ホストで直接起動しているbotは止めておくこと

Discord上でスラッシュコマンドが使える (他のbotと衝突しないよう `ktane-` 接頭辞付き)。

- `/ktane-join` — 実行した人がいるボイスチャンネルに参加し、その人の発話だけを聞き取る
- `/ktane-newbomb` — 新しい爆弾用に会話と爆弾情報をリセットする。実行した人がbotと同じボイスチャンネルに
  いれば、聞き取り対象をその人に切り替える。解除中の誤操作を防ぐため、Defuserが「解除できました」
  「爆発しました」「時間切れです」と伝えてゲーム終了が記録される (`end_game` ツール) までは実行できない
  (`force: True` で強制実行)
- ゲーム終了が記録された後は `/ktane-newbomb` を実行するまで、受け付け音・文字起こし・応答をすべて止める
- `/ktane-leave` — ボイスチャンネルから退出する

起動時に参加中の各サーバーへコマンドを登録する (招待に必要なスコープと権限は手順4を参照)。

## 既知の要検証・要調整ポイント (Phase 1〜3で実測しながら詰める)

- `discord-ext-voice-recv` は非公式拡張のため、`AudioSink.write()` に渡される `VoiceData` の
  正確なフィールド仕様はインストールされたバージョンのソースで確認すること。
- PyPI版の `discord-ext-voice-recv` はDiscordの音声E2EE (DAVE) の復号に未対応のため、
  `voice/dave.py` でdiscord.py本体のDAVEセッションを使って受信音声を復号するパッチを当てている。
  拡張のバージョンを上げる場合は内部実装 (`PacketDecoder._decode_packet`) の変更に注意。
  接続直後などに復号できないフレームがあり、欠損として補間している。
- `voice/receiver.py` の48kHz→16kHzダウンサンプリングは単純間引きの簡易実装。
  文字起こし精度に問題が出る場合はリサンプリングライブラリ (例: `scipy.signal.resample_poly`) への
  差し替えを検討する。
- `voice/vad.py` のsilero-vadしきい値・無音判定時間 (`silence_ms`) は、実際の発話を試しながら
  チューニングが必要。
- Discordは話し終わると音声パケットの送信自体を止めるため、無音フレームが届かずVADだけでは発話終了を
  判定できない。`voice/receiver.py` で発話中に0.3秒パケットが途切れたら無音を補って発話を確定させている。
- 聞き取り対象は `/ktane-join` または `/ktane-newbomb` を実行した1人に限っている (`TranscribingSink` の `target_user_id`)。
  複数人のDefuserに対応する場合は対象ユーザーの絞り込みを見直す。
- 読み上げ中とその直後0.3秒は聞き取りを止めている (半二重)。スピーカーから回り込んだbot自身の声を
  発話として拾わないためで、AI発話中の割り込み (barge-in) はできない。
  読み上げの最後にターン交代のチャイム (`voice/chime.py`) を鳴らし、話し始めてよい合図にしている。
  Defuserの話し終わりを検出した時点でも短い受け付け音を鳴らす (こちらは鳴らしている間も聞き取りを止めない)。
  応答生成中に届いた発話はまとめて扱い、応答生成中に続きが届いた場合はまとめて作り直す。
- 思考型モデルは曖昧な発話で長考して応答が返らないことがある (glm-5.3で上限まで思考し続けた例あり)。
  `LLM_REASONING_EFFORT` と `LLM_TIMEOUT` で抑える。
