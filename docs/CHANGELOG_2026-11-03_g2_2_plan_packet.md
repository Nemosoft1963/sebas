# 2026-11-03 G2-2 送信用packetの生成と承認 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261102_g2_2_plan_packet.zip`（SHA-256 `A867FFE56521F00C70251627830B3C39D026D4DF383DB07D6930218B899CFC57`）の3ファイルを `hashes.txt` と照合し、`app/web.py` の追加差分154行を審査して適用。
- 新規 `app/planning_packet.py` は、APIで明示した公開項目からだけ送信本文を作り、G2-1の情報漏えい検査後にプレビューとして保存する。拒否時は本文と本文ハッシュを保存しない。承認時は計画署名・本文ハッシュ・原本の再検査を行い、G1の用途別許可として保存する。
- `app/web.py` にpacketのプレビュー、承認、取得、取り消し、監査の5 APIを追加。実行中の承認を拒否する。`project_manager.py` の既存送信経路は変更していない。
- ホストと本番コンテナの `planning_packet.py` SHA-256 `4C976AB3DAA6976AE7E15ED27613EA9C0795ADEAC0F5F734E9283574DA5A3293`、`web.py` SHA-256 `FCCA10C386E83360CDE51F254F284CAE786B28E55443C8DDF70418DB3AC657DA` が一致。新Webイメージ `sha256:5e082dada78d84c1f5ea4654863ea7a97bf1d0e2627d1cdb9fa9fd0d547ca017` はhealthy。

## 検証

- G2-2、G1、G2-1、G0-1関連試験: **87 passed**。ログ: `output/g2_2_plan_packet_focused_20261103.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2700 passed / 10 failed / 5 skipped**、675.39秒。失敗10件は直前G2-1本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g2_2_plan_packet_full_20261103.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。
- 実PJでのpacket承認、実外部AI送信、実PJ通し実行は行っていない。

## 限界・次工程

- **G2-2は外部AI送信経路に未接続**。packetの承認がなくても、G0-1/G2-1の送信前検査を通過した従来本文は自動送信され得る。G3で送信直前のpacket・用途別許可を強制する必要がある。
- 公開項目を入力する専用画面は未実装。API操作のみ。拒否リスト検査の通過も安全保証ではない。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/web.py`、停止時の `/data/memory`、展開済みファイルを `backups/20261103_g2_2_plan_packet_final60/` に退避。旧Webイメージ `localsaporter-web:before-g2-2-plan-packet-20261103` = `sha256:04b0961b4530a1af917a3eceb36f3120d1c7a48a657be8f51f58d0a1aabdd0d0`。
- 復旧時はWebだけを停止して退避した `app/web.py` を戻し、新規 `app/planning_packet.py` と試験ファイルを除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
