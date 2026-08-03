---
created: 2026-08-03
status: draft
---

# vidprep — 設計インプット（調査セッションのまとめ）

YouTube 動画編集の下処理を CLI で半自動化するツール。CLI で下処理 → 最終調整と確認は Wondershare Filmora、という運用の前段を担う。本ドキュメントは 2026-08-03 の実現可能性調査セッションの結論をまとめたもので、**設計書を起こす際のインプット**である。調査の詳細・根拠 URL は [research/feasibility-report.md](research/feasibility-report.md) を参照。

## 確定した決定事項

| 論点 | 決定 | 根拠・経緯 |
|---|---|---|
| 出口形式 | **カット済み mp4 + SRT**（.wfp 生成は断念） | Filmora はタイムライン交換形式（FCPXML / Premiere XML / EDL / AAF / OTIO）を一切読めない（実機 Filmora 14.10.5 の Info.plist の CFBundleDocumentTypes で確認。登録は wfp/wfpx/wfpbundle 系のみ）。.wfp は無圧縮 ZIP + JSON で直接生成も可能と判明したが、非公式形式への追随コストを嫌い不採用と決定 |
| 実装言語 | **Python**（uv-template ベース） | 処理時間は ffmpeg / whisper 系のネイティブバイナリが支配し、オーケストレーション言語は速度に効かない。周辺ライブラリ（pysubs2, BudouX, SudachiPy, mlx-whisper）が Python に集中。Rust は libass バインディング 3 年放置・whisper-rs GitHub アーカイブ済みで割に合わないと判定 |
| ASR | **ローカル実行**: whisper.cpp（Metal + CoreML）or mlx-whisper | 無料・オフライン。15 分素材で数十秒〜5 分。faster-whisper は CTranslate2 に Metal サポートがなく Mac で GPU 不可のため除外 |
| トランジション | **CLI スコープ外（Filmora でやる）** | 焼き込むと後から変更不能。編集自由度を優先 |
| テロップ | **ポイント投入**（セグメント単位タイムスタンプ） | 単語レベルのタイムスタンプは日本語で信頼性が低い（漢字↔音素対応の曖昧さ）。カラオケ風の文字送りは断念。セグメント単位なら精度は十分 |
| 字幕 | **フル SRT を常に生成**し資産として保持 | Filmora が読める字幕は SRT のみ（ASS/VTT 不可）。取り込み後に Filmora 上で編集可能。YouTube アップロードで検索性確保、後から「どの動画で話したか」を遡れる |
| 名前 | リポジトリ・CLI・パッケージとも `vidprep` | 2026-08-03 決定 |
| リモート | GitHub に public リポジトリを作成済み（[tomada1114/vidprep](https://github.com/tomada1114/vidprep)） | 2026-08-03 作成 |

## 設計思想（3 原則）

1. **原尺タイムラインが唯一の正本。** カットは「削除区間リスト」、字幕・テロップは「原尺タイムスタンプ」で保持し、mp4 / SRT / プレビューはすべてレンダリング結果として何度でも再生成できる（冪等）。
2. **すべての中間データは機械可読 JSON。** AI（Claude Code スキル）と人間が同じファイルを編集でき、各コマンドは「JSON を読んで JSON を書く」に統一する。CLI 本体に AI 依存は入れない。
3. **破壊的操作の前にレビューゲート。** カット適用前に波形 PNG・カット対象の文字起こしテキストを出力し、確認してから render する。

## パイプライン全体像

```
原尺素材 (mp4)
 │
 ├─ [audio-fix]   ラウドネス正規化 + ノイズ除去 → 処理済み音声
 ├─ [transcribe]  VAD → ASR → transcript.json（原尺タイムスタンプ）
 │                 └─ [correct] 辞書置換 + LLM 校正（テキストのみ変更）
 ├─ [detect]      無音検出 ＋ フィラー検出 → cuts.json（カット候補）
 │                 └─ ★レビューゲート（人間 or Claude Code が cuts.json を編集）
 └─ [render]
      ├─ output.mp4     … smart cut + 境界マイクロクロスフェード（20〜50ms）
      ├─ subtitles.srt  … カット後タイムラインへ写像 → Filmora / YouTube へ
      ├─ preview.mp4    … ASS テロップ焼き込み（確認用、任意）
      └─ report/        … カット境界の波形 PNG、統計
```

処理順序の要点: **カットより先に原尺のまま ASR する**。先にカットすると (a) 境界で語が切れ認識精度が落ちる、(b) Filmora でカットを微修正した際に字幕を作り直す羽目になる、(c) 原尺との対応が失われる。

## モジュール別整理

### A. 音声前処理 — できる（確実）
- 内容: YouTube 基準（-14 LUFS）へのラウドネス正規化、ノイズ除去、軽い声質チェーン（highpass + compressor）
- 使うもの: ffmpeg `loudnorm`（2 パス）/ DeepFilterNet（DL 系スピーチ強化、CLI 単体）/ 軽量代替に ffmpeg `afftdn`・`arnndn`
- ポイント: 決定的で判断不要。チェーンをプロファイル（JSON）化する
- Claude Code 連携: ほぼ不要。処理前後の LUFS 統計を JSON で出し、結果確認のみ

### B. 文字起こし — できる
- 内容: VAD → 日本語 ASR → transcript.json（セグメント + テキスト + 原尺タイムスタンプ）
- 使うもの: whisper.cpp または mlx-whisper。前段に Silero VAD
- 注意: Whisper は無音区間で「ご視聴ありがとうございました」等を幻覚する既知の癖があり **VAD 前段は必須**
- モデル選定（large-v3 / turbo / kotoba-whisper）: 公開ベンチが相互に矛盾しており、**自分の素材での実測で決める**。設計書にベンチ手順を含めること
- Claude Code 連携: transcript.json がすべての起点。スキーマに `id` / `words` / `source` / `edits[]`（編集履歴）を持たせる

### C. カスタム辞書 + 校正 — 工夫すれば
- 内容: 固有名詞の誤認識訂正。①決定的な読みベース置換 → ②LLM 校正の 2 段
- 使うもの: SudachiPy or pyopenjtalk（読み正規化）+ 自作辞書（JSON）+ Claude（スキル経由）
- 根拠: Whisper の `--prompt` は約 224 トークン上限で辞書代わりにならない。ローカルで辞書を正面サポートする ASR は存在しない → 後処理が定石
- 備考: iobsidian の `fix-transcriptions` スキルの誤変換辞書と思想が同じ。辞書資産の共有可否は未決
- Claude Code 連携: LLM 校正はスキルの仕事。返答は「id 集合の一致・件数一致・タイムスタンプ不変」を機械検証してから適用（捏造防止）

### D. カット候補検出 — できる
- 内容: ①無音（閾値 + 前後パディング）②フィラー語（「えーと」等）③将来: 言い直し・重複テイク検出
- 使うもの: auto-editor（`--export v3` で JSON タイムライン出力、活発にメンテ中）→ 自前 cuts.json へ変換。フィラーは transcript.json から自作検出
- ポイント: 「検出」と「適用」の分離が本設計の核。auto-editor v3 は NLE 構造で思想が違うため keep/cut リスト形式の自前スキーマに**変換層を挟む**。閾値・パディングはプロファイルで管理し、保守的な値（長め）から詰める
- Claude Code 連携: cuts.json に `reason`（silence / filler / manual）と `confidence` を持たせ、スキルが採用/却下を編集できる形にする

### E. カット適用 + 検証 — できる（唯一の本気の自作箇所）
- 内容: cuts.json を読んでフレーム精度カット + 連結。境界にマイクロクロスフェード
- 使うもの: ffprobe + ffmpeg で smart cut（境界 GOP のみ再エンコード、残り stream copy）を自作
- 根拠: `-c copy` はキーフレーム単位でしか切れない。参照実装 smartcut（MIT）は 2026-02 に商用移行で開発停止 → アルゴリズムのみ参考に自作（コード流用不可ではないが追随不能）
- 段階戦略: v1 は全再エンコード（CRF 18 / preset slow）で妥協し、smart cut は後から差し替え可能な構造にする
- 検証: `ffmpeg showwavespic` でカット境界前後の波形 PNG を report/ に出力し、目視 or スキルが確認

### F. 字幕・テロップ出力 — できる
- 内容: ①フル SRT（カット後タイムラインへ写像）②テロップ用 ASS（スタイルプリセット + 9 方向配置）③焼き込みプレビュー
- 使うもの: pysubs2（SRT/ASS 生成）/ BudouX（日本語行分割）/ ffmpeg `subtitles` フィルタ（**libass 入りビルドが必要**。Homebrew の標準 ffmpeg では不可の場合あり）
- 注意: macOS の libass は CoreText 経由で `Bold: 1` が効かない事例あり → ウェイト別ファミリー名の直指定で回避
- ポイント: 写像関数（原尺 → カット後時刻）は E と共有する単一実装にする
- Claude Code 連携: telops.json（どのセグメントに・どのプリセットで・何秒表示）をスキルが書き、CLI がレンダリング。「どこにテロップを入れるか」の判断こそ AI の仕事

### スコープ外と決めたもの
- トランジション（xfade）: Filmora 側でやる。調査結果は feasibility-report.md に残してある
- 色補正・ホワイトバランス、BGM・SE 挿入: タイミング・画の判断が本質で GUI 向き
- カラオケ風の文字送りテロップ: 日本語の単語タイムスタンプ精度が不足

### 将来の拡張候補（v1 スコープ外、設計だけ意識）
- 言い直し・重複テイク検出（transcript ベース）
- YouTube チャプター・概要欄の自動生成（transcript.json + 写像機構の副産物でほぼ無償）
- テキスト指定での区間カット（「この発言を消して」を AI が cuts.json に変換）

## 中間データ（スキーマ骨子）

```
transcript.json  {segments: [{id, start, end, text, words?, source, edits: []}]}
cuts.json        {cuts: [{id, start, end, reason: silence|filler|manual, confidence, status: proposed|approved|rejected}]}
telops.json      {telops: [{segment_id|time, text, style_preset, duration}]}
styles.json      {presets: {名前: ASS スタイル定義}}
profile.json     {閾値、パディング、音声チェーン設定}
```

- 時刻はすべて**原尺基準の秒（float）**で統一。カット後時刻はレンダリング時に写像
- `edits[]` は編集履歴。再生成を冪等にし、LLM 校正の適用を追跡可能にする

## CLI の形

- サブコマンド: `vidprep audio-fix | transcribe | correct | detect | render | report | doctor`
- 全コマンドに `--dry-run` と JSON 出力。終了コードで成否を機械判定できること
- `doctor`: 外部バイナリ（libass 入り ffmpeg, whisper.cpp, DeepFilterNet, auto-editor）の導入状態を検証
- 外部バイナリは brew / uv tool で導入し、Python 依存は pysubs2 / budoux / sudachipy 程度に抑える
- Claude Code 連携はリポジトリ内 `.claude/skills/` に置く。スキルは CLI を呼び中間 JSON を読み書きするだけ

## Filmora 受け渡しの前提（実機確認済み）

- Filmora は取り込み時に再エンコードしない（プロキシは別ファイル、書き出しは原素材）→ 劣化は CLI の再エンコード段のみで発生
- カットだけなら ProRes 中間化は不要。H.264 のまま渡すのが最速・最小
- SRT は取り込み後に文言・タイムコード・スタイル・位置を編集でき、SRT 再エクスポートも可能
- カット済み mp4 のため「消しすぎ」の復元は Filmora 上では不可 → cuts.json 修正 + 再レンダリングで対応（レビューゲートと保守的パディングで予防）

## 設計書起こし時の未決事項

1. ASR モデルの確定（自素材での実測ベンチ待ち）
2. smart cut を初期スコープに入れるか、v1 は全再エンコードで妥協するか（推奨: 妥協して差し替え可能な構造に）
3. フィラー検出の辞書と閾値、誤爆時の体験設計
4. `fix-transcriptions` の誤変換辞書を共有するか、リポジトリ独立にするか
5. CLI フレームワークの選定（typer / click / argparse）
6. SRT 写像の端数処理（カット境界をまたぐセグメントを分割するか丸めるか）— 仕様化必須
7. プロジェクトディレクトリ構造（1 動画 = 1 作業ディレクトリの規約など）
