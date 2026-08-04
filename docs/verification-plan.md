---
created: 2026-08-03
status: approved
---

# vidprep 検証計画

[design.md](design.md) と対を成す一級文書。各機能に「完了条件（DoD）/ 効果測定 / チェック項目 / 検証手順」を定義する。実装フェーズは各 Step の完了をここに照らして判定する。

## 1. 原則

1. **二層判定**: 数値化できる項目（尺、ラウドネス、字幕の欠落、CER 等）は機械チェック（コマンドで pass/fail、exit code 判定可能）。「間の自然さ」「声の質感」などの主観品質は tomada の目視・試聴が最終判定。機械チェックが通ってから人間が見る、の順に固定する
2. **ゴールデンサンプル固定**: すべての検証・効果測定・回帰確認は §2 の固定素材で行う。素材を変えた比較はしない
3. **検証器も検証する**: 機械チェックは「わざと壊した入力で fail すること」を確認してから信用する（§10 フォールトインジェクション）
4. **効果測定は before/after**: 各機能は「処理前後で何がどれだけ変わったか」を `report/stats.json` の数値で示す

## 2. ゴールデンサンプル

| 項目 | 値 |
|---|---|
| ファイル | `fixtures/raw/VID_20260507_144024.mp4`（git 管理外） |
| 出所 | `/Users/masuyama/Movies/Wondershare Filmora Mac/Recorded/VID_20260507_144024.mp4` からコピー（2026-08-03） |
| sha256 | `76d8ddd300d1cf12776a1a717cfecc318b4968ce75103d96e4d0f8c79aae1218` |
| 諸元 | 298.92 秒 / 31,152,621 bytes / H.264 1920x1080 25fps yuv420p 約 508kbps / AAC 44.1kHz stereo 320kbps |
| ラウドネス（実測） | integrated **-22.24 LUFS** / true peak -6.22 dBTP / LRA 7.4 |
| 無音（実測） | -40dB・0.5 秒以上の無音が **66 箇所、計 132.8 秒（全体の 44.4%）** |

- 消失時は上記出所から再コピーし sha256 を照合して復元する
- ラウドネスは目標 -14 LUFS まで 8dB 強の余裕があり正規化効果が測りやすく、無音率 44% はカット効果の測定に十分。**現状 1 本のみ**で、性質の異なる素材（低ビットレート収録・長尺）への偏り確認は将来サブサンプル追加で対応する（既知の制約）
- 派生資産の置き場所: 人手リファレンス等は `fixtures/expected/` に置く。**リポジトリは public であり、収録内容のテキストも公開情報になるため `fixtures/` 全体を git 管理外とする**（.gitignore 済み）。再作成手順が本書にあることをもって永続性とする

## 3. 共通検証インフラ

### 3.1 CER（文字誤り率）の計測規約

- ツール: `jiwer`（dev 依存）。`uv run python scripts/cer.py <ref> <hyp>` で CER・置換/欠落/挿入件数を出力する
- 正規化してから比較する: NFKC 正規化 → 空白・句読点（、。！？…）除去 → 英字は小文字化。数字の表記ゆれ（「3つ/三つ」）は正規化しない（辞書・校正の実力として測る）
- 正規化の実体は `vidprep._text.normalize()`（#11 で `scripts/cer.py` から移設。`scripts/cer.py` は同名で re-export しているので既存の import は変わらない）。再文字起こし照合（§8.1）はパッケージ同梱で動く必要があり、`scripts/` を import できないため。CER・ベンチ・照合の 3 者が同じ 1 関数を使い、規約が二重定義にならないようにする

### 3.2 人手リファレンスの作成（1 回だけの投資）

1. Step 1 のベンチで最良だったモデルの transcript を叩き台にする
2. ゴールデンサンプルを試聴しながら全文を人手修正し、`fixtures/expected/golden.reference.txt` に保存（発話内容のみ。フィラーは**発話どおりに残す**）
3. 別途、フィラー箇所に `[F]` マークを付けた `golden.reference-fillers.txt` を作る（フィラー検出の recall 測定用）
4. 想定工数 30〜60 分。以後全 ASR・校正・カット検証の正解として使い回す

### 3.3 before/after 比較

`vidprep report --json` の stats.json を処理段ごとに保存し、`scripts/compare_stats.py` で前回実行分と diff する。回帰確認（§11）もこの仕組みに乗せる。

## 4. 機能別検証: A. 音声前処理（audio-fix）

**完了条件（機械）**
- integrated loudness が **-14.0 ± 0.5 LUFS**、true peak **≤ -1.0 dBTP**（ffmpeg loudnorm 解析パスで検証）
- 処理前後の音声尺の差 **≤ 1ms**
- 無音区間の RMS（ノイズフロア）が処理前より**低下**していること（denoise 有効時）

**効果測定**: LUFS / TP / LRA / ノイズフロア RMS の前後比較表（stats.json）。ゴールデンの期待値: -22.24 → -14 LUFS。

**目視・試聴チェックリスト（tomada 最終判定）**
- [ ] 冒頭 30 秒: 声の自然さ（denoise のこもり・水中感がないか）
- [ ] 無音→発話の境界 3 箇所: ノイズゲート的な不自然な立ち上がりがないか
- [ ] 語尾 3 箇所: リバーブ様のアーティファクトがないか
- [ ] 全体を倍速で流し聴き: 音量の暴れ（ポンピング）がないか

**検証手順**
```
vidprep audio-fix --stats          # 実行 + 前後統計
ffmpeg -i audio/processed.wav -af loudnorm=I=-14:TP=-1:print_format=json -f null -
                                   # 独立系統での再測定（自己申告とのクロスチェック）
afplay audio/processed.wav         # 試聴
```

## 5. 機能別検証: B. 文字起こし（transcribe）

**完了条件（機械）**
- 人手リファレンス比 **CER ≤ 8%**（確定値。Step 1 実測ベンチ（§12.2）で採用した whisper.cpp large-v3-turbo + VAD の実測 CER は 4.94%。実行間のばらつきと、リファレンスが large-v3-turbo のドラフトを叩き台に作成されたことによる turbo 有利バイアス（§12.2 参照）を見込んで約 3pt のマージンを取った値を確定条件とする。旧暫定値 15% から更新）
- ハルシネーション 0 件: VAD 発話区間の外側で開始するセグメントが **0 件**（`report/vad.json` と突き合わせ）。既知の幻覚フレーズ（「ご視聴ありがとうございました」等のリスト照合）の非発話区間での出現 **0 件**
    - ただし whisper.cpp は検出区間を 0.2 秒の無音で連結して認識するため、その連結部に置かれた境界が元タイムラインの無音全体へ引き伸ばされて戻ることがある（#24、実測 0.119s → 1.300s）。**自身の 50% 以上が発話区間に重なるセグメントに限り**開始点を重なっている区間の先頭へ寄せ、警告 1 件として記録する（削除はしない）。発話に重ならないセグメントは従来どおり不変条件違反として exit 3
- transcript.json がスキーマ検証を通る。segments の時刻が単調・非負・素材尺以内

**完了条件（目視）**
- 無作為 10 セグメントのタイムスタンプが実際の発話と **±0.3 秒以内**（動画をシークして確認）

**効果測定**: CER と実行時間（素材尺に対する倍率）。VAD あり/なしのハルシネーション件数比較（VAD 必須判断の裏取り、Step 1 で 1 回だけ実施）。

**検証手順**
```
vidprep transcribe
vidprep report --json              # セグメント数・総発話時間
uv run python scripts/cer.py fixtures/expected/golden.reference.txt <transcript全文>
```

## 6. 機能別検証: C. 辞書校正（correct）

**完了条件（機械）**
- 辞書エントリの対象語がゴールデン transcript 中で **100% 訂正される**（リファレンスと辞書から正解位置リストを作って照合）
- 辞書適用が**冪等**: 2 回適用して 2 回目の変更が 0 件
- 全セグメントの id / start / end / 件数 / 順序が適用前後で**完全一致**（テキスト以外の diff が空）
- LLM パッチ検証が不正パッチを **reject する**（§10 のフォールトインジェクションで確認）

**効果測定**: CER の 3 点比較 — **ASR 素の出力 → 辞書適用後 → LLM 校正後**。各段の置換・修正件数。この 3 点が「辞書と校正にどれだけの価値があるか」の効果測定そのもの。

**目視**: 辞書適用の diff 全件（`correct` が表示する）を確認し、過剰置換（一般文脈の語を固有名詞化）が 0 件であること。

**検証手順**
```
vidprep correct --dry-run          # diff 表示 → 目視
vidprep correct && vidprep correct # 2回目の変更 0 件 = 冪等
# LLM 校正はスキル経由で patch.json 生成 → vidprep correct --apply-patch patch.json
uv run python scripts/cer.py ...   # 3 点測定
```

## 7. 機能別検証: D. カット候補検出（detect）

**完了条件（機械）**
- cuts.json がスキーマ検証を通る（区間妥当・approved 非重複）
- **発話衝突 0 件**: `reason: silence` の各カット区間と transcript 発話セグメントの重なりの合計が **1 カットあたり ≤ 0.2 秒**（パディングの食い込み許容分）。これは ASR とのクロスチェックであり最重要の安全網
- パディング遵守: 各カットの前後に profile の `pad_pre` / `pad_post` が確保されている（検出器出力との差分で機械確認）
- detect 再実行で、レビュー済み status（approved/rejected/manual）が**保持される**（マージ規則の検証）

**完了条件（目視・試聴）**
- フィラー候補の precision: 候補全件（または上限 20 件サンプル）を `report --cuts` の文脈表示で審査し、**8 割以上が「消してよい」**と判定できること
- フィラーの recall: `golden.reference-fillers.txt` の `[F]` マークと候補を突き合わせ、strong フィラーの検出率を記録（目標値は初回計測後に設定。v1 は precision 優先で recall は測定のみ）

**効果測定**: 候補数と削減見込み秒数の reason 別内訳。ゴールデンの期待値: 無音 132.8 秒（-40dB/0.5s 基準）に対しパディング適用後でどれだけ候補化されるか。

**実測（2026-08-04、auto-editor 29.3.1 / 既定 profile）**

| 項目 | 実測値 |
|---|---|
| 無音（`threshold=4%`、`--margin 0s`） | 検出 58 箇所 / うち `min_duration` 0.6s 以上 39 箇所 |
| パディング後 | 30 カット・**118.65 秒**（原尺 298.92 秒の 39.7%）、9 箇所は `min_cut_duration` 未満で破棄 |
| 発話衝突（transcript ∩ VAD） | 全 30 カットで **0.000 秒**（上限 0.2 秒） |
| 再実行（pad 0.3 → 0.25） | 30 件マッチ・status/note 保持、手書き `manual` 1 件保持、新規 2 件は `c0101`/`c0102`（id 再利用なし） |
| フィラー | 候補 **0 件**。ASR transcript に strong 辞書の語が 1 つも出現しない（`golden.reference-fillers.txt` の `[F]` は「そうですね」「はい」の 2 件で、strong 辞書の対象外） |

- フィラーの precision / recall はゴールデン 1 本では測定材料がない（strong フィラー 0 件）。この話者の実際のフィラーは「そうですね」「はい」型で、パッケージ辞書ではなく `<project>/dictionaries/fillers.json` での追加が想定用途になる。目視 AC（#10 完了後）は素材追加後に再実施する
- 「区間が無音カットをまたぐ transcript セグメント」は 35 件中 9 件（whisper.cpp の VAD 併用による end のずれ）。detect は警告のみで transcript を書き換えない

**検証手順**
```
vidprep detect
vidprep report --cuts              # 候補ごとの文脈表示 → フィラー審査
vidprep detect                     # 再実行 → status 保持を確認
```

## 8. 機能別検証: E. カット適用（render）

**完了条件（機械）**
- 尺の整合: `|出力尺 − (原尺 − approved カット総尺)|` **≤ 1 フレーム（40ms @25fps）**
- A/V 同期: 出力の音声ストリーム尺と映像ストリーム尺の差 **≤ 50ms**
- ラウドネス維持: 出力の integrated loudness が **-14.0 ± 0.5 LUFS**（audio-fix の効果がカットで壊れていないこと）
- **再文字起こし照合（§8.1）の境界欠落フラグ 0 件**（v1 は advisory 運用のため exit code には反映しない。昇格条件は §8.1 の結論を参照）
- SRT 写像整合（§9 の F と共通）: 写像で除外されたセグメントが report の警告と一致

**完了条件（目視・試聴）**
- `report/boundary_digest.mp4`（全境界 ±2 秒の連結動画）を通しで試聴し、全境界について: クリック音なし / 語頭・語尾の欠けなし / 「間」が不自然に詰まっていないか
- 出力冒頭・中間・末尾の各 30 秒で映像品質（ブロックノイズ・カクつき）を確認

**効果測定**: 尺削減率（ゴールデン期待値: 無音率 44% に対し **30% 前後の削減**を見込む。実測して基準化する）、カット数、処理時間。

### 8.1 再文字起こし照合（採用）

「文字起こしを先に作れば無音カットの検証に流用できる」という着想は**筋が良いと判断し、採用する**。根拠: 同一モデル・同一音声処理で原尺とカット後を比較するため、モデル固有の定常的な認識誤りは両側に等しく現れて相殺され、**差分がカット起因の欠落に集中する**。カット境界で語が切れる事故（このパイプライン最大のリスク）を、波形目視より高い網羅性で機械検出できる。

**手順**:
1. `out/output.mp4` を transcript と同一のバックエンド・モデル・VAD 設定で再 ASR する
2. 期待テキストを構築する: kept 区間の transcript テキストを連結し、`reason: filler` のカットに対応するフィラー語を除去したもの
3. §3.1 の正規化を両者に適用し、文字単位 diff（`difflib`）を取る
4. **欠落 hunk**（期待側にあり再 ASR 側にない、長さ ≥ 2 文字）のうち、その位置（セグメント時刻から逆写像で原尺に戻す）が**カット境界 ±2 秒以内**のものを境界欠落フラグとする
5. フラグ 0 件で pass。1 件以上は該当境界を boundary_digest で試聴して人間が最終判定
6. 参考値としてグローバル CER（期待 vs 再 ASR）も記録する（ASR 再現性ノイズの水準把握。フラグ判定には使わない）

**限界と運用**: ASR の非決定性・境界の音響変化により誤検知（false positive）は起こりうる。そのため v1 導入時は**警告扱い（advisory）で開始**し、§10 のフォールトインジェクション（故意の語中カット）で検出力を、正常カットで誤検知率を確認したうえで、pass/fail ゲートに昇格させる。コストは ASR 1 回分（ゴールデンで数十秒〜数分）で許容範囲。

**実装上の判断（#11）**:

- **欠落 hunk は `difflib` の `delete` オペコードのみ**を数える。`replace` は「再 ASR が何かを聞き取ったが読み違えた」＝本手法が相殺を狙っているモデルノイズそのものであり、「期待側にあり再 ASR 側にない」（§8.1-4 の文言）には当たらないため対象外とする。語中カットは音声そのものが消えるので純粋な削除として現れる（下記実測で確認）
- **グローバル CER は同じ diff アラインメントから算出**する（`delete`/`insert` はその長さ、`replace` は両側の長い方）。最小編集距離ではないが、報告する 2 つの数値が同一の比較を指すことを優先した。判定には使わない（REQ-007）ため精度要件はない
- **欠落位置は「期待テキストの文字インデックス → セグメント内で線形補間したカット後時刻 → `f⁻¹` で原尺時刻」**で求める。transcript に語単位のタイムスタンプが無い以上、セグメント内は等速発話と仮定するのが唯一誠実な近似
- **フィラー語の除去は辞書スキャン**（`_fillers.scan`）で行い、カットの `note` 文字列には依存しない。レビュアーが note を書き換えても期待テキストが変わらないようにするため
- **advisory / gate の切り替えは `profile.json` の `render.verify_asr_mode`**（既定 `advisory`）。`gate` では境界欠落フラグ 1 件以上で exit 3

**実測（2026-08-04、whisper.cpp large-v3-turbo + Silero VAD、#9 のゴールデン render を対象）**

| 条件 | 期待/再 ASR 文字数 | 欠落 hunk | 境界フラグ | 境界数 | グローバル CER | 所要 |
|---|---|---|---|---|---|---|
| 正常カット（approved 26 件） | 1074 / 1074 | 0 | **0** | 52 | 0.28% | 34.8s |
| 語中カット注入（`manual` 16.000–16.600、s0002「起動し**まして**、」を横切る） | 1074 / 1071 | 1 | **1** | 54 | 0.47% | 35.6s |

- 注入時のフラグ: `{"cut_id": "c9001", "src_time": 16.672, "missing": "まして", "len": 3}`。逆写像で戻した位置はカット終端 16.600 から 0.072 秒、欠落文字列もカットが消した語そのもので、**検出力・位置精度とも実証された**
- **誤検知率（REQ-013）= 0 / 52 = 0.000**（正常カット、実素材 1 本 1 回）。§10 ケース #2（合成素材のフォールトインジェクション）でも同様に検出 1 件・正常カット 0 件

**結論（2026-08-04、advisory 据え置き）**: 検出力・位置精度は実証され、誤検知率も現時点で 0 だが、根拠が**ゴールデン 1 本・各 1 回の実行**に留まる。本手法の既知の弱点である ASR の非決定性（同一入力を複数回走らせたときのばらつき）を一度も標本化していないため、v1 は **advisory のまま据え置く**。昇格条件を次のとおり定める:

1. 同一の正常 render に対する `--verify-asr` を **3 回以上**繰り返し、境界フラグが毎回 0 件であること（非決定性の標本化）
2. 性質の異なる素材（§2 の既知の制約: 現状ゴールデン 1 本）を**もう 1 本**追加し、そちらでも正常カットのフラグが 0 件であること

両方を満たした時点で `profile.json` の `render.verify_asr_mode` を `gate` に切り替え、本節を更新する。

**検証手順**
```
vidprep render --verify-asr        # 再文字起こし照合つき（advisory）
vidprep render --verify-asr --json # verify_asr セクションに flags / global_cer
vidprep report --json              # 尺・LUFS・写像警告の機械チェック
vidprep report                     # boundary_digest.mp4 再生成
open report/boundary_digest.mp4    # フラグが立った境界を試聴して最終判定
```

## 9. 機能別検証: F. 字幕・テロップ出力

**完了条件（機械）**
- SRT が pysubs2 でラウンドトリップ可能（parse → dump で情報欠落なし）
- **欠落 0 件**: 写像対象の全 kept セグメントが SRT に存在する（除外は §4 写像規則の警告リストと 1:1 対応）。**#11 で `render` に組み込み済み**: SRT を書いたあと書いたファイルを読み直し、写像が作ったエントリが時刻**とテキスト**の一致で 1 対 1 に見つからなければ exit 3（`vidprep.verify.missing_subtitle_entries`）。オブジェクトではなくファイルを parse するのが要点
- 時刻が単調増加・エントリ間重なりなし・`min_display`（0.8s）未満は警告リストと一致
- 行制約: 全エントリが `max_chars_per_line`（20 全角）× `max_lines`（2）以内。`max_cps`（8.0 文字/秒）超過は警告として列挙される
- 写像関数の property test（実装フェーズで pytest 化）: 任意のカット集合に対し単調性・連続性・総尺整合が成立

**完了条件（実機・目視）**
- [ ] subtitles.srt を **Filmora に取り込める**（文字化けなし・タイムコード一致）
- [ ] Filmora 上で文言・タイムコードを編集できる
- [ ] preview.mp4 でテロップが指定プリセットどおりに表示される（位置 9 方向・縁取り・**太字が効いているか** = CoreText 問題の実機確認）
- [ ] BudouX 行分割版と改行なし版を見比べ、行分割版の改行位置が不自然でない

**効果測定**: 警告件数（min_display / max_cps / 除外）の推移。行分割の有無の見比べ（主観）。

**検証手順**
```
vidprep render                     # subtitles.srt（+ --no-wrap 版）
vidprep render --preview           # preview.mp4
open -a "Wondershare Filmora Mac" # 実機取り込み → チェックリスト実施
```

## 10. 検証器の検証（フォールトインジェクション）

機械チェックは以下の「わざと壊した入力」で **fail する**ことを確認してから信用する。実装フェーズで `tests/fault_injection/` としてスクリプト化し、チェッカー自体の回帰テストにする。

| # | 壊し方 | fail すべきチェック |
|---|---|---|
| 1 | audio-fix をスキップした素材で render | ラウドネス検証（§8） |
| 2 | 発話中央を横切る `manual` カットを手書きした cuts.json | 再文字起こし照合の境界欠落フラグ（§8.1）+ 発話衝突チェック（§7） |
| 3 | SRT からエントリを 1 件手動削除 | 欠落 0 件チェック（§9） |
| 4 | 存在しない id / id 重複を含む LLM パッチ | correct のパッチ検証（§6） |
| 5 | 区間が重なる approved カットを手書き | cuts.json スキーマ検証（§7） |
| 6 | 素材ファイルを別動画に差し替え | マニフェストの sha256 検証 |

特に #2 は再文字起こし照合（§8.1）の**検出力の実証**であり、advisory → ゲート昇格の判断材料になる。

**実装（#11、2026-08-04）**: `tests/fault_injection/case01..case06_*.py`。各ケースは単体で走り（`uv run python -m tests.fault_injection.case02_midword_cut`）、壊し方と「何が捕まえたか」を印字する。同じ 6 件を `tests/test_fault_injection.py` が収集するので、チェックを弱めた変更はテストが落ちて気づける。素材・ffmpeg・whisper.cpp・auto-editor はいずれも不要（`tests/fault_injection/_harness.py` がプロセス境界で差し替える）ため CI でも走る。

| # | fail させたチェック | 実行結果 |
|---|---|---|
| 1 | ラウドネス検証（§8） | -22.24 LUFS の出力を refuse（`InvariantViolationError`） |
| 2 | 境界欠落フラグ（§8.1）+ 発話衝突（§7） | フラグ 1 件を検出（`{"cut_id":"c0003","missing":"作業状況"}` — cut_id と欠落文字列まで assert する）。発話衝突は §7 が `reason: silence` 限定のため `manual` カットは対象外 — 重なり 0.400s（上限 0.2s）を記録したうえで、同一区間を silence カットとして `detect.verify_speech` に与えると refuse することを確認（メッセージ中の cut_id と上限値も assert） |
| 3 | 欠落 0 件チェック（§9） | エントリを 1 件消した SRT に対し欠落セグメント `s0002` を報告 |
| 4 | correct のパッチ検証（§6） | 存在しない id と重複 id を**両方**列挙して refuse、適用 0 件 |
| 5 | cuts.json スキーマ検証（§7） | approved 同士の重なりを refuse |
| 6 | マニフェストの sha256 検証 | 差し替えを検出（`HashMismatchError`） |

- ケース #2 は同時に**正常カットでの誤検知率**（フラグ件数 / 境界数、REQ-013）を測って印字する。実素材での測定値は §8.1 の実測表を参照
- **ケース #2 が証明する範囲**: 再 ASR はフェイクなので、認識器そのものは試験対象ではない。証明されるのは「削除がフラグになり、正しいカットに帰属し、逆写像で 2 秒窓に収まる位置に置かれる」こと。**実素材で本当に語が消えること・正常 render で本当にフラグが 0 件であること**は §8.1 の実測表（実 whisper.cpp・実 render）が担当する。この 2 つは役割が違い、どちらか一方では足りない
- 各ケースは refuse された**メッセージの中身まで assert する**（`_harness.refusal()` が例外を捕まえて本文を返す）。§8 のようにチェックが 3 条件を 1 つの例外にまとめる箇所では、「何かで落ちた」ではケースが素通りしてしまうため。ケース #1 は加えて**対照実行**（同じ素材を -14.08 LUFS で render して通ること）を行い、「この fixture は常に落ちる」でないことを示す

## 11. 回帰確認の運用

- ゴールデンサンプルに対する全パイプライン実行（audio-fix → transcribe → correct → detect → render → report）を「ゴールデンラン」と呼び、**機能追加・パラメータ変更・依存更新のたびに実行**する
- stats.json と警告リストを前回のゴールデンランと diff し、意図しない変化（CER 悪化、削減率変動、警告増加）がないことを確認する。前回結果は `fixtures/runs/<date>/` にローカル保存（git 管理外）
- 実装フェーズで `just golden` ターゲットとして整備する

**実装（#11、2026-08-04）**

```
just golden        # scripts/golden_run.py — 6 段を順に実行し fixtures/runs/<date>/ へ保存
just golden-diff   # scripts/compare_stats.py — 直近 2 回を diff（前回なしなら「first run」で exit 0）
```

- 保存内容は `stats.json`（`report/stats.json` のコピー）、`warnings.json`（全段の警告）、`summary.json`（各段の `--json` 結果・所要秒・停止段）。同日 2 回目は `<date>-2` になり、上書きしない
- 段は**サブプロセスではなくライブラリ関数として**順に呼ぶ。失敗は例外のまま受け取れるので理由が欠けない。失敗した段でランは止まる（下流は上流の出力を読むため）が、**アーカイブは必ず書く** —「どこで何が理由で止まったか」こそ求められている出力だから
- diff の対象は stats.json の全数値（**警告リストは長さで比較**する。「max_cps 警告が 11 → 14」はこれで出る）＋ そのランの `render` が報告した `verify_asr` セクション。名前に warning / flag / error を含むパスが増えたときだけ ⚠ を付ける
- 素材・ffmpeg・whisper.cpp・auto-editor が要るため **CI では動かさない**（`just check` にも入れない）。ローカル運用のまま

**初回実行の記録（2026-08-04）**: `[2/6] transcribe` で停止。`1 segments start outside every detected speech region (s0022@222.390)` — 既知バグ #24 で、ハーネスがこれをそのまま記録して exit 2 を返すことを確認した（audio-fix は -22.24 → -14.04 LUFS で成功）。

**#24 修正後の記録（2026-08-04）**: `[3/6] correct` まで通過し、`[4/6] detect` で停止。transcribe は 46 発話区間 / 36 セグメント / 0.11x で成功し、s0022 は警告付きで区間先頭へ寄せられた（`222.390 → 223.270`、§5 の「区間外開始 0 件」は維持）。停止理由は #24 とは別件で、auto-editor 29.3.1 の `--export v3` が JSON として読めない出力を返すこと（`timeline_schema: Invalid JSON: expected value at line 1 column 3`）。変換層の更新が要る。

**#30 修正後の記録（2026-08-04、`fixtures/runs/2026-08-04-03/`）**: **6 段すべて完走**（exit 0）。REQ-020（全段の通し実行）と REQ-021（stats.json / warnings.json のアーカイブ）が実測で埋まった。

| 段 | 結果 | 所要 |
|---|---|---|
| audio-fix | -22.24 → -14.04 LUFS、TP -1.00 dBTP、deepfilternet、長さ差 0.0ms | 67.4s |
| transcribe | 46 発話区間 / 154.0s of 298.9s（silero-v5） | 38.4s |
| correct | 辞書で 8 セグメント更新 | 0.3s |
| detect | silence 26 カット / 121.4s（approved、min_cut_duration 未満で 9 件棄却） | 0.3s |
| render | `out/output.mp4` 178.36s（-120.6s）、長さ差 0.0ms、-14.03 LUFS | 64.4s |
| report | `report/stats.json`、境界 PNG 26 枚、boundary_digest 235.76s（期待 235.018s） | 55.3s |

- **再文字起こし照合（§8.1）は境界欠落フラグ 0 / 52 境界**（advisory）。グローバル CER は 7.99%（期待 1101 文字 / 再 ASR 1074 文字）で、#9 の 0.28% より高い。**間に `correct` が入ったため**で、辞書が原稿側だけを直す（「リズーム」→ `resume` 等）一方、再 ASR は同じ聞き違いを繰り返すぶん差分として残る。CER は判定に使わない参考値（REQ-007）であり、フラグは 0 件のまま
- 警告は 2 件: transcribe の s0022 寄せ（#24 の既知挙動）と、detect の「19 セグメントが無音カットをまたぐ」（設計どおり削除せず警告）
- 字幕警告は `max_cps` が s0011 の 1 件のみ（8.65 > 8.0）
- `just golden-diff` は前回ラン（`2026-08-04-02`）が `report` に到達していないため比較できない。次回ランからこのランが基準になる

## 12. Step 1（次セッション）: 環境構築 + ASR 実測ベンチ

### 12.1 環境構築チェックリスト

- [x] `brew install ffmpeg-full` 等で **libass 入り ffmpeg** を導入（`ffmpeg -filters | grep subtitles` で確認）
- [x] auto-editor 導入（`uv tool install auto-editor`）、`--export v3` の動作確認
- [x] whisper.cpp をビルド（Metal + CoreML 有効）+ 候補モデルの ggml を取得
- [x] `uv add mlx-whisper --group asr` 等で mlx-whisper 導入（比較用）
- [x] DeepFilterNet CLI 導入（失敗したら afftdn フォールバックで先へ進む）
- [x] jiwer を dev グループに追加
- [x] 完了判定: `vidprep doctor` が全項目 `ok`（exit 0）を返す

### 12.1.1 構築記録（2026-08-03、Apple M2 / macOS 26）

| 対象 | 版 | 導入手順 |
|---|---|---|
| ffmpeg / ffprobe | 7.1.1 | `brew install ffmpeg`（libass 入り。`ffmpeg -filters` に `subtitles` あり） |
| auto-editor | 29.3.1 | `uv tool install auto-editor` |
| whisper.cpp | 1.9.1 相当（master ビルド） | `brew install cmake` 後、`~/src/whisper.cpp` で CoreML エンコーダを生成し `cmake -B build -DWHISPER_COREML=1 -DGGML_METAL=1` → `~/.local/bin/whisper-cli` に symlink |
| ggml モデル | large-v3 / large-v3-turbo | HuggingFace `ggerganov/whisper.cpp` から `~/.cache/whisper.cpp/` へ取得（`VIDPREP_WHISPER_MODEL_DIR` で変更可） |
| mlx-whisper | 0.4.3 | `uv sync --all-groups`（`asr` グループ。Apple Silicon 限定のマーカーつき） |
| DeepFilterNet | 0.5.6 | GitHub Releases の `deep-filter-0.5.6-aarch64-apple-darwin` を `~/.local/bin/deep-filter` に配置 |
| SudachiPy 辞書 | core (20260428) | `sudachidict-core` を dev グループに追加 |
| jiwer | 4.0.0 | dev グループに追加 |

- CoreML エンコーダ（`ggml-large-v3-turbo-encoder.mlmodelc`）は ggml モデルと同じディレクトリに置く。`whisper-cli` の `system_info` に `COREML = 1` が出れば有効
- **エンコーダはモデルごとに必要**。CoreML 有効ビルドではエンコーダが無いモデルは `failed to load Core ML model` → `failed to initialize whisper context` で起動しない（実測）。現状 `~/.cache/whisper.cpp/` には turbo 用しか無いため、**large-v3 の行を埋めるには先に large-v3 用エンコーダを生成する**（`whisper.cpp/models/generate-coreml-model.sh large-v3`）
- VAD 比較（§12.2）には Silero の ggml が要る。`whisper.cpp/models/download-vad-model.sh` で取得してモデルディレクトリに置くか、`asr_bench.py --vad-model <path>` で渡す
- auto-editor 29 系は `--export v3` の出力拡張子を `.v3` に書き換える（`-o out.json` としても `out.v3` になる）。#8 の変換層はこれを前提にする

### 12.2 ASR ベンチ手順

1. 対象: ゴールデンサンプルの `audio/processed.wav` 相当（ベンチ時点では ffmpeg 手動実行で loudnorm まで済ませた音声。audio-fix 実装前でも実施可能にするため）
2. 人手リファレンスを作成（§3.2。最初のモデル出力を叩き台に）
3. マトリクスを実測（2026-08-04、`fixtures/bench/matrix.md` / `bench.json`）:

| モデル × バックエンド | CER | 実行時間（倍率） | ハルシ件数 | ピーク RSS |
|---|---|---|---|---|
| whisper.cpp large-v3 | 70.81%（非代表値。下記注記参照） | 0.37x | 4 | 3.8 GiB |
| whisper.cpp large-v3-turbo | 5.76% | 0.33x | 6 | 3.3 GiB |
| mlx-whisper large-v3-turbo | 7.32% | 0.16x | 3 | 1.6 GiB |
| kotoba-whisper v2.0 | unavailable（理由: `~/.cache/whisper.cpp` に `ggml-kotoba-whisper-v2.0.bin` が無く、ggml 変換手段が未提供のため。`whisper.cpp/models/convert-h5-to-ggml.py` は OpenAI 形式の checkpoint を前提としており kotoba-whisper（transformers 形式のみ配布）には使えない） |

   - 実行時間は `/usr/bin/time -l`（wall + peak RSS）。各 2 回走らせ 2 回目を採用（モデルロードのキャッシュ差を除外）
   - ハルシ件数: VAD なし素の実行で無音区間に生成されたセグメント数。**加えて VAD あり/なしを最良モデルで比較**し、VAD 前段必須の判断を実測で裏取りする
   - 上記はハーネスで自動化済み: `uv run python scripts/asr_bench.py <loudnorm 済み wav> --reference fixtures/expected/golden.reference.txt`。4 候補を各 2 回実行し、生ログを `fixtures/bench/<model>/run{1,2}.{json,time}` に残し、マトリクスと採用判定を `fixtures/bench/matrix.md` / `bench.json` に書き出す
   - 無音区間は ffmpeg `silencedetect`（-40dB / 0.5 秒以上、§2 の実測基準と同一）で取り、開始時刻が無音区間内にあるセグメントをハルシとして数える
   - VAD 比較は whisper.cpp の `--vad`（Silero の ggml をモデルディレクトリに置くか `--vad-model` で指定）で行い、VAD なし行と 2 行で出力する
   - 実行できない候補（ggml へ未変換の kotoba-whisper v2.0 など）は行を落とさず `unavailable (reason: ...)` として表に残す
   - `--reference` なしでも実行でき、その transcript が §3.2 の人手リファレンスの叩き台になる（CER 列は空欄のまま）

   **large-v3 の異常値について**: whisper.cpp large-v3（VAD なし）の transcript は終盤で同一文の repetition loop に陥り（`transcript.txt` が 2082 bytes と他候補の半分程度）、70.81% という CER は文字起こし精度としては非代表値。無音区間などで誘発される既知の whisper repetition 失敗モードであり、下記 VAD 比較の追加計測で解消することを確認した。

   VAD あり/なし比較（最良モデル + 異常値確認のための large-v3 追加計測）:

   | モデル × バックエンド | CER | 実行時間（倍率） | ハルシ件数 | ピーク RSS |
   |---|---|---|---|---|
   | whisper.cpp large-v3-turbo（VAD なし） | 5.76% | 0.33x | 6 | 3.3 GiB |
   | whisper.cpp large-v3-turbo（VAD あり） | 4.94% | 0.18x | 0 | 4.2 GiB |
   | whisper.cpp large-v3（VAD あり、追加計測） | 5.58% | 0.39x | 0 | 3.8 GiB |

   large-v3 を VAD ありで再計測すると repetition loop は解消し（transcript は他候補と同程度のサイズ、ハルシ 0 件）、クリーンな CER 5.58% が得られた。ただし large-v3-turbo（VAD あり）の 4.94% を上回るため、採用判定は変わらない。

4. 判定基準（tomada 承認、2026-08-04 更新）: **実行時間を問わず CER が最小の候補を採用する**（精度を速度より優先）。CER 差が四捨五入で小数第 2 位まで一致する場合のみ実行時間の短い方を採用する。ハーネスの自動出力は「CER 差 1pt 以内なら実行時間の短い方」という旧ルールに基づく参考情報であり、本判定では上書きする（ハーネス改修は別途）。#7 で VAD がデフォルト必須と定義されるため、判定は VAD ありの数値同士で行う: whisper.cpp large-v3-turbo（VAD あり、CER 4.94%）が whisper.cpp large-v3（VAD あり、CER 5.58%）を下回り最小。mlx-whisper large-v3-turbo（CER 7.32%、VAD 未計測）はいずれにせよ非採用
5. **決定**: **whisper.cpp large-v3-turbo + VAD（Silero）を採用**。design.md §1 の判断 1 と本書 §5 の CER 完了条件（15% 暫定 → 8% 確定）を更新済み

   **注記（リファレンスのバイアス）**: リファレンスは large-v3-turbo ドラフトを人手修正して作成したため、turbo に有利な方向のバイアスがありうる（tomada 了承済み）。

### 12.3 Step 1 の完了条件

- [x] 環境チェックリスト全項目が緑
- [x] ベンチ表が全欄埋まり、採用モデルと根拠が design.md に追記されている
- [x] 人手リファレンス 2 ファイル（§3.2）が fixtures/expected/ に存在する
