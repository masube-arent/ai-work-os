# タスク契約（Task Contract）

すべての作業は、着手前にタスク契約（JSON）として定義する。
構造は `contracts/task.schema.json`、横断的な規則は `scripts/validate-task.py` が検査する。

```bash
python3 scripts/validate-task.py path/to/task.json
```

## フィールド

| フィールド | 内容 |
|---|---|
| `schema_version` | 契約の版。`"0.1"` または `"0.2"` |
| `task_id` | `DEV-0001` 形式。再利用しない |
| `task_type` | PROFILE 名。`profiles/<task_type>.md` が存在すること |
| `objective` | 目的（何を・なぜ） |
| `inputs` | 入力データ。`kind`（file/text/url/reference）と `sensitivity` を持つ |
| `constraints` | 守るべき制約 |
| `allowed_paths` | （0.2、任意）worker が変更してよいパスの接頭辞。外を変更した実行は停止する |
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

## 0.2 で追加した規則（Phase 2）

0.2 は任意項目の追加だけで、0.1 の契約はそのまま有効。

- `evidence` に `sha256` があれば `path` も必須。ディスク上のファイルからハッシュを再計算し、一致しなければ不正
- `passed` / `accepted` では、PASS 判定が引用する証拠はすべて `path` + 検証済み `sha256` を持つ
- `passed` / `accepted` では、実行記録（`run_record`）による独立性の証明が必要（`core/execution.md`）。
  レビュアーが `human` の場合は、本人確認の仕組みが未実装のため `passed` にできない（Security Fix 001）
- `accepted` は、本人確認された承認の証拠がない限り不正（現在は常に不正。`docs/human-approval.md`）
- 新しい実行主体 `aiwos-runner`（ランナー）。`produced_by` / `judged_by` / `history.by` に書けるが、担当にはなれない
- 受入基準に `expected_exit_code`（任意、既定 0）
- 証拠の種類に `run_record`（`runs/<task>/<run>/run.json`、`contracts/run.schema.json` 準拠）

## 検査しないもの（限界）

- 受入基準の文言が目的に対して十分かどうか（人間または ChatGPT のレビュー対象）
- `history` の `by` が本当にその実行者の操作かどうか。CLI は呼び出し元を認証しない。
  このため `accepted` と人間レビューは拒否し（フェイルクローズ）、それ以外の手動遷移には「未検証」と記録する
- 実行記録そのものがランナー以外の手で作られていないこと。ハッシュは改変を検出するが、最初から偽造された記録は検出できない
