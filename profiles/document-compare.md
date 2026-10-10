# PROFILE: document-compare（文書比較）

2つ以上の文書の差異を検出し、一覧化する。契約書・仕様書・規程の版比較を想定する。

## 入力

- 比較元・比較先の文書（テキスト。PDF/Word は Phase 6 以降で抽出手順を定める）
- 文書は事前に正規化（改行コード・文字コード）しておく
- 機密文書は `kind: reference` とし、本文をリポジトリに置かない

## 受入基準の書き方

| 品質軸 | 典型的な基準 | 検証方法 |
|---|---|---|
| spec_conformance | 指定の出力形式（区分・行番号・内容） | `inspection` |
| correctness | 報告した差異が実在する（誤検出ゼロ） | `command`（機械 diff と照合） |
| completeness | 機械 diff の全ハンクを報告している（見落としゼロ） | `command` |

- 機械 diff（`diff`、`git diff --no-index` 等）を一次証拠とし、AI の報告はそれと照合する。
- 表記ゆれ・意味的同一性の判断は「解釈」として事実と分けて記録する。

## 既定の失敗条件

- 見落とし、誤検出 → `rework`
- 入力文書の改変 → `escalate`

## 証拠

- 機械 diff の出力（`command_output`）
- 差異一覧（`file`）
- 照合結果（`review_note`）
