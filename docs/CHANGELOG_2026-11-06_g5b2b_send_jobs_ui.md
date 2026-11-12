# 2026-11-06 G5-b2b 外部AIへの送信画面

## 反映内容

- 配布元: `C:/Users/kanto/Short-video-local/stage4/final70/LOCALSAPORTER_deploy_20261105_g5b2b_send_jobs_ui.zip`。ZIP SHA-256 `94D7AAC131B419EEB047A47FC2BBA6BAC72571549E6AA435F682750F9CC220D2`。`hashes.txt` の4ファイルと展開時に照合した。
- G5-b2aの承認画面の下に「外部AIへの送信」を追加。承認済み内容の再確認、明示チェックと承認者名によるジョブ作成、ジョブ状態と取消を扱う。送付本文はブラウザに再表示するが、外部下書き本文は一覧へ表示しない。
- 読み取り専用 `GET /api/projects/{id}/plan-packets/{packet_id}/job-key` と静的JS配信ルートを追加。`index.html` の差分はJSタグ1件だけ。
- 配布物の確認で、ジョブ一覧APIがフラグ未設定でも空一覧を返すため、送信ボタンが有効になり得ると判明。読み取り専用 `GET /api/projects/{id}/planning-job-capabilities` を追加し、既存の `enabled_for_project` と同じ判定を画面へ返す。取得失敗も送信不可にする。サーバー側の送信ゲートは維持。
- 結果不明時の再送ロックは、同じpacketの古いジョブでは解除せず、今回の冪等キーと一致するジョブを確認できたときだけ解除するよう補強。両件の回帰試験を追加。
- 計画作成・独立評価の機能フラグは **未設定のまま**。実際の外部AI送信、送付承認、PJ設定変更は行っていない。

## 検証

- G5-b2b、G5-b2a、G5-b1、G5-aの関連試験はNode.js付きで **115 passed**。記録 `output/g5b2b_send_jobs_focused_20261106.txt`。
- 全体試験は **2912 passed / 10 failed / 3 skipped**、384.24秒。失敗10件の試験名は前回基準と差分0であり、新しい失敗はない。ただし全体合格ではない。記録 `output/g5b2b_send_jobs_full_20261106.txt`。
- `scripts/healthcheck.ps1` は全項目PASS。記録 `output/g5b2b_healthcheck_20261106.txt`。
- Webは `running / healthy`、イメージ `sha256:c83241489aed006543e4c1b962ea5e0f21bbe76e810da4ced08aae73e7cd6d3f`。新JSはHTTP 200。コンテナ内の計画作成・独立評価フラグはともに未設定。
- 実ブラウザで公開デモPJを選び、送信画面がG5-b2aの下に表示されること、フラグ未設定時に「この機能は管理者の設定で無効です」と表示されることを確認。外部送信操作は行っていない。

## 保全・巻き戻し

- 旧 `app/web.py` と `app/static/index.html` は `backups/20261106_g5b2b_send_jobs_final70/old/`、配布ZIP展開版は同階層 `stage/` に保全。
- 旧Webイメージ: `localsaporter-web:before-g5b2b-20261106` = `sha256:3891cf2e0478f69d320fd861e7369b8bd442bd31ea3d2caaa72d97d998629419`。
- 巻き戻す場合は旧ファイルと旧イメージを戻す。データやDockerボリュームは削除・初期化しない。

## 最終ファイルSHA-256

| ファイル | SHA-256 |
|---|---|
| `app/static/planning_guard_jobs.js` | `B8DE69C903D041D8F185A77D5BCA7F07651E596547E189F6F564CB483A8BAA8F` |
| `app/static/index.html` | `8BB6F1B77D390012C7A73BC743781904180C8C2E296D8C8E911` |
| `app/web.py` | `A3B92834460FB6B0728620B625429E000CDADCCC07297A673CFEC246EF9767B3` |
| `tests/test_g5b2b_planning_guard_jobs.py` | `8CBF7DD726D38FCDED4A22448B4B37783B97264C3C41A22A5BBEAA46AE18ACD0` |

## 残る制約

- UIの結果不明ロックは画面内の状態。ページ再読込後の再送を最終的に防ぐのはサーバーの冪等キー、使用済み許可、日次上限である。
- フラグを有効にした状態での実外部送信は未検証。今回の反映ではフラグを変更していない。
