# Goal Completion Phase 3-C

- 車両案件の未解決事項を影響金額順、最大5件で返す読み取り専用質問ビューを追加。
- 質問カードは不明点、停止条件、確認済みfacts、既存候補の影響、既知範囲の再実行対象を表示する。未知値や按分比率は生成しない。
- `goal_facts` を追加。`project_id + fact_key` ごとに版を1から増やし、訂正時も旧版を保持する。回答者名は必須。
- flag有効案件専用の質問回答APIを追加。既存 `decide` 成功後だけfactを記録する。flag OFFは409 `FLAG_OFF` とし、既存回答画面を維持する。
- 既存 `/vehicle-profit/decision` と `artifact_shelf.js` は変更していない。
- 進行パネルへ安全な `textContent` 描画の最小質問UIを追加。stale、空回答者、二重送信を拒否する。
- 実ブラウザ、本番案件・DB、OCR、GPUを用いた確認は実施しない。
