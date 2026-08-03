# vidprep

YouTube 動画編集の下処理（無音カット・文字起こし・字幕生成）を半自動化する CLI。

実装中です。現在は中間 JSON のスキーマ、プロジェクト作業ディレクトリ、CLI 骨格（`init`）が動きます。

```bash
vidprep init ./work/talk01 --source ~/Movies/VID_20260507_144024.mp4
```

各処理段（`audio-fix` / `transcribe` / `correct` / `detect` / `render` / `report`）と `doctor` はサブコマンドとして登録済みですが中身は未実装です。設計は `docs/design.md`、検証計画は `docs/verification-plan.md`、調査根拠は `docs/research/feasibility-report.md` を参照してください。

[uv-template](https://github.com/tomada1114/uv-template) をベースに構築しています。
