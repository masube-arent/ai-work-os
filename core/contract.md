# タスク契約（Task Contract）

すべての作業は、着手前にタスク契約（JSON）として定義する。
構造は `contracts/task.schema.json`、横断的な規則は `scripts/validate-task.py` が検査する。

```bash
python3 scripts/validate-task.py path/to/task.json
```

## フィールド

| フィールド | 内容 |
|---|---|
| `schema_version` | 契約の版。現在 `"0.1"` |
| `task_id` | `DEV-0001` 形式。再利用しない |
| `task_type` | PROFILE 名。`profiles/<task_type>.md` が存在すること |
| `objective` | 目的（何を・なぜ） |
| `inputs` | 入力データ。`kind`（file/text/url/reference）と `sensitivity` を持つ |
| `constraints` | 守るべき制約 |
| `acceptance_criteria` | 成功条件。品質軸（`dimension`）と検証方法（`verification`）を持つ |
| `failure_conditions` | 失敗条件と、発生時の扱い（rework/escalate/cancel） |
| `assigned_agent` | 実行担当（chatgpt / claude-code / codex-cli / human） |
| `reviewer` | 検証担当。`assigned_agent` と異なること |
| `status` | 現在の状態（`core/state-machine.md`） |
| `evidence` | 実行証拠。判断ではなく事実（コマンド・終了コード・ファイル・差分） |
| `quality` | 3つの品質軸それぞれの判定（`core/quality.md`） |
| `retry_count` / `max_retries` | 再作業回数と上限（上限は最大3） |
| `escalation_reason` | エスカレーション理由。`escalated` のとき必須 |
| `history` | 状態遷移の記録（いつ・誰が・どこからどこへ） |

## 規則（validate-task.py が検査するもの）

構造（JSON Schema）:
- 必須フィールドの欠落、未知のフィールド、未知の状態・エージェント名を拒否する
- 受入基準・失敗条件は1件以上
- 検証方法が `command` の受入基準はコマンドを持つ
- `command_output` 証拠はコマンドと終了コードを持つ
- `PASS` 判定は1件以上の証拠と判定者を持つ
- `escalated` には理由がある
- `max_retries` は 3 以下

横断規則:
- `reviewer` ≠ `assigned_agent`
- `profiles/<task_type>.md` が存在する
- ID はタスク内で一意
- `file` 入力はリポジトリ相対パスで実在する。`confidential` 入力は `reference` のみ
- 証拠が参照する受入基準、判定が参照する証拠が実在する
- `assigned_agent` 自身による `PASS` 判定は無効（自己申告は合格にしない）
- `passed` / `accepted` は3軸すべて `PASS`、かつ全受入基準に証拠がある
- `retry_count ≤ max_retries`、`history` の `rework` 回数と一致
- `history` の遷移が状態遷移表に従う（`core/state-machine.md`）
- 認証情報らしき文字列を含まない

## 検査しないもの（限界）

- 証拠ファイルの中身が本物かどうか（`sha256` は任意項目で、Phase 1 では照合しない）
- 受入基準の文言が目的に対して十分かどうか（人間または ChatGPT のレビュー対象）
- 実際にそのエージェントが作業したかどうか（実行主体の認証は Phase 2 以降）
