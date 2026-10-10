# CLAUDE.md — Claude Code 向け指示

Claude Code はこのリポジトリで **実装・修正・テスト・レビュー** を担当する。
規則の正本は `AGENTS.md` と `core/` 配下であり、ここではそれに従うことと Claude Code 固有の点だけを書く。

## 必須

1. 作業前に `AGENTS.md` と `core/policy.md`、`core/quality.md`、`core/permissions.md` を読む。
2. タスク契約（JSON）で `assigned_agent` または `reviewer` が `claude-code` のタスクだけを扱う。
3. 自分が実装したタスクの品質判定（`quality.*.judged_by`）に自分を書かない。
4. 実装していない機能・検証していない項目を、完了済み・検証済みと報告しない。
5. コミット前に `bash scripts/check.sh` を実行し、`check: PASSED` を確認する。

## 権限

`.claude/settings.json` が破壊的操作と外部送信を制限している。制限を緩める変更は
人間の承認なしに行わない（`core/permissions.md`）。

## エージェントの実行（Phase 2）

- エージェントの起動は `scripts/aiwos.py` を通す（`core/execution.md`）。ドライランで内容を確認してから `--execute` を付ける。
- `--execute` はモデルを動かす。ユーザーの指示がある場合だけ実行する。
- `runs/` の記録と、契約の `evidence` / `history` を手で書き換えない。

## 人間に確認すること

- 受入基準・失敗条件・`max_retries` の変更
- 新しい外部サービスへのデータ送信、新しい AI の追加
- `passed` から `accepted` への遷移（人間のみ可能）
