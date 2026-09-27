# OCR Phase 5-A 変更履歴（2026-09-26）

## 実装範囲

- PaddleX 文書解析サーバー `/layout-parsing` 用の `PaddleXLayoutClient` を追加した。
- `parsing_res_list` を既存契約の `width` / `height` / `blocks` へ変換した。
- `layout_det_res.boxes` はbbox完全一致または IoU 0.9以上で対応付け、対応不能時の信頼度を `0.0` とした。
- ラベルは `table`、`image`、見出し系の `title`、それ以外の `text` へ写像した。HTMLは解釈せず文字列のまま保持する。
- `pypdfium2` を遅延importし、約144dpi（scale 2.0）でPDFの1ページをPNG化する描画関数を追加した。失敗時は画像を捏造せず例外を送出する。
- OCR feature flagが有効な場合だけ、未設定の `OCR_RUNTIME` にクライアントfactoryとrendererを登録する起動時配線を追加した。
- OCR composeをwebのネットワーク名前空間共有、loopback bind、GPU予約、pipeline設定マウントを使う実サービス構成へ更新した。

## エラー写像

- 接続不可および HTTP 502/503/504: `OCR_SERVICE_UNAVAILABLE`（認識は1回だけ再試行）
- タイムアウト: `OCR_TIMEOUT`
- その他のHTTP失敗: `OCR_SERVICE_UNAVAILABLE`
- サービスの `errorCode != 0`、非JSON、不正・欠落した解析結果: `OCR_LAYOUT_FAILED`
- 有効な応答でブロック0件: アダプターでは成功。既存fallbackが `OCR_LAYOUT_FAILED` としてレビュー側へ送る。

## 維持した安全条件

- `LOCALSAPORTER_OCR_ENABLED` の既定無効を維持した。
- 接続先は `127.0.0.1` / `localhost` のみに限定し、認証情報付きURLを拒否する。
- 信頼度を推測で `1.0` にしない。
- 描画・解析失敗を空画像や成功結果へ変換しない。
- 人間レビュー前の後続計算・RAG採用禁止を変更していない。
- 帳票別の小計・合計抽出は実装していない。

## 検証範囲

- `httpx.MockTransport` とPDFiumフェイクによるアダプター・描画テスト。
- YAMLとしてのcompose構造検査。
- Docker、GPU、実OCRサービス、実PDFでの起動・性能確認は本変更の試験環境では実施しない。
