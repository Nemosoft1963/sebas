# 2026-10-24 実走で残った課題の修正・本番反映

## 適用範囲

受領ZIP `LOCALSAPORTER_diff_20261024_postdeploy_residuals.zip`（SHA-256 `1C149F286F43018B5ACAD5D5BA36DEE74029345807EC0EDD6A0347834F085ACF`）の既存8ファイルを審査して本番ソースとWebイメージへ反映した。本体は `app/project_manager.py`、`app/requirement_matrix.py`、`app/requirement_verification.py`、`app/structured_planning.py`、試験は `tests/test_p02_run_closure.py`、`tests/test_p0b_failure_repair_loop.py`、`tests/test_p0c_artifact_grounding.py`、`tests/test_p11_verification_quality.py`。反映前に本体4ファイルが前回のfinal44版とSHA-256一致することを確認した。

受領版の安全上の過大な判定を2点補正した。相対時刻の「N分前」は、登録された準備開始とイベント開始の一意な組の差、かつ該当する文脈でだけ認める。別の時刻の偶然の差や「N分後」は認めない。また、ある成果物の必須語句を別の成果物の見出しで充足させない。対応する回帰試験を追加した。

バックアップは `backups/20261024_postdeploy_residuals_final45/`（元ソース8件、`/data`、項目台帳DB）に保存した。DB `PRAGMA integrity_check=ok`。旧Webイメージは `localsaporter-web:before-postdeploy-residuals-20261024`。Docker volumeの削除、既存PJの初期化、外部AI送信は実施していない。

## 検証結果

- 関連試験: `-W error::RuntimeWarning` で **48 passed**。未待機コルーチン警告なし。StarletteDeprecationWarning 1件。
- 全体試験: `PYTHONHASHSEED=3`、**2370 passed / 12 failed / 5 skipped**。失敗12件はfinal44反映時と同じ既知の12件で、新規失敗はない。受領文書にある `10 failed / 14 skipped` はこの本番ホストでは再現しなかった。
- 最終Webイメージと稼働コンテナのIDはともに `sha256:0f73678d0d4e8911204086fd442e8908779fa848ca3a44c42f274bc9fb451692`。審査済みステージと本番ソース8件のSHA-256は全件一致。ホスト/コンテナ内の主要2ファイルも一致。
- `scripts/healthcheck.ps1` は最終配置後に全項目PASS。選択モデル `qwen3.5:9b`。起動直後の一時的なWebUI/プロキシ準備中は、安定後に解消した。

## ローカルLLMによる架空PJ実走

実在の案件を使わず、架空の「青空ひろば」で案内文と運営チェックリストを別ファイルとして作る同一条件を2回試した。初回はLLMが案内文にチェックリスト見出しを重複させ、計画生成を3回で確定できず停止。再試行は計画生成・承認・実行・案内文/チェックリスト/最終検証書の生成まで進み、時間切れなしで実行サイクルが `finished` となった。目標は `unmet`、`pending_kind=human_decision`、残件10件。未達を成功に変えていない。検証書 `result/final_verification.md` の保存参照とSHA-256は実ファイルと一致。

最新項目台帳は23行中 **PASS 13 / FAIL 3 / UNTESTABLE 7**。準備開始の対象成果物は正しくなり、該当項目はPASS。一方、「架空の問い合わせ先を含め」「終了後確認を含める」は依然として命令文全体が必須語句になり、2件FAIL。残り1件は文意判定が架空設定を矛盾と誤解したFAIL。原本外の「徒歩5分」「0歳」「年齢制限」「保証」などは要確認として残った。ローカルLLMの文意項目には失敗・引用不一致による人判断待ちもある。結果は `output/local_llm_autonomy_20261023/postdeploy_final45_retry/results.json`、生成物は同ディレクトリの `isolated/workspace/projects/aozora_autonomy/result/`。

## 残課題

`app/requirement_matrix.py` の `_p0a_core_items` は実装されているが、実際の項目生成経路から呼ばれていない。単純に接続するとP0-B修復試験5件の前提を壊したため、試行変更は撤回した。次回は要求節の核語化・見出し項目との重複除去・自動修復の対象を一体で再設計し、問い合わせ先と終了後確認の実走再現ケースを受入試験化する必要がある。未達と判断待ちを自動承認で埋めてはならない。

実案件の通し受入、OCR実PDF受入、業務確認事項36項目は本反映では完了していない。
