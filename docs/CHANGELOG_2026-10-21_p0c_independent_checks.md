# 2026-10-21 P0-C 修正案の独立検査

## 反映内容

- 添付 `LOCALSAPORTER_diff_20261021_p0c_independent_checks.zip`（SHA-256: `FBB8B985FAAB232EBA7321CA6A42B848E5900CC5408D85CD376B4031F596D4DD`）の `app/plan_feedback.py` と `tests/test_p0c_independent_checks.py` を確認して反映。
- 修正案の生成・修復・適用直前に、指摘ID、明示的な連絡先と日付、未確認の成功扱い、承認境界、成果物数を決定的に検査する。指摘ごとの差分と開発指示の記録を追加。
- 添付コードの「成果物数が判定不能でも `p0c_checks.passed=True`」を修正。判定不能時は `passed=False` と残項目を記録し、適用直前の再検査で停止する。
- 候補のシリアライズ失敗時に変更前の値を代用して通過させる経路を停止。確認不能としてエラーにする。
- 新規テストで判定不能の非合格記録と適用直前の拒否を検証。

## 退避と巻き戻し

- ソースとテスト: `backups/p0c_independent_checks_preapply_20261021/`
- Web イメージ: `localsaporter-web:pre-p0c-independent-checks-20261021`
- データボリューム: `localsaporter_app_data_pre_p0c_20261021`（退避時 72.8 MB）
- 巻き戻す場合は Web を停止し、退避したソースとイメージを戻して再作成する。データボリュームの復元は実データの更新を消すため、実施前に差分を確認する。

## 検証

- 隔離コピーの関連試験: 25 passed。
- 本番コードの関連試験: 25 passed（安全修正後も 25 passed）。
- 本番ヘルスチェック: PASS。Web、Ollama、LANプロキシ正常。
- コンテナ内と本番ソースの `plan_feedback.py` SHA-256一致: `0C86F0969AAAC9F326CFB27FAE4A62E646997F3BCA9E90AE60452002C89952B7`。
- 本番全体試験: **2284 passed / 12 failed / 5 skipped**（538.17秒）。12件の失敗名はP0-B時点と完全一致し、新規失敗0。読み取り専用E2Eも全体試験に含む。詳細ログ: `output/p0c_independent_checks_full_20261021.txt`。

## 制限

- 実ローカルLLMによる案件実行と外部AI呼出は今回の受入対象外。試験は疑似応答で独立検査を確認した。
- 自由記述の場所や未分類の構造的欠陥について、文意からの完全な検出は保証しない。
- 既存のOCR実PDF受入・実案件受入・業務確認事項36項目は未完了のまま。