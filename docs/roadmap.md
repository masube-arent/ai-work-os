# ロードマップ

## Phase 1: 共通OSの土台（今回）

- [x] 方針・品質・契約・権限・状態遷移の文書化（`core/`）
- [x] 業務別 PROFILE（development / document-compare は記述済み、drawing-analysis / data-analysis は雛形）
- [x] タスク契約スキーマと機械検証（`validate-task.py`）
- [x] サンプルタスク2種（開発・文書比較）を同一契約で検証
- [x] 環境診断（`doctor.sh`）、秘密情報スキャン、ローカル品質ゲート（`check.sh`）
- [x] 手動受け渡し用の brief 出力（`render-brief.py`、ドライランのみ）
- [ ] サンプルタスクを実際に AI に実行させ、証拠付きで `passed` まで進める（Phase 2 の最初の検証）

## Phase 2: Claude Code と Codex CLI の連携

- Codex CLI を導入し、`codex --help` で実行方式とサンドボックス設定を確認して `agents/codex.md` を確定
- 非対話実行（`claude -p` 等）のラッパーを **ドライラン既定** で実装（実行には明示フラグが必要）
- 実装 → 独立レビュー → rework の1サイクルを、`max_retries` を守って半自動化
- 実行ログ・コマンド出力を `runs/<task_id>/` に保存し、`sha256` を `evidence` に記録して照合
- 実行主体の記録（誰が実行したかを CLI 側から取得できるか確認する）
- OS レベルの隔離（専用作業ディレクトリ、ネットワーク制限）の設計

## Phase 3: 実行状態の永続化と自動再開

- 状態更新を CLI 化（JSON の手編集をやめ、遷移規則を通した更新だけを許す）
- 中断点からの自動再開、ロック（同じタスクの二重実行防止）

## Phase 4: BreakLab との品質ゲート連携

- BreakLab の検証結果をこの OS の `evidence` / `quality` に取り込む形式を定義
- 「欠損は 0 ではない」「無効と不良を混同しない」等の BreakLab の測定規則を PROFILE 化

## Phase 5: AI 適性評価と自動ルーティング

- task_type × agent ごとの PASS/FAIL 実績を集計（分母を明示）
- 実績に基づく担当の推薦（決定は人間）

## Phase 6: 複数業務への展開

- drawing-analysis / data-analysis の検証手順を確定
- PDF・Word からのテキスト抽出手順を document-compare に追加
