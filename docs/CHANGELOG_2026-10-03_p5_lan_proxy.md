# 2026-10-03 P5 認証付き LAN プロキシ反映

P5 ZIPを現行 P4 と比較し、LAN 入口の限定 API とトークン認証を採用。既存の計画・OCR・RAGの実装は上書きしていない。

## 変更
- 基底 Compose の Web 公開を localhost のみに固定。プロキシだけが指定 LAN IP の 8099 を公開する。
- nginx は既定拒否で、health、成功事例取込、agent example取込、general learning の指定メソッドのみ中継する。
- ネットワークドライブからの bind mount が Docker Desktop で失敗したため、設定と起動スクリプトを専用イメージに同梱。
- 本番の48文字トークンで nginx が起動できるよう `map_hash_bucket_size 128` を設定。
- 切替スクリプトは Web、プロキシのみを更新。ヘルスチェックは実コンテナの公開IPとプロキシ healthy を検査。
- P4の公開方式に固定されたテストを新しい境界検査へ更新。

## 検証と限界
- Compose config 検証 PASS。P5単体 3 passed, 1 skipped。ヘルスチェック全項目 PASS。
- LAN API: トークンなし 401、正しいトークン 200、許可外パス／エンコードされた許可外パス 403。localhost Web 200。
- Web 実公開 127.0.0.1:8099 のみ、プロキシ実公開は指定 LAN IP:8099 のみ。
- 48G 実機からの送信は未検証。送信元IP制限は本番設定では無効。HTTP 通信に TLS はない。
- OCR実PDF受入、車両別月次損益実案件一連受入、業務確認36項目の残件は未完了。

## 復旧
`temp/p5_backup_20261002` に導入前の基底 Compose と healthcheck を保存。障害時はまずプロキシのみ停止し、Web の localhost 管理経路を維持。認証なしのLAN公開へ自動復帰しない。Docker volume と他サービスは触らない。
