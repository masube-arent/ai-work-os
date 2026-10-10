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

`codex exec --help`（0.162.1）で確認した `-s read-only` / `-s workspace-write` をランナーが指定する
（reviewer は read-only、worker は workspace-write）。`danger-full-access` と
`--dangerously-bypass-approvals-and-sandbox` は使わない。サンドボックスの実際の制限範囲は
ログイン後の実行で確認するまで未検証（`agents/codex.md`）。

### ランナー（Phase 2）

`scripts/aiwos.py` が実行時に強制する制限（`core/execution.md` の停止条件）:

- 子プロセスへ渡す環境変数は許可リストのみ。`GH_TOKEN`・`ANTHROPIC_API_KEY`・`OPENAI_API_KEY` 等は渡さない
- 認証方式がサブスクリプションのログインでなければ実行しない（API キー課金の防止）
- 実行ごとの壁時計タイムアウト、1 コマンド 1 実行（ループなし）、`--execute` なしはドライラン
- 出力・差分・ログの秘密情報検査、`allowed_paths` 外の変更検査、レビュアーによる変更の検査。違反時は停止しエスカレーション
- ランナーは push・デプロイをしない。結果はローカルのブランチ `run/<task>/<run>` と `runs/` の記録に残すだけ

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
OS レベルの隔離（コンテナ、専用ユーザー、ネットワーク制限）は未実装（Phase 2 では論理分離まで。
`core/execution.md` の「作業環境の分離」）。
