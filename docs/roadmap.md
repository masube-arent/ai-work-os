# ロードマップ

## Phase 1: 共通OSの土台（GitHub への反映を除き完了）

- [x] 方針・品質・契約・権限・状態遷移の文書化（`core/`）
- [x] 業務別 PROFILE（development / document-compare は記述済み、drawing-analysis / data-analysis は雛形）
- [x] タスク契約スキーマと機械検証（`validate-task.py`）
- [x] サンプルタスク2種（開発・文書比較）を同一契約で検証
- [x] 環境診断（`doctor.sh`）、秘密情報スキャン、ローカル品質ゲート（`check.sh`）
- [x] 手動受け渡し用の brief 出力（`render-brief.py`、ドライランのみ）
- [ ] サンプルタスクを実際に AI に実行させ、証拠付きで `passed` まで進める → Phase 2 に移管

## Phase 2: Claude Code と Codex CLI の連携（今回）

- [x] Codex CLI を導入し、`--help` で実行方式・サンドボックス・ログイン方法を確認（`agents/codex.md`）
- [x] Claude Code の認証方式・非対話実行・構造化出力を実環境で確認（`agents/claude.md`）
- [x] ランナー `scripts/aiwos.py`: ドライラン既定、隔離ワークスペース、実行記録、check、review、judge、metrics
- [x] 実行ログを `runs/<task_id>/` に保存し、`evidence.sha256` を毎回照合
- [x] 実行主体の記録（CLI 実パス・バージョン・認証方式・セッション ID・対象コミット）と、それによる独立性の検証
- [x] 停止条件（秘密情報・範囲外変更・レビュアーの改変・タイムアウト・未認証・同時更新）
- [x] 担当入替の比較実験の構造（DEV-0002、同一基点・別ブランチ）
- [ ] Codex CLI のログインと実行（未ログインのため Codex による実装・レビューは未実施）
- [ ] DEV-0001 の独立レビュー → `passed`（Codex レビュー待ち）
- [ ] OS レベルの隔離（別ユーザー・コンテナ・ネットワーク制限）

## v0.3: 10 ワーカー継続運転（controller）

- [x] SQLite WAL のキュー・排他・進捗、最大 10 ワーカー、モデル同時実行数の上限（初期値 2）
- [x] GitHub 同期（main の契約を取得、タスクブランチ・PR で結果を返す）、push 失敗の再送
- [x] タイムアウト、AI 実行回数・再試行回数の上限、利用枠のバックオフ、未ログイン agent の保留
- [x] ワーカー・controller の異常終了からの回復、二重実行の防止、systemd ユニット
- [x] 信頼済みコードの実行（タスクブランチ上のスクリプトを実行しない）
- [ ] 実際の systemd 下での運転、実際の GitHub への PR 作成（VPS で検証する）
- [ ] ワーカーごとの OS レベル隔離

## クラウド実行モード（VPS なし）

VPS・常駐プロセスを持たずに、クラウド上の AI 機能だけで「Issue → 実装 → 品質ゲート → Draft PR →
独立レビュー → 人間が判断」を回す運用形態。初回実証は CLOUD-0001（Issue #4）。

### 実行基盤

- Claude Code Cloud セッション: Issue ごとに 1 セッションで実装し、`bash scripts/check.sh` を通して Draft PR を出す
- Claude Code Routine: 定期的に Issue を確認し、未処理タスクを 1 件ずつ実行する

### 役割

| 役割 | 担当 | 必須か |
|---|---|---|
| 実装 | Claude Code | 必須 |
| レビュー | GPT / ChatGPT（PR の差分を確認し、結果を PR に記録する） | 必須 |
| 追加レビュー | Codex Cloud（PR へのコメント `@codex review`） | 任意（受入の条件にしない） |
| マージ・最終承認 | 人間（@masube-arent） | 必須 |

ChatGPT から GitHub へのレビュー投稿が権限不足で失敗した場合は、ChatGPT の確認結果を、誰が記録したかを明記して
PR のコメントに残す。このコメントは GitHub 上の承認レビュー（Approve）ではなく、マージの判断は人間が行う。

### 使わないもの

- VPS、systemd、常駐 Controller（v0.3 の controller はこのモードでは動かさない）
- GitHub Actions
- 従量課金の AI API

### 人間が行うこと

- PR のマージ
- タスクの最終承認（`accepted`）
- AI はマージ（自動マージの有効化を含む）と承認を代行しない

### 未検証の項目

- [ ] Routine の定期実行の安定性
- [ ] ChatGPT から GitHub への PR レビュー投稿（CLOUD-0001 では権限 403 で未完了）
- [ ] Codex Cloud レビューの応答（任意）
- [ ] 並列実行の上限

## Phase 3: 実行状態の永続化と自動再開

- 状態更新の CLI 化と競合検出は Phase 2 で実装済み（`aiwos.py transition`、ハッシュ比較 + ロック）
- 中断点からの自動再開
- 人間承認の本人確認（GitHub の PR 承認との照合。設計案: `docs/human-approval.md`。Security Fix 001 で現在はフェイルクローズ）

## Phase 4: BreakLab との品質ゲート連携

- BreakLab の検証結果をこの OS の `evidence` / `quality` に取り込む形式を定義
- 「欠損は 0 ではない」「無効と不良を混同しない」等の BreakLab の測定規則を PROFILE 化

## Phase 5: AI 適性評価と自動ルーティング

- task_type × agent ごとの PASS/FAIL 実績を集計（分母を明示）
- 実績に基づく担当の推薦（決定は人間）

## Phase 6: 複数業務への展開

- drawing-analysis / data-analysis の検証手順を確定
- PDF・Word からのテキスト抽出手順を document-compare に追加
