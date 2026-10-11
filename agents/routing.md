# ルーティング（担当の決め方）

担当はタスク契約の `assigned_agent`（実装）と `reviewer`（独立レビュー）で決まる。
ランナー（`scripts/aiwos.py`）にはこれを上書きするオプションがない。担当を変えるときは契約を変える
（比較実験なら別の契約を起票する）。自動選択は Phase 5 で、実績データ（`aiwos.py metrics`）が溜まってから設計する。

## 初期ルーティング（Phase 2）

| 局面 | assigned_agent | reviewer |
|---|---|---|
| 実装（既定） | `claude-code` | `codex-cli` |
| 担当入替の比較（例: DEV-0002） | `codex-cli` | `claude-code` |
| 要件・受入基準の作成（draft → ready） | `chatgpt` または人間 | `human` |
| 最終承認（passed → accepted） | — | 本人確認された人間（未実装のため現在は不可） |

## 指定できる担当

| 値 | 実体 | ランナーで実行 |
|---|---|---|
| `claude-code` | Claude Code CLI | 可（`agents/claude.md`） |
| `codex-cli` | Codex CLI | 可（ログイン後。`agents/codex.md`） |
| `chatgpt` | ChatGPT（通常チャット） | 不可。自動接続なし。人間が brief を渡し、結果を記録する |
| `human` | 人間 | 不可。`produced_by: human` は自己申告で検証できないため、人間レビューでの `passed` は現在不可 |
| `aiwos-runner` | ランナー自身 | 担当にはなれない。機械的な手順（check、状態遷移）の実行者としてのみ記録される |

## 独立性

`reviewer` ≠ `assigned_agent` は必要条件にすぎない。`passed` には実行記録による証明が必要
（`core/execution.md` の「独立性の証明」）。

## 新しい AI を追加するとき

1. `contracts/task.schema.json` の `$defs.agent` に名前を追加する
2. `scripts/adapters.py` に Adapter を1つ追加する（`--help` と実行で確認したオプションだけを使う）
3. `scripts/validate-task.py` の `CLI_OF_AGENT` に実行ファイル名を追加する
4. `agents/<name>.md` に確認結果と未確認事項を書く
5. スタブ CLI のテストを追加して `bash scripts/check.sh` を通す

業務ルール（`core/`）・PROFILE・既存のタスク契約・ランナー本体は変更しない。
