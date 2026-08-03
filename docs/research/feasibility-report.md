---
created: 2026-08-03
status: reference
---

<!-- 出典: Claude Code Dynamic Workflow（6領域並列調査 + Opus統合、2026-08-03）。
     主要な主張（FilmoraのInfo.plist、.wfpのZIP+JSON構造、tlBegin/tlEnd、file://の非標準URI）は
     実機（Filmora 14.10.5 / ~/Movies配下の実プロジェクト）で追検証済み。
     ただし .wfp 直接生成は不採用に決定したため、該当セクションは背景資料として読むこと。 -->

# YouTube 下処理 CLI 自動化 — 統合調査レポート（2026-08-03 時点）

## 0. 結論サマリ

- 5機能すべて技術的には到達可能。ただし**出口（Filmora への渡し方）が設計を規定する**。実機の `Info.plist` を直接確認した結果、Filmora は **FCPXML / Premiere XML / EDL / AAF / OTIO を一切インポートできない**（拡張子登録なし）。「カット案 XML を渡す」構想は成立しない。
- 代わりに **`.wfp` プロジェクトファイルの直接生成**が有望。実機の `.wfp` は**無圧縮 ZIP + 素の JSON**で、カットは `tlBegin/tlEnd/inPoint/outPoint` の4整数（単位は **100ns tick、1秒 = 1e7**）。署名・チェックサム防御なし。ただし非公式のため**「生成した .wfp が実際に開くか」の30分スパイクが最優先タスク**。
- 字幕は **SRT のみ**（ASS/VTT/SSA 非対応）。SRT なら Filmora 上で文字・タイムコード・スタイルを編集でき、SRT 再エクスポートも可能。**AI に字幕を直させる用途と完全に噛み合う**ので、これを主経路にする。
- 言語は**率直に言って Python 推奨**。Rust の速度優位はこの構成ではほぼ出ない（後述）。
- 設計思想は一貫して「**原尺タイムラインを唯一の正本とし、中間 JSON を経由する。SRT/ASS/mp4/.wfp はすべてレンダリング結果**」。

---

## 1. 機能ごとの実現可能性

| 機能 | 判定 | 主な手段 | 根拠・注記 |
|---|---|---|---|
| 無音カット | **できる** | `auto-editor`（v31.4.2 / 2026-07-31、★4.7k、Unlicense） | dB/%閾値、前後非対称マージン、`--export v3` で JSON 出力→編集→再レンダリングの2段階が公式サポート。2025年に Python→Nim 全面書き換えで高速化済み |
| 字幕生成（ローカル日本語 ASR） | **できる** | `whisper.cpp`（Metal+CoreML、v1.9.1、★52.6k）または `mlx-whisper` | 15分素材で数十秒〜5分。**faster-whisper は Mac で GPU 不可**（CTranslate2 に Metal なし）＝非推奨 |
| カスタム辞書 | **工夫すれば** | 読みベース置換 + LLM 校正の多段 | `--prompt` は約224トークン上限かつ原則第1ウィンドウのみ＝辞書としては不十分。**Mac ローカルで辞書を正面サポートする ASR は事実上存在しない**。後処理で直すのが定石 |
| テロップ焼き込み | **できる** | ASS + libass（ffmpeg `subtitles` フィルタ）+ `pysubs2`（★436、2026-07-30 push） | スタイル9方向配置・縁取り・背景ボックスすべて表現可。ただし焼き込むと Filmora で直せない |
| トランジション | **できる** | ffmpeg `xfade`（56種、slide/wipe系あり）+ `acrossfade` | 3クリップ以上は offset の累積計算が必要。全体再エンコード確定 |
| 指定区間カット | **できる** | smart cut 方式（境界GOPのみ再エンコード + concat）を自作 | `-c copy` はキーフレーム単位でズレる。参照実装 `skeskinen/smartcut`（MIT）は**2026年2月に商用移行で開発停止**、アルゴリズムを参考に自作が筋 |
| 波形での目視検証 | **できる** | `ffmpeg showwavespic`（追加依存ゼロ）or BBC `audiowaveform`（v1.10.3） | クリック音自動検出は Essentia `ClickDetector` があるが **AGPLv3**。振幅差分の簡易自作を推奨 |
| テキストベース区間カット（将来） | **難しい（自作前提）** | — | `videogrep` は実質メンテ停止（最終 push 2024-04） |

---

## 2. Filmora 取り込みの制約と劣化ポイント

### 実機で確認できた事実（確度：高）
| 項目 | 結果 |
|---|---|
| タイムライン交換形式 | **XML / FCPXML / EDL / AAF / OTIO は登録ゼロ**。`Info.plist` の `CFBundleDocumentTypes` を直接確認 |
| 字幕 | **SRT / LRC のみ**。ASS・VTT・SSA 不可 |
| SRT の扱い | 取り込み後に本文・タイムコード・分割/結合・スタイル・位置を編集可、SRT 再エクスポート可 |
| 取り込み時の再エンコード | **起きない**。プロキシは別ファイルで、書き出しは常に原素材を使う |
| `.wfp` の中身 | 無圧縮 ZIP + JSON。時間単位は **100ns tick**（`speed.offset` 秒値と `inPoint` の照合、全体尺の一致、フレーム整列の3通りで裏取り） |
| 外部からの自動化 | **AppleScript 辞書なし・CLI なし**。フックはゼロ |
| クレジット | Speech-to-Text は **4クレジット/分**（15分＝60）。**Silence Detection は無料** |

### 劣化ポイントの整理
- **Filmora 側の取り込みで劣化しない**ため、CLI 側は無劣化・stream copy 前提で設計してよい。
- 劣化は **CLI の再エンコード段**でのみ発生する。カットは smart cut で境界のみに限定、焼き込みは CRF 18前後 / `-preset slow`。カットのみのパイプラインなら **ProRes 中間化は不要**（H.264 のまま渡すのが最速・最小）。色補正やフィルタを CLI で挟む場合のみ ProRes が効く。
- `.wfp` は**素材を内包せず絶対パス参照**。素材の移動・リネームでリンク切れ（同梱するなら `.wfpbundle`）。

### 未確認（要実機検証）
- **生成した `.wfp` を Filmora が開くか**（壊れたファイルを黙って落とすのか破損扱いにするのかも不明）。
- **テキスト／字幕クリップの `.wfp` スキーマ**。解析対象の5プロジェクトは映像・音声トラックのみで、テロップ入りサンプルが無かった。**カット部分は確定、テロップ部分は未確定**。
- 連番クリップをタイムラインに並べる順序の保証（公式に明記なし）。

---

## 3. 推奨する技術構成と言語

### Rust の判定：**割に合わない。Python を推奨**
理由は3点。①処理時間を支配するのは ffmpeg / whisper.cpp のネイティブ実行で、**外部バイナリを呼んで JSON を受け渡す設計なら呼び出し側の言語は速度にほぼ効かない**。②周辺ライブラリが Python に厚い（`pysubs2` は活発、BudouX は公式実装あり、SudachiPy、LLM SDK）。Rust 側は `ass-rs` が★6でパース寄り、`libass-rs` は**2023年6月から3年更新なし**、`whisper-rs` は **GitHub 本体が 2025-07-30 にアーカイブ**（開発は Codeberg 移行、crates.io 配布は継続）。③Rust ffmpeg バインディングは libav 開発ヘッダのリンクが必要でビルド複雑度が上がる。

ただし**重要な但し書き**：`auto-editor` が 2025年に Python→Nim へ全面書き換えした理由は、**フレーム/サンプル単位の解析ループを Python 自身で回していた**こと。つまり「自前で信号処理ループを書く」実装だけは言語を問わず避けるべきで、無音検出は ffmpeg の `silencedetect`/`astats` か auto-editor に丸投げする。この方針を守る限り Python で問題ない。

### 自作 / 既存の切り分け
| レイヤー | 方針 | 使うもの |
|---|---|---|
| 無音検出・カット | 既存 | auto-editor（`--export v3`） |
| 文字起こし | 既存 | whisper.cpp CLI（サブプロセス）+ Silero VAD |
| 辞書・LLM 校正 | **自作** | 読み正規化（SudachiPy/pyopenjtalk）+ 編集距離 + Claude |
| 日本語行分割 | 既存 + 自作 | BudouX で文節境界 → 文字数/秒数制約は自前 |
| SRT/ASS 生成 | 既存 | pysubs2 |
| smart cut | **自作** | ffprobe + ffmpeg（smartcut のアルゴリズムを参考、コードは流用しない） |
| 中間 JSON スキーマ | **自作（核心）** | 独自定義。auto-editor v3 は NLE 構造で keep/cut リストと設計思想が違うため変換層を挟む |
| `.wfp` 生成 | **自作（要スパイク）** | ZIP(store) + JSON。参考実装のコードはコピー不可（ライセンス無し＝全権利留保） |

---

## 4. パイプラインの処理順序

**推奨：原尺を正本とし、カットは「削除区間リスト（EDL）」として持ち、字幕タイムスタンプを写像する。**

```
素材（原尺）
 ├─ VAD で発話区間検出（ASR と無音カットで共有）
 ├─ 原尺のまま ASR → transcript.json（timestamp は原尺）
 │    ├─ 辞書ルール置換（決定的・冪等）
 │    ├─ LLM 校正（text のみ・差分適用・timestamp 不変を強制）
 │    └─ 行分割（BudouX + 文字数/秒数制約）
 ├─ 無音検出 → cuts.json（削除区間リスト）
 └─ 合成 → 出力
      ├─ カット済み mp4（smart cut）
      ├─ subtitles.srt（カット後タイムラインに写像）→ Filmora へ
      ├─ subtitles.ass + 焼き込み動画（確認用プレビュー）
      └─ project.wfp（スパイク成功後）
```

「先にカットしてから認識」は単純に見えるが、**(a) カット境界で語頭・語尾が切れて認識が落ちる、(b) Filmora 側でカット位置を微修正した瞬間に字幕を作り直す羽目になる、(c) 原尺↔完成尺の対応が失われて後から検証できない**、の3つの負債を抱える。

中間 JSON は `{segments:[{id, start, end, text, words:[...], source, edits:[]}]}` のように**編集履歴を持たせて再生成を冪等に**する。LLM の返答は「id 集合の一致・件数一致」を機械検証してから適用する（timestamp の捏造を防ぐ）。

---

## 5. 段階的ステップ案

| # | 内容 | 成果物 | 検証方法 |
|---|---|---|---|
| **0** | **`.wfp` スパイク（最優先・30分）** | 実機 `.wfp` を展開→`tlEnd` を1箇所書き換え→再 ZIP→Filmora で開く | 開けばこの経路が全設計を変える。開かなければ以降は「mp4 + SRT」に確定 |
| 1 | 環境構築とベンチ | `brew install ffmpeg-full`（**標準 `ffmpeg` に libass 無し**）、auto-editor、whisper.cpp | 15分素材で ASR 時間・カット時間を実測 |
| 2 | ASR → transcript.json | 正規スキーマ確定 | 自分の素材で large-v3 と turbo を比較（他人のベンチは矛盾していて当てにならない） |
| 3 | 辞書置換 + SRT 出力 | `subtitles.srt` | Filmora に取り込んで編集できるか実機確認。改行なし版と BudouX 改行版の両方を吐く |
| 4 | 無音検出 → cuts.json → smart cut | カット済み mp4 + 写像済み SRT | 波形 PNG でカット境界を目視、`acrossfade` 20〜50ms でぶつ切り対策 |
| 5 | LLM 校正の組み込み | Claude Code スキル化 | id/件数の機械検証を必ず通す |
| 6 | ASS テロップ + xfade | 焼き込みプレビュー | offset 計算は単体テストを厚めに |

各ステップの CLI は「JSON を読んで JSON を書く」形に統一し、`--dry-run` と `--stats` を持たせる。これが Claude Code スキルからの呼び出し口になる。

---

## 6. 未確認事項・確認推奨

1. **`.wfp` の生成互換性**（最重要）。作る前に **Filmora 14 か 15 かをバージョン固定**すること。実機は 14.10.5、最新は 15.6.12（2026-07-01）。14.4↔15 で構造一致は確認できているが、Wondershare は無告知で変えうる。
2. **`filename` の URI 形式が非標準**。実データは `file://Users/...`（スラッシュ2つ）。RFC 準拠で `file:///` と書くと読めない可能性が高い。**実測値をそのまま真似ること**。
3. **単位の罠**。唯一公開されている第三者パーサは 100ns tick を `_us`（マイクロ秒）と誤記している。参考にすると**10倍ずれる**。
4. **テロップ入り `.wfp` の再解析**。Filmora でテロップを1本入れて保存し、スキーマを取り直す必要がある。
5. **CoreText の bold 非対応**。macOS の libass は fontconfig 非依存で CoreText を使うため、`Bold: 1` が効かない事例あり。ウェイト違いのファミリー名直指定で回避（実機確認要）。
6. **Whisper のハルシネーション**。無音区間で「ご視聴ありがとうございました」等を生成する既知挙動。**VAD 前段化が必須**。また `tiny` より `base` の方が速いことがある（狭い復号器ほどループでトークンを浪費する）。
7. **単語 timestamp の信頼性**。whisper.cpp の word-level は experimental、日本語は漢字↔音素の対応が曖昧で強制アライメントが英語ほど効かない。**1文字カラオケ用途には危うく、セグメント境界の精緻化までに留めるのが安全**。

---

## 7. 調査間で食い違いがあった点

| 論点 | 食い違い | 統合判断 |
|---|---|---|
| Filmora の XML/EDL 対応 | 一方は「公式ドキュメントに記載なし＝推定で非対応（確度中）」、他方は「`Info.plist` 実機確認で登録ゼロ（確度高）」 | **実機確認を採用。非対応で確定** |
| `whisper-rs` のメンテ状況 | 「0.16.0（2026-03-12）で維持されている」vs「GitHub 本体は 2025-07-30 にアーカイブ」 | **両方正しい**。crates.io 配布は継続、開発は Codeberg 移行。issue/PR ベースの運用は不可 |
| 日本語 ASR のモデル優劣 | kotoba-whisper v2.0 は「large-v3 超え」（開発元）vs「WER 0.534 で最下位」（2026-02 第三者ベンチ、20サンプル） | **真っ向から矛盾。他人のベンチを採用せず、自分の素材で実測すること** |
| 言語選択 | Rust 推しの材料は見つからず全報告が Python 優位 | 一致。**ただし「自前ループを書かない」が前提条件** |

---

## 8. 主要参考URL

- [Filmora Mac 技術仕様](https://filmora.wondershare.com/mac-tech-spec.html) — 字幕は .srt/.lrc、ProRes 入出力
- [Filmora AI クレジット規定](https://filmora.wondershare.com/filmora-ai-credits-rule.html) — STT 4クレジット/分
- [Filmora プロキシ編集](https://filmora.wondershare.com/guide-mac/working-with-proxy.html) — 書き出しは原素材＝取り込み時に再エンコードなし
- [auto-editor](https://github.com/WyattBlue/auto-editor) / [v3 timeline 仕様](https://auto-editor.com/docs/v3) / [Nim 書き換え経緯](https://basswood.io/blog/nim-auto-editor-is-now-in-beta)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) / [faster-whisper の Metal 非対応 issue](https://github.com/SYSTRAN/faster-whisper/issues/515)
- [stable-ts（2026-05-30 アーカイブ）](https://github.com/jianfch/stable-ts) — 字幕整形の定番が停止、自前実装前提に
- [pysubs2](https://github.com/tkarabela/pysubs2) / [BudouX](https://developers-jp.googleblog.com/2023/09/budoux-adobe.html)
- [ffmpeg-full Homebrew formula](https://raw.githubusercontent.com/Homebrew/homebrew-core/master/Formula/f/ffmpeg-full.rb) — 標準 `ffmpeg` に libass が無いことの根拠
- [ffmpeg xfade / subtitles フィルタ](https://ffmpeg.org/ffmpeg-filters.html#xfade)
- [skeskinen/smartcut](https://github.com/skeskinen/smartcut) — smart cut アルゴリズム参照（2026-02 開発停止）
- [Netflix 日本語字幕ガイド](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215767517-Japanese-Timed-Text-Style-Guide) — 13全角/行・4文字/秒

---

**最後に一言**：初期解として「**無音カットは Filmora の Silence Detection（クレジット無料）に任せ、字幕だけ CLI で作って SRT を渡す**」という折衷が最もコスパが良い。CLI 側の投資は「日本語 ASR + 辞書 + 中間 JSON」に集中させ、`.wfp` 生成はスパイクの結果次第で判断するのが安全な進め方。
