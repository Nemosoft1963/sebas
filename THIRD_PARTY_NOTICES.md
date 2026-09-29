# 第三者コンポーネントとライセンス

セバス本体はApache License 2.0で公開しています。依存パッケージ、モデル、Dockerイメージには各提供元のライセンスが適用されます。

## 主な直接依存関係

| コンポーネント | 主なライセンス | 用途 |
|---|---|---|
| FastAPI | MIT | Web API |
| Uvicorn | BSD-3-Clause | ASGIサーバー |
| HTTPX | BSD-3-Clause | HTTPクライアント |
| NumPy | BSD系ほか | 数値処理 |
| PyYAML | MIT | 設定読込 |
| faster-whisper | MIT | 音声認識 |
| pypdf | BSD-3-Clause | PDF処理 |
| pypdfium2 / PDFium | Apache-2.0およびPDFiumの第三者ライセンス | PDF描画 |
| Pillow | HPND | 画像処理 |
| openpyxl | MIT | Excel処理 |
| LangChain Chroma / Ollama | MIT | 経験RAG連携 |
| ChromaDB | Apache-2.0 | ベクトル保存 |
| pytest / pytest-asyncio | MIT / Apache-2.0 | 自動テスト |

## コンテナとモデル

Open WebUI、Open WebUI Computer、PaddleOCR、Ollamaモデルなどはセバス本体とは別に配布され、それぞれの提供元の利用条件に従います。モデルの利用許諾はモデルごとに確認してください。

## 配布時の確認

Releaseや独自イメージを再配布する場合は、対象バージョンのライセンス、NOTICE、モデルライセンスを再確認してください。この一覧は法的助言ではなく、主要な直接依存関係の案内です。
