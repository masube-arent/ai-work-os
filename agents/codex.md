# Codex CLI

## 役割

初期ルーティングでは **独立レビュー担当**。実装者にもなれる（担当入替の比較実験 DEV-0002）。

## 実環境で確認したこと（2026-10-10、codex-cli 0.162.1）

| 項目 | 確認結果 | 確認方法 |
|---|---|---|
| 導入 | npm パッケージ `@openai/codex@0.162.1` | `codex --version` |
| 認証 | **未ログイン**（`codex login status` → "Not logged in"、終了コード 1） | 実行 |
| ログイン方法 | `codex login`（ブラウザ）、`--device-auth`（デバイスコード）、`--with-api-key`、`--with-access-token` | `codex login --help` |
| 非対話実行 | `codex exec`。プロンプトは引数または標準入力（`-`） | `codex exec --help` |
| サンドボックス | `-s, --sandbox read-only / workspace-write / danger-full-access` | `codex exec --help` |
| 出力 | `--json`（JSONL イベント）、`-o FILE`（最終メッセージ）、`--output-schema FILE` | `codex exec --help` |
| その他 | `-C DIR`、`-m MODEL`、`--ephemeral`、`--ignore-user-config`、`codex review --base BRANCH` | `--help` |

**未確認:** ログイン済み状態の `login status` の文言、`--json` のイベント形式（セッション ID・モデル名・
トークン数の項目名）、サンドボックスの実際の制限範囲（ネットワーク・読み取り範囲）。
ログインはしていないため、Codex による実行は一度も行っていない。

## ランナーが使う起動方法（`scripts/adapters.py`）

```
worker:   codex exec -s workspace-write -C <ws> --json --ephemeral -o <run>/last-message.txt -
reviewer: codex exec -s read-only       -C <ws> --json --ephemeral -o <run>/last-message.txt
                     --output-schema contracts/review.schema.json -
```

- 実行前に `codex login status` を確認し、未ログインなら `blocked` として記録して停止する。
- 出力に "api key" を含むログイン状態は拒否する（API キー課金を避けるため）。"chatgpt" を含む場合のみ許可。
  それ以外の文言は「未確認」として拒否する。この判定はログイン済みの出力を観測して確定させる必要がある。
- セッション ID・モデル名は `--json` 出力に該当項目があれば記録し、なければ `null`。

## ログインする場合

```bash
codex login --device-auth   # 表示される URL とコードをブラウザで開き、ChatGPT アカウントでサインイン
codex login status          # 文言を確認し、agents/codex.md と adapters.py の判定を確定させる
```

認証情報は `~/.codex`（`CODEX_HOME`）に保存される。共有環境・一時コンテナでのログインは、
認証情報がその環境に残ることを了承したうえで行う。
