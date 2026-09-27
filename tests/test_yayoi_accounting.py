import io
import zipfile
from xml.etree import ElementTree as ET

import pytest

from app.context_files import extract_context_file
from app.yayoi_accounting import analyze_yayoi_workbook, read_workbook


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column(index):
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_xml(rows):
    root = ET.Element(f"{{{MAIN}}}worksheet")
    data = ET.SubElement(root, f"{{{MAIN}}}sheetData")
    for row_index, values in enumerate(rows, 1):
        row = ET.SubElement(data, f"{{{MAIN}}}row", r=str(row_index))
        for col_index, value in enumerate(values):
            if value == "":
                continue
            cell = ET.SubElement(
                row, f"{{{MAIN}}}c",
                r=f"{_column(col_index)}{row_index}",
            )
            if isinstance(value, (int, float)):
                ET.SubElement(cell, f"{{{MAIN}}}v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{MAIN}}}is")
                ET.SubElement(inline, f"{{{MAIN}}}t").text = str(value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def make_workbook(sheets):
    workbook = ET.Element(f"{{{MAIN}}}workbook")
    sheet_nodes = ET.SubElement(workbook, f"{{{MAIN}}}sheets")
    relationships = ET.Element(f"{{{PKG}}}Relationships")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(
                sheet_nodes, f"{{{MAIN}}}sheet",
                name=name, sheetId=str(index),
                attrib={f"{{{REL}}}id": f"rId{index}"},
            )
            ET.SubElement(
                relationships, f"{{{PKG}}}Relationship",
                Id=f"rId{index}",
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                Target=f"worksheets/sheet{index}.xml",
            )
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))
        archive.writestr(
            "xl/workbook.xml",
            ET.tostring(workbook, encoding="utf-8", xml_declaration=True),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            ET.tostring(relationships, encoding="utf-8", xml_declaration=True),
        )
    return output.getvalue()


def test_yayoi_journal_is_identified_and_balanced():
    data = make_workbook([("仕訳日記帳", [
        ["取引日付", "借方勘定科目", "借方金額", "貸方勘定科目", "貸方金額", "摘要"],
        ["2026/08/01", "現金", 1100, "売上高", 1100, "現金売上"],
        ["2026/08/02", "仕入高", 500, "現金", 500, "仕入"],
    ])])

    result = analyze_yayoi_workbook("仕訳日記帳.xlsx", data)

    assert result["kind"] == "yayoi_journal"
    section = result["sections"][0]
    assert section["summary"]["仕訳行数"] == 2
    assert section["summary"]["借方合計"] == 1600
    assert section["summary"]["貸方合計"] == 1600
    assert section["summary"]["貸借差額"] == 0
    assert section["checks"][0]["status"] == "ok"
    assert section["summary"]["期間開始"] == "2026-08-01"


def test_headerless_yayoi_journal_import_format_is_supported():
    row = ["2000", "1", "", "2026/08/31", "普通預金", "", "", "対象外", 10000, 0,
           "売上高", "", "", "課税売上10%", 10000, 0, "月末売上"]
    result = analyze_yayoi_workbook("仕訳インポート.xlsx", make_workbook([("Sheet1", [row])]))

    assert result["kind"] == "yayoi_journal"
    assert result["sections"][0]["header_row"] is None
    assert result["sections"][0]["summary"]["貸借差額"] == 0


def test_yayoi_trial_balance_sheets_are_previewed_separately():
    header = ["勘定科目", "前月繰越", "借方金額", "貸方金額", "期末残高"]
    data = make_workbook([
        ("貸借対照表", [header, ["現金", 1000, 500, 100, 1400], ["資産合計", 1000, 500, 100, 1400]]),
        ("損益計算書", [header, ["売上高", 0, 0, 2000, 2000], ["収益合計", 0, 0, 2000, 2000]]),
    ])

    result = analyze_yayoi_workbook("残高試算表.xlsx", data)

    assert result["kind"] == "yayoi_trial_balance"
    assert result["sheet_count"] == 2
    assert [section["sheet_name"] for section in result["sections"]] == ["貸借対照表", "損益計算書"]
    assert result["sections"][0]["totals"][0]["name"] == "資産合計"
    assert result["sections"][1]["preview"]["rows"][0][0] == "売上高"


@pytest.mark.parametrize("filename", ["仕訳.xlsx", "仕訳.xlsm"])
def test_context_extraction_exposes_readable_accounting_markdown(filename):
    data = make_workbook([("仕訳", [
        ["取引日付", "借方勘定科目", "借方金額", "貸方勘定科目", "貸方金額"],
        ["2026/09/01", "現金", 100, "売上高", 100],
    ])])

    extracted = extract_context_file(filename, data)

    assert extracted.file_kind == "yayoi_journal"
    assert "弥生会計 仕訳データ" in extracted.content
    assert "貸借差額" in extracted.content
    assert "現金" in extracted.content


def test_invalid_or_oversized_workbook_is_rejected_safely():
    with pytest.raises(ValueError, match="有効なXLSX"):
        read_workbook(b"not-an-excel-file")
