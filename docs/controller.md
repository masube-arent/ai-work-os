# AI WORK OS v0.3 — Controller（10 ワーカー継続運転）

GitHub をタスク・コード・PR・品質証拠の正本とし、AI の実行と品質テストは Ubuntu VPS 上の
ローカルプロセスで行う。**GitHub Actions は使わない**（このリポジトリにワークフローは無い。
追加する場合も手動起動のみ。`tests/test_controller.py` の `ActionsPolicy` が検査する）。

## 構成

```
                GitHub（正本: main の契約 / タスクブランチ / PR）
                   ▲ fetch            ▲ push・PR作成（GH_TOKEN は controller と worker だけが持つ）
                   │                  │
 systemd ─▶ controller ──────────────────────────────────────────────┐
            │ sync: origin/main の契約を読む（Issue/PR 本文は読まない）     │
            │ 監視: 10 ワーカーの生存・リース回収・孤児プロセス停止          │
            │ agent 状態: aiwos.py auth（ログイン確認のみ。モデル呼び出しなし） │
            ▼                                                        │
     SQLite WAL（state/aiwos.db）: tasks / workers / model_slots / agents / events
            ▲ BEGIN IMMEDIATE で取得・解放（二重取得なし）              │
   ┌────────┼─────────┬──── … ────┐                                   │
 worker01  worker02   …        worker10   各自の git worktree（state/worktrees/wNN）
   │ 1 段階だけ実行: 信頼済み code_dir の scripts/aiwos.py を AIWOS_ROOT=worktree で起動
   │   work → check → review → judge（v0.2 のコマンドそのもの）
   └▶ commit → push aiwos/task/<id> と run/<id>/* → （publish=github なら）PR 作成
```

| 部品 | ファイル |
|---|---|
| 設定 | `controller/config.py`, `config/controller.example.json` |
| キュー・排他 | `controller/store.py`（SQLite WAL） |
| 段階の決定 | `controller/stages.py`（契約と実行記録だけから決める） |
| ワーカー | `controller/worker.py` |
| 監視・同期 | `controller/main.py` |
| Git/GitHub | `controller/gitops.py` |
| プロセス木 | `controller/procs.py` |
| systemd | `deploy/systemd/aiwos-controller.service` |

## タスクの流れ

1. **同期**（`sync_interval_s` ごと）: `git fetch` し、`origin/main` 上の `task_globs` に一致する契約を読む。
   リモートにタスクブランチ `aiwos/task/<id>` があれば、そちらの契約を新しい状態として使う
   （キューの DB を失っても、ここから復元できる）。`draft` は取り込まない。
2. **取得**: 空いたワーカーが `BEGIN IMMEDIATE` の中で最古の実行可能タスクをリースする。
   モデルを使う段階（work / review）は、同じトランザクションでモデル枠（`model_concurrency`、初期値 2）も取る。
   枠が無ければモデル不要の段階（check / judge）だけを取る。
3. **実行**: ワーカーは自分の worktree にタスクブランチを出し、信頼済みの `scripts/aiwos.py` を 1 回だけ実行する。
   実行中は `heartbeat_s` ごとにリースを更新する。
4. **記録**: `runs/` と契約をコミットし、タスクブランチと実行ブランチを push する。
   `publish=github` のときは、PR が無ければ `gh pr create` で作る。
5. **次の段階**を契約から決めてキューに戻す。`passed`・`escalated`・人間による中断は **held**（人間待ち）。

| 契約の状態 | 次の段階 | 備考 |
|---|---|---|
| ready / rework / in_progress | work（assigned_agent） | |
| review | check → review（reviewer）→ judge | 同じ結果コミットへの記録があれば飛ばす |
| review（judge 後も UNVERIFIED） | held | 人間か追加レビューが必要 |
| suspended（ランナー自身の中断） | resume → work | 人間による中断は held |
| passed | held | **controller は `accepted` にしない**（Security Fix 001） |
| escalated | held | 理由は契約の `escalation_reason` |
| accepted / cancelled | done | |

## 制御条件

| 条件 | 実装 |
|---|---|
| 最大ワーカー数 10 | `workers`（1〜10。11 以上は設定エラー） |
| モデル同時実行数 | `model_concurrency`（初期値 2）。`model_slots` 表で全ワーカー共通に制限 |
| 排他 | タスク取得・モデル枠・解放は `BEGIN IMMEDIATE`。解放はリース保持者のみ可能 |
| タイムアウト | 段階ごと `stage_timeout_s`（ランナーへ `--timeout`）＋ controller 側の強制停止（＋`kill_grace_s`＋60 秒） |
| AI 実行回数 | `max_ai_runs_per_task`（work + review の合計、既定 6）。超えたら held |
| 再作業回数 | 契約の `max_retries`（v0.2 のまま、上限 3） |
| 同じ段階の再試行 | `max_stage_attempts`（クラッシュによる回収を含む、既定 3）。超えたら held |
| 利用枠制限 | 失敗した実行の出力に usage limit / rate limit / 429 などがあれば、その agent を指数バックオフ（`backoff_initial_s`〜`backoff_max_s`）。バックオフ中はその agent の段階を誰も取らない。成功で解除 |
| 未ログイン | `aiwos.py auth` で確認し、使えない agent の段階は取らない（`auth_recheck_s` ごとに再確認。ログインされれば自動で再開） |
| 二重実行の防止 | リースは 1 タスク 1 ワーカー。リースを失ったワーカーは結果を捨てる。死んだワーカーの子プロセスは回収前に停止し、その worktree を解放する |
| 再起動後の復元 | キューは SQLite に残る。起動時に前回のワーカーを停止し、所有者の死んだリースを回収する |
| ログと証拠 | `state/logs/worker-NN.log`、`state/logs/stages/<task>/<時刻>-<段階>.log`（ローカルのみ）。実行証拠は v0.2 どおり `runs/` に記録し、タスクブランチで GitHub に反映 |
| 人間待ち | passed / escalated / 人間の中断 / 予算超過 / 繰り返しのクラッシュは held。controller はマージも `accepted` もしない |

## セキュリティ

- **信頼済みコードの実行**: ワーカーはタスクブランチ上のスクリプトを実行しない。`code_dir`（運用者が配置した
  チェックアウト）の `scripts/aiwos.py` と、そのスキーマ・PROFILE を使い、データだけを `AIWOS_ROOT` で worktree から読む。
  エージェントがブランチ上で `scripts/` を書き換えても、判定規則は変わらない（テスト済み）。
- **認証情報**: `GH_TOKEN` は controller とワーカー（push・PR 作成）だけが持つ。ランナーへ渡す環境からは除き、
  さらにランナーがエージェントへ渡す環境は許可リストのみ（v0.2）。トークンはリポジトリに置かず、
  `/etc/aiwos/controller.env`（root 所有、0600）に置く。対象リポジトリだけに限定した fine-grained token を使い、
  権限は contents: write と pull requests: write に絞る。
- **外部入力**: Issue・PR の本文やコメントは読まない。タスクは main に入った契約ファイルだけから取る。
- **マージ・承認の代行なし**: 使う `gh` のサブコマンドは `pr list` と `pr create` だけ（テストで検査）。
- **公開リポジトリ**: コミットされるのは v0.2 のランナーが秘密情報を検査した `runs/` だけ。
  ワーカー・段階のログはローカル（`state/logs`）に残り、push されない。
- **OS レベルの隔離**: systemd ユニットで専用ユーザー・`ProtectSystem=strict`・`ProtectHome`・
  書き込み可能パスの限定・`NoNewPrivileges` を設定する。ワーカーごとの別ユーザーやコンテナ分離は未実装（下記）。

## 配置手順（Ubuntu VPS）

```bash
# 1. 専用ユーザーとディレクトリ
sudo useradd --system --home-dir /var/lib/aiwos/home --create-home --shell /usr/sbin/nologin aiwos
sudo mkdir -p /opt/aiwos /srv/aiwos /var/lib/aiwos/state /etc/aiwos
sudo chown aiwos:aiwos /srv/aiwos /var/lib/aiwos /var/lib/aiwos/state

# 2. 信頼済みコード（タグ付きのコミットを使う）と作業用クローン
sudo git clone https://github.com/masube-arent/ai-work-os /opt/aiwos/code
sudo -u aiwos git clone https://github.com/masube-arent/ai-work-os /srv/aiwos/repo
sudo apt-get install -y python3-jsonschema git    # または pip install -r requirements.txt

# 3. 設定と認証情報（トークンは publish=github のときだけ）
sudo cp /opt/aiwos/code/config/controller.example.json /etc/aiwos/controller.json
sudo install -m 0600 /dev/null /etc/aiwos/controller.env   # GH_TOKEN=... を書く
#    push に使う git の認証情報は aiwos ユーザーの credential helper 等で設定する

# 4. エージェントのログイン（aiwos ユーザーで。API キーは使わない）
sudo -u aiwos -H claude auth login          # サブスクリプションでログイン
sudo -u aiwos -H codex login --device-auth  # ChatGPT アカウントでログイン
sudo -u aiwos -H python3 /opt/aiwos/code/scripts/aiwos.py auth   # 両方 usable: true を確認

# 5. 事前検証（本番配置前に全テスト）
cd /opt/aiwos/code && bash scripts/check.sh

# 6. 起動
sudo cp /opt/aiwos/code/deploy/systemd/aiwos-controller.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aiwos-controller
```

## 運用

```bash
sudo -u aiwos python3 -m controller status --config /etc/aiwos/controller.json        # 10 ワーカー・枠・agent・タスク
sudo -u aiwos python3 -m controller status --config /etc/aiwos/controller.json --json
sudo -u aiwos python3 -m controller sync-once --config /etc/aiwos/controller.json     # ワーカーなしで同期だけ
journalctl -u aiwos-controller -f
sudo systemctl stop aiwos-controller    # 実行中の段階は中断され、次回起動時にやり直す
```

- **同時実行数を増やす**: `model_concurrency` を上げて再起動する。利用枠（バックオフの発生頻度、`events` 表）を
  確認しながら 1 ずつ上げる。
- **held のタスク**: `status` の reason を見て人間が判断する。`passed` は人間がタスクブランチの PR を確認する
  （`accepted` への遷移は本人確認の仕組みができるまで不可。`docs/human-approval.md`）。
- **コードの更新**: `/opt/aiwos/code` を更新したら controller を再起動する。ワーカーは実行時に毎回このコードを読む。

## 検証の範囲（2026-10-11）

| 項目 | 方法 | 結果 |
|---|---|---|
| 10 ワーカー・20 タスク・二重実行なし・モデル同時実行 ≤ 2 | スタブ CLI（`tests/test_controller.py`） | 成功 |
| ワーカー強制終了・controller 強制終了・正常停止からの回復 | スタブ CLI | 成功 |
| 利用枠バックオフ・未ログイン時の保留と再開・AI 実行回数の上限 | スタブ CLI | 成功 |
| main 以外の契約を取らない・タスクブランチのスクリプトを実行しない | スタブ CLI | 成功 |
| PR 作成（1 タスク 1 件、merge/review を呼ばない） | 偽の `gh` | 成功（実際の GitHub API では未検証） |
| systemd ユニット | `systemd-analyze verify` | 構文 OK。実際の systemd 下での起動は未検証（この環境の PID 1 は systemd ではない） |
| 実 CLI での同時実行 | `tests/results/2026-10-11-v0.3-controller.md` | 記録を参照 |

## 未実装・残課題

- ワーカーごとの OS ユーザー分離・コンテナ・ネットワーク制限（現状は全ワーカーが同じ aiwos ユーザー）
- 利用枠の検出は出力の文言によるヒューリスティック。CLI が別の文言を出すと検出できない
  （その場合も実行は失敗として記録され、契約はエスカレーションされる）
- ワーカー実行の失敗（usage limit を含む）でランナーが契約を `escalated` にした場合、自動では再開しない（人間待ち）
- GitHub への push に使う認証情報の設定方法は運用者に委ねている（controller は git の設定をそのまま使う）
- `gh pr create` は実際の GitHub では未実行（この環境の gh は未認証）
