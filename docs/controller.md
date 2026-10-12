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
   既知のタスク（ワーカーが保持していないもの）は GitHub 側の変更と照合する（下記「GitHub との状態同期」）。
2. **取得**: 空いたワーカーが `BEGIN IMMEDIATE` の中で最古の実行可能タスクをリースする。
   モデルを使う段階（work / review）は、同じトランザクションでモデル枠（`model_concurrency`、初期値 2）も取る。
   枠が無ければモデル不要の段階（check / judge）だけを取る。
3. **実行**: ワーカーは自分の worktree にタスクブランチを出し、信頼済みの `scripts/aiwos.py` を 1 回だけ実行する。
   実行中は `heartbeat_s` ごとにリースを更新する。
4. **記録**: `runs/` と契約をコミットし、タスクブランチと実行ブランチを push する。
   `publish=github` のときは、PR が無ければ `gh api`（REST API）で作る（GraphQL は使わない）。
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
| 同じ段階の再試行 | `max_stage_attempts`（既定 3）。ワーカー内の例外、タイムアウト、契約の更新競合、クラッシュによる回収のすべてに適用し、使い切ったら held。正常に進んだ段階で 0 に戻る。controller の停止による中断は数えない。失敗した段階でもモデルが起動していれば AI 実行回数に数える |
| 利用枠制限 | 失敗した実行の出力に usage limit / rate limit / 429 などがあれば、その agent を指数バックオフ（`backoff_initial_s`〜`backoff_max_s`）。バックオフ中はその agent の段階を誰も取らない。成功で解除 |
| 未ログイン | `aiwos.py auth` で確認し、使えない agent の段階は取らない（`auth_recheck_s` ごとに再確認。ログインされれば自動で再開） |
| 二重実行の防止 | リースは 1 タスク 1 ワーカー。ワーカーは段階の間ずっと（checkout・commit・push を含む）別スレッドでリースを更新し、リースを失ったら結果を捨てる。リースの期限切れ、または所有者の死亡を見つけた controller は、次の順で回収する: 所有者が生きていれば（凍結・ハング）停止 → 子プロセスを停止 → 両方の消滅を確認 → worktree を解放 → キューへ戻す。確認できなければ回収しない |
| 再起動後の復元 | キューは SQLite に残る。起動時に前回のワーカーを停止し、所有者の死んだリースを回収する |
| ログと証拠 | `state/logs/worker-NN.log`、`state/logs/stages/<task>/<時刻>-<段階>.log`（ローカルのみ）。実行証拠は v0.2 どおり `runs/` に記録し、タスクブランチで GitHub に反映 |
| 人間待ち | passed / escalated / 人間の中断 / 予算超過 / 繰り返しのクラッシュは held。controller はマージも `accepted` もしない |

## GitHub との状態同期

同期のたびに、ワーカーが保持していない既知のタスクを GitHub と照合する。**自動で上書きはしない。**

| GitHub 側の変化 | 扱い |
|---|---|
| タスクブランチに人の commit が積まれた（ローカルはその祖先） | 取り込む（fast-forward）。契約から次の段階を決め直す。例: escalated のタスクを人が ready に戻して push → 再開 |
| ローカルにだけ未 push の commit がある | そのまま。再送（republish）で push する |
| ローカルとリモートが分岐した | **held**（"diverged"）。どちらの commit も消さない。人が統合して push すれば取り込む |
| main の契約が変わった（作業前） | 新しい契約で段階を決め直す（担当の変更など） |
| main の契約が変わった（作業開始後） | **held**。作業はタスクブランチ上の契約に基づいて行われたため、人が判断する |

ワーカーも checkout のときに同じ規則を使う（リモートが新しければ fast-forward、分岐していれば held）。

**PR の再試行:** push の成否と PR 作成の成否を別々に記録する（`remote_sha`、`pr_url`、`pr_error`）。push に成功し
PR 作成だけが失敗した場合、同期のたびに PR 作成だけを再試行する。既存の PR を先に探すため、重複して作らない。

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
  書き込み可能パスの限定を設定する（`NoNewPrivileges` は controller が agent user へ sudo するため off。
  隔離は OS レベルのユーザー分離で行う。下記「AI 実行ユーザーの OS レベル分離」）。
- **AI 実行ユーザーの OS レベル分離（Security Fix 002）**: 信頼済みの controller・ワーカー・GitHub publisher は
  `agent_user`（例 aiwos-ctl）で動き、AI の CLI と AI が書いたコード・テスト・シェルコマンドはすべて非特権の
  `agent_user`（例 aiwos-agent）で動く。両者は workspace 用の `agent_group` だけを共有する。
  - ワーカーは `sudo -n -u <agent_user> -- python3 scripts/agent-exec.py ... -- <argv>` でエージェント側を起動する
    （`scripts/isolation.py`）。sudoers はこの 1 コマンドだけを許可する（`deploy/isolation/`）。
  - `agent-exec.py` はエージェント側のプロセスを新しいセッションで起動し、環境は引数で渡した許可リストだけにする
    （`GH_TOKEN` 等は一切渡らない）。親（sudo）が消えれば自分も終わり、タイムアウトでセッションごと停止する。
  - エージェントが触れるのは workspace の `tree/`（worker/check は書込可、reviewer は読取専用）と `out/` だけ。
    git メタデータは `git/` に分離し、エージェントは触れない（core.hooksPath 等を仕込めない）。
  - **AI 実行のフェイルクローズ（Security Fix 002-2 / 監査 item 4）**: GitHub の認証情報（`GH_TOKEN` 等の環境変数、
    または `credential_files` に挙げたファイル）が、AI と同じ OS ユーザーから読める状態では、`publish: none` でも
    ローカル宛てでも、untrusted な段階（work / check / review）を実行しない。隔離が確認済みなら実行する
    （AI は別ユーザーで読めない）。認証情報が一切無い場合も実行する（スタブ試験）。該当時はタスクを waiting にして
    保留し、隔離の確認が取れるか認証情報が外れると自動で再開する。
  - **ローカルremote の判定（監査 item 1）**: `publish: git` のローカル例外は、`git remote get-url --push --all` が返す
    **実効 push 先がすべてローカルパス（`/` か `file:///`）で、かつ 1 件以上ある場合のみ**許可する。`pushurl`・複数 push 先・
    `insteadOf`・`pushInsteadOf` による書換はこのコマンドに反映されるため、ネットワーク宛てを見逃さない。判定不能・混在は
    ローカルとみなさない（フェイルクローズ）。
  - **publish のフェイルクローズ**: ネットワーク push と `publish: github` は、エージェント user として実行する
    侵入テスト（`scripts/isolation-probe.py`）が「トークン env ファイル・信頼済み state・親プロセスの
    `/proc/<pid>/environ`・設定した認証情報ファイルを読めず、信頼済みコード・state・リポジトリに書けない」ことを
    確認できている間だけ許可される。確認できなければ publish しない（記録はローカルに残す）。
    `fs.protected_hardlinks=1` も必須とする。
  - **publish ゲート**: publisher は、タスクブランチが `runs/<task>/` と契約以外を変更していないこと、各 run
    ブランチが `allowed_paths` 内だけを変更していること、秘密情報らしき文字列が無いこと、信頼済みコードで契約が
    検証できることを確認した差分だけを push する。
  - 隔離されていない設定（`agent_user` 未設定）では、AI は信頼済みユーザーと同じ権限で動くため、ネットワークへの
    publish はフェイルクローズする（ローカルパスの remote への `publish: git` は可）。

## 配置手順（Ubuntu VPS）

```bash
# 1. 信頼済みユーザー・AI 実行ユーザー・共有グループと sudoers を作る（Security Fix 002）
sudo git clone https://github.com/masube-arent/ai-work-os /opt/aiwos/code
sudo bash /opt/aiwos/code/deploy/isolation/setup.sh /opt/aiwos/code aiwos-ctl aiwos-agent aiwos-ws
sudo mkdir -p /srv/aiwos /var/lib/aiwos/state /var/lib/aiwos/workspaces /etc/aiwos
sudo chown aiwos-ctl:aiwos-ctl /srv/aiwos /var/lib/aiwos /var/lib/aiwos/state
sudo chown aiwos-ctl:aiwos-ws /var/lib/aiwos/workspaces   # agent_group で共有

# 2. 作業用クローン（信頼済みコードは手順1で /opt/aiwos/code に置いた）
sudo -u aiwos-ctl git clone https://github.com/masube-arent/ai-work-os /srv/aiwos/repo
sudo apt-get install -y python3-jsonschema git    # または pip install -r requirements.txt

# 3. 設定と認証情報（トークンは publish=github のときだけ）
sudo cp /opt/aiwos/code/config/controller.example.json /etc/aiwos/controller.json
sudo install -m 0600 /dev/null /etc/aiwos/controller.env   # GH_TOKEN=... を書く
#    push に使う git の認証情報は aiwos-ctl ユーザーの credential helper 等で設定する（agent user からは読めない配置にする）

# 4. エージェントのログイン（AI 実行ユーザー aiwos-agent で。API キーは使わない）
sudo -u aiwos-agent -H claude auth login          # サブスクリプションでログイン
sudo -u aiwos-agent -H codex login --device-auth  # ChatGPT アカウントでログイン
# config に agent_user/agent_group/credential_files/github_repo を設定し、
# GH_TOKEN は /etc/aiwos/controller.env（aiwos-ctl だけが読める 0600）に置く
sudo -u aiwos-ctl python3 -m controller sync-once --config /etc/aiwos/controller.json  # isolation ok を確認

# 5. 事前検証（本番配置前に全テスト）
cd /opt/aiwos/code && bash scripts/check.sh

# 6. 起動
sudo cp /opt/aiwos/code/deploy/systemd/aiwos-controller.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aiwos-controller
```

## 運用

```bash
sudo -u aiwos-ctl python3 -m controller status --config /etc/aiwos/controller.json        # 10 ワーカー・枠・agent・タスク
sudo -u aiwos-ctl python3 -m controller status --config /etc/aiwos/controller.json --json
sudo -u aiwos-ctl python3 -m controller sync-once --config /etc/aiwos/controller.json     # ワーカーなしで同期だけ
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
| 異常終了からの回復・バックオフ・保留・上限・信頼済みコード・GitHub 同期の各規則 | スタブ CLI（`tests/test_controller*.py`） | 成功 |
| **実際の GitHub への push** | 実 Claude Code・実 GitHub・合成タスク 2 件（`tests/results/2026-10-11-v0.3-live-verification.md`） | **成功**（タスクブランチ・実行ブランチ。push された契約は検証器で valid） |
| **実環境でのワーカー強制終了・controller 強制終了・正常再起動** | 同上 | **成功**（完了済みの worker 実行は各タスク 1 件のみ、再実行・重複なし） |
| REST API での PR 作成・重複防止・失敗後の再試行 | 偽の `gh api`（`tests/_fake_gh.py`、`tests/test_isolation.py`） | 成功（作成 1 回・GET による重複防止・POST 失敗後の再試行を確認） |
| 隔離未確認での PR 作成の拒否 | スタブ（`tests/test_isolation.py`） | 成功（`gh` を一度も呼ばない） |
| 実際の GitHub への PR 作成（実 `gh api`） | — | **未検証**: この環境では GitHub API が遮断されるため実呼び出しは未確認（方式は REST に統一済み。GraphQL 非依存） |
| systemd ユニット | `systemd-analyze verify` | 構文 OK。実際の systemd 下での起動は未検証（この環境の PID 1 は systemd ではない） |

## 未実装・残課題

- 信頼済み（aiwos-ctl）と AI 実行（aiwos-agent）の OS ユーザー分離は実装済み。未実装なのは、ワーカーごとに
  AI を別ユーザーに分けること（現状は全ワーカーの AI が同じ aiwos-agent を共有）、コンテナ分離、ネットワーク制限
- 利用枠の検出は出力の文言によるヒューリスティック。CLI が別の文言を出すと検出できない
  （その場合も実行は失敗として記録され、契約はエスカレーションされる）
- ワーカー実行の失敗（usage limit を含む）でランナーが契約を `escalated` にした場合、自動では再開しない（人間待ち）
- GitHub への push に使う認証情報の設定方法は運用者に委ねている（controller は git の設定をそのまま使う）
- SIGKILL でも消えないプロセス（D 状態など）が残った場合、そのタスクは回収されず止まったままになる（安全側）。
  この状態は試験で再現できていない
- held になったタスクを人が再開する手段は、タスクブランチへの push（または main の契約変更）。controller 側の
  held を解除するコマンドは無い
- 実 GitHub への `gh api` による PR 作成は未実行（この環境は GitHub API 遮断。方式は REST。VPS で検証する）
- `run.json` の `models_reported` が示すとおり、モデルを指定しない実行では CLI の既定モデルが実行ごとに変わりうる
  （実測: opus と sonnet）。性能比較をする場合はモデルの固定が必要
