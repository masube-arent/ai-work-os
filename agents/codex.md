# Codex CLI

## 役割

代替実装・独立レビュー・原因分析。

## 状態

**この初期構築環境には Codex CLI がインストールされていない。** 起動方法・オプションは実機で未確認。
`scripts/doctor.sh` は Codex CLI がある環境で `codex --version` と `codex --help` を実行し、
ヘルプに記載された実行方式（`exec` サブコマンド、`--sandbox`、`--ask-for-approval` の有無）だけを表示する。
ここに書かれていないオプションは使わない。

## 起動（Phase 1: 手動）

```bash
cd ai-work-os
python3 scripts/render-brief.py examples/development/task.json --role reviewer
codex             # 対話モードで起動し、上の出力を渡す
```

## 権限

- 作業規則は `AGENTS.md`（Codex CLI が読み込むリポジトリ指示ファイル）。
- サンドボックス・承認モードの設定は、`doctor.sh` で存在を確認してから
  `core/permissions.md` に追記する。それまではレビュー（読み取り中心）用途に限定する。
