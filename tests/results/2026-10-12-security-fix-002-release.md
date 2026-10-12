# テスト結果 — Security Fix 002 リリース準備（REST PR 回帰テスト・古い記述の修正）2026-10-12

- 実行者: Claude Code（実装者自身。独立レビューではない）
- 基点: PR #3 `security/fix-002-isolation`、`6b5ab69` の続き
- 環境: Linux コンテナ。GitHub Actions なし、モデル追加実行なし、実秘密情報での公開テストなし（偽 gh のみ）

## 背景

旧 `PullRequestRetry`（`tests/test_controller_audit.py`）は skip のままで、移設先に REST 方式の
PR 作成成功・重複防止・再試行テストが無かった。これを埋める。

## 追加テスト（偽 `gh api` = `tests/_fake_gh.py`、GraphQL 不使用・ネットワーク不使用）

### `tests/test_isolation.py::RestPullRequestGitops`（3 件、item 1）
`gitops.ensure_pull_request`（`gh api -X GET/POST .../pulls`）を直接検証。

- 既存 PR が無い → GET で確認 → POST で 1 件作成（呼び出し順 GET, POST を確認）
- 既存 PR がある → GET が URL を返す → **POST を呼ばない**（重複防止、POST はちょうど 1 回）
- POST 失敗 → `GitError` を送出

### `tests/test_isolation.py::RestPullRequestRetry`（1 件、item 1）
push 成功・PR 作成失敗の状態から、`controller.main.republish` が PR だけを再試行することを検証。

- 1 回目（偽 gh の POST を失敗させる）→ `pr_url` は未設定、`pr_error` を記録、POST 1 回
- 2 回目（回復）→ `pr_url` を記録、POST は計 2 回（再試行が成功）
- 3 回目 → 既に PR があるので **POST は増えない**（重複防止）

### `tests/test_isolation.py::RestPullRequestNotCalledWithoutIsolation`（1 件、item 2）
隔離未確認で `publish=github` のとき、`publish_allowed` が False（"fail closed"）で、`republish` が
`gh` を **一度も呼ばない**ことを検証（REST API 呼び出し自体が拒否される）。

## ミューテーション確認（手動、その後戻した）

| 無効化 | 失敗した試験 |
|---|---|
| `ensure_pull_request` の GET による重複防止 | `RestPullRequestGitops.test_does_not_create_a_second_pr` |
| `republish` の PR だけ再試行する分岐 | `RestPullRequestRetry.test_pr_failure_is_retried_then_deduped` |

## docs/controller.md などの古い記述の修正（item 3）

- `gh pr create`（GraphQL 方式）→ `gh api`（REST）に統一した記述へ
- 運用コマンドの `sudo -u aiwos` → `sudo -u aiwos-ctl`、認証情報の配置ユーザーも aiwos-ctl に
- 「OS ユーザー分離は未実装」→ 信頼済み（aiwos-ctl）と AI 実行（aiwos-agent）の分離は実装済みと明記。
  未実装なのはワーカーごとの AI ユーザー分割・コンテナ・ネットワーク制限、と限定
- 検証表の「`gh pr create` 未検証（GraphQL 遮断）」→ 偽 gh api での作成/重複防止/再試行は成功、
  実 GitHub への実呼び出しのみ未検証（REST 統一・GraphQL 非依存）と修正
- `core/execution.md` の「OS レベルの隔離ではない／未実装」→ Security Fix 002 の OS レベル分離を反映

## 全テスト

```
OK      contracts/task.example.json (DEV-0000, status=draft)
OK      examples/development/task-swap.json (DEV-0002, status=ready)
OK      examples/development/task.json (DEV-0001, status=review)
OK      examples/document-compare/task.json (DOC-0001, status=ready)
validate-task: 4 valid, 0 invalid
test_pr_creation_failed_after_push_is_retried_once_it_can_succeed (test_controller_audit.PullRequestRetry.test_pr_creation_failed_after_push_is_retried_once_it_can_succeed) ... skipped 'moved to tests/test_isolation.py: publish=github now requires verified isolation (Security Fix 002)'
Ran 159 tests in 290.601s
OK (skipped=1)
scan-secrets: OK (89 file(s) checked)
check: PASSED
```

159 件成功、1 件 skip（旧 `PullRequestRetry` は上記 3 クラスに置換。skip 理由に置換先を明記）。

## 未検証（この環境では確認不能・リリース後の VPS 限定実証で確認）

- 実 GitHub への `gh api` による PR 作成（この環境は GitHub API 遮断。方式は REST に統一済み）
- 実 VPS の権限分離（実ユーザー・sudoers・所有権）、実 systemd 下の起動
- Codex 実 CLI の隔離下実行

## リリース判定

- コード修正は完了。`bash scripts/check.sh` PASSED（159/1 skip）。
- 残るのは統合（PR #1 → #2 → #3）と VPS 限定実証（上記の未検証項目の現地確認）。いずれも人間の操作。
