# vidprep

[English](README.md) | **日本語**

収録した話し動画を YouTube 用に下処理する CLI パイプライン。音声を整え、文字起こしし、
カット候補を提案し、承認したものだけを適用して、その結果に合った字幕を書き出す。
出力は Filmora などの編集ソフトにそのまま渡せる。

各段は読んで編集できる素の JSON を書き、そして**書こうとしている内容が間違っていると
判断したら処理を止める**。これが設計の中心にある考え方で、パイプラインは何度でも
再実行できる速さなので、確信が持てない段は推測せずに止まって人間に聞く。

## パイプライン

| 段 | コマンド | 生成物 |
|---|---|---|
| 音声前処理 | `vidprep audio-fix` | `audio/processed.wav` |
| 文字起こし | `vidprep transcribe` | `transcript.json`、`report/vad.json` |
| 辞書校正 | `vidprep correct` | `transcript.json`（上書き） |
| カット候補検出 | `vidprep detect` | `cuts.json` |
| カット適用 | `vidprep render` | `out/output.mp4`、`out/subtitles.srt` |
| レポート | `vidprep report` | `report/stats.json`、境界波形、カットダイジェスト |

`audio-fix` は DeepFilterNet（未インストールなら ffmpeg の `afftdn`）でノイズ抑制し、
80 Hz のハイパスを通し、`loudnorm` の 2 パスで -14 LUFS / true peak -1.0 dBTP に
合わせる。`transcribe` は whisper.cpp の前に
Silero の発話区間検出を置き、すべてのタイムスタンプを原尺の秒で記録する。`detect` は
auto-editor が見つけた無音と、文字起こしから見つけたフィラー語を候補にする。`render` は
承認したものだけを適用する。

## このツールが「やらない」こと

チェックこそがこのツールの本体なので、インストール手順より先に書いておく。

- **発話を消すカットは作らない。** `silence` 候補はひとつずつ文字起こしと検出済み発話区間の
  両方に照合され、語を消してしまう実行は書き込まずに拒否される
- **発話区間検出は無効にできない。** これがないと whisper は無音部分に文を捏造し、それが
  後で字幕として出てくる。セグメントが検出済み発話と噛み合わない文字起こしは拒否される
- **動画と字幕は同じカット計画から生成される。** 両者がずれることが原理的に起きない
- **出力は何かを置き換える前に測定される。** 尺はカットリストと 1 フレーム以内、2 つの
  ストリームは 50 ms 以内で一致し、ラウドネスは目標値に乗っていなければならない。失敗した
  render は既存の `out/output.mp4` をそのまま残す
- **`render --verify-asr` は完成したファイルを読み返す。** `out/output.mp4` を
  `transcript.json` が記録したのと同じバックエンド・モデル・検出器で再度文字起こしし
  （こうすると両方の認識が同じ間違いをするので、その間違いは相殺される）、2 回目が一度も
  聞き取らなかったテキストがカット境界の近くにあれば報告する。これはゲートで、1 件でも
  フラグが立てば exit 3

## 必要なもの

Python 3.12 以降と、いくつかの外部ツール。`vidprep doctor` がすべてを検査し、足りない
ものについて何をインストールすればよいかを表示する。

| ツール | 用途 | 備考 |
|---|---|---|
| ffmpeg / ffprobe | 全段 | `render --preview` には libass 付きビルドが必要 |
| auto-editor | `detect` | `uv tool install auto-editor`。`--export v3` が必要 |
| whisper.cpp または mlx-whisper | `transcribe` | `~/.cache/whisper.cpp` に ggml モデルを配置 |
| Silero VAD の重み | `transcribe` | `ggml-silero-v5.1.2.bin`、同じディレクトリ |
| SudachiPy 辞書 | `correct` | `uv pip install sudachidict_core` |
| DeepFilterNet | `audio-fix` | 任意。無ければ ffmpeg の `afftdn` にフォールバック |

`doctor` は必須ツールが欠けていれば exit 3、DeepFilterNet だけが無い場合は exit 0 を返す。

## インストール

vidprep はまだ PyPI で公開していない。リポジトリからインストールする。

```bash
git clone https://github.com/tomada1114/vidprep.git
cd vidprep
just install          # または: uv sync --all-groups
uv run vidprep doctor
```

チェックアウト外で使うなら、CLI をツールとしてインストールする。

```bash
uv tool install --from . vidprep
```

## クイックスタート

```bash
vidprep doctor          # まず外部ツールを検査する
vidprep init ./work/talk01 --source ~/Movies/talk01.mp4

vidprep audio-fix --stats   # ノイズ抑制 → ハイパス 80 Hz → loudnorm、前後の数値つき
vidprep transcribe          # Silero VAD → ASR → transcript.json（原尺タイムスタンプ）
vidprep correct --dry-run   # 誤変換辞書の置換 diff を確認する（書き換えなし）
vidprep detect              # 無音 + フィラーのカット候補 → cuts.json

vidprep report --cuts       # 候補ごとに「消える発話 + 前後の文脈」を表示
# cuts.json の各候補の `status` を編集する: approved / rejected

vidprep render              # approved カットを適用 → out/output.mp4 + out/subtitles.srt
vidprep report              # stats.json + 境界波形 PNG + boundary_digest.mp4
```

素材の動画は絶対パスと sha256 で参照される。変更されることはなく、`--copy-source` を
明示したときだけプロジェクト内にコピーされる。

すべてのサブコマンドが `--project/-p`、`--json`、`--dry-run` を受け付ける。`detect` は
何度でも再実行してよい。既に判断済みの候補は区間だけが更新され、`status` と `note` は
保持され、識別子が再利用されることはない。

## 人間がレビューする場所

vidprep は人間が決めるべきことを決めない。そのための場所が 3 つある。

- `vidprep report --cuts` は候補ごとに、消える発話とその前後の文字起こしを一覧する。
  `cuts.json` の `status` を人間が設定する
- `report/boundary_digest.mp4` はすべてのカット境界を連続再生する。フラグが立った境界は
  議論するのではなく聴いて判断できる
- `vidprep render --preview` は `telops.json` を libass 経由で `out/preview.mp4` に
  焼き込む。画面上のテロップを確定前に確認できる

LLM を使う部分については Claude Code のスキルを 3 つ同梱している
（`correct-transcript` / `review-cuts` / `place-telops`）。どれも中間 JSON を読み、
成果物をちょうど 1 つだけ書き、検証は CLI に委ねる。

## プロジェクトディレクトリ

```
work/talk01/
├── vidprep.json       # マニフェスト: 素材パス、sha256、各段の実行記録
├── profile.json       # 処理パラメータ（同梱の既定値からコピーされる）
├── audio/
│   └── processed.wav  # audio-fix の出力。render の音声はここから取る
├── transcript.json    # 原尺の秒で記録されたセグメント
├── cuts.json          # カット候補と、人間がつけた status
├── out/
│   ├── output.mp4
│   ├── subtitles.srt  # --no-wrap で subtitles.nowrap.srt も
│   ├── telops.ass     # --preview 時
│   └── preview.mp4    # --preview 時
└── report/
    ├── stats.json
    ├── vad.json
    ├── noise_floor.json        # audio-fix --stats で生成される
    ├── boundaries/             # 境界ごとに波形 PNG が 1 枚
    └── boundary_digest.mp4
```

## 回帰確認

```bash
just golden        # ゴールデンサンプルで全段を通し fixtures/runs/<date>/ に保存する
just golden-diff   # 直近 2 回のランの差分を見る
```

どちらもローカル専用で、素材・ffmpeg・whisper.cpp・auto-editor を必要とするため
`just check` には含まれない。もう半分が `tests/fault_injection/` で、意図的に壊した入力を
並べ、それを捕まえるはずのチェックが実際に捕まえることを検証している。

## 開発

```bash
just install   # 依存関係と git hooks
just check     # フォーマット、lint、型チェック、テスト
just docs      # ドキュメントをローカルで配信する
```

## ドキュメント

- [Getting Started](docs/getting-started.md) — セットアップと各段のウォークスルー（英語）
- [API Reference](docs/reference.md) — 公開 API（英語）
- [設計メモ](docs/design.md) — アーキテクチャとその判断根拠
- [検証計画](docs/verification-plan.md) — 各要件をどう検証するか
- [実現可能性調査](docs/research/feasibility-report.md) — 設計が依拠している調査結果
- [CONTRIBUTING.md](CONTRIBUTING.md) — 貢献の手引き（英語）、[CHANGELOG.md](CHANGELOG.md)

## ライセンス

MIT。[uv-template](https://github.com/tomada1114/uv-template) をベースに構築している。
