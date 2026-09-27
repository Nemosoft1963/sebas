"""PaddleX 文書解析サービス用OCRアダプターとPDFページ描画。"""
from __future__ import annotations

import base64
import io
import os
import threading
from collections.abc import Mapping
from typing import Any

import httpx

from app.capability_registry import OCR_FEATURE_FLAG_ENV
from app.ocr_client import (
    DEFAULT_BASE_URL,
    DOCUMENT_TIMEOUT_SECONDS,
    MAX_CONCURRENT_OCR,
    PAGE_TIMEOUT_SECONDS,
    TRANSIENT_STATUS,
    assert_local_ocr_url,
)
from app.ocr_schema import OcrErrorCode, OcrOutcome


TITLE_LABELS = frozenset({
    "doc_title", "paragraph_title", "figure_title", "table_title",
    "header", "header_title", "footer_title", "section_title", "title",
})
IMAGE_LABELS = frozenset({"image", "figure", "chart", "seal"})


def _label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw == "table":
        return "table"
    if raw in IMAGE_LABELS:
        return "image"
    if raw in TITLE_LABELS or raw.endswith("_title"):
        return "title"
    return "text"


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _iou(left: list[float], right: list[float]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _confidence(bbox: list[float], boxes: list[Any]) -> float:
    best_iou = -1.0
    best_score = 0.0
    for raw in boxes:
        if not isinstance(raw, Mapping):
            continue
        coordinate = _bbox(raw.get("coordinate"))
        if coordinate is None:
            continue
        overlap = 1.0 if coordinate == bbox else _iou(bbox, coordinate)
        if overlap < 0.9 or overlap <= best_iou:
            continue
        try:
            score = float(raw.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        best_iou = overlap
        best_score = min(1.0, max(0.0, score))
    return best_score


def _layout_failure(message: str, *, status_code: int | None = None, retried: bool = False) -> OcrOutcome:
    return OcrOutcome(
        ok=False,
        error_code=OcrErrorCode.OCR_LAYOUT_FAILED.value,
        message=message,
        status_code=status_code,
        retried=retried,
    )


class PaddleXLayoutClient:
    """PaddleX `/layout-parsing` をアプリ共通ブロック形式へ変換する。"""

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

    def __enter__(self) -> "PaddleXLayoutClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> OcrOutcome:
        try:
            response = self._client.get("/health")
        except httpx.TimeoutException as exc:
            return OcrOutcome(
                ok=False, error_code=OcrErrorCode.OCR_TIMEOUT.value,
                message=str(exc) or "ヘルスチェックがタイムアウトしました",
            )
        except httpx.HTTPError as exc:
            return OcrOutcome(
                ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                message=str(exc) or "OCRサービスに接続できません",
            )
        if response.status_code == 200:
            try:
                payload: Any = response.json()
            except ValueError:
                payload = {"ok": True}
            return OcrOutcome(ok=True, status_code=200, data=payload)
        return OcrOutcome(
            ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
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
        del extra
        if not image_bytes:
            return OcrOutcome(
                ok=False, error_code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
                message="認識入力画像が空です",
            )
        acquired = self._sema.acquire(timeout=self.timeout_document)
        if not acquired:
            return OcrOutcome(
                ok=False, error_code=OcrErrorCode.OCR_TIMEOUT.value,
                message="同時OCR数の上限で待機が時間超過しました",
            )
        try:
            return self._recognize(image_bytes, page=page)
        finally:
            self._sema.release()

    def _recognize(self, image_bytes: bytes, *, page: int) -> OcrOutcome:
        request_payload = {
            "file": base64.b64encode(image_bytes).decode("ascii"),
            "fileType": 1,
            "visualize": False,
        }
        retried = False
        for attempt in range(2):
            try:
                response = self._client.post("/layout-parsing", json=request_payload)
            except httpx.TimeoutException as exc:
                return OcrOutcome(
                    ok=False, error_code=OcrErrorCode.OCR_TIMEOUT.value,
                    message=str(exc) or "OCR認識がタイムアウトしました", retried=retried,
                )
            except httpx.HTTPError as exc:
                unavailable = OcrOutcome(
                    ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    message=str(exc) or "OCRサービスに接続できません", retried=retried,
                )
                if attempt == 0:
                    retried = True
                    continue
                return unavailable
            if response.status_code in TRANSIENT_STATUS:
                unavailable = OcrOutcome(
                    ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    status_code=response.status_code, message="OCRサービスの一時障害です",
                    retried=retried,
                )
                if attempt == 0:
                    retried = True
                    continue
                return unavailable
            if response.status_code != 200:
                return OcrOutcome(
                    ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
                    status_code=response.status_code, message="文書解析エンドポイントが失敗しました",
                    retried=retried,
                )
            try:
                payload = response.json()
            except ValueError:
                return _layout_failure("文書解析応答がJSONではありません", status_code=200, retried=retried)
            return self._map_response(payload, page=page, retried=retried)
        return OcrOutcome(ok=False, error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value, retried=retried)

    @staticmethod
    def _map_response(payload: Any, *, page: int, retried: bool) -> OcrOutcome:
        if not isinstance(payload, Mapping):
            return _layout_failure("文書解析応答がobjectではありません", status_code=200, retried=retried)
        try:
            error_code = int(payload.get("errorCode", -1))
        except (TypeError, ValueError):
            error_code = -1
        if error_code != 0:
            return _layout_failure(
                str(payload.get("errorMsg") or "文書解析サービスが失敗を返しました"),
                status_code=200, retried=retried,
            )
        result = payload.get("result")
        layouts = result.get("layoutParsingResults") if isinstance(result, Mapping) else None
        if not isinstance(layouts, list) or not layouts or not isinstance(layouts[0], Mapping):
            return _layout_failure("layoutParsingResults がありません", status_code=200, retried=retried)
        pruned = layouts[0].get("prunedResult")
        if not isinstance(pruned, Mapping):
            return _layout_failure("prunedResult がありません", status_code=200, retried=retried)
        parsing = pruned.get("parsing_res_list")
        if not isinstance(parsing, list):
            return _layout_failure("parsing_res_list がありません", status_code=200, retried=retried)
        try:
            width = float(pruned["width"])
            height = float(pruned["height"])
        except (KeyError, TypeError, ValueError):
            return _layout_failure("ページ寸法が不正です", status_code=200, retried=retried)
        detection = pruned.get("layout_det_res")
        boxes = detection.get("boxes") if isinstance(detection, Mapping) else []
        boxes = boxes if isinstance(boxes, list) else []
        blocks: list[dict[str, Any]] = []
        for index, raw in enumerate(parsing, 1):
            if not isinstance(raw, Mapping):
                return _layout_failure("解析ブロックがobjectではありません", status_code=200, retried=retried)
            bbox = _bbox(raw.get("block_bbox"))
            if bbox is None:
                return _layout_failure("解析ブロックのbboxが不正です", status_code=200, retried=retried)
            content = raw.get("block_content")
            if not isinstance(content, str):
                return _layout_failure("解析ブロックの文字列が不正です", status_code=200, retried=retried)
            blocks.append({
                "block_id": f"p{page}-b{index:03d}",
                "label": _label(raw.get("block_label")),
                "bbox": bbox,
                "text": content,
                "confidence": _confidence(bbox, boxes),
            })
        return OcrOutcome(
            ok=True, status_code=200,
            data={"width": width, "height": height, "blocks": blocks},
            retried=retried,
        )


def render_page_png(original_bytes: bytes, page: int) -> bytes:
    """PDFの1始まりページを約144dpiのPNGへ描画する。失敗時は例外。"""
    if not original_bytes:
        raise ValueError("PDFバイト列が空です")
    if page < 1:
        raise IndexError("page は1以上である必要があります")
    import pypdfium2

    document = pypdfium2.PdfDocument(original_bytes)
    try:
        if page > len(document):
            raise IndexError("PDFページ範囲外です")
        pdf_page = document[page - 1]
        try:
            bitmap = pdf_page.render(scale=2.0)
            image = bitmap.to_pil()
            output = io.BytesIO()
            image.save(output, format="PNG")
            png = output.getvalue()
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("PNG描画結果が不正です")
            return png
        finally:
            close_page = getattr(pdf_page, "close", None)
            if callable(close_page):
                close_page()
    finally:
        close_document = getattr(document, "close", None)
        if callable(close_document):
            close_document()


def _enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(OCR_FEATURE_FLAG_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def install_default_runtime(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """OCR有効時だけ未設定の実サービス接続を登録する。"""
    env = os.environ if environ is None else environ
    summary: dict[str, Any] = {
        "enabled": _enabled(env), "client_factory": False, "renderer": False,
    }
    if not summary["enabled"]:
        summary["reason"] = "feature_disabled"
        return summary
    url = str(env.get("LOCALSAPORTER_OCR_URL") or DEFAULT_BASE_URL).strip()
    try:
        safe_url = assert_local_ocr_url(url)
    except ValueError as exc:
        summary["reason"] = "invalid_local_url"
        summary["message"] = str(exc)
        return summary
    from app.ocr_review import OCR_RUNTIME
    if OCR_RUNTIME.get("client_factory") is None:
        OCR_RUNTIME["client_factory"] = lambda: PaddleXLayoutClient(safe_url)
        summary["client_factory"] = True
    if OCR_RUNTIME.get("renderer") is None:
        OCR_RUNTIME["renderer"] = render_page_png
        summary["renderer"] = True
    summary["url"] = safe_url
    summary["reason"] = "installed" if summary["client_factory"] or summary["renderer"] else "already_configured"
    return summary
