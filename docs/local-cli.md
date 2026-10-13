# ローカル CLI（Windows PowerShell → WSL2 Ubuntu の v0.3 Controller）

VPS も GitHub Actions も使わずに、手元の Windows PC で v0.3 Controller を動かすための CLI。
Windows の PowerShell から操作し、Controller は WSL2 の Ubuntu の中で動く。
Claude Code CLI と Codex CLI は、それぞれのサブスクリプションのログインで動く（API キーは使わない）。

```
Windows PowerShell
  deploy\windows\aiwos.ps1 <command>
        │  wsl.exe -d Ubuntu -u aiwos-ctl --exec /usr/bin/python3 ...（シェルを通さない）
        ▼
WSL2 Ubuntu
  aiwos-ctl   scripts/aiwos-local.py → python3 -m controller（ワーカー 10、モデル同時 1、publish=none）
        │  sudo（scripts/agent-exec.py だけ）
        ▼
  aiwos-agent claude / codex（このユーザーのログインだけを持つ。GitHub の認証情報は持たない）
```

| コマンド | 内容 | AI を起動するか |
|---|---|---|
| `init` | 設定を作る（publish=none、require_isolation=true、ワーカー 10、モデル 1、対象は `tasks/` だけ）。既存の設定は上書きしない | しない |
| `doctor` | 環境・設定・隔離・WSL の穴・sudo 規則・AI CLI のログイン状態を確認する | しない（ログイン状態の確認だけ） |
| `start` | 隔離の検査（agent ユーザーとして実行）に通ったときだけ Controller を起動する。この設定のプロセスが Controller なしで残っていれば起動しない（先に `stop`） | 待ち行列にタスクがあれば、隔離確認後に起動する |
| `status` | Controller・ワーカー・モデル枠・隔離の判定・タスクを表示する。Controller なしで残っているプロセスがあれば警告する | しない |
| `stop` | 起動した Controller だと確かめてから SIGTERM を送り、終了を待つ（Controller には SIGKILL しない）。続けて、異常終了で残ったこの設定のワーカーと段階のプロセス（その下の AI を含む）を止める | しない |
| `sync` | データ用 clone の remote から契約を 1 回取り込む。隔離の検査が必要。Controller の稼働中・残留プロセスがあるときは拒否する（Controller 自身が `sync_interval_s` ごとに同期するため、Git の操作が競合しないように） | しない |
| `review TASK` | 契約で指定されたレビュアーを 1 回実行する。Controller の停止中で、残留プロセスが無いときだけ | する（隔離確認後） |

## 安全条件（初回の動作条件）

- **publish=none 以外では動かない。** GitHub への push・PR 作成はしない。結果はローカルに残るだけ。
- **隔離を確認できなければ AI を 1 回も起動しない。** `start` / `sync` / `review` の前に隔離の検査を必ず行い、
  失敗したら何もせずに止まる。Controller も `require_isolation=true` のもとで、隔離の判定が期限切れ・失敗のあいだは
  AI の段階を実行しない（従来は「認証情報が無ければ隔離なしでも実行可」だったが、ローカル CLI では許さない）。
- **WSL の穴を塞いでいない WSL では AI を起動しない。** WSL2 では Windows の機能が Linux のユーザー分離を素通りする:
  - interop: どの Linux ユーザー（agent を含む）も `cmd.exe` / `powershell.exe` を **Windows ユーザーの権限で** 起動できる
  - 自動マウント: `/mnt/c` などに Windows のドライブが見え、agent から Windows 側のファイル
    （ほかの AI のログイン情報、git の認証情報、SSH 鍵）を読める可能性がある

  隔離の検査は、WSL のときこの 2 つが無効であることも確かめる（`controller/wsl.py`）。
- **root と agent ユーザーでは動かない。** CLI は Controller 用ユーザー（aiwos-ctl）でだけ動く。
- **ワーカー 10・モデル同時実行 1・対象は `tasks/` だけ。** 設定がこの値から外れていれば、`start` / `sync` / `review` は
  何もせずに止まる（`task_globs` は `tasks/` で始まり `..` を含まないパターンだけを許す）。`examples/` は自動実行しない。
- **対象はダミータスクだけ。** 実案件は扱わない。
- **start / stop / sync / review は 1 つずつ。** state ディレクトリのロックで直列にする。
- **常駐・自動起動はしない。** systemd・タスクスケジューラへの登録はしない。PC のスリープ・シャットダウンで止まる。

## 異常終了したときの扱い

| 状況 | 何が残るか（コンテナで確認） | CLI の扱い |
|---|---|---|
| Controller だけが強制終了 | ワーカーは Controller の消滅を検知し、実行中の段階（AI を含む）をプロセスツリーごと止めてから終わる。数秒で何も残らない | `status` は STOPPED。`start` で再開できる |
| ワーカーだけが強制終了（Controller は稼働中） | 段階のランナーと AI が残る。Controller は次の起動時か、リースの期限切れ後に片付ける | `stop` は Controller を止めたあと、残ったランナーと AI も止める |
| ワーカーと Controller が同時に強制終了（電源断に近い） | 段階のランナーと AI が、段階のタイムアウト（既定 900 秒）まで監督なしで動き続ける | `status` が警告し、`start` / `sync` / `review` は拒否する。`stop` がそれらを止める（ランナーに SIGINT → AI 側へ中継 → 猶予後に SIGKILL）。そのタスクは、次の Controller がリースの期限切れを確認してから待ち行列に戻す |

残ったプロセスは、この設定のファイルパスを含むコマンド行（ワーカー）と、段階のプロセスの環境変数
（`AIWOS_LEASE_TAG` と、この設定の `worktrees/` を指す `AIWOS_ROOT`）で見分ける。agent ユーザーの AI プロセスは
Controller 用ユーザーから直接は止められないため、その上のランナーを止め、sudo がシグナルを AI 側へ中継する。
WSL ごと止まった場合（`wsl --shutdown`、PC の電源断）は、すべてのプロセスが一緒に止まる。

## ZIP（aiwos-local-cli.zip）の監査結果

受け取った ZIP: SHA-256 `657f975e3217270fa00998476b6ad0eb71989b2b060b30e91ae16019e76c0d3f`（送り主の
`Get-FileHash` と一致）。7 ファイル（`aiwos-local.py`、`aiwos`、`aiwos.ps1`、`test_local_cli.py`、README 2 件）。
展開時にパスの `..` や絶対パスが無いことを確認した。ZIP 本体はこのリポジトリに入れていない。

| # | 重大度 | 指摘 | 対応 | 確認方法 |
|---|---|---|---|---|
| 1 | High | `aiwos.ps1` の既定の設定ファイル `/var/lib/aiwos/state/controller-local.json` が、`aiwos-local.py` の PID ファイル `<state_dir>/controller-local.json` と同じパスになる。`start` で設定が PID の JSON に上書きされ、以後どのコマンドも動かない | PID・ログ・ロックを `local-controller.{pid,log,lock}` に変更。設定は state・workspaces・repo・code の中に置けないようにした。PowerShell の既定は `/var/lib/aiwos-ctl/local.json`（aiwos-ctl のホーム、0700） | `test_pid_log_and_lock_files_never_share_the_config_path`、`test_powershell_default_config_is_outside_the_state_dir` |
| 2 | High | WSL2 の interop と自動マウントが、OS ユーザー分離を素通りする（上記）。ZIP にも既存の隔離検査にも、この確認が無い | `controller/wsl.py` を追加し、WSL のときは隔離の判定に含めた（失敗なら AI を起動しない）。`/etc/wsl.conf` で両方を無効にする手順を追加 | `WslChecks`（5 件）。実機の WSL では未検証 |
| 3 | High | 既存 Controller は「隔離が未確認でも、認証情報が見つからなければ AI を実行してよい」。ZIP の `start` は起動時に 1 回だけ検査するため、その後に隔離の判定が失敗・期限切れになっても AI が動きうる。指示の「OS 隔離未確認では AI を起動しない」を満たさない | 設定 `require_isolation` を追加（既定は false で従来どおり）。true のとき `execution_allowed` は隔離の確認済みだけを許す。`init` は true にし、CLI は true でなければ動かない | `test_no_ai_without_a_verified_probe_even_with_no_credential` |
| 4 | Medium | `aiwos.ps1` は WSL の一覧を取得するが、指定したディストリビューションがあるか確かめていない（変数が未使用）。`wsl.exe` の出力は UTF-16 | 一覧の NUL を除いて照合し、無ければ止める | 偽の `wsl.exe` を使い PowerShell 7 で確認（UTF-16 の一覧、存在しない名前の拒否） |
| 5 | Medium | `wsl -u aiwos-ctl -- python3 ...` は Linux のシェルを通すが、setup.sh が作るユーザーのシェルは `/usr/sbin/nologin` で、起動できない可能性が高い | `--exec /usr/bin/python3`（シェルを通さない）と `--cd /` に変更 | 引数の受け渡しを偽の `wsl.exe` で確認。実機の WSL では未検証 |
| 6 | Medium | 実行ユーザーの判定に環境変数 `USER` を使っている（偽装・未設定がありうる）。root での実行を拒否していない | 実効 UID（`pwd.getpwuid(os.geteuid())`）で判定し、root を拒否 | `test_main_refuses_root`、`test_local_policy_insists_on_publish_none_and_isolation` |
| 7 | Medium | `review` は repo_dir の中なら `examples/` の契約なども実行できる。環境変数をすべて引き継ぐ | 待ち行列の `task_globs` に合う契約だけを許し、Controller と同じ環境（`stage_env`、GitHub トークン除去）で実行 | `test_review_rejects_paths_outside_the_queue_before_any_probe` |
| 8 | Medium | `doctor` は aiwos-ctl の環境で `doctor.sh` を実行するだけで、agent ユーザーのログイン・sudo 規則・隔離・WSL を見ていない | 上記をすべて確認するよう作り直した（モデル呼び出しはしない） | `test_doctor_reports_without_starting_anything` |
| 9 | Low | `start` が `check-config` の出力（設定全体）を表示する | 表示しない | 手動確認 |
| 10 | Low | `stop` で PID ファイルが古いとき、そのファイルを残す | 削除する（どのプロセスにもシグナルは送らない） | `test_stop_never_signals_an_unverified_pid` |

追加の強化: 設定の `code_dir` が CLI 自身のチェックアウトと一致しなければ動かない（別のチェックアウトのスクリプトを
混ぜて実行しない）。

ZIP の良い点はそのまま残した: PID の再利用対策（`/proc` の起動時刻とコマンドラインの両方で照合）、`stop` で SIGKILL しない、
`init` は既存の設定を上書きしない、設定ファイルは 0600、PowerShell で `Invoke-Expression` を使わない。

## 実機セットアップ手順（Windows 11 + WSL2）

**この手順は実機の Windows / WSL2 では未検証。** 各手順の結果を見ながら進め、想定と違ったらそこで止めること。
AI WORK OS 専用の WSL ディストリビューションを使うことを勧める（interop と自動マウントを無効にするため、
普段使いの Ubuntu では不便になる）。以下はディストリビューション名を `Ubuntu` とした例。

### 1. WSL2 と Ubuntu（Windows、管理者の PowerShell）

```powershell
wsl --install -d Ubuntu      # 初回はユーザー名とパスワードを決める。再起動を求められたら従う
wsl --list --verbose         # Ubuntu の VERSION が 2 であることを確認
```

### 2. WSL の穴を塞ぐ（Ubuntu の中）

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[automount]
enabled = false
mountFsTab = false

[interop]
enabled = false
appendWindowsPath = false

[boot]
command = "sysctl -w fs.protected_hardlinks=1 fs.protected_symlinks=1"
EOF
```

Windows の PowerShell で `wsl --shutdown` を実行し、Ubuntu を開き直す。その後、Ubuntu の中で次を確認する:

```bash
ls /mnt/c 2>&1                                  # 何も見えない、または存在しない
cat /proc/sys/fs/binfmt_misc/WSLInterop 2>&1    # 存在しない、または 1 行目が disabled
cat /proc/sys/fs/protected_hardlinks            # 1
```

以後、Windows のファイルは WSL から見えない。コードもデータも WSL の中で git clone する。

### 3. 必要なソフト（Ubuntu の中）

```bash
sudo apt update
sudo apt install -y git python3 python3-jsonschema sudo ca-certificates curl
# Node.js 18 以上をすべてのユーザーが使える場所に入れる（例: NodeSource の Node.js 22）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g @anthropic-ai/claude-code @openai/codex
command -v claude codex   # 例: /usr/bin/claude /usr/bin/codex（/usr/bin か /usr/local/bin であること）
```

この時点では aiwos-ctl / aiwos-agent はまだ無い。`sudo -u aiwos-...` を使うのは手順 4 でユーザーを作った後。

### 4. 信頼済みコードを置く（root 所有、ほかのユーザーは書き込めない）

```bash
sudo git clone https://github.com/masube-arent/ai-work-os.git /opt/aiwos/code
sudo git -C /opt/aiwos/code checkout feature/local-cli     # main に取り込まれるまではこのブランチ
sudo chown -R root:root /opt/aiwos/code
sudo bash /opt/aiwos/code/deploy/isolation/setup.sh /opt/aiwos/code   # aiwos-ctl / aiwos-agent / aiwos-ws と sudo 規則
id aiwos-ctl && id aiwos-agent                                        # 両方のユーザーができたことを確認
sudo -u aiwos-agent -H sh -c 'command -v claude codex'                # agent ユーザーから AI CLI が見えること
```

`sudo` は PATH を `secure_path` に置き換えるため、claude / codex は `/usr/bin` か `/usr/local/bin` に入っている必要がある
（`doctor` が `CLI not found` と出したら、ここを見直す。別の場所なら設定の `agent_bins` で絶対パスを指定する）。

### 5. データ用の場所とダミータスク（ローカルだけの remote。GitHub には何も送らない）

```bash
sudo install -d -o aiwos-ctl -g aiwos-ctl /var/lib/aiwos/state /var/lib/aiwos/workspaces /var/lib/aiwos/repo-dummy
sudo -u aiwos-ctl -H bash -s <<'EOF'
set -e
git init -q --bare -b main /var/lib/aiwos-ctl/dummy-remote.git
git clone -q /var/lib/aiwos-ctl/dummy-remote.git /var/lib/aiwos-ctl/seed
cd /var/lib/aiwos-ctl/seed
mkdir -p tasks && touch tasks/.keep
git add -A && git -c user.name=aiwos -c user.email=aiwos@localhost commit -qm "empty queue"
git push -q origin main
git clone -q /var/lib/aiwos-ctl/dummy-remote.git /var/lib/aiwos/repo-dummy
EOF
```

この時点では `tasks/` は空で、AI は起動しない。

### 6. AI CLI のログイン（agent ユーザーで。API キーは使わない）

```bash
sudo -u aiwos-agent -H claude auth login          # 表示された URL を Windows のブラウザで開く
sudo -u aiwos-agent -H codex login --device-auth  # 表示されたコードを Windows のブラウザで承認する
sudo -u aiwos-agent -H codex login status         # "Logged in using ChatGPT" を確認（API キーなら従量課金になる）
```

interop を無効にしたため、ブラウザは自動では開かない。URL をコピーして Windows 側で開く。
WSL のログインは Windows 側の Claude / Codex のログインとは別で、引き継がれない。

### 7. Windows の PowerShell から操作する

Windows 側の clone（例: `C:\Users\<名前>\ai-work-os`、ブランチ `feature/local-cli`）で:

```powershell
.\deploy\windows\aiwos.ps1 init --repo-dir /var/lib/aiwos/repo-dummy --state-dir /var/lib/aiwos/state `
    --workspaces-dir /var/lib/aiwos/workspaces --agent-user aiwos-agent --agent-group aiwos-ws
.\deploy\windows\aiwos.ps1 doctor     # [NG] が無いこと。claude / codex が logged in であること
.\deploy\windows\aiwos.ps1 start      # 隔離の検査に失敗したら起動しない
.\deploy\windows\aiwos.ps1 status     # RUNNING、Isolation: ok=True、workers 10、model slots 0/1
.\deploy\windows\aiwos.ps1 stop
```

スクリプトの実行が止められる場合は、そのウィンドウだけ `Set-ExecutionPolicy -Scope Process RemoteSigned` を使う。
ディストリビューション名が違うときは `-Distro <名前>` を付ける。

### 8.（任意）ダミータスクで AI を 1 回動かす

**ここで初めてモデルが動き、サブスクリプションの利用枠を使う。** 実案件の契約は入れない。

```bash
sudo -u aiwos-ctl -H bash -s <<'EOF'
set -e
cd /var/lib/aiwos-ctl/seed
mkdir -p tasks/DUMMY-0001
cp /opt/aiwos/code/examples/local-cli/dummy-task.json tasks/DUMMY-0001/task.json
git add -A && git -c user.name=aiwos -c user.email=aiwos@localhost commit -qm "dummy task DUMMY-0001"
git push -q origin main
EOF
```

その後 PowerShell で `start` → `status` を見る。Claude Code が `tasks/DUMMY-0001/out.txt` を書き、検査コマンド、
Codex のレビュー、判定と進む。publish=none のため結果は GitHub に送られない。終わったら `stop`。

## GPT / ChatGPT レビューへの対応（2026-10-13）

| # | 指摘 | 対応 |
|---|---|---|
| 1 | `local_policy()` にワーカー 10・モデル 1・`tasks/` 配下だけの検査がない | 追加。外れていれば start / sync / review は何もせずに止まる |
| 2 | sync と稼働中 Controller の Git 操作の競合 | sync を state のロックで直列にし、Controller の稼働中・残留プロセスがあるときは拒否 |
| 3 | 手順書で aiwos-agent を作る前に `sudo -u aiwos-agent` を実行している | 手順 3 から手順 4（setup.sh の後）へ移した。順序をテストで固定 |
| 4 | Controller の異常終了中に AI が動いていた場合の stop | 再現した: ワーカーと Controller が同時に止まると AI が残り、stop は何もしなかった。残留プロセスの検出・警告・停止を追加（「異常終了したときの扱い」） |

あわせて見つけた不具合: ダミー契約の履歴の時刻が未来（2026-10-13T00:00Z）で、それより前の時計ではランナーが
「時刻が逆行している」と拒否していた。過去の時刻に直した。

## 検証の範囲（2026-10-13）

| 項目 | 結果 | 環境 |
|---|---|---|
| CLI の単体テスト（設定・PID・停止・レビューの経路・root 拒否・WSL 判定・require_isolation・ラッパー・ワーカー数/モデル数/task_globs の強制・sync のロックと稼働中の拒否・残留プロセスの検出と停止・手順書の順序） | 29 件合格 | Linux コンテナ |
| aiwos-ctl / aiwos-agent を使った結合テスト（init → sync → start → status → 二重起動の防止 → Controller の強制終了 → ワーカーの消滅 → 再起動で 10 ワーカー → stop で全プロセス終了） | 合格 | Linux コンテナ（実際の sudo 規則と隔離の検査） |
| agent から読める認証情報ファイルがあるとき、`start` と `sync` が AI を起動せずに止まる | 合格 | 同上 |
| 稼働中の Controller があるとき `sync` を拒否する | 合格 | 同上 |
| AI の段階の実行中にワーカーと Controller を同時に強制終了 → AI が残ることを確認 → `status` が警告、`start` / `sync` を拒否 → `stop` で AI 側のプロセスまで 0 になる（stub の AI CLI、モデル呼び出しなし） | 合格 | 同上 |
| `aiwos.ps1` の構文と引数の受け渡し（`; rm -rf /` を含む引数が 1 つの引数のまま届く、UTF-16 の一覧、存在しない名前の拒否、終了コード） | 合格 | Linux 上の PowerShell 7.4 と偽の `wsl.exe` |
| この手順の 5〜8 の Linux 部分（実際のパス `/var/lib/aiwos/...`、`aiwos` ラッパー）: init → doctor → start → ダミータスクの投入 → status。AI CLI が使えない状態ではタスクは `queued`・`ai_runs=0` のままで、agent ユーザーのプロセスは 0 | 合格 | Linux コンテナ |
| 安全チェックを 1 つずつ外す改変（require_isolation、WSL の判定、PID ファイル名、起動時刻の照合、start 前の検査、ワーカー数、モデル数、task_globs、稼働中の sync、sync のロック、stop の残留プロセス停止、残留プロセスがあるときの start）がテストで検出されること | 12/12 検出 | Linux コンテナ |

**未検証（実機の Windows / WSL2 が必要）**

- Windows PowerShell 5.1 での `aiwos.ps1` の動作（確認は PowerShell 7.4 のみ）
- `wsl.exe --exec` と `--cd /` が、シェルが nologin のユーザーで期待どおり動くこと
- `/etc/wsl.conf` の設定で interop と自動マウントが実際に無効になり、`controller/wsl.py` がそれを正しく判定すること
  （`/proc/mounts` と binfmt_misc の書式は公開情報に基づく想定）
- `[boot] command` による `fs.protected_hardlinks=1` の設定
- WSL 内での Claude Code / Codex のサブスクリプションログイン（agent ユーザー）と、実際のモデル実行
- `review` コマンドで契約のレビュアーを実際に動かすこと（経路の拒否条件だけをテストした）
- 残留プロセスの停止は、AI 側に stub を使って確認した。本物の Claude Code / Codex が SIGTERM を受けたときの終わり方は未確認
- `wsl.exe` が戻ったあとも Controller が動き続けるか（WSL がアイドル時にディストリビューションを止める場合がある）。
  止まった場合も、次の `start` で古いワーカーと PID ファイルは安全に扱われる（コンテナで確認済み）
- PC のスリープ・休止・シャットダウン後の挙動、長時間の運転

## 残るリスク

- agent ユーザーは自分の Claude / Codex のログイン情報を読める（AI CLI が使うため）。AI が書いたコマンドも agent として
  動くので、そのログイン情報は AI から守られない。GitHub の認証情報は agent に渡らない（隔離の検査で確認）。
- ネットワークの制限は無い。agent は外部に通信できる。機密データは扱わない。
- Windows ユーザーは `wsl -u root` で WSL の任意のユーザーになれる。ここでの隔離は AI（aiwos-agent）に対するもので、
  Windows 側の利用者や Windows 上のマルウェアに対するものではない。
- 1 つの設定が監視するのは 1 つのデータ用リポジトリだけ。複数のプロダクトを横断する運用は別の機能が必要。
- GPT / ChatGPT の最終判断は CLI から無人では起動しない。人間がレビュー結果を見て判断する。
