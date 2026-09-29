# 対応環境と必要資源

## 対応状況

| 項目 | 対応状況 |
|---|---|
| Windows 11 | 主対象・継続確認 |
| Docker Desktop | 必須 |
| PowerShell 7 | 推奨 |
| Ollama | ローカルLLM利用時に必須 |
| NVIDIA GPU | 任意。OCRと大規模モデルでは推奨 |
| Linux / macOS | 未検証 |

## 推奨資源

| 利用範囲 | メモリ | GPU | 空き容量 |
|---|---:|---:|---:|
| 開発・自動テスト | 16 GB以上 | 不要 | 10 GB以上 |
| 通常のローカルLLM利用 | 32 GB以上 | VRAM 12 GB以上推奨 | 40 GB以上 |
| OCRと20B級モデルを含む構成 | 64 GB推奨 | VRAM 16 GB以上推奨 | 80 GB以上 |

必要量は選択するOllamaモデル、OCRイメージ、入力PDFの量で変わります。初回取得ではDockerイメージとモデルのために数十GBを使用する場合があります。

## ネットワーク

標準UIは127.0.0.1のみへ公開します。セバス、Open WebUI、Computer、ChromaDBなどのサービスを認証なしでLANやインターネットへ公開しないでください。

## 現在の確認基準

具体的な確認済みバージョンは [VERSIONS.md](VERSIONS.md) を参照してください。Preview Releaseでは、実機・実PDF・実案件による全構成の受入完了を保証しません。
