# 2026-11-04 G3-b 外部下書きのローカル統合 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261104_g3b_external_draft_hint.zip`（SHA-256 `EA68D7A6AE7AA330FAE0D6A76059B04DFC3B518AA5B5134F6F38C99B4236540D`）の4ファイルを `hashes.txt` と照合し、差分を審査して適用。
- `app/external_draft_hint.py` は外部生成ジョブのPJ・状態・計画署名を検証し、外部下書きから最大12工程のタイトルと短い要旨だけをローカルLLMへの参考文に整形する。外部工程の出力経路・依存・未確認事実・完了主張を直接計画に取り込まない。
- `app/project_manager.py` は、指定された場合だけContextVarに参考文を設定して構造化計画の依頼文末尾に追加し、処理後に必ず復元する。車両損益の決定的経路や従来計画では `unsupported_plan_path` として止める。本文を含まない利用イベントを記録。
- `app/web.py` の計画生成APIは任意の `external_job_id` を受ける。未指定の従来経路は維持。指定時は機能フラグ、ジョブ、署名を検証し、ローカルLLMで計画を生成する。外部下書きの成功を評価合格・人の承認・実行可能とみなさない。
- ホストと本番コンテナのSHA-256: `external_draft_hint.py` = `058E6A9B5B34C9A889BFE1EF972C22C69A49D87479E9FABBBEF70BD47DCEDF76`、`project_manager.py` = `ADBC2FE3EB5D8621161A681CAEE871C57B70C4C47FE47A995128750B87E7565C`、`web.py` = `41E361827F708275E50A2A24DB5EF19CCF4DFC1DDEAB308A93B841E1A122B4B6`。試験ファイル = `0DA851B031A63889CCA7642DFC682823C5430F12D5CC61CD7B7CC3391A869CF3`。Webイメージ `sha256:151dcfba9836fbcf7265064ea22a95f02437d454ef65bd057b16679f2f1cea5c` はhealthy。

## 検証

- G3-b、G3-a、計画入力分離、構造化計画、目標検証の関連試験: **101 passed**。ログ: `output/g3b_external_draft_hint_focused_20261104.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2736 passed / 10 failed / 5 skipped**、721.32秒。失敗10件は直前G3-a2本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g3b_external_draft_hint_full_20261104.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。`SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS` は空で全PJ無効。
- 実外部AI送信、実ローカルLLMによる外部下書き採用試験、実PJ通し実行は行っていない。

## 限界・次工程

- 外部骨子は参考文であり、計画品質の向上やローカルLLMが参考を使うことは保証されない。効果は実PJまたは公開用ダミーPJで別途測定が必要。
- 車両損益の決定的経路、従来計画、決定的な達成条件提案では参考を使わない。画面は未実装。既存4つの外部送信経路も未変更でpacket許可が未接続。
- 拒否リスト方式の検査通過は安全保証ではない。OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/project_manager.py`、旧 `app/web.py`、停止時の `/data/memory` と展開済みファイルを `backups/20261104_g3b_external_draft_hint_final63/` に退避。旧Webイメージ `localsaporter-web:before-g3b-external-draft-hint-20261104` = `sha256:f5c59f2edef9997ba071c389584cd401883954f65933fa35a7b2c351b96d8b7f`。
- 復旧時はWebだけを停止して退避済み2ファイルを戻し、新規 `app/external_draft_hint.py` と試験ファイルを除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
