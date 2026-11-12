# 2026-11-05 G4-c 独立評価の実行開始ゲート 本番反映

## 反映内容

- 配布物 `LOCALSAPORTER_deploy_20261105_g4c_start_gate.zip`（SHA-256 `0901D3AB5994AB9E65C4D16115982389E8564555047D315BF5C980FC7452F0A1`）の5ファイルを `hashes.txt` と照合し、差分を審査して適用。
- `app/independent_review_gate.py` を追加。`external_guarded` のPJでは現在の計画署名に結び付いた有効な独立評価 `review_passed` を実行開始の必要条件にする。未評価・不合格・不明・設定読取不能はそれぞれ固定コードで停止する。ゲートは状態を書き換えない。
- `ProjectOrchestrator.start` の既存承認ゲート直後に強制チェックを追加。`workflow_readiness.py` に開始できない理由の表示を追加。既存の人間承認や他のゲートは維持した。
- 配布手順で未確認とされた `safe_auto_resume.resume` は `ProjectOrchestrator.start` を通らず、ローカル文書タスクを直接実行することを確認。将来の迂回を防ぐため、同じゲートを自動再開の入口にも追加した。現在、自動再開フラグは無効。
- 配布物のG4-c試験に自動再開の迂回防止試験を1件追加。旧P2自動再開試験は設定ストア全体を偽装していたため、そのfixtureだけ新ゲートを明示的に模擬した。実ゲートの試験はG4-cの専用試験で行う。
- 配布物から補正・追加した3ファイルの最終SHA-256: `app/safe_auto_resume.py` = `16C7673E6DB726011CE4D88AE3915AB3B4D9AAAFA9C52DC96A7A79CB1F4203D1`、`tests/test_g4c_independent_review_gate.py` = `00C58A59C12549284230C3D321E1C84FB3A578439C1982490C2789E572F9FDC2`、`tests/test_p2_safe_auto_resume.py` = `58AF4CCD5A6198F7A1FE475FD37FE7E77B9798A6A81825D8E6FDCF8C1563CBD8`。元の `hashes.txt` と相違するのは、この安全補正・試験補正による。

## 検証

- `scripts/healthcheck.ps1` 全項目PASS。Web healthy、選択Ollamaモデル `qwen3.5:9b`。新イメージ `sha256:40e1f1b9f727d68ba52c38e609d10a9813b1688b6b548163b8d27ed09e582f4f`。
- G4-c/G4-b/G4-a/G3-a/目標評価/P2自動再開: **114 passed、警告1**。`output/g4c_start_gate_focused_20261105.txt`。最初の試験では旧fixture偽装に起因する6件が失敗し、fixtureを補正して再試験で解消した。
- 全体回帰試験 (`PYTHONUTF8=1`, `PYTHONHASHSEED=3`): **2795 passed / 10 failed / 5 skipped、警告1、811.21秒**。前回G4-bとの失敗試験名の差分は0。新規失敗なし、全体合格ではない。`output/g4c_start_gate_full_20261105.txt`。
- `SEBAS_INDEPENDENT_REVIEW_PROJECTS`、`SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS`、`LOCALSAPORTER_AUTO_RESUME_AVAILABLE` は未設定のまま。実外部AI送信および実PJの実行は行っていない。

## 限界

- G4-cは `external_guarded` のPJに適用される。設定なし・`local_only` のPJは従来の実行開始条件を維持する。破損した設定は安全側で停止する。
- 評価合格は開始の十分条件ではなく、既存の承認・実行ゲートも必要。独立性の評価範囲は提供元IDと実モデル名まで。
- 自動再開の入口は補正したが、将来追加される別の実行入口は同じゲートに接続する必要がある。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 変更前の5ファイル、停止時の `/data/memory`、展開物を `backups/20261105_g4c_start_gate_final66/` に退避。旧Webイメージ `localsaporter-web:before-g4c-start-gate-20261105` = `sha256:f2a9fb332f931c6418d8744172f1e7e91618df16f3e3c74f226faf31ce5851b5`。
- 復旧時はWebだけを停止し、退避した5ファイルを戻し、新規 `independent_review_gate.py` とG4-c試験を除外して旧イメージで再作成する。保存データは影響を調べずに巻き戻さない。



