# 2026-11-06 G5-b2a 送付内容の承認画面

## 反映

- 配布元: `C:/Users/kanto/Short-video-local/stage4/final69/LOCALSAPORTER_deploy_20261105_g5b2a_packet_approval_ui.zip`。ZIP SHA-256 `DF2FEE86C7A3458CEFFBD3EF2A3F828510046BE448D9A0D3EB625B662CF209A6`。展開時に `hashes.txt` の4ファイルと照合。
- G5-aパネル内へ「外部AIへ送る内容の承認」を追加。サーバーが作った送付本文の確認、明示的な承認、承認一覧と取消を扱う。承認は送信ではない。ジョブ作成と外部送信は追加していない。用途別の外部計画・独立評価フラグは変更していない。
- 読み取り専用の `GET /api/projects/{id}/plan-packets/approvals` と新JS配信ルートを追加。`index.html` は新JSタグ1つの追加。
- 承認台帳の読取失敗を「承認なし」と返していた配布コードを補強し、HTTP 503に変更。画面には「確認できず」と出す回帰試験を追加。
- G5-b1以降も親見出しに残っていた「(見るだけ)」を削除し、対応する2試験の期待値を変更。

## 検証結果

- 新規G5-b2a、G5-b1、G5-aの関連試験: **73 passed**、スキップなし。Node.jsによる画面操作試験を含む。記録 `output/g5b2a_packet_approval_focused_20261106.txt`。
- 全体試験の最終一括実行: **2868 passed / 12 failed / 3 skipped**、423.42秒。失敗のうち10件は前回と同じ試験名。追加2件は使い捨て試験コンテナで `git` コマンドと `docs/P0B_QUARANTINE_RUNBOOK_2026-10-03.md` が欠けたため。記録 `output/g5b2a_packet_approval_full_20261106.txt`。全体合格とは記載しない。
- 上記2件の環境を補い、見出し修正後の関連試験と合わせて **75 passed**。記録 `output/g5b2a_packet_approval_final_focused_20261106.txt`。全体試験はこの後に再実行していない。
- `scripts/healthcheck.ps1`: 全項目PASS。記録 `output/g5b2a_healthcheck_20261106.txt`。
- Webは `running / healthy`、イメージ `sha256:3891cf2e0478f69d320fd861e7369b8bd442bd31ea3d2caaa72d97d998629419`。新JSはHTTP 200で配信。
- 実ブラウザでG5-aパネル内のG5-b1とG5-b2aの表示位置、入力前の「内容を確認する」無効化を確認。実PJで承認・取消は実行していない。

## 保全と巻き戻し

- 元の `app/web.py`、`app/static/index.html`、`app/static/planning_guard_panel.js` と見出し関連試験は `backups/20261106_g5b2a_packet_approval_final69/old/` に保全。配布ZIPの展開版は同階層 `stage/` に保全。
- 旧Webイメージ: `localsaporter-web:before-g5b2a-20261106`。
- 巻き戻し時は旧ファイルと旧イメージを戻す。データ・ボリュームを削除・初期化しない。

## 最終ファイルSHA-256

| ファイル | SHA-256 |
|---|---|
| `app/static/planning_guard_packets.js` | `1954A2520BE3348398D3E469BAB18342B850593D7B1989F48E6674486A0A5F45` |
| `app/static/planning_guard_panel.js` | `483DAD3F43AFBC9434164474A542B0AB4FA112BCA63D7B483A13940CBBE4F4A0` |
| `app/static/index.html` | `91B0B9F86620742EF3E26A4EA34994C17D6E852C5DB18562B6C979978A27C3B0` |
| `app/web.py` | `BE332184CE74BAD7EDC5A332B9BDBAFC1FB98B09F4417048A285B0C222705BE8` |
| `tests/test_g5b2a_planning_guard_packets.py` | `5B5A59E34968AA6F1CE8886082F4D8F978D6389DD4AA6AC0A0592B86E6EA7716` |
| `tests/test_g5b1_planning_guard_settings.py` | `67486F31746E4036D555D6EBB46189657956B272F8B05BEFDE20E63E3D9AA3B8` |
| `tests/test_g5a_planning_guard_panel.py` | `1DCA8751AFEA6164CF0EB829374232D8F7DC0B0F5C2ABF671BA2184AD18A6730` |
