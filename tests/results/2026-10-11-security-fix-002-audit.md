# テスト結果 — Security Fix 002 最終監査指摘（公開先判定）2026-10-11

- 実行者: Claude Code（実装者自身。独立レビューではない）
- 基点: PR #3 `security/fix-002-isolation`、修正前 `1be6b26`
- 環境: Linux コンテナ（root、Python 3.13.16、git 2.43.0）。GitHub Actions なし、モデル追加実行なし、実秘密情報での公開テストなし（sentinel のみ）

## 指摘の確認（1be6b26 を読んで確認）

| 指摘 | コード上の確認 |
|---|---|
| item 1: `remote_is_local()` が `remote.<name>.url` だけを見る | `git config --get remote.<name>.url` のみ。`pushurl`・複数 push 先・`insteadOf`・`pushInsteadOf` を無視。ローカル判定された remote が実際はネットワークに push しうる |
| item 4: 認証情報が同一 OS ユーザーから読める状態での AI 実行 | 実行の可否は隔離確認と無関係に進んでいた。`publish: none` やローカル宛てでは隔離未確認でも AI が動き、同一ユーザーからトークンを読めた |

## 回帰テスト（修正前に失敗 → 修正後に成功）

### item 1: 実効 push 先（`tests/test_isolation.py::EffectivePushUrls`、10 件）

`git remote get-url --push --all` を使い、全 push 先がローカルのときだけローカル判定（1 件以上必須、判定不能はフェイルクローズ）。

- ローカル url のみ / `file:///` → local
- ネットワーク url / scp 形式（`git@github.com:...`）→ not local
- `pushurl` がネットワーク（ローカル url を上書き）→ not local
- 複数 push 先の 1 つがネットワーク → not local
- `pushInsteadOf` でローカル→ネットワーク書換 → not local
- `insteadOf` でローカル→ネットワーク書換 → not local
- remote 未設定（push 先不明）→ not local（フェイルクローズ）

修正前のコード（`remote.<name>.url` だけ読む）をミューテーションで再現すると、上記のうち pushurl・複数・insteadOf・pushInsteadOf の 4 件が「ローカル」と誤判定され失敗する（＝指摘の再現）。

### item 4: 実行ゲート（`tests/test_isolation.py::Gates` 5 件、`tests/test_controller.py::ExecutionGate` 1 件）

- 認証情報なし・隔離なし → 実行可（スタブ試験が継続できる）
- `GH_TOKEN` が環境にある・隔離なし → **実行不可**（理由にトークン値は出さない）
- `credential_files` が存在・隔離なし → 実行不可
- 隔離確認済み → トークンがあっても実行可
- 隔離確認が古い（stale）・トークンあり → 実行不可
- 統合: 認証情報あり・ローカル remote・隔離なしで controller を起動 → タスクは waiting で保留、AI の CLI は一度も起動せず、publish もされない

## 全テスト

```
OK      contracts/task.example.json (DEV-0000, status=draft)
OK      examples/development/task-swap.json (DEV-0002, status=ready)
OK      examples/development/task.json (DEV-0001, status=review)
OK      examples/document-compare/task.json (DOC-0001, status=ready)
validate-task: 4 valid, 0 invalid
test_pr_creation_failed_after_push_is_retried_once_it_can_succeed (test_controller_audit.PullRequestRetry.test_pr_creation_failed_after_push_is_retried_once_it_can_succeed) ... skipped 'moved to tests/test_isolation.py: publish=github now requires verified isolation (Security Fix 002)'
Ran 154 tests in 300.181s
OK (skipped=1)
scan-secrets: OK (87 file(s) checked)
check: PASSED
```

154 件成功、1 件 skip（既存の移動分）。既存テストは無修正で通過（スタブ harness からは認証情報を外したため、認証情報なしの経路として継続）。

## ミューテーション確認（手動、その後戻した）

| 無効化 | 結果 |
|---|---|
| 実効 push 先の「全件ローカル」判定を「先頭だけ」に | `test_one_of_several_pushurls_network_is_not_local` 失敗 |
| 実効 push 先の取得を旧実装（`config --get remote.url`）に戻す | insteadOf / pushInsteadOf / pushurl / 複数 の 4 件が失敗（指摘の再現） |
| 実行ゲートのフェイルクローズを無効化 | `Gates` 3 件 + `ExecutionGate` 失敗 |

## 残存リスク

- 認証情報の検出は、`GH_TOKEN`/`GITHUB_TOKEN`/`GH_ENTERPRISE_TOKEN` 環境変数と `credential_files` に挙げたファイルの存在で判定する。
  git の credential helper や `~/.git-credentials`・`~/.config/gh/hosts.yml` 等は、`credential_files` に列挙しない限り検出されない
  （本番配置ではこれらを列挙することを `docs/controller.md` に記載）。
- 実効 push 先の判定は `git remote get-url --push --all` に依存する。git のバージョンでこの挙動が変われば要再確認（2.43.0 で確認）。
- 実 VPS の権限分離・実 systemd・実 GitHub への REST PR 作成・Codex 実行は引き続き未検証（この環境では確認不能）。
