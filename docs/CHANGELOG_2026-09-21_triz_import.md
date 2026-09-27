# 変更記録 2026-09-21: TRIZ収集データの取り込み機能の追加

対象: LOCALSAPORTER(Local Cowork + Local Voice AI)
作業ツール: ジェンキンス(汎用コード作成エージェント)
種別: 機能追加(既存ファイルの変更・削除なし)

## 1. 目的

別システム「TRIZ情報収集エージェント」が公開情報から集め、人間が承認した設計ネタ(問題定義)74件を、
このシステムの既存の入力口 `POST /api/projects/{pid}/agent-examples/{eid}/learning/general`
(分野横断TRIZの課題定義)へ、既存コードを変えずに取り込めるようにする。

## 2. 変更内容(すべて新規追加)

| パス | 内容 |
|---|---|
| `app/triz_import.py` | 変換の純粋関数。エクスポート1件を `learning/general` の `values` に変換する `export_item_to_values`、SHA-256照合 `verify_export_file`、出典URL抽出など |
| `scripts/import_triz_problems.py` | 取り込みCLI。実行例の作成 → 課題ごとの投入。`--dry-run` / `--limit` / `--base-url` / `--project` / `--file` / `--meta` |
| `tests/test_triz_import.py` | 上記のテスト11件 |
| `inputs/triz-problems-export.json` | 取り込み対象データ(承認済み74件) |
| `inputs/triz-problems-export.meta.json` | 取得元URL・取得時刻・SHA-256・件数 |
| `docs/analysis/claude-20260921-115043.md` | 改修前の調査報告(読み取り専用で実施) |
| `docs/改修依頼_1_TRIZ取り込み.md` | 改修の指示書(要件と制約) |
| `docs/CHANGELOG_2026-09-21_triz_import.md` | 本ファイル |

## 3. 仕様上の要点

- **変換規則:** エクスポートの8項目(`goal`, `improve`, `worsens`, `ideal`, `physical`, `constraints`, `resources`, `evidence`)は、
  既存の `app/triz_common.FIELDS` と同名で、そのまま対応する。`domain` は `app/triz_adapters.DOMAINS` に含まれるものだけ受け付ける。
- **`physical` の扱い:** `None` / 空 / `"False"` は空文字にする。既存の `validate_problem` は `str(None)` を文字列 `"None"` にしてしまうため。
  (既存コードは変更していない。取り込み側で回避している。)
- **切り詰めない:** 必須項目が空、または各2500文字を超える場合は、切り詰めずにその件を失敗として扱う。
- **`human_checks`:** 「自動生成であり、人が確認済みという意味ではない」と明記した定型文を付ける。採用・実験の前に人が別途確認する前提。
- **改ざん検出:** 取り込み前に、メタファイルの SHA-256 と実ファイルの SHA-256 を照合し、不一致なら停止する。
- **二重投入の防止(冪等):** `GET .../learning/general` が返す `sessions[].problem.evidence` の「出典URL」で登録済みか判定し、同じURLはスキップする。
  409(処理中/競合)は待って再試行し、422は理由を記録して続行する。
- **`--dry-run`:** HTTPリクエストを一切送らず、変換結果の件数と失敗理由だけを表示する。

## 4. 使い方

```bash
# 1) まず変換だけ確認(通信しない)
python scripts/import_triz_problems.py --project <プロジェクトID> --dry-run

# 2) 問題なければ実行(実アプリのポート既定 8099)
python scripts/import_triz_problems.py --project <プロジェクトID>
```

## 5. 検証結果(実行して確認したもの)

- `python -m pytest tests -q`: **414 passed, 1 skipped**(新規テスト11件を含む)。
  作業者の報告と、別途こちらで再実行した結果が一致した。
- `--dry-run` を実データ74件に対して実行: **変換 74件 / 失敗 0件**。
- 取り込み時点との差分は「追加7件・変更0件・削除0件」。既存ファイルに変更はない。
- 入力データ: 74件、SHA-256 `8d9462aca905d7f9ae8ccd26d6afb1c1258951248bab5d4670be4b7587adabc2`、取得 2026-09-21 12:02(UTC)。

## 6. 未実施・制限事項

- **実アプリ(ポート 8099)への実際の投入は、まだ行っていない。** テストは、ダミーのLLMと FastAPI の `TestClient` だけで行った。
- **比較試験(`/general/{sid}/experiment`)までは進められない。** 試験には原本の本文(`cases`)が必要だが、
  エクスポートには出典URLと短い引用しかない。取り込みで到達できるのは課題定義までで、その先は原本の登録が別途必要。
- **`domain='software'` の課題を採用するには、Python原本と実行時テストの合格が必要**(`app/triz_general.review` の既存仕様)。
- 調査報告で指摘された既存コードの課題(例: `validate_problem` の `None` 処理、認証なし、`app/web.py` の肥大化、
  テスト件数の記録が `MANIFEST.json`=13 / `SYSTEM_DESCRIPTION_FOR_AI.md`=50 / `BUILD_REPORT.md`=403 と不一致)は、
  今回は**変更していない**。詳細は `docs/analysis/claude-20260921-115043.md` の第7章を参照。
- テスト実行のため、作業環境(使い捨てコンテナ)に `pytest-asyncio`、`openpyxl`、`numpy`、`PyYAML`、`pypdf`、`python-multipart` を追加した。
  リポジトリの依存定義(`pyproject.toml` / `requirements.txt`)は変更していない。

## 7. 作業の経緯(参考)

- 取り込みの前に、`.env`・鍵・`.venv`・`backups`・`logs`・`data` などを除外したコピーで作業した。元フォルダの `.env` は読んでいない。
- 調査は Claude(読み取り専用モード)で実施した。実装は、Claude のAPI残高不足により、自動引き継ぎの結果 Grok が担当した。
  最初の3ファイルの書き込みは、内容を読んでから承認した。その後の `tests/test_triz_import.py` の修正版(TestClient の使い方の修正)は、
  内容を読まずに承認し、テスト実行の結果(414 passed)と差分(追加3ファイルのみ)で確認した。
- 書き戻しは `scripts/apply-writeback.ps1` で実施した(競合なし。ハッシュ検証済み)。
  書き戻し時のバックアップ用フォルダ `.jenkins_backup` が、このフォルダ直下に作られている。

## 8. 訂正（2026-09-23追記）

2026-09-21時点の §6 は「実アプリ(ポート 8099)への実際の投入は、まだ行っていない」と記載していた。これは当時の事実である。

**2026-09-22確認時点**（`docs/Local_Cowork_変更確認_2026-09-22.md`）では、案件 `f8daae1bf0204e93acdde0b22be359c6` へ `triz-problems-export.json` が登録済みで、学習DBの general_inventions は **74件投入済み・全件 status=defined** だった。候補0、検証ケース0、実験0。

したがって「本番未投入」は最新の状態ではない。一方で、74件は課題定義までであり、**検証済み手順・採用済み・業務回復済みではない**。車両損益処理へ自動接続されたことも示さない。本追記は二次情報（2026-09-22のDBコピー確認）に基づき、本セッションでは本番DBを読んでいない。
