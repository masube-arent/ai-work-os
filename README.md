# AI WORK OS

GitHub を共通の情報基盤として、ChatGPT・Claude Code・Codex CLI・人間が同じルールで協調するための
**モデル非依存の業務基盤**。業務ルール・品質基準・実行記録をリポジトリに置き、
実行主体（AI）を交換しても同じ契約・同じ品質ゲートで仕事を回せるようにする。

> 現在のバージョン: **v0.3（10 ワーカー継続運転の controller）**。Claude Code の実行は確認済み、
> Codex CLI は導入済みだが未ログインのため実行していない。GitHub Actions は使わない。
> 何が実装済みで何が未実装かは [docs/architecture.md](docs/architecture.md) と [docs/roadmap.md](docs/roadmap.md) を参照。

## 考え方

1. **GitHub が正本** — 仕様・タスク契約・判断履歴はここにあるものが正しい。
2. **成功・失敗条件は実行前に決める** — タスク契約（JSON）に書いてから着手する。
3. **自己申告は合格ではない** — 実装者と検証者を分け、証拠のない合格はスキーマで拒否する。
4. **品質は3軸で判定** — 仕様適合性 / 正確性 / 完成度。検証できないものは `UNVERIFIED`。
5. **自動修正には上限** — `max_retries`（最大3）を超えたら人間へエスカレーション。
6. **最終判断は人間** — `accepted` は本人確認された人間の承認でのみ到達できる。
   本人確認は未実装のため、現在は `--by human` を含むすべての経路で拒否される（[docs/human-approval.md](docs/human-approval.md)）。

## 構成

```
README.md, AGENTS.md, CLAUDE.md   入口・AI向け作業規則
core/          共通OS: policy / quality / contract / permissions / state-machine / execution
profiles/      業務別ルール: development, document-compare, drawing-analysis(雛形), data-analysis(雛形)
controller/    v0.3: 10 ワーカーの controller（SQLite WAL キュー、systemd 常駐）
config/        controller.example.json
deploy/        systemd/aiwos-controller.service
contracts/     task.schema.json（タスク契約 0.2）, run.schema.json（実行記録）, review.schema.json（レビュー出力）,
               task.example.json（起票テンプレート）
agents/        routing（担当の決め方）, claude, codex
scripts/       aiwos.py（ランナー）, adapters.py（CLI ごとの差異）, doctor.sh, validate-task.py,
               render-brief.py, scan_secrets.py, check.sh
examples/      development/（DEV-0001, 担当入替の DEV-0002）, document-compare/（DOC-0001）
runs/          実行記録（run.json・プロンプト・出力・差分・検証ログ。SHA-256 で照合）
tests/         unittest（全テストでエージェントはスタブ。controller の負荷・障害試験を含む）, fixtures/, results/（テスト・実証実験の記録）
docs/          architecture, roadmap
.claude/settings.json   Claude Code の権限制限（deny / ask）
.githooks/pre-commit    コミット前の秘密情報スキャン
```

## セットアップ

必要なもの: `git`、`python3`（3.9 以上）、Python パッケージ `jsonschema`。
任意: `gh`（GitHub CLI）、`claude`（Claude Code）、`codex`（Codex CLI）。

```bash
git clone https://github.com/masube-arent/ai-work-os.git
cd ai-work-os
python3 -m pip install -r requirements.txt
git config core.hooksPath .githooks     # コミット前の秘密情報スキャンを有効化
bash scripts/doctor.sh                  # 環境診断
```

## 使い方

### 1. 環境を診断する

```bash
bash scripts/doctor.sh
```

必須項目（git / python3 / jsonschema）が揃っていれば exit 0。
gh の認証状態、Claude Code・Codex CLI の有無と、`--help` に記載された実行方式を表示する。
トークンや認証情報は表示しない。AI セッションは起動しない。

### 2. タスクを起票する

```bash
cp contracts/task.example.json tasks/DEV-0002.json   # tasks/ は任意の置き場所
# objective, inputs, acceptance_criteria, failure_conditions, assigned_agent, reviewer を書く
python3 scripts/validate-task.py tasks/DEV-0002.json
```

項目の意味と検証規則は [core/contract.md](core/contract.md)。

### 3. AI に渡す（手動）

```bash
python3 scripts/render-brief.py examples/development/task.json                  # 実装者向け
python3 scripts/render-brief.py examples/development/task.json --role reviewer  # レビュアー向け
```

出力（Markdown）を Claude Code / Codex CLI / ChatGPT に貼り付ける。このスクリプトは表示するだけで、
AI の起動や外部送信は一切しない。

### 4. 結果を記録し、検証する

作業結果はタスク契約の `evidence`（実行事実）と `quality`（判定）に、状態遷移は `history` に追記し、
再度 `validate-task.py` で検証する。証拠のない `PASS`、実装者自身の `PASS`、
遷移表にない状態変更は拒否される。

### 5. エージェントを動かす（Phase 2）

```bash
python3 scripts/aiwos.py auth                                      # 使える CLI・認証状態
python3 scripts/aiwos.py run   examples/development/task.json --role worker              # ドライラン
python3 scripts/aiwos.py run   examples/development/task.json --role worker --execute    # 実装（Claude Code）
python3 scripts/aiwos.py check examples/development/task.json --execute                  # 受入コマンドを実行
python3 scripts/aiwos.py run   examples/development/task.json --role reviewer --execute  # 独立レビュー（Codex CLI）
python3 scripts/aiwos.py judge examples/development/task.json --write                    # 3軸判定・状態遷移
python3 scripts/aiwos.py status examples/development/task.json
python3 scripts/aiwos.py metrics                                   # AI 別の性能記録
```

`--execute` / `--write` がなければ何も実行・変更しない。実行は隔離ワークスペースで行われ、
結果は `run/<task>/<run>` ブランチと `runs/` に記録される。停止条件・独立性の証明・測定項目は
[core/execution.md](core/execution.md)。

### 6. 継続運転（v0.3 controller）

最大 10 ワーカーが GitHub の main にある契約を取り、上の手順（work → check → review → judge）を
ローカルで実行して、結果をタスクブランチと PR で GitHub に返す。キューは SQLite（WAL）、
常駐は systemd。人間の判断が必要な段階（`passed` など）で止まり、マージも `accepted` もしない。

```bash
python3 -m controller check-config --config config/controller.example.json
python3 -m controller sync-once    --config /etc/aiwos/controller.json   # 同期のみ
python3 -m controller run          --config /etc/aiwos/controller.json   # 常駐（通常は systemd から）
python3 -m controller status       --config /etc/aiwos/controller.json
```

構成・制御条件・配置手順・残課題は [docs/controller.md](docs/controller.md)。

### 7. コミット前の品質ゲート

```bash
bash scripts/check.sh
```

全タスク契約の検証・ユニットテスト・秘密情報スキャン・環境診断を実行する。`check: PASSED` 以外ではコミットしない。

## サンプルタスクと実証実験（Phase 2）

| ID | 種別 | 内容 | 担当 → レビュー | 状態 |
|---|---|---|---|---|
| DEV-0001 | development | `greet()` に日本語挨拶を追加 | claude-code → codex-cli | `review`（Claude 実装・機械検証 2/2 PASS、Codex レビュー未実施） |
| DEV-0002 | development | DEV-0001 と同一課題・担当入替 | codex-cli → claude-code | `ready`（Codex 未ログインで停止） |
| DOC-0001 | document-compare | 仕様書 v1/v2 の差異を一覧化 | codex-cli → claude-code | `ready`（未着手） |

すべて同じタスク契約スキーマで検証される。記録は `runs/`、経緯と数値は
[tests/results/2026-10-10-phase2.md](tests/results/2026-10-10-phase2.md)。
**`passed` に到達したタスクはまだない**（独立レビューが未実施のため、完成度の軸が UNVERIFIED）。

## 次の実装ステップ

1. Codex CLI にログインし（`codex login --device-auth`）、`login status` と `--json` 出力の形式を確認して `adapters.py` の判定を確定する
2. DEV-0001 を Codex でレビューし（`aiwos.py run ... --role reviewer --execute` → `judge --write`）、`passed` まで進める
3. DEV-0002（Codex 実装・Claude レビュー）を DEV-0001 と同じ基点コミットから実行し、`aiwos.py metrics` で比較する
4. OS レベルの隔離（別ユーザー・コンテナ）と、GitHub の認証済み承認による `accepted`（[docs/human-approval.md](docs/human-approval.md)）

詳細は [docs/roadmap.md](docs/roadmap.md)。
