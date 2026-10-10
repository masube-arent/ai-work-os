# Claude Code

## 役割

初期ルーティングでは **主担当（実装）**。レビュアーにもなれる（担当は契約で決まる）。

## 実環境で確認したこと（2026-10-10、Claude Code 2.1.296）

| 項目 | 確認結果 | 確認方法 |
|---|---|---|
| 認証 | `oauth_token` / `firstParty`（サブスクリプションのログイン。API キーではない） | `claude auth status`（JSON） |
| 非対話実行 | `-p` + `--output-format json` が動作。結果 JSON に `session_id`、`modelUsage`（モデル別トークン数）、`num_turns`、`is_error`、`total_cost_usd` が含まれる | 実行して出力を確認 |
| 構造化出力 | `--json-schema '<schema>'` で、結果 JSON の `structured_output` に入る | 実行して確認 |
| 権限制御 | `--tools`、`--allowedTools`、`--disallowedTools`、`--permission-mode`（acceptEdits など）、`--strict-mcp-config`、`--no-session-persistence` | `--help` |
| 環境変数を絞った実行 | PATH・HOME・プロキシ・CA 証明書だけの環境で認証・実行できる。親セッションとは別のセッション ID が報告される | 実行して確認 |

`total_cost_usd` は定価ベースの参考値として出力されるもので、サブスクリプション利用時の請求額ではない。

## ランナーが使う起動方法（`scripts/adapters.py`）

```
worker:   claude -p --output-format json --no-session-persistence --strict-mcp-config
                 --disallowedTools "Bash(git push:*),Bash(git remote:*),Bash(curl:*),Bash(wget:*),WebFetch,WebSearch"
                 --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep,Bash
                 --allowedTools "Bash(python3 -m unittest:*)"
reviewer: claude -p --output-format json --no-session-persistence --strict-mcp-config
                 --disallowedTools ... --tools Read,Glob,Grep --json-schema <contracts/review.schema.json>
```

- プロンプトは標準入力で渡す。作業ディレクトリは隔離ワークスペース。
- レビュアーは読み取り系ツールのみで、ファイルを変更できない。
- 認証方式が `oauth_token` 以外なら実行を拒否する（API キー課金を避けるため）。
- 子プロセスには許可リストの環境変数だけを渡す（`GH_TOKEN`、`ANTHROPIC_API_KEY` 等は渡さない）。

## 作業規則

`CLAUDE.md` と `AGENTS.md`。ワークスペースに含まれる `.claude/settings.json` の deny / ask も適用される。
