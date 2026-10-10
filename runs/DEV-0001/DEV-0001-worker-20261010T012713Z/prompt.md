# DEV-0001 — worker brief (for: claude-code)

- task_type: `development` (profiles/development.md)
- status: `ready`, retry 0/2

## 目的
greet() に言語指定を追加し、lang="ja" のとき日本語で挨拶できるようにする。既存の英語の挙動は変えない。

## 入力
- `src` [file, public] examples/development/src/greet.py — 変更対象のプログラム
- `existing-tests` [file, public] examples/development/src/test_greet.py — 既存テスト。変更後も通ること

## 制約
- 変更は examples/development/src/ 配下に限る
- 外部ライブラリを追加しない
- 既存テストを削除・無効化しない

## 受入基準
- **AC1** (spec_conformance) greet("Ada", lang="ja") が "こんにちは、Adaさん" を返す — 検証: command
  - `cd examples/development/src && python3 -c 'from greet import greet; assert greet("Ada", lang="ja") == "こんにちは、Adaさん"'`（期待: exit 0）
- **AC2** (correctness) 既存テストと追加テストがすべて通る（英語の挙動が不変） — 検証: command
  - `cd examples/development/src && python3 -m unittest -v`（期待: exit 0, failures=0）
- **AC3** (completeness) lang="ja" のテストが追加され、未対応の lang の扱いが文書化されている — 検証: inspection

## 失敗条件
- **FC1** 既存テストが失敗する → rework
- **FC2** 制約外のファイルが変更される → escalate
- **FC3** テストを削除・skip して合格させようとした → escalate

## 作業規則
- 受入基準と制約の範囲だけを作業する。基準を自分で変えない。
- 完了の申告は合格ではない。各受入基準の検証コマンドを実行し、コマンドと終了コードを証拠として報告する。
- 失敗条件に該当したら作業を止め、該当した条件 ID を報告する。
- 秘密情報をコード・ログ・報告に書かない。

## 実行環境の規則（AI WORK OS runner）
- この作業ディレクトリはこのタスク専用のコピーです。ここ以外のファイルを読まない・変更しない。
- git commit / git push をしない。ファイルを編集するだけでよい（runner が差分を記録しコミットする）。
- 変更してよいパス: examples/development/src
- 受入基準の検証コマンドは runner が別途実行し、その結果が判定に使われる。あなたの完了報告は合格の根拠にならない。
- 最後に、何をどう変えたかを3行以内で報告する。
