# Claude Code

## 役割

技術調査・実装・修正・テスト。レビュアーとして他エージェントの成果物も検証できる。

## 起動（Phase 1: 手動）

```bash
cd ai-work-os
python3 scripts/render-brief.py examples/development/task.json > /tmp/brief.md
claude            # 対話モードで起動し、brief.md の内容を渡す
```

この環境で `claude --help` により確認できた実行方式（`scripts/doctor.sh` が毎回表示する）:

- 対話モード（既定）
- 非対話モード `-p` / `--print`
- `--permission-mode`、`--disallowedTools`

Phase 1 では非対話モードでの自動起動は行わない（Phase 2 で、ドライランから導入する）。

## 権限

- このリポジトリの `.claude/settings.json` が適用される（force push・`git reset --hard`・
  `rm -rf`・`curl`/`wget`・`.env` 読み取りを deny、`git push` は都度確認）。
- 作業規則は `CLAUDE.md`。
