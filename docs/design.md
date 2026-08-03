---
created: 2026-08-03
status: approved
---

# vidprep 設計書

[design-input.md](design-input.md)（実現可能性調査の結論）を前提とし、そこで未決だった事項の決定と、実装に入れる粒度の仕様を定める。検証・効果測定は独立した一級文書 [verification-plan.md](verification-plan.md) に定義し、本書と対で読む。

## 1. 確定した設計判断

design-input.md の未決事項 7 件と、本設計セッションで追加した判断。

| # | 論点 | 決定 | 理由 |
|---|---|---|---|
| 1 | ASR モデル | **実測ベンチで決定**（Step 1）。候補: whisper.cpp large-v3 / large-v3-turbo、mlx-whisper large-v3-turbo、kotoba-whisper v2.0 | 公開ベンチが相互矛盾。ベンチ手順は verification-plan.md §12 に定義。決定後この表を更新する |
| 2 | smart cut | **v1 は全再エンコード**（CRF 18 / preset slow）。`Renderer` プロトコルで差し替え可能にする | 劣化は 1 世代のみで Filmora 取り込みでは再劣化しない（実機確認済み）。検証体系を先に固める |
| 3 | フィラー検出 | 辞書 2 段階（strong / weak）+ セグメント境界依存の候補化。§5.4 参照 | 文中フィラーの切り出しは単語タイムスタンプに依存し日本語で非信頼のため v1 で扱わない |
| 4 | 誤変換辞書 | **スキーマ互換の独立辞書**。iobsidian `fix-transcriptions` と同一スキーマを `yomi` フィールドで拡張し、初期エントリは同辞書からコピー | 用途が違う（iobsidian は LLM の文脈参照、vidprep は読みベース決定的置換）。リポジトリ間結合を避ける |
| 5 | CLI フレームワーク | **typer** | 型ヒントから CLI 生成、mypy strict 構成と親和 |
| 6 | SRT 写像の端数処理 | 分割せず**クリップ**方式。§4 に仕様化 | 表示中の 1 文を分割すると読み時間が壊れる |
| 7 | プロジェクト構造 | **1 動画 = 1 作業ディレクトリ、場所は任意**。`vidprep init` で作成、各コマンドは cwd または `--project` で指定 | ツールが置き場所を強制しない。§3.1 参照 |
| 8 | カット候補の初期ステータス | 無音 = `approved`、フィラー = `proposed` | 無音検出は誤爆が少ない。レビューの注意をフィラーに集中させる |
| 9 | 境界のぶつ切り対策 | クロスフェード（重なり）ではなく**境界フェード in/out（既定 10ms、尺不変）** | acrossfade は境界ごとに尺が縮み、原尺→カット後の写像関数が壊れる。クリック防止目的はフェードで足りる。design-input の「マイクロクロスフェード 20〜50ms」からの変更点 |
| 10 | スキーマ実装 | **pydantic v2** | 中間 JSON が設計の核でありバリデーションが本質的。mypy strict とも親和 |
| 11 | 検証素材 | `fixtures/raw/VID_20260507_144024.mp4`（ゴールデンサンプル、git 管理外） | verification-plan.md §2 参照 |

## 2. アーキテクチャ

### 2.1 パイプライン

```
原尺素材 (mp4)  ※プロジェクトからは絶対パス + sha256 で参照
 │
 ├─ [audio-fix]   denoise → highpass → 2パス loudnorm → audio/processed.wav
 │                 ↓ 以降の ASR・render はすべて処理済み音声を使う
 ├─ [transcribe]  Silero VAD → ASR → transcript.json（原尺タイムスタンプ）
 │                 └─ [correct] 辞書置換 →（スキル経由 LLM 校正 → 機械検証つき適用）
 ├─ [detect]      auto-editor（無音）+ transcript ベースのフィラー検出 → cuts.json
 │                 └─ ★レビューゲート: report で境界ダイジェスト・波形を確認し
 │                    人間 / Claude Code が cuts.json の status を編集
 └─ [render]      approved のカットのみ適用
      ├─ out/output.mp4      全再エンコード + 境界フェード
      ├─ out/subtitles.srt   カット後タイムラインへ写像
      ├─ out/preview.mp4     ASS テロップ焼き込み（--preview 指定時）
      └─ report/             統計 JSON・境界波形 PNG・境界ダイジェスト動画
```

処理順序の不変条件: **ASR は常に原尺（の処理済み音声）に対して行う**。カット後の再 ASR は検証（再文字起こし照合）のためだけに行う。

### 2.2 モジュール構成

```
src/vidprep/
├── __init__.py      # 公開 API（当面は CLI のみが利用者。__all__ は最小）
├── cli.py           # typer アプリ。各サブコマンド定義（薄く保つ）
├── project.py       # プロジェクト（作業ディレクトリ）の init / load / ステージ記録
├── models.py        # pydantic スキーマ: Manifest, Transcript, Cuts, Telops, Styles, Profile
├── timeline.py      # ★核心: カット区間の正規化と写像関数（原尺 → カット後）
├── audio.py         # audio-fix 実装
├── transcribe.py    # VAD + ASR バックエンド呼び出し
├── correct.py       # 辞書置換 + LLM パッチの機械検証つき適用
├── detect.py        # auto-editor 変換層 + フィラー検出 + cuts.json マージ
├── render.py        # Renderer プロトコル + ReencodeRenderer + SRT/ASS 出力
├── report.py        # 統計・波形 PNG・境界ダイジェスト
├── doctor.py        # 外部依存の検査
└── _ffmpeg.py       # ffmpeg / ffprobe サブプロセスの共通ラッパ
```

設計原則（design-input の 3 原則に加えて）:

- 各サブコマンドは「JSON を読んで JSON（+成果物）を書く」。モジュール間の受け渡しはファイル経由に統一し、オンメモリの密結合を作らない
- 信号処理ループを Python で書かない。ffmpeg / auto-editor / whisper.cpp に丸投げする
- `timeline.py` の写像関数は render（動画）と SRT/ASS 出力で**同一実装を共有**する

### 2.3 依存

| 種別 | もの | 用途 |
|---|---|---|
| Python（本体） | typer, pydantic, pysubs2, budoux, sudachipy | CLI / スキーマ / 字幕生成 / 行分割 / 読み正規化 |
| Python（dev） | jiwer | CER 計測（検証用） |
| 外部バイナリ | ffmpeg（libass 入り。`ffmpeg-full` 等）, ffprobe, auto-editor, whisper.cpp（または mlx-whisper）, DeepFilterNet（任意） | doctor が検査する |

## 3. プロジェクトとデータ設計

### 3.1 作業ディレクトリ

`vidprep init <dir> --source <mp4>` で作成。素材はコピーせず絶対パス + sha256 で参照する（`--copy-source` で取り込みも可）。

```
<project>/
├── vidprep.json         # マニフェスト（§3.2）
├── profile.json         # 処理パラメータ（テンプレートからコピーされ、動画ごとに調整可）
├── audio/processed.wav  # audio-fix の出力（PCM。ASR と render が使う）
├── transcript.json
├── cuts.json
├── telops.json          # 任意
├── out/                 # output.mp4 / subtitles.srt / preview.mp4
└── report/              # stats.json / boundaries/*.png / boundary_digest.mp4
```

各コマンドは cwd をプロジェクトとみなし、`--project/-p <dir>` で明示指定できる。

### 3.2 マニフェスト（vidprep.json）

```json
{
  "version": "1",
  "created_at": "2026-08-03T16:00:00+09:00",
  "source": {
    "path": "/Users/masuyama/Movies/.../VID_20260507_144024.mp4",
    "sha256": "76d8ddd3...",
    "duration": 298.92,
    "video": {"codec": "h264", "width": 1920, "height": 1080, "fps": "25/1"},
    "audio": {"codec": "aac", "sample_rate": 44100, "channels": 2}
  },
  "stages": {
    "audio_fix": {"done_at": "...", "params_sha256": "...", "tool_versions": {"ffmpeg": "7.x"}}
  }
}
```

- `stages` は各コマンドが完了時に記録する（入力パラメータのハッシュとツールバージョン）。下流コマンドは上流の記録と現在の profile を突き合わせ、**古い成果物の上で動くときは警告する**（ブロックはしない）
- source の sha256 は各コマンド開始時に検証する（素材差し替え事故の防止）

### 3.3 transcript.json

```json
{
  "version": "1",
  "audio_source": "audio/processed.wav",
  "asr": {"backend": "whisper.cpp", "model": "large-v3-turbo", "vad": "silero-v5"},
  "segments": [
    {
      "id": "s0001",
      "start": 1.234,
      "end": 4.567,
      "text": "こんにちは、とまだです。",
      "source": "asr",
      "edits": []
    }
  ]
}
```

- `id` は `s0001` 形式の連番。**一度振った id は不変**（correct はテキストのみ変更、削除・並べ替えをしない）
- `source` は `asr | dict | llm`（最後にテキストを変更した主体）
- `edits[]` は `{"at": ISO8601, "tool": "dict|llm|manual", "before": "旧テキスト"}` の履歴。冪等性の検証（同じ入力に再適用して変化しないこと）に使う
- `words` は v1 では持たない（日本語の単語タイムスタンプ非信頼の決定による）。スキーマ上は将来の追加を許す

### 3.4 cuts.json

```json
{
  "version": "1",
  "cuts": [
    {"id": "c0001", "start": 10.500, "end": 13.240, "reason": "silence",
     "confidence": 0.95, "status": "approved", "note": null},
    {"id": "c0002", "start": 45.100, "end": 45.900, "reason": "filler",
     "confidence": 0.7, "status": "proposed", "note": "「えーと」+前後無音"}
  ]
}
```

- `reason`: `silence | filler | manual`。`status`: `proposed | approved | rejected`
- **render が適用するのは `approved` のみ**
- 不変条件（models.py が強制）: 区間は `0 <= start < end <= duration`、**approved 同士は重ならない**（proposed との重なりは許す）

**detect 再実行時のマージ規則**（レビュー結果を消さないための核心仕様）:

1. 新検出区間と既存カットを `reason` が同じで区間の IoU ≥ 0.5 のものどうしで対応付ける
2. 対応が付いた場合: 既存の `id` / `status` / `note` を保持し、区間と confidence は新検出値で更新する
3. 対応が付かない既存カット: `manual` と `rejected` と `approved` は無条件で保持、`proposed` は削除
4. 新規候補には新しい id を採番する。**id は再利用しない**（過去の最大値 + 1）

### 3.5 telops.json / styles.json

```json
// telops.json — Claude Code スキルまたは人間が書き、render --preview が読む
{"version": "1", "telops": [
  {"segment_id": "s0012", "text": "ここが重要", "style_preset": "emphasis",
   "start": null, "duration": null}
]}
// styles.json — ASS スタイルプリセット。リポジトリ同梱の既定 + プロジェクトで上書き
{"version": "1", "presets": {"emphasis": {"fontname": "Hiragino Sans W6",
  "fontsize": 64, "alignment": 8, "primary_colour": "&H00FFFFFF", "...": "..."}}}
```

- テロップの時刻は原則 `segment_id` 参照（そのセグメントの表示期間に追従）。`start`（原尺秒）+ `duration` の直指定も許す
- macOS の libass は CoreText 経由で `Bold: 1` が効かない事例があるため、プリセットは**ウェイト別ファミリー名**（例: `Hiragino Sans W6`）で指定する

### 3.6 profile.json（既定値つき）

```json
{
  "version": "1",
  "audio": {"denoise": "deepfilternet", "highpass_hz": 80,
            "loudnorm": {"i": -14.0, "tp": -1.0, "lra": 11.0}},
  "silence": {"threshold": "4%", "min_duration": 0.6,
              "pad_pre": 0.3, "pad_post": 0.3, "min_cut_duration": 0.4},
  "filler": {"enable_weak": false, "require_adjacent_silence": 0.2},
  "render": {"crf": 18, "preset": "slow", "boundary_fade": 0.010},
  "subtitle": {"max_chars_per_line": 20, "max_lines": 2,
               "min_display": 0.8, "max_cps": 8.0}
}
```

- `pad_pre/pad_post` は「発話側に残す余白」。カット区間を両端からこの分だけ縮める。保守的（長め）から始め、ゴールデンサンプルでの試聴で詰める（verification-plan.md §7）
- `subtitle` の既定は YouTube 想定。Netflix 準拠（13 全角/行・4 文字/秒）はプロファイルの値変更で選べる
- 時刻・秒値はすべて **float 秒・小数 3 桁（ms）丸め**で統一

### 3.7 誤変換辞書（dictionaries/asr-dict.json）

iobsidian の `misconversion-dict.json` スキーマに `yomi` を追加した拡張。リポジトリに同梱し git 管理する。

```json
{"version": "1.0.0", "entries": [
  {"correct": "Claude Code", "misrecognized": ["クロードコード", "クラウドコード"],
   "yomi": "クロードコード", "confidence": "always"},
  {"correct": "vidprep", "misrecognized": ["ビッドプレップ"], "yomi": "ビッドプレップ",
   "confidence": "always"}
]}
```

- 置換は 2 段: ①`misrecognized` の表層一致（決定的）②`yomi` と SudachiPy 読みの一致による検出（誤認識バリエーションの取りこぼし対策）。②は `confidence: always` のエントリのみ自動置換し、`context` は LLM 校正に委ねる
- 初期エントリは iobsidian 辞書から流用コピーする（YouTube で話す技術用語と重なりが大きい）。以後は独立に育てる

## 4. タイムライン写像仕様（timeline.py）

**入力**: approved カット区間の集合。**前処理（正規化）**: start 昇順に整列し、隣接・重複区間を結合して互いに素な区間列 `C = [(a1,b1), ..., (an,bn)]` を得る。

**写像関数** `f: 原尺秒 → カット後秒`:

```
removed(t) = Σ |(ai,bi) ∩ [0,t)|          # t までに削除された総尺
f(t) = t - removed(t)                       # カット内の t は f(bi) に写る（連続）
```

境界フェードは重なりを持たない（§1 判断 9）ため、**写像は区間ごとの平行移動のみ**で表せる。逆写像 `f⁻¹`（カット後 → 原尺）も同じ区間表で実装し、report の境界表示と再文字起こし照合（verification-plan.md §8.1）が使う。

**字幕セグメントの写像規則**（未決 6 の仕様化）:

| ケース | 扱い |
|---|---|
| セグメントがカットに完全に含まれる | SRT から除外し、report に警告として記録（「発話を消すカット」の兆候） |
| セグメントの端がカットと重なる | 重なった端をカット境界まで**クリップ**してから写像 |
| セグメントの内部にカットが完全に含まれる | **分割しない**。1 エントリのまま写像し、表示時間は自然に短縮される |
| 写像後の表示時間 < `min_display`（既定 0.8s） | 出力はするが report に警告（自動削除はしない） |
| 写像後に前後エントリと時刻が接触 | end を次エントリ start まで切り詰め（ms 精度、単調増加を保証） |

丸めは ms 単位・最終出力時のみ（中間計算は float のまま）。この規則により「フィラー 1 語をセグメント中央から消した」場合も字幕テキストは全文のまま短い表示になる — 読み速度警告（`max_cps` 超過）が report に出るので、そこで人間が判断する。

## 5. 各処理段の仕様

### 5.1 audio-fix

チェーン: `denoise（DeepFilterNet、無ければ afftdn にフォールバック）→ highpass 80Hz → loudnorm 2 パス（linear モード）`。出力は `audio/processed.wav`（PCM 16bit、ソースのサンプルレート維持）。

- loudnorm は 1 パス目で measured 値を取得し、2 パス目に `measured_*` を渡す linear モードで実行する（dynamic モードのポンピング回避）
- 尺を変えてはならない（完了条件: 尺差 ≤ 1ms。verification-plan.md §4）
- `--stats` で処理前後の LUFS / TP / LRA / 無音区間 RMS を JSON 出力

### 5.2 transcribe

1. Silero VAD で発話区間を検出（ハルシネーション対策として**必須**、スキップ不可）
2. 発話区間ごとに ASR を実行し、タイムスタンプを原尺に補正して結合
3. VAD 区間情報は `report/vad.json` に保存（detect のフィラー判定と検証が使う）

バックエンドは `whisper.cpp`（subprocess）と `mlx-whisper` の 2 実装を持ち、profile で選ぶ。モデルは Step 1 のベンチで確定。

### 5.3 correct

- `vidprep correct` : 辞書置換（決定的・冪等）。§3.7 の 2 段方式
- `vidprep correct --apply-patch <patch.json>` : LLM 校正パッチの適用。パッチは `{"edits": [{"id": "s0001", "text": "新テキスト"}]}` 形式で、適用前に機械検証する:
  - 存在する id のみか / id の重複がないか
  - **タイムスタンプ・セグメント数・順序を変更していないか**（パッチ形式上そもそも書けないが、適用後の不変条件としても検証）
  - 変更セグメント数と diff サマリを表示し、`--yes` がなければ確認を求める

LLM 校正そのもの（プロンプト・文脈判断）は Claude Code スキルの仕事で、CLI は検証つき適用だけを担う。

### 5.4 detect

- 無音: `auto-editor --export v3` の JSON タイムラインを keep/cut リストへ変換（変換層は auto-editor のバージョンを記録し、v3 スキーマ変化を検知したらエラーにする）。パディング適用後 `min_cut_duration` 未満の区間は捨てる
- フィラー: transcript.json のセグメントに対し辞書照合で検出する。**候補化するのは次のどちらかのみ**:
  - (a) セグメント全体がフィラー語のみ（例: 「えーと」だけのセグメント）→ セグメント区間 + 隣接無音を一体のカット候補にする
  - (b) セグメントの先頭/末尾がフィラー語で、`require_adjacent_silence` 秒以上の無音に隣接 → VAD 境界を使ってフィラー部分を切り出す
  - 文中フィラーは検出のみ（`note` に記録、カット候補にしない）
- フィラー辞書（profile とは別にリポジトリ同梱、プロジェクトで追記可）:
  - strong（既定で候補化）: えー、えーと、えっと、あのー、そのー、うーん
  - weak（`enable_weak: true` のときのみ候補化）: まあ、なんか、こう
- 出力は §3.4 のマージ規則で既存 cuts.json に統合する

### 5.5 render

```python
class Renderer(Protocol):
    def render(self, source: Path, keep: list[Interval],
               audio: Path, profile: Profile, out: Path) -> RenderResult: ...
```

v1 実装は `ReencodeRenderer`: keep 区間を `trim` + `concat` フィルタで連結し、映像 CRF 18 / preset slow / 元解像度・fps 維持、音声は processed.wav の対応区間 + 境界フェード（`afade` 10ms、尺不変）で AAC 320kbps に再エンコード。smart cut は将来 `SmartCutRenderer` として同一プロトコルで差し替える。

- `subtitles.srt`: §4 の写像で生成（BudouX + `max_chars_per_line` で行分割した版。`--no-wrap` で改行なし版も出せる）
- `--preview`: telops.json + styles.json から ASS を組み、libass 焼き込みの preview.mp4 を出す
- render は開始前に cuts.json の不変条件と、transcript / cuts の元になった素材ハッシュの一致を検証する
- `--verify-asr`: レンダリング後に出力を再 ASR し、カット境界での語の欠落を照合する（仕様は verification-plan.md §8.1）

### 5.6 report

レビューゲートと検証の道具。`vidprep report` で以下を再生成する:

- `report/stats.json`: 原尺 / カット後尺 / 削減率 / reason 別カット数と秒数 / LUFS 前後 / 字幕警告一覧（写像時の除外・min_display 未満・max_cps 超過）
- `report/boundaries/*.png`: 各カット境界前後 ±2s の波形 PNG（`showwavespic`）
- `report/boundary_digest.mp4`: **全カット境界の前後 ±2s だけを連結した確認用動画**（境界位置に無音の 0.5s 黒フレームを挟む）。カットが 30 箇所あっても数分で全境界を試聴でき、レビューゲートの主力になる
- `--cuts`: カット候補ごとに「削除される transcript テキスト + 前後の文脈」を表示（人間 / スキルが status を判断する材料）

### 5.7 doctor

検査対象: ffmpeg（`subtitles` フィルタ = libass の有無も確認）、ffprobe、auto-editor、ASR バックエンド（whisper.cpp バイナリ + モデルファイル / mlx-whisper import）、DeepFilterNet（任意扱い）、SudachiPy 辞書。結果を JSON 出力し、必須が欠けていれば exit 3。

## 6. CLI 仕様

サブコマンド: `init | doctor | audio-fix | transcribe | correct | detect | render | report`

共通仕様:

- `--project/-p <dir>`（既定 cwd）、`--json`（結果 JSON を stdout、人間向けログは stderr）、`--dry-run`（実行計画の表示のみ。外部コマンド列を含む）
- exit code: `0` 成功 / `1` 使用法・環境エラー / `2` 処理実行の失敗 / `3` 検証 NG（スキーマ不正、ハッシュ不一致、doctor の必須欠如など）
- 破壊的でない: すべての出力は上書き前に生成し、成功時にアトミックに置き換える。ソース素材には一切書き込まない

## 7. Claude Code 連携（.claude/skills/）

CLI 本体は AI 非依存。スキルは中間 JSON の読み書きと CLI 呼び出しだけを行う。v1 で用意するスキルは 3 つ（実装はスキル作成時に詰める。ここでは契約のみ定義）:

| スキル | 読む | 書く | 契約 |
|---|---|---|---|
| correct-transcript | transcript.json, dictionaries/ | patch.json | パッチ形式（§5.3）のみ。適用は必ず `vidprep correct --apply-patch` を通す |
| review-cuts | cuts.json, `report --cuts` 出力, transcript.json | cuts.json（status のみ） | 区間・id の変更禁止。判断根拠を `note` に書く |
| place-telops | transcript.json, styles.json | telops.json | 検証は `render --preview` のスキーマ検証に委ねる |

## 8. 拡張ポイント（v1 では作らないが壊さない）

- **smart cut**: `Renderer` プロトコル差し替え（§5.5）。cuts.json・写像仕様は変更不要
- **テキスト指定カット**: 「この発言を消して」→ スキルが transcript から区間を引いて `reason: manual` のカットを書く。既存スキーマで表現可能
- **チャプター・概要欄生成**: transcript.json + 写像関数の副産物。新規スキーマ不要
- **言い直し検出**: detect に検出器を追加し `reason` を増やすだけで載る（スキーマの `reason` は将来値を許容するバリデーションにする）
