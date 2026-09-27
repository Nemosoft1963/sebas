"""ローカル OCR サービスへの限定クライアント（Phase 1）。実ネットワーク前提の既定呼び出しはテストしない。"""
from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlparse

import httpx

from app.ocr_schema import OcrErrorCode, OcrOutcome


DEFAULT_BASE_URL = "http://127.0.0.1:18118"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
PAGE_TIMEOUT_SECONDS = 120
DOCUMENT_TIMEOUT_SECONDS = 600
MAX_CONCURRENT_OCR = 1
TRANSIENT_STATUS = frozenset({502, 503, 504})


class OcrClientConfigError(ValueError):
    """接続先がローカルに限定されていないなど、設定上の拒否。"""


def assert_local_ocr_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise OcrClientConfigError("OCR接続先のスキームが不正です")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise OcrClientConfigError("OCR接続先は 127.0.0.1 または localhost に限定されます")
    if parsed.username or parsed.password:
        raise OcrClientConfigError("OCR接続先に認証情報を含めてはなりません")
    return url.rstrip("/")


class OcrClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_page: float = PAGE_TIMEOUT_SECONDS,
        timeout_document: float = DOCUMENT_TIMEOUT_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_OCR,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = assert_local_ocr_url(base_url)
        self.timeout_page = float(timeout_page)
        self.timeout_document = float(timeout_document)
        self.max_concurrent = max(1, int(max_concurrent))
        self._sema = threading.BoundedSemaphore(self.max_concurrent)
        self._owns_client = client is None
        timeout = httpx.Timeout(self.timeout_page, connect=min(10.0, self.timeout_page))
        self._client = client or httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=timeout,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OcrClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> OcrOutcome:
        try:
            response = self._client.get("/health")
        except httpx.TimeoutException as exc:
            return OcrOutcome(
                ok=False,
                error_code=OcrErrorCode.OCR_TIMEOUT.value,
                message=str(exc) or "ヘルスチェックがタイムアウトしました",
            )
        except httpx.HTTPError as exc:
            return OcrOutcome(
                ok=False,
                error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                message=str(exc) or "OCRサービスに接続できません",
            )
        if response.status_code == 200:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = {"ok": True}
            return OcrOutcome(ok=True, status_code=200, data=payload)
        return OcrOutcome(
            ok=False,
            error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
            status_code=response.status_code,
            message="OCRサービスの /health が 200 ではありません",
        )

    def recognize_page(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> OcrOutcome:
        if not image_bytes:
            return OcrOutcome(
                ok=False,
                error_code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
                message="認識入力画像が空です",
            )
        acquired = self._sema.acquire(timeout=self.timeout_document)
        if not acquired:
            return OcrOutcome(
                ok=False,
                error_code=OcrErrorCode.OCR_TIMEOUT.value,
                message="同時OCR数の上限で待機が時間超過しました",
            )
        try:
            return self._recognize_once(image_bytes, page=page, extra=extra)
        finally:
            self._sema.release()

    def _recognize_once(
        self,
        image_bytes: bytes,
        *,
        page: int,
        extra: dict[str, Any] | None,
    ) -> OcrOutcome:
        files = {"image": ("page.png", image_bytes, "image/png")}
        data = {"page": str(page)}
        if extra:
            for key, value in extra.items():
                data[str(key)] = str(value)
        retried = False
        last_unavailable: OcrOutcome | None = None
        for attempt in range(2):
            try:
                response = self._client.post("/ocr/recognize", files=files, data=data)
            except httpx.TimeoutException as exc:
                return OcrOutcome(
                    ok=False,
                    error_code=OcrErrorCode.OCR_TIMEOUT.value,
                    message=str(exc) or "OCR認識がタイムアウトしました",
                    retried=retried,
                )
            except httpx.HTTPError as exc:
                last_unavailable = OcrOutcome(
                    ok=False,
                    error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    message=str(exc) or "OCRサービスに接続できません",
                    retried=retried,
                )
                if attempt == 0:
                    retried = True
                    continue
                return last_unavailable
            if response.status_code in TRANSIENT_STATUS:
                last_unavailable = OcrOutcome(
                    ok=False,
                    error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    status_code=response.status_code,
                    message="OCRサービスの一時障害です",
                    retried=retried,
                )
                if attempt == 0:
                    retried = True
                    continue
                return last_unavailable
            if response.status_code != 200:
                return OcrOutcome(
                    ok=False,
                    error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    status_code=response.status_code,
                    message="OCR認識エンドポイントが失敗しました",
                    retried=retried,
                )
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.text}
            # 200 の認識不良は再試行しない（PERF-06）
            return OcrOutcome(ok=True, status_code=200, data=payload, retried=retried)
        return last_unavailable or OcrOutcome(
            ok=False,
            error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
            retried=retried,
        )
