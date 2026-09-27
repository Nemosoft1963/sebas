from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ROWS_PER_SHEET = 100_000
MAX_COLUMNS = 128
PREVIEW_ROWS = 100

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

YAYOI_JOURNAL_HEADERS = [
    "識別フラグ", "伝票No.", "決算", "取引日付", "借方勘定科目", "借方補助科目",
    "借方部門", "借方税区分", "借方金額", "借方税金額", "貸方勘定科目",
    "貸方補助科目", "貸方部門", "貸方税区分", "貸方金額", "貸方税金額",
    "摘要", "番号", "期日", "タイプ", "生成元", "仕訳メモ", "付箋1", "付箋2",
    "調整",
]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    if not letters:
        return 0
    result = 0
    for char in letters.group(0).upper():
        result = result * 26 + ord(char) - 64
    return result - 1


def _text(node: ET.Element) -> str:
    return "".join(part.text or "" for part in node.iter() if _local(part.tag) == "t")


def _safe_member(archive: zipfile.ZipFile, name: str) -> bytes:
    clean = PurePosixPath(name)
    if clean.is_absolute() or ".." in clean.parts:
        raise ValueError("Excel archive contains an unsafe path")
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"Excel part is missing: {name}") from exc
    if info.file_size > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Excel part is too large")
    return archive.read(info)


def _cell_value(cell: ET.Element, shared: list[str]) -> object:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return _text(cell)
    value_node = next((x for x in cell if _local(x.tag) == "v"), None)
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e"}:
        return raw
    if raw == "":
        return ""
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def read_workbook(data: bytes) -> list[dict]:
    """Read the cell values of an OOXML .xlsx/.xlsm workbook without macros."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("有効なXLSX/XLSMファイルではありません") from exc
    with archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Excel展開後サイズが50MBを超えています")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(_safe_member(archive, "xl/sharedStrings.xml"))
            shared = [_text(item) for item in root if _local(item.tag) == "si"]

        workbook = ET.fromstring(_safe_member(archive, "xl/workbook.xml"))
        relationships = ET.fromstring(_safe_member(archive, "xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
            for rel in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        sheets: list[dict] = []
        for sheet in workbook.iter(f"{{{MAIN_NS}}}sheet"):
            name = sheet.attrib.get("name", "Sheet")
            rel_id = sheet.attrib.get(f"{{{REL_NS}}}id", "")
            target = targets.get(rel_id, "")
            if not target:
                continue
            part = target.lstrip("/")
            if not part.startswith("xl/"):
                part = "xl/" + part
            root = ET.fromstring(_safe_member(archive, part))
            rows: list[list[object]] = []
            for row_number, row in enumerate(root.iter(f"{{{MAIN_NS}}}row"), 1):
                if row_number > MAX_ROWS_PER_SHEET:
                    raise ValueError("Excelの1シートが100,000行を超えています")
                values: list[object] = []
                for cell in row.findall(f"{{{MAIN_NS}}}c"):
                    index = _column_index(cell.attrib.get("r", ""))
                    if index >= MAX_COLUMNS:
                        continue
                    if len(values) <= index:
                        values.extend([""] * (index + 1 - len(values)))
                    values[index] = _cell_value(cell, shared)
                while values and values[-1] == "":
                    values.pop()
                rows.append(values)
            sheets.append({"name": name, "rows": rows})
        if not sheets:
            raise ValueError("読み取れるワークシートがありません")
        return sheets


def _normalized(value: object) -> str:
    return re.sub(r"[\s　・_\-（）()\[\]【】]", "", str(value or "")).lower()


def _header_map(row: list[object]) -> dict[str, int]:
    return {_normalized(value): index for index, value in enumerate(row) if str(value).strip()}


def _find_column(mapping: dict[str, int], *terms: str) -> int | None:
    normalized_terms = [_normalized(term) for term in terms]
    for key, index in mapping.items():
        if any(term == key or term in key for term in normalized_terms):
            return index
    return None


def _find_header(rows: list[list[object]], kind: str) -> tuple[int | None, dict[str, int]]:
    for index, row in enumerate(rows[:50]):
        mapping = _header_map(row)
        if kind == "journal":
            required = [
                _find_column(mapping, "借方勘定科目", "借方科目"),
                _find_column(mapping, "貸方勘定科目", "貸方科目"),
                _find_column(mapping, "借方金額"),
                _find_column(mapping, "貸方金額"),
            ]
            if all(value is not None for value in required):
                return index, mapping
        else:
            account = _find_column(mapping, "勘定科目", "科目")
            measures = sum(_find_column(mapping, term) is not None for term in (
                "前月繰越", "借方金額", "貸方金額", "残高", "期末残高"
            ))
            if account is not None and measures >= 2:
                return index, mapping
    return None, {}


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value or "").strip().replace(",", "").replace("￥", "").replace("¥", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if text in {"", "-", "―"}:
        return None
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def _date(value: object) -> str:
    if isinstance(value, (int, float)) and 1 <= float(value) <= 100_000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    text = str(value or "").strip()
    for pattern in (r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", r"(\d{1,2})[/-](\d{1,2})"):
        match = re.search(pattern, text)
        if match:
            parts = [int(x) for x in match.groups()]
            if len(parts) == 3:
                return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
            return f"{parts[0]:02d}-{parts[1]:02d}"
    return text


def _cell(row: list[object], index: int | None) -> object:
    return row[index] if index is not None and index < len(row) else ""


def _preview(headers: list[str], rows: list[list[object]]) -> dict:
    width = min(max([len(headers)] + [len(row) for row in rows], default=0), 40)
    output_headers = (headers + [f"列{index + 1}" for index in range(len(headers), width)])[:width]
    output_rows = [
        [row[index] if index < len(row) else "" for index in range(width)]
        for row in rows[:PREVIEW_ROWS]
    ]
    return {"headers": output_headers, "rows": output_rows, "shown_rows": len(output_rows)}


def _journal_section(sheet: dict, header_index: int | None, mapping: dict[str, int]) -> dict:
    fixed = header_index is None
    if fixed:
        header_index = -1
        headers = YAYOI_JOURNAL_HEADERS
        columns = {
            "date": 3, "debit_account": 4, "debit_amount": 8,
            "credit_account": 10, "credit_amount": 14, "description": 16,
        }
    else:
        headers = [str(x or "") for x in sheet["rows"][header_index]]
        columns = {
            "date": _find_column(mapping, "取引日付", "日付"),
            "debit_account": _find_column(mapping, "借方勘定科目", "借方科目"),
            "debit_amount": _find_column(mapping, "借方金額"),
            "credit_account": _find_column(mapping, "貸方勘定科目", "貸方科目"),
            "credit_amount": _find_column(mapping, "貸方金額"),
            "description": _find_column(mapping, "摘要"),
        }
    data_rows = [row for row in sheet["rows"][header_index + 1:] if any(str(x).strip() for x in row)]
    debit = credit = 0.0
    dates: list[str] = []
    accounts: dict[str, float] = {}
    invalid_amount_rows = 0
    for row in data_rows:
        debit_value = _number(_cell(row, columns["debit_amount"]))
        credit_value = _number(_cell(row, columns["credit_amount"]))
        if debit_value is None and credit_value is None:
            invalid_amount_rows += 1
        debit += debit_value or 0.0
        credit += credit_value or 0.0
        date_value = _date(_cell(row, columns["date"]))
        if date_value:
            dates.append(date_value)
        for key, amount in (("debit_account", debit_value), ("credit_account", credit_value)):
            account = str(_cell(row, columns[key]) or "").strip()
            if account:
                accounts[account] = accounts.get(account, 0.0) + abs(amount or 0.0)
    difference = round(debit - credit, 2)
    return {
        "sheet_name": sheet["name"], "kind": "journal", "label": "仕訳データ",
        "header_row": header_index + 1 if header_index >= 0 else None,
        "row_count": len(data_rows),
        "summary": {
            "仕訳行数": len(data_rows), "期間開始": min(dates) if dates else "不明",
            "期間終了": max(dates) if dates else "不明", "借方合計": round(debit, 2),
            "貸方合計": round(credit, 2), "貸借差額": difference,
            "勘定科目数": len(accounts), "金額未判定行": invalid_amount_rows,
        },
        "checks": [{
            "name": "貸借一致", "status": "ok" if abs(difference) < 0.005 else "warning",
            "detail": "借方合計と貸方合計が一致しています" if abs(difference) < 0.005 else f"差額 {difference:,.2f} 円を確認してください",
        }],
        "top_accounts": [
            {"account": name, "amount": round(amount, 2)}
            for name, amount in sorted(accounts.items(), key=lambda item: item[1], reverse=True)[:10]
        ],
        "preview": _preview(headers, data_rows),
    }


def _trial_section(sheet: dict, header_index: int, mapping: dict[str, int]) -> dict:
    headers = [str(x or "") for x in sheet["rows"][header_index]]
    data_rows = [row for row in sheet["rows"][header_index + 1:] if any(str(x).strip() for x in row)]
    account_col = _find_column(mapping, "勘定科目", "科目")
    debit_col = _find_column(mapping, "借方金額")
    credit_col = _find_column(mapping, "貸方金額")
    balance_col = _find_column(mapping, "期末残高", "残高")
    total_rows = []
    for row in data_rows:
        account = str(_cell(row, account_col) or "").strip()
        if "合計" in account or account in {"総合計", "当期純利益", "当期純損失"}:
            total_rows.append({
                "name": account,
                "debit": _number(_cell(row, debit_col)),
                "credit": _number(_cell(row, credit_col)),
                "balance": _number(_cell(row, balance_col)),
            })
    account_rows = sum(bool(str(_cell(row, account_col) or "").strip()) for row in data_rows)
    return {
        "sheet_name": sheet["name"], "kind": "trial_balance", "label": "試算表",
        "header_row": header_index + 1, "row_count": len(data_rows),
        "summary": {"表示行数": len(data_rows), "勘定科目行数": account_rows, "合計行数": len(total_rows)},
        "checks": [{
            "name": "合計行", "status": "ok" if total_rows else "info",
            "detail": f"{len(total_rows)}件の合計行を検出しました" if total_rows else "合計行は自動判定できませんでした。表で確認してください",
        }],
        "totals": total_rows[:20], "preview": _preview(headers, data_rows),
    }


def _looks_like_fixed_journal(sheet: dict) -> bool:
    candidates = [row for row in sheet["rows"][:100] if len(row) >= 15]
    if not candidates:
        return False
    matches = 0
    for row in candidates:
        has_accounts = bool(str(_cell(row, 4)).strip() or str(_cell(row, 10)).strip())
        has_amounts = _number(_cell(row, 8)) is not None or _number(_cell(row, 14)) is not None
        has_date = bool(_date(_cell(row, 3)))
        if has_accounts and has_amounts and has_date:
            matches += 1
    return matches >= max(1, min(3, len(candidates)))


def analyze_yayoi_workbook(filename: str, data: bytes) -> dict:
    extension = Path(filename).suffix.lower()
    if extension not in {".xlsx", ".xlsm"}:
        raise ValueError("内容確認に対応する形式はXLSXまたはXLSMです")
    sheets = read_workbook(data)
    sections: list[dict] = []
    for sheet in sheets:
        journal_header, journal_map = _find_header(sheet["rows"], "journal")
        trial_header, trial_map = _find_header(sheet["rows"], "trial_balance")
        normalized_name = _normalized(sheet["name"] + " " + filename)
        if journal_header is not None or _looks_like_fixed_journal(sheet):
            sections.append(_journal_section(sheet, journal_header, journal_map))
        elif trial_header is not None or any(term in normalized_name for term in ("試算表", "貸借対照表", "損益計算書")):
            if trial_header is not None:
                sections.append(_trial_section(sheet, trial_header, trial_map))
            else:
                rows = [row for row in sheet["rows"] if any(str(x).strip() for x in row)]
                sections.append({
                    "sheet_name": sheet["name"], "kind": "spreadsheet", "label": "会計表（列未判定）",
                    "header_row": None, "row_count": len(rows), "summary": {"表示行数": len(rows)},
                    "checks": [{"name": "列判定", "status": "warning", "detail": "弥生の標準見出しを判定できませんでした"}],
                    "preview": _preview([], rows),
                })
        else:
            rows = [row for row in sheet["rows"] if any(str(x).strip() for x in row)]
            sections.append({
                "sheet_name": sheet["name"], "kind": "spreadsheet", "label": "Excelシート",
                "header_row": None, "row_count": len(rows), "summary": {"表示行数": len(rows)},
                "checks": [], "preview": _preview([], rows),
            })
    kinds = {section["kind"] for section in sections}
    if "journal" in kinds:
        kind, label = "yayoi_journal", "弥生会計 仕訳データ"
    elif "trial_balance" in kinds:
        kind, label = "yayoi_trial_balance", "弥生会計 試算表"
    else:
        kind, label = "spreadsheet", "Excelワークブック"
    warnings = []
    if kind == "spreadsheet":
        warnings.append("弥生会計の仕訳・試算表としては自動判定できませんでした。各セルの内容を確認できます。")
    return {
        "filename": filename, "kind": kind, "label": label,
        "sheet_count": len(sheets), "sections": sections, "warnings": warnings,
        "note": "XLSMのマクロは実行せず、セル値だけを読み取ります。" if extension == ".xlsm" else "数式は保存済みの計算結果を表示します。",
    }


def workbook_to_markdown(analysis: dict) -> str:
    lines = [f"# {analysis['label']}", "", f"ファイル: {analysis['filename']}", f"シート数: {analysis['sheet_count']}", ""]
    for section in analysis["sections"]:
        lines.extend([f"## {section['sheet_name']} — {section['label']}", ""])
        lines.extend(f"- {key}: {value}" for key, value in section.get("summary", {}).items())
        preview = section["preview"]
        headers = [str(x).replace("|", "\\|") or "-" for x in preview["headers"][:20]]
        if headers and preview["rows"]:
            lines.extend(["", "| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]) 
            for row in preview["rows"][:30]:
                values = [str(x).replace("|", "\\|").replace("\n", " ") for x in row[:len(headers)]]
                lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    return "\n".join(lines).strip()
