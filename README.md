# vidprep

YouTube 動画編集の下処理（無音カット・文字起こし・字幕生成）を半自動化する CLI。

実装中です。現在は中間 JSON のスキーマ、プロジェクト作業ディレクトリ、CLI 骨格（`init` / `doctor`）、音声前処理（`audio-fix`）、辞書校正（`correct`）が動きます。

```bash
vidprep doctor          # 外部依存（ffmpeg / auto-editor / ASR ほか）を検査する
vidprep init ./work/talk01 --source ~/Movies/VID_20260507_144024.mp4
vidprep audio-fix --stats   # ノイズ抑制 → highpass 80Hz → loudnorm 2 パス
vidprep correct --dry-run   # 誤変換辞書の置換 diff を確認する（書き換えなし）
vidprep correct --apply-patch patch.json   # LLM 校正パッチを検証してから適用する
```

残りの処理段（`transcribe` / `detect` / `render` / `report`）はサブコマンドとして登録済みですが中身は未実装です。設計は `docs/design.md`、検証計画は `docs/verification-plan.md`、調査根拠は `docs/research/feasibility-report.md` を参照してください。

[uv-template](https://github.com/tomada1114/uv-template) をベースに構築しています。
