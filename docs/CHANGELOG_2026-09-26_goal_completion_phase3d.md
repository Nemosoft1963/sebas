# Goal Completion Phase 3-D

- 汎用案件向けに、ローカルLLMが原文から要求の節を逐語引用する分解 (`POST /goal-contract/decompose`) を追加した。保存しない。
- LLM出力は信用しない。空白正規化のみ許して原文に含まれる引用だけを採用し、言い換え・要約・捏造は `dropped(not_verbatim)` にする。
- 原文対応表はコードが文分割（。．、改行、`;`、箇条書き行頭）して作り、どの採用項目とも重ならない文を `uncovered` として返す。
- 人間確認は削除・統合のみ。`reviewed_by` 必須。確認後は契約ドラフトを保存し、`activate` はしない。flag OFF は 409 `FLAG_OFF`。
- 汎用案件の `evaluate` は従来どおり `achieved` を出さない（車両以外は `UNTESTABLE` / `GENERIC_NOT_ACHIEVED`）。`settle_achieved` はゲート不合格で拒否される。車両経路は変更していない。
- 既存の goal-contract GET/PUT/activate/retention-check は変更していない。
- 実Ollamaでの分解品質、本番案件・DB、OCR、GPUは未検証。
