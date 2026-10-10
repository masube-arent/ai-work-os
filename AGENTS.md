# AGENTS.md — AI エージェント共通の作業規則

このファイルは Codex CLI など、`AGENTS.md` を読む AI エージェント向けの指示である。
Claude Code 向けの `CLAUDE.md` も同じ規則を参照する。正本は `core/` 配下。

## 作業の前に

1. `core/policy.md`、`core/quality.md`、`core/permissions.md` を読む。
2. 対象タスクの契約 JSON と `profiles/<task_type>.md` を読む。
3. 自分が `assigned_agent` か `reviewer` かを確認する。どちらでもなければ作業しない。

## 必ず守ること

- 受入基準・失敗条件を作業中に書き換えない。不適切なら `escalated` にして理由を書く。
- 実装者は自分の成果物に `PASS` を付けない。証拠（コマンド・終了コード・ファイル）を `evidence` に記録する。
- レビュアーは実装者の報告を信じず、検証コマンドを自分で実行する。証拠がなければ `UNVERIFIED`。
- `retry_count` が `max_retries` に達したら再作業せず `escalated` にする。
- 秘密情報（トークン、鍵、顧客データ）をコード・ログ・タスク契約・コミットに書かない。
- force push、履歴の書き換え、本番環境への反映、未承認の外部送信をしない。
- タスク契約を変更したら `python3 scripts/validate-task.py <file>` を実行する。

## ランナー経由で実行されるとき（Phase 2）

- 作業ディレクトリはタスク専用のコピーで、変更はランナーが差分として記録・コミットする。自分で commit / push しない。
- `allowed_paths` の外を変更すると実行は停止し、タスクはエスカレーションされる。
- レビュアーとして実行されたときは、ファイルを一切変更しない（変更すると停止する）。
- `runs/` の実行記録は証拠であり、編集しない。ハッシュで照合される。

## コミットの前に

```bash
bash scripts/check.sh
```

`check: PASSED` 以外ではコミットしない。
