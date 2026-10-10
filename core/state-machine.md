# 実行状態の管理（State Machine）

タスクの状態は `status` に、状態遷移の記録は `history` に保存する。
遷移表は `scripts/validate-task.py` の `TRANSITIONS` と同一であり、
両者がずれるとテスト（`tests/test_validate_task.py`）が失敗する。

## 状態

| 状態 | 意味 |
|---|---|
| draft | 起票中。受入基準が未確定でもよい |
| ready | 受入基準・失敗条件が確定し、着手可能 |
| in_progress | 担当エージェントが作業中 |
| review | 作業完了の申告後、レビュアーが検証中 |
| rework | レビューで不合格。再作業待ち（`retry_count` を1増やす） |
| passed | 3つの品質軸がすべて証拠付きで PASS |
| accepted | 人間が最終承認した（終端） |
| escalated | 人間の判断が必要（`escalation_reason` 必須） |
| suspended | 中断中。再開時は中断前の状態に戻る |
| cancelled | 取り消し（終端） |

## 遷移表

| 遷移元 | 遷移先 |
|---|---|
| `draft` | `ready`, `cancelled` |
| `ready` | `in_progress`, `suspended`, `cancelled` |
| `in_progress` | `review`, `escalated`, `suspended`, `cancelled` |
| `review` | `passed`, `rework`, `escalated`, `suspended`, `cancelled` |
| `rework` | `in_progress`, `escalated`, `suspended`, `cancelled` |
| `passed` | `accepted`, `rework`, `escalated` |
| `escalated` | `ready`, `cancelled` |
| `suspended` | `ready`, `in_progress`, `review`, `rework`, `cancelled` |
| `accepted` | （なし） |
| `cancelled` | （なし） |

## 機械的に検査される規則

1. `history` は `draft` から始まり、各遷移の `from` は直前の `to` と一致する。
2. 最後の遷移の `to` は現在の `status` と一致する。履歴が空なら `status` は `draft`。
3. `rework` への遷移回数は `retry_count` と一致し、`retry_count ≤ max_retries ≤ 3`。
   上限に達したら `rework` ではなく `escalated` にする。
4. `suspended` からの再開は、中断直前の状態へ戻る（または `cancelled`）。
5. `accepted` へ遷移できるのは `human` だけ。
6. 時刻はタイムゾーン付き ISO 8601 で、逆行しない。

## 中断と再開

状態はすべてタスク契約 JSON に入っており、外部データベースを持たない。
作業を再開するエージェントは、タスク JSON・`history`・`evidence` を読めば
どこまで進んだかを把握できる。セッションやモデルが変わっても同じ手順で再開する。

Phase 1 では状態の更新は手動（JSON を編集して `validate-task.py` で検証）。
自動永続化と自動再開は Phase 3 で扱う。
