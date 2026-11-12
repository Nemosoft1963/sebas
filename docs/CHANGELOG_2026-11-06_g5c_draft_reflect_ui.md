# 2026-11-06 G5-c 外部AIの下書きと計画の作り直し画面

## 反映内容

- 配布元: `C:/Users/kanto/Short-video-local/stage4/final71/LOCALSAPORTER_deploy_20261107_g5c_draft_reflect_ui.zip`。ZIP SHA-256 `BB09B7A1B425324D161162834F79643444DA03AF3272D840832C16D0D414C05C`。展開時に `hashes.txt` の4ファイルと照合した。
- 作業開始時、本番にはG5-cの4ファイルが未反映だった。現行 `app/web.py` と `app/static/index.html` の差分は、静的配信ルートとスクリプトタグ各1件のみと確認した。
- G5-b2bの下に「外部AIの下書き」区画を追加。未検証の参考情報として下書きを読み、実行者名とチェックを伴う確認画面からローカルAIで計画を作り直す。計画の作り直しは既存の `POST /mission/plan/generate` を使う。外部送信、独立評価、計画承認、実行開始はこの画面では行わない。
- 新規のサーバー変更は `planning_guard_drafts.js` の静的配信ルートだけ。外部送信と独立評価のフラグは未設定のまま。

## 検証

- G5-c、G5-b2b、G5-b2a、G5-b1、G5-a、fail-open検査の関連試験: Node.js付きで **145 passed**。初回は試験コンテナに `fail_open_baseline.json` が無く3件失敗したが、基準ファイルを入れた再試験で全件成功。記録 `output/g5c_draft_reflect_focused_20261107.txt`。
- 全体試験: **2932 passed / 10 failed / 3 skipped**、400.77秒。失敗10件は前回G5-b2bと試験名差分0。全体合格ではない。記録 `output/g5c_draft_reflect_full_20261106.txt`。
- `scripts/healthcheck.ps1` は全項目PASS。記録 `output/g5c_healthcheck_20261106.txt`。
- Webは `running / healthy`、イメージ `sha256:7fe461abdaf16257ac86b7a424b9fc5ef2d2aa3393b1bc2e4bc75135a82a1b4f`。新JSはHTTP 200。計画作成と独立評価の送信フラグはともに未設定。
- 実ブラウザで公開デモPJを選び、G5-b2bの下にG5-c区画、現在の計画版、下書きが無い表示、機能無効の表示を確認。実下書きでの計画置換は実行していない。

## 保全・巻き戻し

- 旧 `app/web.py` と `app/static/index.html` は `backups/20261107_g5c_draft_reflect_final71/old/`、配布ZIPの展開版は同階層 `stage/` に保全。
- 旧Webイメージ: `localsaporter-web:before-g5c-20261107` = `sha256:c83241489aed006543e4c1b962ea5e0f21bbe76e810da4ced08aae73e7cd6d3f`。
- 巻き戻す場合は旧ファイルと旧イメージを戻す。PJの計画やDockerボリュームは削除・初期化しない。

## 最終ファイルSHA-256

| ファイル | SHA-256 |
|---|---|
| `app/static/planning_guard_drafts.js` | `AECF5B82920DFCA9A7C9A2282B0A5A15A600EB9056F3DB401175AC97FE0CD53A` |
| `app/static/index.html` | `63073990AD23E1F04FCFE3282E286EDF252E3D614C753E09F171E8822CB95FB1` |
| `app/web.py` | `26EFFE13915E1B088FB7EDC3C727338F3BF63A1FE2C434454A3DD25DBFDBBCA9` |
| `tests/test_g5c_planning_guard_drafts.py` | `BD4F28ACD8142B2F1A67C95AAF409CAC9A5C4E68FAA6947FB7704997C7F1C20E` |

## 残る確認

- 実際の外部AIジョブで生成された下書きの表示と、ローカルAIによる実計画の置換は未確認。フラグは未設定で、今回PJ設定・計画は変更していない。
