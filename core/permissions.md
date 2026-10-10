# 権限と安全性（Permissions）

## 禁止事項（Phase 1）

| 禁止事項 | 文書上の規則 | 実行環境側の制限 |
|---|---|---|
| 本番環境への自動反映 | デプロイはタスクの対象外 | 本リポジトリにデプロイ設定・認証情報を置かない。GitHub Actions を持たない |
| 既存リポジトリの破壊的変更 | force push・履歴書き換え・一括削除の禁止 | `.claude/settings.json` で `git push --force` 等を deny、`git push` は都度確認（ask） |
| 機密情報の GitHub への保存 | 機密入力は `reference` のみ | `validate-task.py` が confidential 入力の実体保存と秘密情報らしき文字列を拒否。`scan_secrets.py` を pre-commit フックで実行 |
| 無制限の自動修正 | `max_retries` 上限 | スキーマで `max_retries ≤ 3`、`retry_count ≤ max_retries` を検証 |
| 未承認の外部サービスへのデータ送信 | 送信先は人間が承認 | `.claude/settings.json` で `curl` / `wget` を deny、WebFetch は ask |
| 認証情報のログ出力 | ログ・証拠に秘密情報を残さない | `doctor.sh` は認証の有無だけを表示し、トークンや CLI の出力本文を表示しない |

## 実行環境側の設定

### Claude Code

`.claude/settings.json`（このリポジトリに同梱）がプロジェクト単位の許可設定になる。
Claude Code はこのリポジトリ内で起動したときに読み込む。

### Codex CLI

Codex CLI はこの初期構築環境にインストールされておらず、オプションを実機で確認していない。
サンドボックス・承認モードの指定方法は `codex --help` と公式ドキュメントで確認してから使う
（`scripts/doctor.sh` が `--help` に記載された実行方式を表示する）。
確認するまでは Codex CLI を **読み取り専用のレビュー用途** に限定する運用とする。

### Git フック

```bash
git config core.hooksPath .githooks   # コミット前に scan_secrets.py を実行
```

## 機密度の区分

| `sensitivity` | 例 | リポジトリへの保存 |
|---|---|---|
| `public` | 公開仕様、サンプル | 可 |
| `internal` | 社内手順書（顧客情報なし） | 非公開リポジトリに限り可 |
| `confidential` | 顧客データ、契約書、図面、認証情報 | 不可。`reference`（場所の記述）のみ |

## 限界

設定ファイルによる制限は、そのツールが設定を読み込む場合にのみ有効である。
OS レベルの隔離（コンテナ、専用ユーザー、ネットワーク制限）は Phase 2 で設計する。
