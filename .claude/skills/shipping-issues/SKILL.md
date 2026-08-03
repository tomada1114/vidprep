---
name: shipping-issues
description: vidprep のオープン Issue を依存関係・優先度から選定して実装し、PR 作成 → CI 緑 → マージまで進める（このリポジトリ専用の運用ルール込み。vidprep 内ではグローバル同名スキルより本スキルを優先）。並列可能な Issue は worktree で並列実行、人手作業が絡む Issue は単独実行して人手ステップ手前で停止する。引数なし = 1 バッチ（1 Issue または 1 並列セット）、"all" = 着手可能な Issue が尽きるまで、番号指定 = その Issue のみ。Use when Issue を進めて、Issue 消化、次のイシューやって、チケット消化、残ってる Issue をやって、実装してマージまで、ship issues, work through the issues.
---

# shipping-issues（vidprep）

メインコンテキストはオーケストレータに徹する: 選定・委譲・CI 監視・マージ判断・報告のみを行い、実装はサブエージェントに委譲する。サブエージェントからは要約（結論・変更点・AC 充足状況・未解決事項）だけ受け取り、生ログや全文 diff をメインに戻させない。

引数: なし = 1 バッチ / `all` = ready な Issue が尽きるまでバッチを繰り返す / 番号（`#5` 等）= その Issue のみ。

## Phase 0: 状況把握

1. main を最新化（`git pull`）。working tree が dirty なら停止して報告する
2. 依存解析を実行:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py
   ```
   ready（依存がすべて closed）、blocks（閉塞している下流）、can_parallel_with、human_keywords、open PR の一覧が JSON で得られる。スクリプトは本文の注記に含まれる `#N` も依存として拾う保守的な解析なので、`deps_open` が疑わしいときは `gh issue view <N>` で本文の Dependencies 節を確認して最終判断する
3. open PR に対応する Issue（ブランチ名 `feature/issue-<N>-*`）は進行中としてスキップする

## Phase 1: 選定

ready な Issue から次の優先度で選ぶ: (1) `foundation` ラベル → (2) blocks が多い（下流を最も解放する）→ (3) 番号が小さい。選定と理由を 1〜2 文で報告してから実行に移る。

**並列判断（デフォルトで検討する）**: ready が複数あり、本文の Can Parallel With と「ファイル接触面」の記述から作業ファイルが重ならないと確認でき、いずれも人手ブロッカーでない場合、最大 3 件を worktree 並列で進める。接触面が 1 ファイルでも重なる場合（例: 両方が `cli.py` を触る）は並列にせず順次にする。

**人手ブロッカー判断**: human_keywords が付いた Issue は本文を読んで 2 種に分ける:

- **工程の途中に人手が必須**（例: 人手リファレンスの作成、Filmora 実機での取り込み確認が完了条件の中核）→ 単独バッチで実行する。自動化できるところまで実装して Draft PR を作り、人手ステップの手前で停止して「tomada に何をしてほしいか」を具体的に報告する。マージしない
- **目視・試聴が実装後の最終確認**（AC の二層構成の人間側）→ 通常フローで進める。PR 本文に「人間の確認が必要な項目」としてチェックリストを転記し、CI 緑でマージし、報告に残す

## Phase 2: 実装（委譲）

**モデル選定**:

- 既定は **opus**: Issue 本文 + docs/design.md + docs/verification-plan.md で仕様が閉じる実装
- **fable** にするのは広い視野が要るとき: 要件・設計・検証計画そのものの見直しが絡む、複数 Issue や文書にまたがる整合判断が必要、Issue の前提が現状のコードと食い違っている
- fable に委譲した場合のみ、その内部で機械的な部分（一括置換、テスト追加、CI 緑化）を sonnet/haiku へ **1 段だけ**再委譲してよい。opus のサブエージェントは自己完結させる（委譲の往復コストが実行コストを上回る再委譲はしない）

**ブランチ**: 単独実行は main からブランチ（`feature/issue-<N>-<slug>`）。並列実行は worktree:

```bash
git worktree add ../vidprep-issue-<N> -b feature/issue-<N>-<slug>
# worktree 内で最初に: just install
```

**委譲プロンプト**は自己完結にする — 意図、読むファイル、作業場所、規約、出力契約の 5 点を必ず含める:

<example>
Issue #5 (audio-fix) をマージ可能な状態に実装する仕事です。レビューは CI と Issue の Acceptance Criteria で行われます。

先に読むもの:
1. `gh issue view 5` — 要件・AC・検証手順のすべて（EARS 形式、具体値つき）
2. docs/design.md §5.1 と docs/verification-plan.md §4 — Issue の「参照」欄にある該当節
3. AGENTS.md — リポジトリ規約

作業場所: ../vidprep-issue-5（ブランチ feature/issue-5-audio-fix、`just install` 済み）

規約: コード・コミット・PR は英語 / コミットは Conventional Commits / `just check` がパスすること / fixtures/ は git 管理外で CI からは参照できない（fixtures 依存のテストは存在チェックで skip させる）

PR まで作成すること。手順は .claude/skills/create-pr/SKILL.md に従い、本文に `close #5` と、AC の目視・試聴項目を「人間の確認が必要な項目」チェックリストとして転記する。

返答は次だけ: 結論 / 変更ファイル一覧 / AC 充足表（機械チェックは実行結果、目視項目は「未実施」と明記）/ 未解決事項。仕様の判断に迷った点は自分で決めずに未解決事項として返すこと。
</example>

## Phase 3: CI → マージ

1. `gh pr checks <PR番号> --watch` で CI を待つ
2. 失敗したら修正サブエージェント（**sonnet**）に委譲: 同一ブランチで、失敗ジョブ名とログの要点（`gh run view --log-failed` から抽出した該当行のみ）を貼って修正 → push させる。2 回連続で失敗したら **opus** に切り替える。計 3 回失敗したら停止して状況を報告する
3. 緑になったら `gh pr merge <PR番号> --squash --delete-branch`
4. worktree を使った場合は `git worktree remove ../vidprep-issue-<N>`、main を pull
5. `all` のときは Phase 0 に戻る（マージで closed になった Issue が新しい ready を解放する）

## 報告

バッチごとに: 選定した Issue と理由 / PR と CI 結果 / マージ有無 / 人間の確認・作業待ち項目（目視 AC、人手ステップ）/ 次に ready になった Issue。

## Critical Rules

- CI が緑でない PR をマージしない。`--no-verify` とプレーンな force push は使わない（guard hook でも遮断される）
- AC の目視・試聴項目を「済」として扱わない — 未実施と明記して人間に渡す
- 人手ブロッカーの Issue を並列バッチに混ぜない
- サブエージェントには必ずモデルを指定する（既定 opus）
