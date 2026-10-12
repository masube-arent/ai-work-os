# 実行制御（Execution）— Phase 2

タスク契約からエージェントを動かし、何が実際に起きたかを記録する仕組み。
実装は `scripts/aiwos.py`（実行・記録・判定）と `scripts/adapters.py`（CLI ごとの差異）。

## 手順（すべて手動で1コマンドずつ実行する）

| # | 段階 | コマンド | 状態遷移 |
|---|---|---|---|
| 1-2 | タスク読み込み・契約検証 | すべてのコマンドが最初に `validate-task.py` と同じ検証を行う | — |
| 3 | 実行AI選択 | 契約の `assigned_agent` / `reviewer` で決まる（上書きオプションなし） | — |
| 4-5 | 作業開始・実行ログ保存 | `aiwos.py run TASK --role worker --execute` | ready/rework → in_progress → review |
| 6 | 機械的テスト | `aiwos.py check TASK --execute` | — |
| 7 | レビュー | `aiwos.py run TASK --role reviewer --execute` | — |
| 8-9 | 品質判定・結果記録 | `aiwos.py judge TASK --write` | review → passed / rework / escalated |
| 10 | 中断・再開 | `aiwos.py transition TASK --to suspended --by human` など | 遷移表に従う |
| — | 最終承認 | 本人確認された承認が必要。CLI からは拒否される（`docs/human-approval.md`） | passed → accepted（現在は不可） |

`--execute` / `--write` を付けない場合はドライラン。実行するコマンド・作業場所・プロンプトを表示し、
何も起動せず何も書かない。`aiwos.py status TASK` が現在の状態と次の操作を表示する。

## 1回の実行で記録されるもの

`runs/<task_id>/<run_id>/` に保存し、Git で管理する。

| ファイル | 内容 |
|---|---|
| `run.json` | 実行記録（`contracts/run.schema.json`）: 実行した CLI の実パスとバージョン、認証方式、argv、CLI が報告したモデル・セッション ID・ターン数、基点/対象/結果コミット、開始・終了時刻、終了コード、変更ファイル、各ファイルの SHA-256、セキュリティ検査の結果 |
| `prompt.md` | エージェントに渡したプロンプト（契約から生成） |
| `agent-stdout.json(l)` / `agent-stderr.txt` | CLI の出力（上限 2 MB） |
| `patch.diff` | 実装者の変更差分（worker） |
| `review.json` | レビュー結果（reviewer、`contracts/review.schema.json` 準拠） |
| `checks.json` / `checks/<AC>.log` | 受入コマンドの実行結果（check） |

タスク契約の `evidence` には、これらのファイルへのパスと SHA-256 が追記される。
`validate-task.py` は毎回ディスク上のバイト列からハッシュを再計算し、一致しなければ不正とする。

## 独立性の証明

識別子（`assigned_agent` ≠ `reviewer`）が異なるだけでは独立とみなさない。`passed` には次がすべて必要:

1. 実装者の完了済み worker 実行記録があり、記録上の CLI が担当に対応する（claude-code → `claude`、codex-cli → `codex`）
2. レビュアーの完了済み reviewer 実行記録があり、**対象コミットが worker の結果コミットと一致**する
3. 両者の CLI 実体パスが異なり、CLI が報告したセッション ID が両方存在し、異なる
4. レビュアーが対象ツリーを変更していない（`git status` で確認）
5. コマンド検証のある基準は、同じ結果コミットに対する check 実行記録がある

ランナーもレビュー開始前に、worker と同じ実行ファイルでのレビューを拒否する。

## 作業環境の分離

- 各実行は `~/.cache/aiwos/workspaces/<run_id>/`（`AIWOS_WORKSPACES` で変更可）に、
  指定コミットのツリーを `git archive` で書き出した**新しい Git リポジトリ**で動く。
  `runs/` は含めず、元リポジトリのオブジェクトも共有しないため、他の実行のブランチや記録は Git 経由で見えない。
- worker の変更はランナーが `run/<task_id>/<run_id>` ブランチとしてコミットする。main には入らない。
- 担当入替の比較実験（DEV-0002）は、DEV-0001 と同じ `--base` コミットから開始する。

**限界:** これは論理的な分離であり、OS レベルの隔離ではない。同じマシン上の別パスにあるファイル
（元リポジトリの `runs/` など）を絶対パスで読むことは、CLI 側の権限設定が許せば可能である。
完全な隔離（別ユーザー・コンテナ）は未実装。

## 停止条件（処理を止め、契約をエスカレーションする）

| 条件 | 判定方法 | 結果 |
|---|---|---|
| 認証情報らしき文字列が出力・差分・ログにある | `scan_secrets.py` のパターン | `security_violation`。該当出力は保存しない（位置とパターン名だけ記録）。ブランチを作らない |
| `allowed_paths` 外、または `runs/` の変更 | 変更ファイル一覧と照合 | `scope_violation` |
| レビュアーが対象ツリーを変更した | `git status` / HEAD の比較 | `security_violation` |
| 時間切れ | 実行ごとの壁時計タイムアウト（worker 900 秒、reviewer 600 秒、check 300 秒/コマンド） | プロセスグループを停止、`timeout` |
| CLI 未導入・未ログイン・未確認の認証方式 | 実行前の認証確認 | `blocked`（契約は変更しない。記録のみ残す） |
| 契約の同時更新 | 読み込み時の SHA-256 と書き込み直前の値を比較、ロックファイル | 書き込まない（終了コード 4） |
| 同じタスクで別の実行が進行中 | `runs/<task>/.active` | 開始しない |
| 再作業の上限 | `retry_count` が `max_retries` に到達 | `rework` ではなく `escalated` |

ランナーはループしない。1 コマンドで 1 回だけ実行し、再試行は人間が次のコマンドを打つことで行う。

## 品質判定の規則（judge）

- コマンド検証の基準: check の終了コードだけで決める（期待値は `expected_exit_code`、既定 0）。
  レビュアーの意見で覆らない（機械的検証を優先）。
- `inspection` の基準: レビュアーの判定を使う。
- `human` の基準: 人間の記録があるまで `UNVERIFIED`。
- レビュアーが報告した blocker / high の欠陥は、その品質軸を `FAIL` にする。
- 3軸すべて PASS → `passed`。FAIL がある → `rework`（上限なら `escalated`）。それ以外 → `review` のまま。

## 性能測定

`aiwos.py metrics [--json]` が `runs/*/*/run.json` から集計する（記録が唯一の情報源）。

| 項目 | 出典 | 測定できない場合 |
|---|---|---|
| タスク種別・使用AI | run.json | — |
| モデル情報 | CLI 出力に含まれるモデル名（Claude Code: `modelUsage`） | `null`（推測しない） |
| 実行時間 | ランナーが計測した壁時計時間 | — |
| 修正回数 | 実行開始時の `retry_count` | — |
| テスト成功率 | 同じ結果コミットへの check の合格数/総数（受入コマンド単位） | check 未実行なら `null` |
| 品質欠陥数 | 同じ結果コミットへのレビューが報告した欠陥数（重大度別） | レビュー未実行なら `null` |
| 完了状態 | 実行の状態と、現在の契約の状態 | — |

`cost_usd_reported` は CLI が表示した参考値（Claude Code は定価ベース）であり、請求額ではない。
率は実行ごとに示し、異なるタスクの件数を合算しない。
