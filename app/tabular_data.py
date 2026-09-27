from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from app.yayoi_accounting import read_workbook


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_ROWS = 100_000
MAX_RESULT_ROWS = 50_000
MAX_COLUMNS = 128
MAX_OPERATIONS = 20
MAX_JOINS = 5

PROFILE_ACTIONS = {"profile", "inspect", "analyze", "analyse", "preview"}
TRANSFORM_ACTIONS = {
    "transform", "extract", "select", "filter", "aggregate", "group",
    "join", "export", "convert", "compute", "calculate", "calculation",
}
CREATE_ACTIONS = {"create", "create_table", "build_table", "generate_table"}


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = _text(value).replace(",", "").replace("￥", "").replace("¥", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if text in {"", "-", "―"}:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _date_value(value: object) -> datetime | None:
    number = _number(value)
    if number is not None and 1 <= number <= 100_000:
        return datetime(1899, 12, 30) + timedelta(days=number)
    text = _text(value)
    for pattern, order in (
        (r"^(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", "ymd"),
        (r"^(\d{4})[年/\-.](\d{1,2})", "ym"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        try:
            return datetime(parts[0], parts[1], parts[2] if order == "ymd" else 1)
        except ValueError:
            return None
    return None


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV/TSVの文字コードをUTF-8、CP932、UTF-16として判定できません")


def _unique_headers(row: list[object]) -> list[str]:
    headers: list[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(row[:MAX_COLUMNS], 1):
        base = _text(value) or f"列{index}"
        counts[base] = counts.get(base, 0) + 1
        headers.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return headers


def _detect_header_row(matrix: list[list[object]]) -> int:
    best_index = 0
    best_score = -1
    for index, row in enumerate(matrix[:50]):
        values = [_text(value) for value in row[:MAX_COLUMNS]]
        nonempty = [value for value in values if value]
        if len(nonempty) < 2:
            continue
        strings = sum(_number(value) is None for value in nonempty)
        duplicates = len(nonempty) - len(set(nonempty))
        score = len(nonempty) * 3 + strings - duplicates * 4 - index
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _table_from_matrix(matrix: list[list[object]], header_row: int | None) -> tuple[list[str], list[dict[str, object]]]:
    if not matrix:
        raise ValueError("表に行がありません")
    index = _detect_header_row(matrix) if header_row is None else int(header_row) - 1
    if index < 0 or index >= min(len(matrix), 50):
        raise ValueError("header_rowは先頭50行の1始まり行番号で指定してください")
    headers = _unique_headers(matrix[index])
    if not headers:
        raise ValueError("見出し行に列名がありません")
    rows: list[dict[str, object]] = []
    for raw in matrix[index + 1:index + 1 + MAX_SOURCE_ROWS]:
        values = list(raw[:len(headers)]) + [""] * max(0, len(headers) - len(raw))
        if any(_text(value) for value in values):
            rows.append(dict(zip(headers, values)))
    return headers, rows


def _csv_matrix(data: bytes, extension: str) -> list[list[object]]:
    text = _decode_text(data)
    sample = text[:16_384]
    delimiter = "\t" if extension == ".tsv" else ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    rows: list[list[object]] = []
    for index, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if index >= MAX_SOURCE_ROWS + 50:
            raise ValueError("CSV/TSVは100,000データ行以内にしてください")
        rows.append(row[:MAX_COLUMNS])
    return rows


def _source_ref(source: object) -> str:
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, dict):
        raise ValueError("sourceはcontext:<ID>またはworkspace:<相対パス>で指定してください")
    reference = source.get("reference") or source.get("ref") or source.get("source")
    if reference:
        return _source_ref(reference)
    if source.get("context_file_id") or source.get("file_id") or source.get("id"):
        return "context:" + str(source.get("context_file_id") or source.get("file_id") or source.get("id")).strip()
    if source.get("workspace_path") or source.get("path"):
        return "workspace:" + str(source.get("workspace_path") or source.get("path")).strip()
    raise ValueError("sourceにcontext_file_idまたはworkspace_pathが必要です")


class SafeTableExecutor:
    """Bounded declarative CSV/XLSX processing without arbitrary code execution."""

    def __init__(self, memory, workspace) -> None:
        self.memory = memory
        self.workspace = workspace

    def inventory(self, project: dict, project_id: str, source_ids: set[str] | None = None) -> str:
        lines = [
            "安全なローカル表データ実行器（原本は外部送信しません）:",
            "対応: CSV/TSV/XLSX/XLSM、profile、filter、日付派生、group集計、一対一join、CSV/Markdown保存",
            "存在しない中間ファイルを推測せず、以下の正確なsource・sheet・columnsを使ってください。",
        ]
        for item in self.memory.list_context_files(project_id):
            if item.get("source") == "memo" or (source_ids and item.get("id") not in source_ids):
                continue
            extension = Path(item.get("filename", "")).suffix.lower()
            if extension in {".csv", ".tsv", ".xlsx", ".xlsm"}:
                lines.append(
                    f"- source=context:{item['id']} | {item['filename']} | kind={item.get('file_kind', 'table')}"
                )
                try:
                    full = self.memory.get_context_file(project_id, item["id"])
                    data = full.get("original_data") if full else None
                    if data is None and full:
                        data = str(full.get("content", "")).encode("utf-8")
                    if not data:
                        raise ValueError("原本データなし")
                    if extension in {".csv", ".tsv"}:
                        matrices = [(Path(item["filename"]).name, _csv_matrix(data, extension))]
                    else:
                        matrices = [
                            (sheet["name"], sheet["rows"])
                            for sheet in read_workbook(data)[:20]
                        ]
                    for sheet_name, matrix in matrices:
                        try:
                            header_index = _detect_header_row(matrix)
                            headers, rows = _table_from_matrix(matrix, header_index + 1)
                            column_text = ", ".join(headers[:30])
                            if len(headers) > 30:
                                column_text += f", ... ({len(headers)} columns)"
                            lines.append(
                                f"  sheet={sheet_name} | header_row={header_index + 1} | "
                                f"rows={len(rows)} | columns=[{column_text}] | column_count={len(headers)}"
                            )
                        except (ValueError, KeyError, TypeError) as exc:
                            lines.append(f"  sheet={sheet_name} | schema_preview_error={str(exc)[:300]}")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    lines.append(f"  file_schema_preview_error={str(exc)[:300]}")
        if self.workspace:
            try:
                listing = self.workspace.list_entries(project.get("workspace_path", ""), project_id)
                for item in listing.get("entries", []):
                    if item.get("kind") == "file" and Path(item.get("path", "")).suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm"}:
                        lines.append(f"- source=workspace:{item['path']} | generated/local table")
            except (OSError, ValueError):
                pass
        return "\n".join(lines)[:20_000]

    def _read_source(self, project: dict, project_id: str, spec: dict) -> tuple[str, list[str], list[dict[str, object]]]:
        ref = _source_ref(spec.get("source"))
        if not ref.startswith(("context:", "workspace:")):
            registered = [
                item for item in self.memory.list_context_files(project_id)
                if item.get("source") != "memo"
                and (item.get("id") == ref or item.get("filename") == ref)
            ]
            if len(registered) == 1:
                ref = "context:" + registered[0]["id"]
        if ref.startswith("context:"):
            file_id = ref.removeprefix("context:").strip()
            item = self.memory.get_context_file(project_id, file_id)
            if not item or item.get("source") == "memo":
                raise ValueError("指定されたプロジェクト表データが見つかりません")
            filename = str(item["filename"])
            data = item.get("original_data")
            if data is None:
                data = str(item.get("content", "")).encode("utf-8")
        elif ref.startswith("workspace:"):
            if not self.workspace:
                raise ValueError("Workspaceは無効です")
            relative = ref.removeprefix("workspace:").strip()
            _, _, path = self.workspace.resolve_file(
                project.get("workspace_path", ""), project_id, relative, must_exist=True
            )
            if not path.is_file():
                raise ValueError("表データsourceはファイルで指定してください")
            filename, data = relative, path.read_bytes()
        else:
            raise ValueError("sourceはcontext:<ID>またはworkspace:<相対パス>で指定してください")
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("表データ原本は20MB以内にしてください")
        extension = Path(filename).suffix.lower()
        if extension in {".csv", ".tsv"}:
            matrix = _csv_matrix(data, extension)
            table_name = Path(filename).name
        elif extension in {".xlsx", ".xlsm"}:
            sheets = read_workbook(data)
            sheet_name = str(spec.get("sheet", "")).strip()
            if sheet_name:
                sheet = next((item for item in sheets if item["name"] == sheet_name), None)
                if not sheet and sheet_name.lower() in {"sheet1", "sheet 1"}:
                    sheet = sheets[0] if sheets else None
                if not sheet:
                    raise ValueError(f"Excelシートが見つかりません: {sheet_name}")
            else:
                sheet = sheets[0]
            matrix, table_name = sheet["rows"], f"{Path(filename).name}/{sheet['name']}"
        else:
            raise ValueError("表データ処理はCSV/TSV/XLSX/XLSMだけに対応しています")
        headers, rows = _table_from_matrix(matrix, spec.get("header_row"))
        return ref, headers, rows

    @staticmethod
    def _require_column(rows: list[dict[str, object]], column: str) -> None:
        if rows and column not in rows[0]:
            raise ValueError(f"列が見つかりません: {column}")

    def _derive(self, rows: list[dict[str, object]], definitions: list[dict]) -> None:
        if len(definitions) > 20:
            raise ValueError("derived_columnsは20件以内です")
        for definition in definitions:
            if not isinstance(definition, dict):
                raise ValueError("derived_columnsの各要素はオブジェクトです")
            output = str(definition.get("as", "")).strip()
            if not output:
                raise ValueError("派生列にはasが必要です")
            if "date_part" in definition:
                column = str(definition.get("column", "")).strip()
                part = str(definition.get("date_part", "")).lower()
                if part not in {"year", "month", "date"}:
                    raise ValueError("date_partはyear/month/dateだけです")
                self._require_column(rows, column)
                for row in rows:
                    value = _date_value(row.get(column))
                    row[output] = "" if value is None else (
                        f"{value:%Y}" if part == "year" else f"{value:%Y-%m}" if part == "month" else f"{value:%Y-%m-%d}"
                    )
                continue
            operation = str(definition.get("operation", "")).lower()
            columns = [str(item) for item in definition.get("columns", [])]
            if operation not in {"add", "subtract", "multiply", "divide"} or not 1 <= len(columns) <= 10:
                raise ValueError("数値派生列はadd/subtract/multiply/divideとcolumnsで指定してください")
            for column in columns:
                self._require_column(rows, column)
            for row in rows:
                values = [_number(row.get(column)) for column in columns]
                if any(value is None for value in values):
                    row[output] = ""
                    continue
                numbers = [float(value) for value in values if value is not None]
                result = numbers[0]
                if operation == "add":
                    result = sum(numbers)
                elif operation == "subtract":
                    result = numbers[0] - sum(numbers[1:])
                elif operation == "multiply":
                    for value in numbers[1:]:
                        result *= value
                elif operation == "divide":
                    for value in numbers[1:]:
                        if value == 0:
                            result = math.nan
                            break
                        result /= value
                row[output] = round(result, 10) if math.isfinite(result) else ""

    def _filter(self, rows: list[dict[str, object]], filters: list[dict]) -> list[dict[str, object]]:
        if len(filters) > 20:
            raise ValueError("filtersは20件以内です")
        output = rows
        for condition in filters:
            column = str(condition.get("column", "")).strip()
            operator = str(condition.get("operator", "eq")).lower()
            expected = condition.get("value")
            self._require_column(output, column)
            if operator not in {"eq", "ne", "contains", "starts_with", "in", "gt", "gte", "lt", "lte", "not_empty"}:
                raise ValueError(f"未許可のfilter operatorです: {operator}")

            def matches(row: dict[str, object]) -> bool:
                actual = row.get(column)
                if operator == "not_empty":
                    return bool(_text(actual))
                if operator == "in":
                    values = expected if isinstance(expected, list) else [expected]
                    return _text(actual) in {_text(value) for value in values}
                if operator in {"contains", "starts_with"}:
                    left, right = _text(actual), _text(expected)
                    return right in left if operator == "contains" else left.startswith(right)
                left_number, right_number = _number(actual), _number(expected)
                left: object = left_number if left_number is not None and right_number is not None else _text(actual)
                right: object = right_number if left_number is not None and right_number is not None else _text(expected)
                return {"eq": left == right, "ne": left != right, "gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[operator]

            output = [row for row in output if matches(row)]
        return output

    def _join(self, project: dict, project_id: str, rows: list[dict[str, object]], joins: list[dict]) -> list[dict[str, object]]:
        if len(joins) > MAX_JOINS:
            raise ValueError(f"joinsは{MAX_JOINS}件以内です")
        output = rows
        for join in joins:
            _, _, right_rows = self._read_source(project, project_id, join)
            left_on = join.get("left_on", [])
            right_on = join.get("right_on", [])
            left_on = [left_on] if isinstance(left_on, str) else [str(value) for value in left_on]
            right_on = [right_on] if isinstance(right_on, str) else [str(value) for value in right_on]
            if not left_on or len(left_on) != len(right_on) or len(left_on) > 8:
                raise ValueError("joinのleft_on/right_onは同数の1～8列です")
            for column in left_on:
                self._require_column(output, column)
            for column in right_on:
                self._require_column(right_rows, column)
            index: dict[tuple[str, ...], dict[str, object]] = {}
            for right in right_rows:
                key = tuple(_text(right.get(column)) for column in right_on)
                if key in index and str(join.get("duplicate_policy", "error")) != "first":
                    raise ValueError("join右表のキーが重複しています。先にaggregateするかduplicate_policy=firstを明示してください")
                index.setdefault(key, right)
            selected = join.get("select", [])
            if not selected and join.get("include_all") and right_rows:
                selected = []
                existing = set(output[0]) if output else set()
                for column in right_rows[0]:
                    if column in right_on:
                        continue
                    alias = column if column not in existing else f"right_{column}"
                    selected.append({"column": column, "as": alias})
            if not isinstance(selected, list) or len(selected) > 50:
                raise ValueError("join selectは50列以内の配列です")
            how = str(join.get("how", "left")).lower()
            if how not in {"left", "inner"}:
                raise ValueError("join howはleftまたはinnerだけです")
            joined: list[dict[str, object]] = []
            for left in output:
                match = index.get(tuple(_text(left.get(column)) for column in left_on))
                if not match and how == "inner":
                    continue
                merged = dict(left)
                for item in selected:
                    if isinstance(item, str):
                        column, alias = item, item
                    else:
                        column = str(item.get("column", ""))
                        alias = str(item.get("as", column))
                    if not column or not alias:
                        raise ValueError("join selectにはcolumn/asが必要です")
                    merged[alias] = match.get(column, "") if match else ""
                joined.append(merged)
            output = joined
        return output

    def _aggregate(self, rows: list[dict[str, object]], group_by: list[str], definitions: list[dict]) -> list[dict[str, object]]:
        if len(group_by) > 8 or not 1 <= len(definitions) <= 20:
            raise ValueError("group_byは8列以内、aggregationsは1～20件です")
        for column in group_by:
            self._require_column(rows, column)
        groups: dict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            groups[tuple(_text(row.get(column)) for column in group_by)].append(row)
        if not group_by and not groups:
            groups[()] = []
        output: list[dict[str, object]] = []
        for key, members in groups.items():
            result: dict[str, object] = dict(zip(group_by, key))
            for definition in definitions:
                function = str(definition.get("function", "")).lower()
                column = str(definition.get("column", "")).strip()
                alias = str(definition.get("as", f"{function}_{column or 'rows'}")).strip()
                if function not in {"sum", "count", "count_distinct", "average", "min", "max"}:
                    raise ValueError(f"未許可の集計関数です: {function}")
                if function != "count":
                    self._require_column(members or rows, column)
                values = [member.get(column) for member in members] if column else []
                numeric = [value for value in (_number(item) for item in values) if value is not None]
                if function == "count":
                    value: object = len(members) if not column else sum(bool(_text(item)) for item in values)
                elif function == "count_distinct":
                    value = len({_text(item) for item in values if _text(item)})
                elif function == "sum":
                    value = round(sum(numeric), 10)
                elif function == "average":
                    value = round(sum(numeric) / len(numeric), 10) if numeric else ""
                elif function == "min":
                    value = min(numeric) if numeric else min((_text(item) for item in values if _text(item)), default="")
                else:
                    value = max(numeric) if numeric else max((_text(item) for item in values if _text(item)), default="")
                result[alias] = value
            output.append(result)
        return output

    @staticmethod
    def _select(rows: list[dict[str, object]], select: list[object]) -> list[dict[str, object]]:
        if not select:
            return rows
        if len(select) > MAX_COLUMNS:
            raise ValueError("selectは128列以内です")
        output = []
        for row in rows:
            selected: dict[str, object] = {}
            for item in select:
                if isinstance(item, str):
                    column, alias = item, item
                else:
                    column, alias = str(item.get("column", "")), str(item.get("as", ""))
                if column not in row:
                    raise ValueError(f"列が見つかりません: {column}")
                selected[alias or column] = row.get(column, "")
            output.append(selected)
        return output

    @staticmethod
    def _sort(rows: list[dict[str, object]], sort_by: list[object]) -> None:
        if len(sort_by) > 8:
            raise ValueError("sort_byは8列以内です")
        for item in reversed(sort_by):
            column = item if isinstance(item, str) else str(item.get("column", ""))
            descending = False if isinstance(item, str) else str(item.get("direction", "asc")).lower() == "desc"
            def key(row: dict[str, object]) -> tuple[bool, int, object]:
                text = _text(row.get(column))
                number = _number(row.get(column))
                return text == "", 0 if number is not None else 1, number if number is not None else text
            rows.sort(key=key, reverse=descending)

    @staticmethod
    def _markdown(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "結果は0行です。\n"
        headers = list(rows[0])
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
        for row in rows:
            values = [_text(row.get(header)).replace("|", "\\|").replace("\n", " ") for header in headers]
            lines.append("| " + " | ".join(values) + " |")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _csv(rows: list[dict[str, object]]) -> str:
        stream = io.StringIO(newline="")
        headers = list(rows[0]) if rows else ["result"]
        writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            safe = {}
            for header in headers:
                value = row.get(header, "")
                text = _text(value)
                if isinstance(value, str) and text.startswith(("=", "+", "@")):
                    text = "'" + text
                safe[header] = text
            writer.writerow(safe)
        return "\ufeff" + stream.getvalue()

    @staticmethod
    def _xlsx(rows: list[dict[str, object]]) -> bytes:
        headers = list(rows[0]) if rows else ["result"]
        matrix = [headers] + [[row.get(header, "") for header in headers] for row in rows]

        def column_name(index: int) -> str:
            name = ""
            while index:
                index, remainder = divmod(index - 1, 26)
                name = chr(65 + remainder) + name
            return name

        sheet_rows = []
        for row_index, values in enumerate(matrix, 1):
            cells = []
            for column_index, value in enumerate(values, 1):
                reference = f"{column_name(column_index)}{row_index}"
                number = _number(value)
                if number is not None and not isinstance(value, str):
                    cells.append(f'<c r="{reference}"><v>{number:g}</v></c>')
                else:
                    text = escape(_text(value))
                    cells.append(
                        f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
                    )
            sheet_rows.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>'
        )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>')
            archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>')
            archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Result" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        return stream.getvalue()
    def _write(self, project: dict, project_id: str, output_path: str, rows: list[dict[str, object]]) -> dict:
        extension = Path(output_path).suffix.lower()
        if extension not in {".csv", ".md", ".xlsx"}:
            raise ValueError("表データ出力は.csv、.md、.xlsxのいずれかで指定してください")
        if extension == ".xlsx":
            data = self._xlsx(rows)
            audit = self.workspace.write_bytes(
                project.get("workspace_path", ""), project_id, output_path, data
            )
        else:
            content = self._csv(rows) if extension == ".csv" else self._markdown(rows)
            data = content.encode("utf-8")
            audit = self.workspace.apply_operations(
                project.get("workspace_path", ""), project_id,
                [{"action": "write_text", "path": output_path, "content": content}],
            )[0]
        return {**audit, "sha256": hashlib.sha256(data).hexdigest()}

    def _profile(self, project: dict, project_id: str, operation: dict) -> dict:
        source, headers, rows = self._read_source(project, project_id, operation)
        profile_rows = []
        for header in headers:
            values = [row.get(header) for row in rows]
            numeric = [value for value in (_number(item) for item in values) if value is not None]
            profile_rows.append({
                "列": header, "データ行数": len(rows),
                "空欄数": sum(not _text(item) for item in values),
                "数値件数": len(numeric),
                "最小": min(numeric) if numeric else "", "最大": max(numeric) if numeric else "",
            })
        output_path = str(operation.get("output_path", "output/table_profile.md"))
        saved = self._write(project, project_id, output_path, profile_rows)
        return {"action": "table_profile", "source": source, "source_rows": len(rows),
                "columns": len(headers), "output_path": output_path, "output_rows": len(profile_rows), "write": saved}

    def _transform(self, project: dict, project_id: str, operation: dict) -> dict:
        source, headers, rows = self._read_source(project, project_id, operation)
        source_rows = len(rows)
        joins = operation.get("joins", [])
        filters = operation.get("filters", [])
        derived = operation.get("derived_columns", [])
        if not all(isinstance(value, list) for value in (joins, filters, derived)):
            raise ValueError("joins/filters/derived_columnsは配列で指定してください")
        rows = self._join(project, project_id, rows, joins)
        self._derive(rows, derived)
        rows = self._filter(rows, filters)
        group_by = operation.get("group_by", [])
        aggregations = operation.get("aggregations", [])
        if not isinstance(group_by, list) or not isinstance(aggregations, list):
            raise ValueError("group_by/aggregationsは配列で指定してください")
        available = set(rows[0]) if rows else set(headers)
        group_by = [str(value) for value in group_by if str(value) in available]
        aggregations = [
            definition for definition in aggregations
            if isinstance(definition, dict)
            and str(definition.get("function", "")).lower() in {"count", "sum", "count_distinct", "average", "min", "max"}
            and (str(definition.get("function", "")).lower() == "count" or str(definition.get("column", "")) in available)
        ]
        if aggregations:
            rows = self._aggregate(rows, group_by, aggregations)
        select = operation.get("select", [])
        if isinstance(select, list) and rows:
            available_after = set(rows[0])
            select = [
                item for item in select
                if (isinstance(item, str) and item in available_after)
                or (isinstance(item, dict) and str(item.get("column", "")) in available_after)
            ]
        rows = self._select(rows, select)
        self._sort(rows, operation.get("sort_by", []))
        limit = max(1, min(int(operation.get("limit", MAX_RESULT_ROWS)), MAX_RESULT_ROWS))
        rows = rows[:limit]
        output_path = str(operation.get("output_path", "output/table_result.csv"))
        saved = self._write(project, project_id, output_path, rows)
        return {"action": "table_transform", "source": source, "source_rows": source_rows,
                "output_path": output_path, "output_rows": len(rows), "columns": len(rows[0]) if rows else 0,
                "joins": len(joins), "write": saved}

    def _create(self, project: dict, project_id: str, operation: dict) -> dict:
        raw_rows = operation.get("rows", [])
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("create_tableのrowsは1行以上の配列で指定してください")
        if len(raw_rows) > MAX_RESULT_ROWS:
            raise ValueError(f"create_tableのrowsは{MAX_RESULT_ROWS}行以内です")
        raw_columns = operation.get("columns", [])
        if raw_columns and not isinstance(raw_columns, list):
            raise ValueError("create_tableのcolumnsは配列で指定してください")
        if raw_columns:
            headers = _unique_headers(raw_columns)
        elif isinstance(raw_rows[0], dict):
            headers = _unique_headers(list(raw_rows[0]))
        else:
            raise ValueError("create_tableはcolumns、またはオブジェクト形式のrowsが必要です")
        if not headers or len(headers) > MAX_COLUMNS:
            raise ValueError(f"create_tableのcolumnsは1～{MAX_COLUMNS}列です")
        rows: list[dict[str, object]] = []
        scalar_types = (str, int, float, bool, type(None))
        for raw in raw_rows:
            if isinstance(raw, dict):
                values = [raw.get(header, "") for header in headers]
            elif isinstance(raw, list):
                values = raw[:len(headers)] + [""] * max(0, len(headers) - len(raw))
            else:
                raise ValueError("create_tableの各rowはオブジェクトまたは配列です")
            if any(not isinstance(value, scalar_types) for value in values):
                raise ValueError("create_tableのセル値は文字列・数値・真偽値・nullだけです")
            rows.append(dict(zip(headers, values, strict=True)))
        output_path = str(operation.get("output_path", "output/generated.csv"))
        saved = self._write(project, project_id, output_path, rows)
        return {
            "action": "table_create", "source": "generated:inline", "source_rows": len(rows),
            "output_path": output_path, "output_rows": len(rows), "columns": len(headers),
            "write": saved,
        }

    @staticmethod
    def _normalize_operation_spec(operation: dict) -> dict:
        normalized = dict(operation)
        if "action" not in normalized and normalized.get("operation"):
            normalized["action"] = normalized["operation"]
        if "action" not in normalized and normalized.get("type"):
            normalized["action"] = normalized["type"]
        if "group_by" not in normalized and normalized.get("groupby") is not None:
            normalized["group_by"] = normalized["groupby"]
        if "output_path" not in normalized and isinstance(normalized.get("result"), str):
            result_path = normalized["result"].strip()
            if "." in Path(result_path).name:
                normalized["output_path"] = result_path
        if "source" not in normalized and normalized.get("input") is not None:
            normalized["source"] = normalized["input"]
        if "filters" not in normalized and isinstance(normalized.get("condition"), dict):
            normalized["filters"] = [normalized["condition"]]
        inputs = normalized.get("inputs")
        if "source" not in normalized and isinstance(inputs, list) and inputs:
            normalized["source"] = inputs[0]
            keys = normalized.get("keys", [])
            if isinstance(keys, str):
                keys = [keys]
            if isinstance(keys, list) and keys:
                generated_joins = []
                for source in inputs[1:MAX_JOINS + 1]:
                    join = dict(source) if isinstance(source, dict) else {"source": source}
                    join.setdefault("left_on", keys)
                    join.setdefault("right_on", keys)
                    join.setdefault("include_all", True)
                    generated_joins.append(join)
                if generated_joins and "joins" not in normalized:
                    normalized["joins"] = generated_joins
        if "source" not in normalized:
            reference = next((normalized.get(key) for key in (
                "source_reference", "source_ref", "input_source", "source_file",
                "input_file", "file",
            ) if normalized.get(key)), None)
            if isinstance(reference, dict):
                normalized["source"] = reference
            elif reference:
                reference = str(reference).strip()
                normalized["source"] = reference if ":" in reference else "context:" + reference
        if "sheet" not in normalized and (normalized.get("sheet_name") or normalized.get("table_name")):
            normalized["sheet"] = normalized.get("sheet_name") or normalized["table_name"]
        if "select" not in normalized and isinstance(normalized.get("columns"), list):
            normalized["select"] = normalized["columns"]
        group_by = normalized.get("group_by")
        if isinstance(group_by, str):
            normalized["group_by"] = [group_by]
        aggregations = normalized.get("aggregations")
        if isinstance(aggregations, dict):
            if "function" in aggregations:
                normalized["aggregations"] = [aggregations]
            else:
                converted_aggregations = []
                for alias, definition in aggregations.items():
                    if isinstance(definition, dict):
                        item = dict(definition)
                        item.setdefault("as", str(alias))
                    else:
                        item = {"column": str(alias), "function": str(definition), "as": str(alias)}
                    converted_aggregations.append(item)
                normalized["aggregations"] = converted_aggregations
        if "output_path" not in normalized:
            output_path = next((normalized.get(key) for key in (
                "file_path", "output_file", "destination", "target", "path", "output",
            ) if normalized.get(key)), None)
            if isinstance(output_path, str) and "." in Path(output_path).name:
                normalized["output_path"] = output_path
        return normalized
    @staticmethod
    def _normalize_action(operation: dict) -> str:
        requested = str(operation.get("action", "")).strip().lower()
        forbidden_keys = {"code", "python", "script", "shell", "command", "sql"}
        if forbidden_keys.intersection(operation):
            return ""
        if requested in CREATE_ACTIONS:
            return "create"
        if requested in {"table_operation", "table_extract", "extract_table", "read", "read_excel", "read_table", "read_csv", "read_xlsx", "load_csv", "load_excel", "load_table", "merge", "write_csv", "write_xlsx", "save_csv", "save_xlsx", "export_csv", "export_xlsx"}:
            requested = "extract"
        if requested in PROFILE_ACTIONS:
            return "profile"
        requested_base = requested.removesuffix("_rows")
        if requested in TRANSFORM_ACTIONS or requested_base in TRANSFORM_ACTIONS:
            return "transform"
        forbidden_keys = {"code", "python", "script", "shell", "command", "sql"}
        if forbidden_keys.intersection(operation):
            return ""
        source = operation.get("source")
        output_path = str(operation.get("output_path", "")).strip()
        if not source or not output_path:
            return ""
        transform_keys = {
            "select", "filters", "joins", "derived_columns", "group_by",
            "aggregations", "sort_by", "limit",
        }
        return "transform" if transform_keys.intersection(operation) else "profile"
    def apply_operations(self, project: dict, project_id: str, operations: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for operation in operations:
            if (isinstance(operation, dict) and not operation.get("action")
                    and isinstance(operation.get("operations"), list)):
                inherited = {key: value for key, value in operation.items() if key != "operations"}
                for nested in operation["operations"]:
                    if not isinstance(nested, dict):
                        raise ValueError("nested table operations must be objects")
                    expanded.append({**inherited, **nested})
            else:
                expanded.append(operation)
        operations = expanded
        if not self.workspace:
            raise ValueError("表データ結果を保存するWorkspaceが無効です")
        if len(operations) > MAX_OPERATIONS:
            raise ValueError(f"table_operationsは1タスク{MAX_OPERATIONS}件以内です")
        audit = []
        previous_output = ""
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("table_operationsの各要素はオブジェクトです")
            operation = self._normalize_operation_spec(operation)
            if not operation.get("source") and previous_output:
                operation["source"] = "workspace:" + previous_output
            requested_action = str(operation.get("action", "")).strip().lower()
            normalized_action = self._normalize_action(operation)
            if normalized_action == "profile":
                result = self._profile(project, project_id, operation)
            elif normalized_action == "transform":
                result = self._transform(project, project_id, operation)
            elif normalized_action == "create":
                result = self._create(project, project_id, operation)
            else:
                keys = ",".join(sorted(str(key) for key in operation))
                raise ValueError(
                    "許可された表データ操作はprofileまたはtransform、またはcreate_tableだけです"
                    f" (action={requested_action or '(empty)'}, keys={keys})"
                )
            if requested_action not in {"profile", "transform", "create"}:
                result["requested_action"] = requested_action
            audit.append(result)
            previous_output = str(result.get("output_path", "")).strip()
        return audit
