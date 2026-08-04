# vidprep

YouTube 動画編集の下処理（無音カット・文字起こし・字幕生成）を半自動化する CLI。

実装中です。中間 JSON のスキーマ、プロジェクト作業ディレクトリ、CLI 骨格（`init` / `doctor`）と、音声前処理（`audio-fix`）から文字起こし（`transcribe`）、辞書校正（`correct`）、カット候補検出（`detect`）、カット適用と字幕出力（`render`）、レビュー用レポート（`report`）までの各処理段が動きます。

```bash
vidprep doctor          # 外部依存（ffmpeg / auto-editor / ASR ほか）を検査する
vidprep init ./work/talk01 --source ~/Movies/VID_20260507_144024.mp4
vidprep audio-fix --stats   # ノイズ抑制 → highpass 80Hz → loudnorm 2 パス
vidprep transcribe          # Silero VAD → ASR → transcript.json（原尺タイムスタンプ）
vidprep correct --dry-run   # 誤変換辞書の置換 diff を確認する（書き換えなし）
vidprep correct --apply-patch patch.json   # LLM 校正パッチを検証してから適用する
vidprep detect              # 無音 + フィラーのカット候補 → cuts.json（再実行で status 保持）
vidprep report --cuts       # 候補ごとに「消える発話 + 前後の文脈」を表示（status 判断の材料）
vidprep render --no-wrap    # approved カットを適用 → out/output.mp4 + out/subtitles.srt
vidprep render --preview    # telops.json + styles.json → out/telops.ass + out/preview.mp4
vidprep render --verify-asr # 出力を再文字起こしし、境界で消えた語を検出する（advisory）
vidprep report              # stats.json + 境界波形 PNG + boundary_digest.mp4 を再生成
just golden                 # ゴールデンサンプルで全段を通し fixtures/runs/<date>/ に保存
just golden-diff            # 直近 2 回のゴールデンランを diff する
```

設計は `docs/design.md`、検証計画は `docs/verification-plan.md`、調査根拠は `docs/research/feasibility-report.md` を参照してください。

[uv-template](https://github.com/tomada1114/uv-template) をベースに構築しています。
