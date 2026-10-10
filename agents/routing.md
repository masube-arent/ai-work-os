# ルーティング（担当の決め方）

Phase 1 では **手動指定** を基本とする。タスク契約の `assigned_agent` と `reviewer` に人間が書く。
自動選択は Phase 5 で、実績データ（どのエージェントがどの task_type で何回 PASS/FAIL したか）が
溜まってから設計する。

## 指定できる担当

| 値 | 実体 | 主な用途 |
|---|---|---|
| `chatgpt` | ChatGPT（通常チャット） | 目的整理、要件・受入基準の作成、重大な仕様変更の評価 |
| `claude-code` | Claude Code CLI | 技術調査、実装、修正、テスト |
| `codex-cli` | Codex CLI | 代替実装、独立レビュー、原因分析 |
| `human` | 人間 | 最終承認、権限・外部送信・仕様変更の判断 |

## 既定の組み合わせ

| 局面 | assigned_agent | reviewer |
|---|---|---|
| 要件・受入基準の作成（draft → ready） | `chatgpt` | `human` |
| 実装 | `claude-code` | `codex-cli` |
| 代替実装（同じ課題を別実装で比較） | `codex-cli` | `claude-code` |
| 不具合の原因分析 | `codex-cli` | `claude-code` |
| 最終承認（passed → accepted） | — | `human`（遷移の実行者） |

組み合わせは固定ではない。条件は1つだけ: **`reviewer` は `assigned_agent` と異なる**（検証済み）。

## 接続についての前提

- ChatGPT の通常チャットと、Claude Code / Codex CLI は **自動接続されていない**。
  ChatGPT とのやり取りは、人間がタスク契約 JSON や `render-brief.py` の出力をコピーして渡す。
- Claude Code と Codex CLI の自動連携は Phase 2。Phase 1 では人間が各 CLI を起動し、
  `python3 scripts/render-brief.py TASK.json` の出力を渡す。

## 新しい AI を追加するとき

1. `contracts/task.schema.json` の `$defs.agent` に名前を追加する
2. `agents/<name>.md` に起動方法・権限設定・制約を書く（未確認の機能は「未確認」と書く）
3. `scripts/doctor.sh` に存在確認を追加する
4. テストを追加して `bash scripts/check.sh` を通す

業務ルール（`core/`）・PROFILE・既存のタスク契約は変更しない。
