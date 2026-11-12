# 2026-11-10 AG2 計画評価への構造伝達

## 反映結果

- 配布元: `LOCALSAPORTER_deploy_20261110_ag2_plan_review_structure.zip`。ZIP SHA-256: `F25E0F62D4C09F121296FDDE1F2FD10F3A675B36D27EABE28059C66A5D23EC3F`。同梱 `hashes.txt` の7件と展開内容を照合した。
- 現行AG1コードを確認し、Webだけを停止。旧コード5件と `/data` を `backups/20261110_ag2_plan_review_structure/` に保全した後、7ファイルを本番に配置しWebを再ビルド・起動した。
- `SEBAS_PLAN_GATE_V2_PROJECTS` は未設定のまま。既存PJの開始ゲートは切り替えていない。外部AIへの実送信は行っていない。

## 変更内容

- `plan_review` 用の送信packetプレビューに、工程の種別、依存、出力種類、先行工程、検証方法、承認境界を固定語彙で示す計画構造を付加。構造はサーバー側で生成し、クライアントからの構造テキストは採用しない。送信には従来どおりpacket承認が必要。
- 独立評価の指示を実行前の計画評価に合わせ、成果物本体の未作成だけを理由に判定不能としないよう明示。新規評価ジョブに `review_spec=plan-review-v2` を記録する。
- v2ゲートでは現行仕様の合格評価が必要。旧仕様の評価を自動で合格扱いにしない。従来の `gate_error` は変更していない。

## 配布物の監査で補った修正

旧仕様の正常な合格評価と、新仕様タグを持つ不完全な評価を別々に満たしてv2が通る経路を発見した。**同じ評価1件**が現行仕様と既存の合格要件（packet SHA、model、評価時刻を含む）を満たすことを確認するよう修正し、回帰テストを追加した。このため、下記2ファイルは配布元 `hashes.txt` と異なる。

| ファイル | 本番配置SHA-256 |
|---|---|
| `app/plan_review_structure.py` | `6EEFACC366B9B255FAECC5496BEBCEEB75B560EFD8D9ED4ED8B68C856D394A99` |
| `app/independent_review.py` | `C7B047239D388A6CB0B626C249F7E0633A2B4BABA45FC2A80ED0ADC7A00B45D8` |
| `app/plan_gate_decision.py` | `29E1FE9DF3D3FA9B1BEAE2A6B35293BA0F2526FD76AC180ED9FA4D51E0C8922F` |
| `app/planning_packet.py` | `1697AE6824E812C6D6DB56313E427EC276681EB486A453418CA2D65FA0DD80F6` |
| `app/web.py` | `40A9730FFCCE88ACF3856E062EC5E67ADC3DCCF17BD3D97E51A744133CDF1CD9` |
| `tests/test_ag1_plan_gate_decision.py` | `4AA763A89571A585175236B9C99AB9463A5F50AF238E55D76E599FD710DE3B61` |
| `tests/test_ag2_plan_review_structure.py` | `3948609205EB763BCF4F9ACF537538A039DD0C9FD0FA0F6DA722CF3FBD6C96D9` |

## 検証

- 隔離コンテナのAG2・AG1・独立評価・fail-open関係試験: **108 passed**。
- 全体試験: **2908 passed / 10 failed / 83 skipped**。失敗10件はAG1反映後の記録と試験名が完全一致。全体合格ではない。記録: `backups/20261110_ag2_plan_review_structure/full_suite_final_20261110.txt`。
- 本番コンテナ内の5コードファイルのSHA-256一致を確認。`scripts/healthcheck.ps1` は起動直後のProxy起動待ちで一度FAIL、再実行で全項目PASS。WebとProxyはhealthy。

## 限界

- v2の架空PJでの実走、外部AIの実応答評価は未実施。現時点ではコード反映と隔離試験まで。
- 実行前packetの構造は入力事実の充足を保証しない。最終検証工程の検証方法が「不明」と表示される場合がある。
- 設計案のP0-3、P0-4、P1-1、P1-2は未実装。OCR実PDF受入、車両損益の実案件受入、業務確認事項36項目も未完了。

## 差し戻し

1. `docker compose stop web` でWebだけ停止する。現行コードを別途保全する。
2. `backups/20261110_ag2_plan_review_structure/old/` の旧コード5件を元の位置へ戻し、新規の `app/plan_review_structure.py` と `tests/test_ag2_plan_review_structure.py` を除く。
3. `docker compose up -d --build --no-deps web` で起動し、`scripts/healthcheck.ps1` を実行する。`/data` は無条件に戻さず、必要な場合にだけ `data_before_ag2` から個別に復元する。

