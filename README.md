# AI WORK OS

GitHub を共通の情報基盤として、ChatGPT・Claude Code・Codex CLI・人間が同じルールで協調するための
**モデル非依存の業務基盤**。業務ルール・品質基準・実行記録をリポジトリに置き、
実行主体（AI）を交換しても同じ契約・同じ品質ゲートで仕事を回せるようにする。

> 現在のバージョン: **v0.1（Phase 1: 土台）**。AI の自動起動・自動連携はまだ実装していない。
> 何が実装済みで何が未実装かは [docs/architecture.md](docs/architecture.md) と [docs/roadmap.md](docs/roadmap.md) を参照。

## 考え方

1. **GitHub が正本** — 仕様・タスク契約・判断履歴はここにあるものが正しい。
2. **成功・失敗条件は実行前に決める** — タスク契約（JSON）に書いてから着手する。
3. **自己申告は合格ではない** — 実装者と検証者を分け、証拠のない合格はスキーマで拒否する。
4. **品質は3軸で判定** — 仕様適合性 / 正確性 / 完成度。検証できないものは `UNVERIFIED`。
5. **自動修正には上限** — `max_retries`（最大3）を超えたら人間へエスカレーション。
6. **最終判断は人間** — `accepted` に遷移できるのは `human` だけ。

## 構成

```
README.md, AGENTS.md, CLAUDE.md   入口・AI向け作業規則
core/          共通OS: policy / quality / contract / permissions / state-machine
profiles/      業務別ルール: development, document-compare, drawing-analysis(雛形), data-analysis(雛形)
contracts/     task.schema.json（タスク契約の構造）, task.example.json（起票テンプレート）
agents/        routing（担当の決め方）, claude, codex
scripts/       doctor.sh, validate-task.py, render-brief.py, scan_secrets.py, check.sh
examples/      development/（DEV-0001）, document-compare/（DOC-0001）
tests/         unittest（55件）, fixtures/invalid-task.json, results/（実行記録）
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

### 5. コミット前の品質ゲート

```bash
bash scripts/check.sh
```

全タスク契約の検証・ユニットテスト・秘密情報スキャン・環境診断を実行する。`check: PASSED` 以外ではコミットしない。

## サンプルタスク

| ID | 種別 | 内容 | 担当 → レビュー |
|---|---|---|---|
| DEV-0001 | development | `greet()` に日本語挨拶を追加 | claude-code → codex-cli |
| DOC-0001 | document-compare | 仕様書 v1/v2 の差異を一覧化 | codex-cli → claude-code |

どちらも同じタスク契約スキーマで検証される。どちらも状態は `ready`（未着手）であり、
**AI による実際の実行と `passed` までの到達はまだ行っていない**（Phase 2 の最初の検証項目）。

## 次の実装ステップ

1. Codex CLI を導入し、`doctor.sh` でヘルプ記載の実行方式・サンドボックス設定を確認、`agents/codex.md` を確定する
2. DEV-0001 を Claude Code で実装し、Codex CLI でレビューして、証拠付きで `passed` まで手動で進める
3. 非対話実行のラッパーを **ドライラン既定** で実装する（Phase 2）
4. 実行ログを `runs/` に保存し、`evidence.sha256` で照合する
5. 状態更新の CLI 化と自動再開（Phase 3）

詳細は [docs/roadmap.md](docs/roadmap.md)。
