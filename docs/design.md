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
| 1 | ASR モデル | **whisper.cpp large-v3-turbo（VAD あり、Silero）に確定**（Step 1 実測ベンチ、2026-08-04） | ゴールデンサンプル実測（verification-plan.md §12.2）。VAD あり同士の比較で CER が最小（large-v3-turbo 4.94% vs large-v3 5.58%）。採用ルールは実行時間を問わず CER 最小を採用（tomada 承認、CER 差が四捨五入 2 桁で同値の場合のみ実行時間で決める）。VAD ありでハルシネーションが 6→0 件に消え（large-v3 側も無音区間での repetition loop が解消）、実行時間も 0.18x とむしろ最速。ピーク RSS 4.2GB。mlx-whisper large-v3-turbo（CER 7.32%、VAD 未計測）・kotoba-whisper v2.0（ggml 変換不可）は不採用。詳細は verification-plan.md §12.2 |
| 2 | smart cut | **v1 は全再エンコード**（CRF 18 / preset slow）。`Renderer` プロトコルで差し替え可能にする | 劣化は 1 世代のみで Filmora 取り込みでは再劣化しない（実機確認済み）。検証体系を先に固める |
| 3 | フィラー検出 | 辞書 2 段階（strong / weak）+ セグメント境界依存の候補化。§5.4 参照 | 文中フィラーの切り出しは単語タイムスタンプに依存し日本語で非信頼のため v1 で扱わない |
| 4 | 誤変換辞書 | **スキーマ互換の独立辞書**。iobsidian `fix-transcriptions` と同一スキーマを `yomi` フィールドで拡張し、初期エントリは同辞書からコピー | 用途が違う（iobsidian は LLM の文脈参照、vidprep は読みベース決定的置換）。リポジトリ間結合を避ける |
| 5 | CLI フレームワーク | **typer** | 型ヒントから CLI 生成、mypy strict 構成と親和 |
| 6 | SRT 写像の端数処理 | 分割せず**クリップ**方式。§4 に仕様化 | 表示中の 1 文を分割すると読み時間が壊れる |
| 7 | プロジェクト構造 | **1 動画 = 1 作業ディレクトリ、場所は任意**。`vidprep init` で作成、各コマンドは cwd または `--project` で指定 | ツールが置き場所を強制しない。§3.1 参照 |
| 8 | カット候補の初期ステータス | 検出を有効化した場合、無音 = `approved`、フィラー = `proposed` | 無音検出は誤爆が少ない。レビューの注意をフィラーに集中させる。両検出は既定オフ |
| 9 | 境界のぶつ切り対策 | クロスフェード（重なり）ではなく**境界フェード in/out（既定 10ms、尺不変）** | acrossfade は境界ごとに尺が縮み、原尺→カット後の写像関数が壊れる。クリック防止目的はフェードで足りる。design-input の「マイクロクロスフェード 20〜50ms」からの変更点 |
| 9b | 動画の終わり方 | 末尾無音は**素材の最後まで**カットし、最後の発話の後に `silence.tail_pad` 秒（既定 2.0）を残す。その残り全体に `render.fade_out` 秒（既定 2.0）の**暗転**をかける。素材が足りないときは最終フレームを保持（`tpad=stop_mode=clone`）して不足分を作る | 末尾に `pad_post` を効かせると素材末尾の 0.3 秒だけが飛び地として残り、ハードカットで終わる。フェードは「最後の発話より後」からしか始めない（発話中に画が暗くなるのを避ける）ため、フェード長 = 残す余白長を既定とする。黒フレーム挿入ではなく最終フレーム保持にするのは、一瞬で真っ黒に切り替わらないようにするため |
| 10 | スキーマ実装 | **pydantic v2** | 中間 JSON が設計の核でありバリデーションが本質的。mypy strict とも親和 |
| 11 | 検証素材 | `fixtures/raw/VID_20260507_144024.mp4`（ゴールデンサンプル、git 管理外） | verification-plan.md §2 参照 |
| 12 | 既定の自動加工 | **denoise / 無音カット / フィラーカットは既定オフ。highpass + loudnorm は既定オン** | 素材に対する変更を明示的なオプトインに限定する。既存 profile は互換扱いにする |

## 2. アーキテクチャ

### 2.1 パイプライン

```
原尺素材 (mp4)  ※プロジェクトからは絶対パス + sha256 で参照
 │
 ├─ [audio-fix]   denoise（任意）→ highpass → 2パス loudnorm → audio/processed.wav
 │                 ↓ 以降の ASR・render はすべて処理済み音声を使う
 ├─ [transcribe]  Silero VAD → ASR → transcript.json（原尺タイムスタンプ）
 │                 └─ [correct] 辞書置換 →（スキル経由 LLM 校正 → 機械検証つき適用）
 ├─ [detect]      （profile で有効化時）auto-editor（無音）+
 │                transcript ベースのフィラー検出 → cuts.json
 │                 └─ ★レビューゲート: report で境界ダイジェスト・波形を確認し
 │                    人間 / agent skill が cuts.json の status を編集
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
| 外部バイナリ | ffmpeg（libass 入り。`ffmpeg-full` 等）, ffprobe, whisper.cpp（または mlx-whisper）, auto-editor（`silence.enabled` 時のみ）, DeepFilterNet（`audio.denoise=deepfilternet` 時のみ） | doctor が検査する。後二者は任意チェック |

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
├── out/                 # output.mp4 / subtitles.srt / transcript.txt / preview.mp4
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
    "audio_fix": {"done_at": "...", "params_sha256": "...", "inputs_sha256": {}, "tool_versions": {"ffmpeg": "7.x"}},
    "render": {"done_at": "...", "params_sha256": "...", "inputs_sha256": {"transcript.json": "...", "cuts.json": "..."}, "tool_versions": {}}
  }
}
```

- `stages` は各コマンドが完了時に記録する（入力パラメータのハッシュとツールバージョン）。下流コマンドは上流の記録と現在の profile を突き合わせ、**古い成果物の上で動くときは警告する**（ブロックはしない）
- `inputs_sha256` は、そのステージが**読んだ成果物**の完了時点でのダイジェスト（`STAGE_INPUTS`）。`prep` はこれを現在の内容と突き合わせ、違えばそのステージを再実行する。これがないと `correct --apply-patch` のようにパイプラインの外で `transcript.json` を書き換えたとき、profile もステージ実行記録も動かないので render が「最新」と判定され、**字幕が古い文言のまま、終了コード 0 で配られる**。ステージではなく成果物を追うので、誰がどう書き換えても捕まり、何も変わっていなければ何も再実行しない
  - 対象は小さな JSON 成果物のみ。`audio/processed.wav` の変化は必ず `audio_fix` の実行を伴い、それは実行記録側で捕まるため、毎回数百 MB をハッシュする価値はない
  - ダイジェストを持たない古い記録は「陳腐化なし」として扱う。既存プロジェクトを更新しただけで全ステージが再実行されるのを避けるため
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
// telops.json — agent skill または人間が書き、render --preview が読む
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
- `styles.json` は**フィールド単位のマージ**。同梱既定のプリセットに対し、プロジェクト側が書いたフィールドだけを上書きし、書かなかったフィールド（特に `fontname`）は既定のまま残す。既定にない名前のプリセットは追加として受理する
- **`Bold: 1` の実機結果（2026-08-04、ffmpeg 7.1.1 + libass / macOS 15）**: 焼き込み実測で、`Hiragino Sans W3` + `Bold: 1` は `Hiragino Sans W3` 素のままと**見た目が変わらなかった**（= `Bold: 1` は効かない）。一方 `Hiragino Sans W6` は明確に太く描画される。よって上記の「ウェイト別ファミリー名で指定する」は回避策ではなく**必須**。検証素材は `fixtures/telops-12/`（`preview.mp4` と `frame-weights.png`）。Filmora 取り込み側での最終確認は verification-plan.md §9 のチェックリストに残る

### 3.6 profile.json（既定値つき）

```json
{
  "version": "1",
  "audio": {"denoise": "none", "deepfilternet_atten_lim_db": 12.0,
            "highpass_hz": 80,
            "loudnorm": {"i": -14.0, "tp": -1.0, "lra": 11.0}},
  "asr": {"backend": "whisper.cpp", "model": "large-v3-turbo",
          "language": "ja", "vad": "silero-v5"},
  "silence": {"enabled": false, "threshold": "4%", "min_duration": 0.6,
              "pad_pre": 0.3, "pad_post": 0.3, "min_cut_duration": 0.4,
              "tail_pad": 2.0},
  "filler": {"enabled": false, "enable_weak": false,
             "require_adjacent_silence": 0.2},
  "render": {"crf": 18, "preset": "slow", "boundary_fade": 0.010,
             "fade_out": 2.0, "verify_asr_mode": "gate"},
  "subtitle": {"max_chars_per_line": 20, "max_lines": 2,
               "min_display": 0.8, "max_cps": 8.0}
}
```

- `audio.denoise` は `none`（既定）、`deepfilternet`、`afftdn` のいずれか。`none` でも
  `highpass` と `loudnorm` 2 パス（I=-14 / TP=-1 / LRA=11）は実行する。DeepFilterNet は
  `deepfilternet` を選んだときだけ必要で、無い場合は `afftdn` にフォールバックせずエラーにする
- `silence.enabled` は無音候補検出、`filler.enabled` はフィラー候補検出のオプトインスイッチ。
  新規 profile では両方 `false` なので、detect / prep は既定では素材をカットしない。旧 profile
  にこのキーが無い場合は後方互換のため `true` として読み込む（`vidprep init` が生成する明示的な
  `false` は優先される）
- `asr.vad` は値が 1 つしかない（`silero-v5`）。VAD はスキップ不可なので、profile でも CLI でも無効化できないことをスキーマで保証する（§5.2）
- `render.verify_asr_mode` は `render --verify-asr`（再文字起こし照合）の扱い。`gate`（既定）は境界欠落フラグ 1 件でも exit 3。`advisory` は同じフラグを警告として報告し exit code を変えない。#11 の導入時は `advisory` が既定で、#32 の再現性実測（同一 render に 3 回照合してフラグ 0 件・報告値全一致、誤検知 通算 0/364 境界）を経て `gate` へ昇格した（verification-plan.md §8.1）
- `pad_pre/pad_post` は「発話側に残す余白」。カット区間を両端からこの分だけ縮める。保守的（長め）から始め、ゴールデンサンプルでの試聴で詰める（verification-plan.md §7）
- `silence.tail_pad` は末尾無音だけに効く。素材の最後に届く無音には `pad_post` を適用せず（後続の語がないため守る対象がない）、カットを素材末尾まで走らせたうえで最後の発話の後に `tail_pad` 秒だけ残す。これが `render.fade_out` の暗転が乗る土台なので、既定では両者を同じ 2.0 秒に揃えている
- `render.fade_out` は末尾の暗転長。`0` にすると素材が終わった瞬間に動画も終わる（#39 以前の挙動）
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

チェーン: `denoise（profile で選択したときだけ）→ highpass 80Hz → loudnorm 2 パス（pass 2 は linear モードを要求）`。denoise の既定値は `none` であり、highpass と loudnorm は常に実行する。`deepfilternet` を選んだときだけ DeepFilterNet を外部プロセスとして呼び、未インストールなら `afftdn` にフォールバックせず、インストール方法を示す UsageError にする。出力は `audio/processed.wav`（PCM 16bit、ソースのサンプルレート維持）。DeepFilterNet の `deepfilternet_atten_lim_db` は原音との混合を残すための抑制上限で、既定値は 12dB とする。

- loudnorm は 1 パス目で measured 値を取得し、2 パス目に `measured_*` を渡して linear モードを要求する（dynamic モードのポンピング回避）。I / TP / LRA の組み合わせを一定ゲインで満たせない素材では loudnorm が `dynamic` を返すため、audio-fix は処理を継続しつつ warning で明示する
- 尺を変えてはならない（完了条件: 尺差 ≤ 1ms。verification-plan.md §4）
- audio-fix は処理前後の LUFS / TP / LRA を既定で JSON 出力し、`--no-stats` で省略できる（`--stats` も指定可能）。`audio.denoise` が `none` のときはノイズフロア測定を行わず、`noise_floor` も出力しない
- denoise が有効で stats も有効なときだけ、denoise 直後（loudnorm 前）の無音区間 RMS を処理前と比較し、`report/noise_floor.json` に記録する。この測定点はチェーン実行中にしか存在せず後から再現できないため、`report` は自分で測らずこのファイルを引用して REQ-007 を判定する（verification-plan.md §4.1）。denoise 無効時の `report` は古い noise_floor ファイルを判定に使わない
- LRA の既定目標 11.0 は話し声素材では実測 LRA より十分大きく、正規化を拘束しない場合がある。その場合も linear の成立可否は LRA だけで判断せず、pass 2 の `normalization_type` と TP を確認する

### 5.2 transcribe

1. Silero VAD で発話区間を検出（ハルシネーション対策として**必須**、スキップ不可）
2. 発話区間ごとに ASR を実行し、タイムスタンプを原尺に補正して結合
3. VAD 区間情報は `report/vad.json` に保存（detect のフィラー判定と検証が使う）

バックエンドは `whisper.cpp`（subprocess）と `mlx-whisper` の 2 実装を持ち、profile で選ぶ。モデルは Step 1 のベンチで確定。

実装上の Silero VAD の担い手は whisper.cpp（`--vad`）に一本化する。whisper.cpp は発話区間の検出・区間だけの認識・原尺への時刻補正を 1 プロセスで行い、検出した区間をログに出す（`vad_segment_info`）。したがって:

- `whisper.cpp` バックエンド: 1 回の実行から transcript と `report/vad.json` の両方を得る（ベンチ実測値と同じ実行形）
- `mlx-whisper` バックエンド: 検出だけを行う whisper.cpp の実行（`-d 1`）で区間を得て、区間ごとに ffmpeg で切り出して mlx に渡し、返ってきた区間内時刻に区間 start を加算して原尺に戻す。`--clip-timestamps` で一括処理する形は実測で棄却した（ゴールデンサンプルで 106 セグメント中 23 件が発話区間外に着地し、末尾は素材尺を超えて repetition loop に入った）
- どちらの経路でも、書き出す前に「全セグメントの start が検出区間の内側か」「既知の幻覚フレーズが無音上に乗っていないか」を検証し、破れていれば何も書かずに exit 3

### 5.3 correct

- `vidprep correct` : 辞書置換（決定的・冪等）。§3.7 の 2 段方式
- `vidprep correct --apply-patch <patch.json>` : LLM 校正パッチの適用。パッチは `{"edits": [{"id": "s0001", "text": "新テキスト"}]}` 形式で、適用前に機械検証する:
  - 存在する id のみか / id の重複がないか
  - **タイムスタンプ・セグメント数・順序を変更していないか**（パッチ形式上そもそも書けないが、適用後の不変条件としても検証）
  - 変更セグメント数と diff サマリを表示し、`--yes` がなければ確認を求める

LLM 校正そのもの（プロンプト・文脈判断）は agent skill の仕事で、CLI は検証つき適用だけを担う。

### 5.4 detect

- `silence.enabled` が `false` なら auto-editor を呼ばず、無音候補を作らない。`true` のときだけ `auto-editor --export v3` の JSON タイムラインを keep/cut リストへ変換する（変換層は auto-editor のバージョンを記録し、v3 スキーマ変化を検知したらエラーにする）。パディング適用後 `min_cut_duration` 未満の区間は捨てる
- `filler.enabled` が `false` なら transcript を走査せず、フィラー候補を作らない。`true` のときだけ transcript.json のセグメントに対し辞書照合で検出する。**候補化するのは次のどちらかのみ**:
  - (a) セグメント全体がフィラー語のみ（例: 「えーと」だけのセグメント）→ セグメント区間 + 隣接無音を一体のカット候補にする
  - (b) セグメントの先頭/末尾がフィラー語で、`require_adjacent_silence` 秒以上の無音に隣接 → VAD 境界を使ってフィラー部分を切り出す
  - 文中フィラーは検出のみ（`note` に記録、カット候補にしない）
- フィラー辞書（profile とは別にリポジトリ同梱、プロジェクトで追記可）:
- strong（フィラー検出を有効にした場合の既定）: えー、えーと、えっと、あのー、そのー、うーん
- weak（`enable_weak: true` のときのみ候補化）: まあ、なんか、こう
- フィラーも既定オフにする。`prep` は enabled なフィラー候補を自動承認するため、既定でオンにするとレビューなしに素材が変わる。`filler.enabled` を明示した利用者には従来どおり候補化と `prep` の承認を提供し、`filler.enable_weak` がオンのときは strong だけを選別できないため承認を見送る
- 末尾に届く無音は例外扱い: `[gap.start + tail_pad, 素材尺]` をカットする。`pad_post` を効かせると素材末尾の `pad_post` 秒が飛び地として残り、除去区間をまたいでクリックと画の飛びになる
- 出力は §3.4 のマージ規則で既存 cuts.json に統合する

実装時に確定した詳細（auto-editor 29.3.1 実測）:

- 無音検出の入力は `audio/processed.wav`（detect は audio-fix の下流）。`--margin 0s` を明示し、パディングは vidprep 側で行う。auto-editor の margin に任せると「検出器出力との差分」でパディングを機械確認できなくなるため
- `min_duration` に相当する CLI オプションは auto-editor 29.3.1 に存在しないため、変換層で「gap 長 < min_duration の無音は検出しない」として適用する
- **`--progress none` を明示する**。タイムラインは `-o -` で stdout から受け取るが、auto-editor は進捗バーも stdout に描くうえ `--quiet` ではバーが止まらない。音量解析が進捗バーを描く程度に遅いランでは `Analyzing audio volume | ... ETA ...` が JSON の前に混ざり、`timeline_schema`（exit 2）で停止する。バーが出るかは auto-editor の音量キャッシュ（`$TMPDIR/ae-29.3.1/`）が温まっているかで変わるため、同じコマンドが通ったり落ちたりする（#30 実測）。なお `-o <file>` は §12.1.1 のとおり拡張子を `.v3` に書き換えるので、stdout 経由をやめる選択肢は取らない
- **発話衝突（1 カットあたり ≤ 0.2 秒）は transcript セグメント区間そのものではなく「transcript ∩ VAD 発話区間」で測る**。whisper.cpp の VAD 併用時、セグメントの end が次の発話区間の末尾まで伸びることがある（ゴールデン実測: 1 セグメントが 55 秒の無音をまたいだ）。生の区間で測ると 30 カット中 15 件が閾値超過となり、実際には発話を消していないカットで detect が止まる
- 区間が無音カットをまたぐ transcript セグメントは**削除せず警告**する（#7 の二重防御の結論）。transcript は transcribe の所有物であり、detect は書き換えない
- 条件 (b) は「セグメント内部に VAD 境界があるとき」のみ候補化する。境界がなければフィラーの終端を推測できないため note のみに留める（precision 優先）
- フィラー候補は同じ実行で検出した無音カットとの重なりを引いてから出力する。両方を approved にしても「approved 同士は重ならない」不変条件が破れないようにするため

### 5.5 render

```python
class Renderer(Protocol):
    def render(self, job: RenderJob) -> RenderResult: ...
```

v1 実装は `ReencodeRenderer`: keep 区間を `trim` + `concat` フィルタで連結し、映像 CRF 18 / preset slow / 元解像度・fps 維持、音声は processed.wav の対応区間 + 境界フェード（`afade` 10ms、尺不変）で AAC 320kbps に再エンコード。smart cut は将来 `SmartCutRenderer` として同一プロトコルで差し替える。

末尾の暗転（§1 判断 9b）は concat の後段に付ける。`RenderJob.tail` は「カット後タイムラインで最後の発話が終わってから出力が終わるまでの秒数」で、render が transcript の写像済みセグメントから測って渡す（keep 区間だけを見てもどこまでが発話かは分からないため）。`Closing` はそこから `pad = max(0, fade_out - tail)` を決める:

- `tail >= fade_out`（通常）: 素材の無音の上に `fade` / `afade` を乗せるだけ。尺は変わらない
- `tail < fade_out`（話し終わってすぐ録画を止めた素材）: `tpad=stop_mode=clone` / `apad` で不足分だけ最終フレームと無音を継ぎ足してから暗転する。**尺を変える唯一の処理**なので、`expected_duration = Σkeep + pad` として §8 の尺検証に織り込む

フェード開始位置は常に「最後の発話の終わり」以降になるため、発話が暗転や音量低下に巻き込まれることはない（`--verify-asr` の再文字起こしと loudnorm 実測値もこれで動かない）。

実装時に確定した詳細（ffmpeg 7.1.1 実測）:

- **`tpad` の前に `fps` でレートを固定する**。`tpad` は与えられたストリームのフレーム長から追加フレーム数を求めるが、`concat` の出力はそれが読めず、`concat` の後段に置いた `tpad` は**何も追加せず、何も言わない**。音声側の `apad` だけが効いて映像と 1.5 秒ずれた出力になる。出力に強制するのと同じレートを `fps` で先に噛ませると意図どおり保持される
- **保持長はフレーム単位に切り上げてから渡す**。`tpad` は整数フレームしか足せないので自分で切り上げる。こちらで先に丸めておけば `expected_duration = Σkeep + pad` が実ファイル尺と一致し（§8 の 1 フレーム許容を消費しない）、映像と音声に同じ値を渡すので AV 差も出ない

- `subtitles.srt`: §4 の写像で生成（BudouX + `max_chars_per_line` で行分割した版。`--no-wrap` で改行なし版も出せる）
- `transcript.txt`: 同じ写像済みエントリを `[MM:SS] 本文`（1 時間以降は `[H:MM:SS]`）の段落に組んだプレーンテキスト。vidprep は話題境界を判定できないため、段落の区切りはデータに既にある信号だけで決める機械的な規則: 累積幅が `MIN_PARAGRAPH_WIDTH`（全角 100 字）未満では区切らず、以降は文末記号（`。．！？!?`）かエントリ間の間が `PARAGRAPH_PAUSE`（0.6 秒）以上あれば区切り、`MAX_PARAGRAPH_WIDTH`（全角 300 字）に達したら信号の有無に関わらず区切る。しきい値は `_subtitles.py` の名前付き定数で、`profile.json` には出さない — render が params_sha256 に含めるのは `render` / `subtitle` セクションで、そこに段落しきい値を足すとテキストの折り返し調整だけで動画の全再エンコードが走ってしまうため
- `--preview`: telops.json + styles.json から ASS を組み、libass 焼き込みの preview.mp4 を出す
- render は開始前に cuts.json の不変条件と、transcript / cuts の元になった素材ハッシュの一致を検証する
- render は出力を公開する前に尺、A/V 同期、integrated loudness に加えて true peak が profile の上限以下であることも検証する
- `--verify-asr`: レンダリング後に出力を再 ASR し、カット境界での語の欠落を照合する（仕様は verification-plan.md §8.1）

### 5.6 report

レビューゲートと検証の道具。`vidprep report` で以下を再生成する:

- `report/stats.json`: 原尺 / カット後尺 / 削減率 / reason 別カット数と秒数 / LUFS 前後 / ノイズフロア（denoise 有効時のみ。`denoise` = REQ-007 判定、`output` = 完成音声の参考値）/ 字幕警告一覧（写像時の除外・min_display 未満・max_cps 超過）
- report は source の integrated loudness から target までの必要ゲインが +12dB を超える場合、録音レベルが低いことを warning で指摘する
- `report/boundaries/*.png`: 各カット境界前後 ±2s の波形 PNG（`showwavespic`）
- `report/boundary_digest.mp4`: **全カット境界の前後 ±2s だけを連結した確認用動画**（境界位置に無音の 0.5s 黒フレームを挟む）。カットが 30 箇所あっても数分で全境界を試聴でき、レビューゲートの主力になる
- `--cuts`: カット候補ごとに「削除される transcript テキスト + 前後の文脈」を表示（人間 / スキルが status を判断する材料）

### 5.7 doctor

検査対象: ffmpeg（`subtitles` フィルタ = libass の有無も確認）、ffprobe、ASR バックエンド（whisper.cpp バイナリ + モデルファイル / mlx-whisper import）、Silero VAD の重み（`ggml-silero-*.bin`。§5.2 の VAD は両バックエンドで必須なので必須項目扱い。モデルファイルとしては ASR 側の候補から除外する）、SudachiPy 辞書、auto-editor、DeepFilterNet。auto-editor と DeepFilterNet はそれぞれ `silence.enabled` / `audio.denoise=deepfilternet` のオプトイン機能なので任意チェックとし、無くても doctor は警告付き exit 0 とする。結果を JSON 出力し、必須が欠けていれば exit 3。profile で機能を有効にした実行時にだけ、そのステージが不足を UsageError として報告する。

## 6. CLI 仕様

ステージサブコマンド: `init | doctor | audio-fix | transcribe | correct | detect | render | report`

複合サブコマンド: `prep`

- ステージサブコマンドは §5 と 1:1。`prep` は動画 1 本を位置引数に取り、
  audio-fix → transcribe → correct → detect → render → report の順に呼ぶだけ
  で、独自の処理は持たない。`--project` の既定が cwd ではなく `<video>.vidprep`
  （素材の隣）になる点だけ他のサブコマンドと異なる。レンダーが終わった実行では
  `render` が出した `output.mp4` / `subtitles.srt` / `transcript.txt`（§5.5）を
  素材の隣に `<video>.edited.mp4` / `<video>.srt` / `<video>.txt` としてコピーする
- `prep` は新規 profile では denoise / 無音カット / フィラーカットを行わず、`audio-fix` の highpass + loudnorm だけを実行する。`silence.enabled` / `filler.enabled` を有効にした場合は、ユーザーに代わって 2 つの判断を下す: 文字起こし直後に 1 度だけ停止
  して `correct-transcript` スキルでの校正を促す（`--yes` で省略可）ことと、
  `detect` が人間向けに残した enabled なフィラー候補を `filler.enable_weak` が off の間
  だけ承認する（`--keep-fillers` で無効化可）こと。どちらも実行上の既定値で
  あり、CLI 本体が AI 依存になるわけではない（§7 の原則はそのまま）

共通仕様:

- `--project/-p <dir>`（既定 cwd。`prep` のみ既定 `<video>.vidprep`）、`--json`
  （結果 JSON を stdout、人間向けログは stderr）、`--dry-run`（実行計画の表示
  のみ。外部コマンド列を含む）
- exit code: `0` 成功 / `1` 使用法・環境エラー / `2` 処理実行の失敗 / `3` 検証 NG（スキーマ不正、ハッシュ不一致、doctor の必須欠如など）
- 破壊的でない: すべての出力は上書き前に生成し、成功時にアトミックに置き換える。ソース素材には一切書き込まない

## 7. Agent連携（Claude Code / Codex）

CLI 本体は AI 非依存。スキルは中間 JSON の読み書きと CLI 呼び出しだけを行う。スキルの正本は `.claude/skills/` に置き、Codex からは `.agents/skills/` の symlink 経由で同じ実体を読む。v1 のパイプラインスキルは 3 つ（実装はスキル作成時に詰める。ここでは契約のみ定義）:

| スキル | 読む | 書く | 契約 |
|---|---|---|---|
| correct-transcript | transcript.json, dictionaries/ | patch.json | パッチ形式（§5.3）のみ。適用は必ず `vidprep correct --apply-patch` を通す |
| review-cuts | cuts.json, `report --cuts` 出力, transcript.json | cuts.json（status のみ） | 区間・id の変更禁止。判断根拠を `note` に書く |
| place-telops | transcript.json, styles.json | telops.json | 検証は `render --preview` のスキーマ検証に委ねる |

この 3 つに加えて `.claude/skills/` にはもう一種類、知識スキル（knowledge skill）が
存在する。パイプラインスキルのように JSON を読み書きして CLI を呼ぶものではなく、
このリポジトリの規約・設計判断・アンチパターンを保持し、該当パス（`src/vidprep/*.py`、
`tests/**`、CI 設定など）を触ったときに条件付きで読み込まれるプロンプトである。
AGENTS.md には常時読まれる不変条件だけを残し、その根拠・具体例・境界事例は知識スキル
側に置く方針を取っている。索引は AGENTS.md の `## Skills` セクション、各スキルの
責務分担は `.claude/skills/authoring-skills/SKILL.md` を参照。

## 8. 拡張ポイント（v1 では作らないが壊さない）

- **smart cut**: `Renderer` プロトコル差し替え（§5.5）。cuts.json・写像仕様は変更不要
- **テキスト指定カット**: 「この発言を消して」→ スキルが transcript から区間を引いて `reason: manual` のカットを書く。既存スキーマで表現可能
- **チャプター・概要欄生成**: transcript.json + 写像関数の副産物。新規スキーマ不要
- **言い直し検出**: detect に検出器を追加し `reason` を増やすだけで載る（スキーマの `reason` は将来値を許容するバリデーションにする）
