# VER-0002 — worker brief (for: claude-code)

- task_type: `development` (profiles/development.md)
- status: `ready`, retry 0/2

## 目的
Synthetic verification task for the v0.3 controller (no product value): create the file tasks/VER-0002/out.txt whose content is the single line 'done'.

## 入力
- （なし）

## 制約
- synthetic verification task; change nothing outside tasks/VER-0002

## 受入基準
- **AC1** (spec_conformance) out.txt says done — 検証: command
  - `grep -q done tasks/VER-0002/out.txt`
- **AC2** (correctness) out.txt is not empty — 検証: command
  - `test -s tasks/VER-0002/out.txt`
- **AC3** (completeness) nothing else needed — 検証: inspection

## 失敗条件
- **FC1** file missing → rework

## 作業規則
- 受入基準と制約の範囲だけを作業する。基準を自分で変えない。
- 完了の申告は合格ではない。各受入基準の検証コマンドを実行し、コマンドと終了コードを証拠として報告する。
- 失敗条件に該当したら作業を止め、該当した条件 ID を報告する。
- 秘密情報をコード・ログ・報告に書かない。

## 実行環境の規則（AI WORK OS runner）
- この作業ディレクトリはこのタスク専用のコピーです。ここ以外のファイルを読まない・変更しない。
- git commit / git push をしない。ファイルを編集するだけでよい（runner が差分を記録しコミットする）。
- 変更してよいパス: tasks/VER-0002
- 受入基準の検証コマンドは runner が別途実行し、その結果が判定に使われる。あなたの完了報告は合格の根拠にならない。
- 最後に、何をどう変えたかを3行以内で報告する。
