"""Local source adapters. Preserve ambiguity; never ask for wholesale JSON transcription."""
from __future__ import annotations
import csv
import io
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

CATEGORIES = ('revenue', 'payroll', 'insurance', 'fuel', 'toll', 'lease', 'other')
VERSION = 'vehicle-auto-v1'
REVISION = '20260927.2'
TOTAL_WORDS = ('合計', '小計', '総計')


def text(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def compact(value):
    return re.sub(r'\s+', '', text(value))


@dataclass(frozen=True)
class MoneyParseResult:
    status: Literal['value', 'blank', 'invalid', 'out_of_range']
    amount: int | None
    raw_text: str
    raw_type: str
    reason: str = ''


def parse_money(value) -> MoneyParseResult:
    raw_type = type(value).__name__
    if isinstance(value, bool):
        return MoneyParseResult('invalid', None, str(value), raw_type, 'boolean')
    if value is None:
        return MoneyParseResult('blank', None, '', raw_type, 'blank')
    if isinstance(value, (int, float, Decimal)):
        raw_text = str(value)
        try:
            n = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            return MoneyParseResult('out_of_range', None, raw_text, raw_type, 'out_of_range')
        if (isinstance(value, float) and value != value) or not n.is_finite() or abs(n) > Decimal('1e12'):
            return MoneyParseResult('out_of_range', None, raw_text, raw_type, 'out_of_range')
        amount = int(n.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        return MoneyParseResult('value', amount, raw_text, raw_type)
    s = unicodedata.normalize('NFKC', str(value)).strip()
    if not s:
        return MoneyParseResult('blank', None, s, raw_type, 'blank')
    cleaned = s.replace(',', '').replace('円', '').replace('¥', '')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', cleaned):
        return MoneyParseResult('invalid', None, s, raw_type, 'non_numeric')
    n = Decimal(cleaned)
    if not n.is_finite() or abs(n) > Decimal('1e12'):
        return MoneyParseResult('out_of_range', None, s, raw_type, 'out_of_range')
    amount = int(n.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return MoneyParseResult('value', amount, s, raw_type)


def money(value):
    parsed = parse_money(value)
    return parsed.amount if parsed.status == 'value' else None


def month(value, year=None):
    s = text(value)
    m = re.search(r'(20\d{2})[-/年]?(\d{2})(?:[-/月]|\d{2}|(?!\d))', s)
    if not m:
        m = re.search(r'(20\d{2})[-/年]\s*(\d{1,2})', s)
    if m:
        y, n = map(int, m.groups())
    else:
        m = re.search(r'令和\s*(\d+)年\s*(\d+)月', s)
        if m:
            y, n = 2018 + int(m[1]), int(m[2])
        else:
            m = re.fullmatch(r'(\d{1,2})月?', s)
            if not m or not year:
                return None
            y, n = int(year), int(m[1])
    return f'{y:04d}-{n:02d}' if 1 <= n <= 12 else None


def company(value):
    s = compact(value)
    return '弘和' if '弘和' in s else ('関東' if '関東' in s else '')


def plate(value):
    s = compact(value)
    s=re.sub(r'(?<=\d)-(?=\d)','',s)
    m=re.fullmatch(r'([^\d\W]{1,8}\d{2,3}[ぁ-んァ-ヶa-zA-Z]\d{1,4})(?:[.。]?\(\d{3,4}\))?',s)
    return m[1] if m else None


def repair_encoding(value):
    # The PDF embeds Shift-JIS bytes as Latin-1 characters. Reversibility is required.
    def replace(m):
        s = m[0]
        try:
            b = s.encode('latin1'); decoded = b.decode('cp932', errors='surrogateescape')
            if decoded.encode('cp932', errors='surrogateescape') != b:return s
            return ''.join(chr(ord(c)-0xdc00) if 0xdc80<=ord(c)<=0xdcff else c for c in decoded)
        except (UnicodeError, LookupError):
            return s
    return re.sub(r'[\x00-\xff]+', replace, value)


@dataclass
class InvoiceDocument:
    """請求文書メタデータ（設計書 第3.1節）。アダプターが組み立て、関係分類へ渡す。"""
    source_ref: str
    vendor: str
    company: str
    billing_month: str
    invoice_number: str | None
    invoice_date: str | None
    printed_total: int | None
    detail_count_printed: int | None
    document_role: Literal['detail', 'cover', 'combined', 'unknown']
    detail_fingerprints: list[str]
    extraction_complete: bool
    extraction_issues: list[dict]


@dataclass
class AdapterResult:
    """アダプター結果（設計書 第3.4節）。1件読めただけでは complete にしない。"""
    matched: bool
    records_added: int
    parsed_total: int
    printed_total: int | None
    expected_detail_count: int | None
    parsed_detail_count: int
    complete: bool
    issues: list[dict]


def classify_invoice_relation(a: InvoiceDocument, b: InvoiceDocument) -> Literal[
    'exact_duplicate', 'same_invoice_complement', 'different_invoice',
    'partial_overlap', 'unknown'
]:
    """請求文書の関係分類（設計書 第3.2節の優先規則）。"""
    fps_a = set(a.detail_fingerprints)
    fps_b = set(b.detail_fingerprints)
    overlap = fps_a & fps_b
    same_vendor = bool(a.vendor) and a.vendor == b.vendor
    same_company = bool(a.company) and a.company == b.company
    same_month = bool(a.billing_month) and a.billing_month == b.billing_month
    same_number = bool(a.invoice_number) and a.invoice_number == b.invoice_number
    both_numbers = bool(a.invoice_number) and bool(b.invoice_number)
    totals_equal = a.printed_total is not None and a.printed_total == b.printed_total
    totals_differ = (
        a.printed_total is not None
        and b.printed_total is not None
        and a.printed_total != b.printed_total
    )
    fps_equal = fps_a == fps_b and bool(fps_a)
    fps_disjoint = bool(fps_a) and bool(fps_b) and not overlap
    fps_partial = bool(overlap) and fps_a != fps_b
    complementary_roles = (
        a.document_role != b.document_role
        and {a.document_role, b.document_role} <= {'detail', 'cover', 'combined'}
    )

    # 1. vendor/company/invoice_number が一致し、printed_total と明細集合も一致
    if same_vendor and same_company and same_number and totals_equal and fps_equal:
        return 'exact_duplicate'
    if same_vendor and same_company and same_number and totals_equal and not fps_a and not fps_b:
        return 'exact_duplicate'

    # 2. 請求番号一致、CSVとPDFが相補的で総額一致
    if same_number and totals_equal and (fps_partial or fps_disjoint or complementary_roles):
        return 'same_invoice_complement'

    # 3. 請求番号が異なる、または同一業者・会社・月で指紋が完全に不一致
    if both_numbers and a.invoice_number != b.invoice_number:
        return 'different_invoice'
    if same_vendor and same_company and same_month and fps_disjoint:
        return 'different_invoice'

    # 4. 請求番号なしでも明細集合・合計が完全一致
    if not both_numbers and fps_equal and totals_equal:
        return 'exact_duplicate'

    # 5. 一部だけ一致、合計差あり（件数が違う場合も部分重複）
    if fps_partial and (totals_differ or len(fps_a) != len(fps_b)):
        return 'partial_overlap'

    # 6. 比較不能
    return 'unknown'


def source_vendor(source, content=''):
    name = source.get('filename') or ''
    lower = name.lower()
    if '太陽' in name:
        return 'taiyo'
    if any(token in lower for token in ('wing', 'eneos')):
        return 'wing'
    if any(token in name for token in ('宇佐美', '御買上明細', '請求明細', '請求書(鑑)')):
        return 'usami'
    if '車番別集計' in compact(content):
        return 'wing'
    return ''


def document_role_of(source):
    if '請求書(鑑)' in (source.get('filename') or ''):
        return 'cover'
    return 'detail'


def line_fingerprint(mon, hint, product='', qty='', tax='', amount=''):
    tax_s = text(tax)
    if tax_s in ('0', '0.0'):
        tax_s = ''
    amount_s = text(amount)
    if amount_s.endswith('.0'):
        amount_s = amount_s[:-2]
    return '|'.join([text(mon), compact(hint), compact(product), compact(qty), tax_s, amount_s])


def extract_invoice_number(content, filename=''):
    s = text(content) + '\n' + text(filename)
    found = re.search(r'(?:請求(?:書)?番号|伝票番号|Invoice\s*No\.?)[:：\s#]*([A-Za-z0-9][A-Za-z0-9\-]{1,})', s, re.I)
    return found[1] if found else None


def extract_printed_total(content):
    s = text(content)
    patterns = (
        r'ご請求(?:金額|額)[^\d\-]*([+-]?[\d,]+)',
        r'請求(?:書)?(?:合計|金額)[^\d\-]*([+-]?[\d,]+)',
        r'合計金額[^\d\-]*([+-]?[\d,]+)',
        r'総合計[^\d\-]*([+-]?[\d,]+)',
    )
    for pattern in patterns:
        found = re.search(pattern, s)
        if not found:
            continue
        parsed = parse_money(found[1])
        if parsed.status == 'value':
            return parsed.amount
    return None


def extract_printed_count(content):
    s = text(content)
    found = re.search(r'(?:明細件数|件数|明細数)[^\d]*(\d+)', s)
    if not found:
        return None
    parsed = parse_money(found[1])
    return parsed.amount if parsed.status == 'value' else None


def split_csv_preamble(content):
    lines = (content or '').splitlines()
    header_i = 0
    for i, line in enumerate(lines):
        packed = compact(line)
        if any(token in packed for token in ('車番', '車両番号', '合計金額', '給油日付', '実車番')) and (',' in line or '\t' in line):
            header_i = i
            break
    return '\n'.join(lines[:header_i]), '\n'.join(lines[header_i:])


def _is_usami_fuel_detail_line(line: str) -> bool:
    s = text(line)
    if not s:
        return False
    if re.fullmatch(r'\d{5}\s+\d{4}\s+\d+', s):
        return False
    m = re.match(r'^\s*(\d+)\s+(\d{4})\s+', s)
    if not m:
        return False
    rest = s[m.end():]
    return bool(re.search(r'\d+', rest))


def empty_adapter_result(matched=False, issues=None):
    return AdapterResult(
        matched=matched,
        records_added=0,
        parsed_total=0,
        printed_total=None,
        expected_detail_count=None,
        parsed_detail_count=0,
        complete=False,
        issues=list(issues or []),
    )


def adapter_complete(parsed_count, expected_count, parsed_total, printed_total, issues, terminator_ok=False, unparsed=False):
    if issues or unparsed:
        return False
    if terminator_ok and expected_count is not None and parsed_count == expected_count and parsed_count > 0:
        return True
    if expected_count is not None and printed_total is not None:
        return parsed_count == expected_count and parsed_total == printed_total and parsed_count > 0
    if printed_total is not None:
        return parsed_total == printed_total and parsed_count > 0 and not unparsed
    return False


class Extractor:
    def __init__(self, months, zero=False):
        self.months = months
        self.zero = zero
        self.vehicles = {}
        self.records = []
        self.issues = []
        self.dispositions = []
        self.recoveries = []
        self.employee_links = defaultdict(set)
        self.employee_assignments = []
        self.decision_log = []
        self.seen = {}
        self.book_sources = []
        self.formula_gaps=set()
        self.source_controls = []
        self._pending_invoices = {}
        self.invoice_documents = []
        self.detail_fingerprints = defaultdict(list)
        self.usami_ocr_adoptions = {}

    def _control_key(self, item):
        return (
            item.get('source_ref'),
            item.get('company'),
            item.get('month'),
            item.get('category'),
            item.get('tax_basis'),
            item.get('invoice_id'),
            item.get('origin'),
            item.get('control_kind'),
            item.get('source_locator'),
        )

    def add_control(self, **item):
        item.setdefault('invoice_id', None)
        item.setdefault('tax_basis', 'inclusive')
        item.setdefault('category', item.get('category') or 'unknown')
        item.setdefault('extraction_method', 'adapter:v1')
        if item.get('category') in (None, ''):
            item['category'] = 'unknown'
            self.issue('control', str(item.get('source_ref') or '').replace('context:', '', 1), item.get('source_locator') or '',
                       '費目が不明な統制値です', month=item.get('month'), category='unknown')
        key = self._control_key(item)
        if any(self._control_key(existing) == key for existing in self.source_controls):
            return
        self.source_controls.append(item)

    def add_source_control(self, source, locator, mon, category, amount, origin, control_kind, comp='',
                           invoice_id=None, tax_basis='inclusive', status='available', reason='', reason_code='',
                           extraction_method='adapter:v1'):
        item = dict(
            source_ref='context:' + source['id'],
            company=comp or company(source.get('filename') or '') or '未特定',
            month=mon,
            category=category or 'unknown',
            tax_basis=tax_basis,
            invoice_id=invoice_id,
            control_kind=control_kind,
            origin=origin,
            amount=amount,
            source_locator=locator,
            extraction_method=extraction_method,
            status=status,
        )
        if reason:
            item['reason'] = reason
        if reason_code:
            item['reason_code'] = reason_code
        self.add_control(**item)
        return item

    def remember_invoice(self, invoice: InvoiceDocument):
        self._pending_invoices[invoice.source_ref] = invoice
        self.invoice_documents = [inv for inv in self.invoice_documents if inv.source_ref != invoice.source_ref]
        self.invoice_documents.append(invoice)

    def _row_is_total(self, value):
        packed = compact(value)
        return bool(packed) and any(word in packed for word in TOTAL_WORDS)

    def _total_kind(self, value):
        packed = compact(value)
        if '総計' in packed:
            return 'invoice_total'
        if '小計' in packed:
            return 'sheet_subtotal'
        return 'category_total'

    def drop_source_records(self, source_ref):
        self.records = [r for r in self.records if r.get('source_ref') != source_ref]

    def source_amount_total(self, source_ref, category='fuel'):
        return sum(int(r.get('amount') or 0) for r in self.records if r.get('source_ref') == source_ref and r.get('category') == category)

    def issue(self, kind, source, locator, message, **extra):
        from app.vehicle_workflow import digest
        key = digest([kind, source, locator, message, extra.get('month'), extra.get('category')])[:20]
        item = dict(id=key, kind=kind, source_ref='context:'+source, locator=locator,
                    message=message, status='unresolved', **extra)
        if key not in {x['id'] for x in self.issues}:
            self.issues.append(item)
        return item

    def vehicle(self, value, comp='', employee='', effective_from=None, effective_to=None):
        vid = plate(value)
        if not vid:
            return None
        # Exact registration, not a suffix, defines identity. Conflicting ownership is unresolved.
        if vid in self.vehicles and comp and self.vehicles[vid].get('company') not in ('', comp):
            return None
        self.vehicles.setdefault(vid, dict(id=vid, label=text(value), company=comp or '未特定'))
        if employee:
            emp = compact(employee)
            self.employee_links[(comp, emp)].add(vid)
            self.employee_assignments.append(dict(
                company=comp, employee=emp, vehicle_id=vid,
                effective_from=effective_from, effective_to=effective_to,
            ))
        return vid

    def linked_vehicles(self, comp, employee, mon):
        """その月に有効な assignment。期間無しの複数台は自動確定せず呼び出し側で確認する。"""
        employee = compact(employee)
        perioded, unperioded = [], []
        for item in self.employee_assignments:
            if item.get('company') != comp or item.get('employee') != employee:
                continue
            start, end = item.get('effective_from'), item.get('effective_to')
            if start or end:
                if (not start or mon >= start) and (not end or mon <= end):
                    perioded.append(item['vehicle_id'])
            else:
                unperioded.append(item['vehicle_id'])
        if perioded:
            return sorted(set(perioded))
        if unperioded:
            return sorted(set(unperioded))
        return sorted(self.employee_links.get((comp, employee), set()))

    def match(self, hint, comp=''):
        hint = plate(hint) or compact(hint)
        if hint in self.vehicles and (not comp or self.vehicles[hint]['company'] == comp):
            return [hint]
        if not re.fullmatch(r'\d{3,4}', hint):
            return []
        return [v for v, item in self.vehicles.items() if (not comp or item['company'] == comp)
                and re.search(r'\d+$', v)[0].zfill(4).endswith(hint)]

    def add(self, source, locator, mon, category, parsed, targets, comp='', basis='inclusive', allocations=None, **extra):
        from app.vehicle_workflow import digest
        if mon not in self.months:
            return
        if not isinstance(parsed, MoneyParseResult):
            parsed = parse_money(parsed)
        if (source['id'], locator) in self.formula_gaps:
            self.issue('read', source['id'], locator, '数式セルの計算済み値がありません。原本コピーの再計算が必要です',
                       raw_text=parsed.raw_text, reason_code='formula_missing', month=mon, category=category)
            return
        if parsed.status == 'blank':
            return
        if parsed.status in ('invalid', 'out_of_range'):
            if parsed.reason == 'boolean':
                reason_code = 'boolean'
            elif parsed.reason == 'unreadable':
                reason_code = 'unreadable'
            elif parsed.status == 'out_of_range':
                reason_code = 'out_of_range'
            else:
                reason_code = 'non_numeric'
            self.issue('read', source['id'], locator, '金額を数値として読み取れません。読取失敗を0円として確定しません',
                       raw_text=parsed.raw_text, reason_code=reason_code, month=mon, category=category)
            return
        amount = parsed.amount
        key = (source['id'], locator, mon, category)
        if key in self.seen:
            return
        self.seen[key] = True
        targets = sorted(set(targets))
        extra.pop('allocations', None)
        if allocations is None:
            allocations = [{'vehicle_id': targets[0], 'ratio': 1}] if len(targets) == 1 else []
        else:
            allocations = list(allocations)
        r = dict(id=digest(key)[:24], source_ref='context:'+source['id'], source_locator=locator,
                 month=mon, category=category, amount=amount, company=comp or '未特定',
                 tax_basis=basis, quality='actual', allocations=allocations, **extra)
        self.records.append(r)
        if not allocations and amount:
            subject = extra.get('allocation_subject') or extra.get('vehicle_hint')
            self.issue(
                'allocation', source['id'], locator,
                '原本の金額の帰属を確認してください' + (('（' + str(subject) + '）') if subject else ''),
                record_id=r['id'], amount=amount, candidates=targets, month=mon, category=category,
                allocation_subject=extra.get('allocation_subject') or '',
                group=extra.get(
                    'allocation_group',
                    ('fuel:' + comp + ':' + str(extra['vehicle_hint']) + ':' + mon) if extra.get('vehicle_hint') else r['id'],
                ),
            )

    def read_books(self, docs):
        from openpyxl import load_workbook
        for source, raw, content in docs:
            if Path(source['filename']).suffix.lower() not in ('.xlsx', '.xlsm') or not raw:
                continue
            try:
                book = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
                sheets = [(s.title, list(s.values)) for s in book]
                book.close()
                formulas=load_workbook(io.BytesIO(raw),data_only=False,read_only=True)
                cached={name:rows for name,rows in sheets}
                for sheet in formulas:
                    for row in sheet:
                        for cell in row:
                            if cell.data_type=='f' and cached[sheet.title][cell.row-1][cell.column-1] is None:
                                self.formula_gaps.add((source['id'],f'{sheet.title}!{cell.coordinate}'))
                formulas.close()
                self.book_sources.append((source, sheets))
            except Exception as exc:
                self.issue('read', source['id'], '', 'Excel読取失敗: '+type(exc).__name__)
        # Prefer the expressly named T3 workbook; duplicate exports are not additional transactions.
        masters = []
        for source, sheets in self.book_sources:
            for name, rows in sheets:
                for i, row in enumerate(rows[:10]):
                    labels = [compact(x) for x in row]
                    if '車両番号' in labels and '月額税込' in labels:
                        masters.append((0 if 'T3' in source['filename'] else 1, source, name, rows, i))
                        break
        masters.sort(key=lambda x:(x[0], x[1]['filename'], '(2)' not in x[2]))
        if masters:
            self.master(*masters[0][1:])
        for source, sheets in self.book_sources:
            used = any(r['source_ref']=='context:'+source['id'] for r in self.records)
            for name, rows in sheets:
                used = self.payroll(source, name, rows) or used
                used = self.generic(source, name, rows) or used
            if not used:
                if any(m[1]['id']==source['id'] for m in masters) or '年間燃料' in source['filename']:
                    self.dispositions.append(dict(source_ref='context:'+source['id'], status='excluded', evidence='車両台帳の重複版または年平均。月次実績への重複加算を除外'))
                else:
                    self.issue('adapter', source['id'], '', '対応する表形式を確認できません。月次明細抽出アダプターが必要です')

    def master(self, source, name, rows, header):
        from openpyxl.utils import get_column_letter
        labels = [compact(x) for x in rows[header]]
        cols = {x:labels.index(x) for x in ('車両番号', '月額税込', '開始', '終了') if x in labels}
        month_cols = [(i, month(x)) for i, x in enumerate(rows[header]) if month(x) in self.months]
        for rn, row in enumerate(rows[header+1:], header+2):
            def get(i): return row[i] if i < len(row) else None
            start = month(get(cols.get('開始', -1))) if '開始' in cols else None
            end = month(get(cols.get('終了', -1))) if '終了' in cols else None
            vid = self.vehicle(get(cols['車両番号']), company(get(4)), get(13), start, end)
            if not vid:
                raw_vehicle=text(get(cols['車両番号']))
                if self._row_is_total(raw_vehicle):
                    kind=self._total_kind(raw_vehicle)
                    row_comp=company(get(4)) or company(source['filename'])
                    for col, mon in month_cols:
                        parsed=parse_money(get(col))
                        if parsed.status=='value':
                            self.add_source_control(source,f'{name}!{get_column_letter(col+1)}{rn}',mon,'revenue',parsed.amount,
                                                    'source_total',kind,row_comp,tax_basis='exclusive',extraction_method='excel_total_row:v1')
                    if '月額税込' in cols:
                        parsed=parse_money(get(cols['月額税込']))
                        if parsed.status=='value':
                            for mon in self.months:
                                self.add_source_control(source,f'{name}!{get_column_letter(cols["月額税込"]+1)}{rn}',mon,'lease',parsed.amount,
                                                        'source_total',kind,row_comp,extraction_method='excel_total_row:v1')
                    continue
                if raw_vehicle and not self._row_is_total(raw_vehicle):
                    self.issue('scope',source['id'],f'{name}!行{rn}','車両番号を正規化できません。対象から黙って除外しません')
                continue
            comp = self.vehicles[vid]['company']
            for col, mon in month_cols:
                self.add(source, f'{name}!{get_column_letter(col+1)}{rn}', mon, 'revenue', parse_money(get(col)), [vid], comp, 'exclusive')
            lease = parse_money(get(cols['月額税込']))
            for mon in self.months:
                if (not start or mon >= start) and (not end or mon <= end):
                    self.add(source, f'{name}!{get_column_letter(cols["月額税込"]+1)}{rn}', mon, 'lease', lease, [vid], comp,
                             evidence='税込月額。契約開始・終了月内に適用。税の再加算なし')
        self.dispositions.append(dict(source_ref='context:'+source['id'], status='included', evidence='車両台帳、明示された月別売上と契約月額を使用'))

    def payroll(self, source, name, rows):
        from openpyxl.utils import get_column_letter
        from app.vehicle_workflow import employer_insurance_column, insurance_column_rejected
        h = next((i for i,row in enumerate(rows[:10]) if '支給合計' in [compact(x) for x in row] and '氏名' in [compact(x) for x in row]), None)
        if h is None:
            return False
        labels = [compact(x) for x in rows[h]]
        if '月/回' not in labels:
            return False
        idx = {x:labels.index(x) for x in ('氏名','月/回','支給合計')}
        comp = company(source['filename']); employee = ''
        insurance = employer_insurance_column(labels)
        rejected_personal = [i for i, x in enumerate(labels) if insurance_column_rejected(x)]
        months_with_payroll = set()
        for rn,row in enumerate(rows[h+1:],h+2):
            name_cell=text(row[idx['氏名']]) if idx['氏名'] < len(row) else ''
            if name_cell: employee = compact(name_cell)
            if self._row_is_total(name_cell):
                mon = month(row[idx['月/回']]) if idx['月/回'] < len(row) else None
                if mon in self.months:
                    for category, col in [('payroll',idx['支給合計']), ('insurance',insurance)]:
                        if col is not None and col < len(row):
                            parsed=parse_money(row[col])
                            if parsed.status=='value':
                                self.add_source_control(source,f'{name}!{get_column_letter(col+1)}{rn}',mon,category,parsed.amount,
                                                        'source_total',self._total_kind(name_cell),comp,tax_basis='non_taxable',
                                                        extraction_method='excel_total_row:v1')
                continue
            mon = month(row[idx['月/回']])
            if mon not in self.months:
                continue
            months_with_payroll.add(mon)
            matches = list(self.linked_vehicles(comp, employee, mon))
            if not matches:
                # Surname-only master must resolve to exactly one payroll employee, in this company.
                payroll_names = {compact(r[idx['氏名']]) for r in rows[h+1:] if len(r)>idx['氏名'] and r[idx['氏名']]}
                for (c, short), vehicles in self.employee_links.items():
                    names = [n for n in payroll_names if n.startswith(short)]
                    if c == comp and short and len(names)==1 and names[0]==employee:
                        matches = list(self.linked_vehicles(c, short, mon)) or sorted(vehicles)
            allocations = [{'vehicle_id': matches[0], 'ratio': 1}] if len(matches) == 1 else []
            group = source['id']+':'+employee+':'+mon
            self.add(source, f'{name}!{get_column_letter(idx["支給合計"]+1)}{rn}',mon,'payroll',parse_money(row[idx['支給合計']]),matches,comp,'non_taxable',
                     allocations=allocations,allocation_group=group,allocation_subject=employee)
            if insurance is not None:
                parsed = parse_money(row[insurance]) if insurance < len(row) else parse_money(None)
                extra = dict(
                    allocations=allocations, allocation_group=group, allocation_subject=employee,
                    insurance_basis=dict(kind='explicit_employer_amount', source_refs=['context:'+source['id']], rule_version=None),
                )
                self.add(source, f'{name}!{get_column_letter(insurance+1)}{rn}',mon,'insurance',parsed,matches,comp,'non_taxable',**extra)
        if insurance is None:
            for mon in sorted(months_with_payroll):
                self.issue(
                    'insurance_basis', source['id'], name,
                    '会社負担社会保険の明示列がありません。料率計算では埋めません',
                    month=mon, category='insurance',
                )
        if rejected_personal and insurance is None:
            self.issue(
                'insurance_basis', source['id'], name,
                '本人控除列は会社負担保険へ転用しません',
                category='insurance', reason_code='personal_deduction_rejected',
            )
        self.dispositions.append(dict(source_ref='context:'+source['id'],status='included',evidence='支給合計のみ使用。本人控除の社会保険は会社負担へ転用しない'))
        return True

    def generic(self, source, name, rows):
        # Explicit long ledger; no inference from an arbitrary numeric column.
        aliases = {'車両番号':'vehicle', '車番':'vehicle', '車両ID':'vehicle', '年月':'month', '対象月':'month',
                   '売上':'revenue','売上税込':'revenue', '総支給額':'payroll','会社負担社会保険':'insurance',
                   '燃料費':'fuel','税込リース':'lease','高速料金':'toll','その他費用':'other'}
        from openpyxl.utils import get_column_letter
        for h,row in enumerate(rows[:10]):
            fields = {aliases[compact(v)]:i for i,v in enumerate(row) if compact(v) in aliases}
            if not {'vehicle','month'} <= fields.keys() or not set(CATEGORIES)&fields.keys():
                continue
            for rn,row in enumerate(rows[h+1:],h+2):
                mon = month(row[fields['month']]); hint = text(row[fields['vehicle']])
                comp = company(source['filename'])
                if self._row_is_total(hint):
                    if mon in self.months:
                        for cat in set(CATEGORIES)&fields.keys():
                            col = fields[cat]
                            parsed=parse_money(row[col] if col < len(row) else None)
                            if parsed.status=='value':
                                self.add_source_control(source,f'{name}!{get_column_letter(col+1)}{rn}',mon,cat,parsed.amount,
                                                        'source_total',self._total_kind(hint),comp,
                                                        tax_basis='non_taxable' if cat in ('payroll','insurance') else 'inclusive',
                                                        extraction_method='excel_total_row:v1')
                    continue
                self.vehicle(hint,comp)
                for cat in set(CATEGORIES)&fields.keys():
                    col = fields[cat]
                    self.add(source,f'{name}!{get_column_letter(col+1)}{rn}',mon,cat,parse_money(row[col]),self.match(hint,comp),comp,
                             'non_taxable' if cat in ('payroll','insurance') else 'inclusive')
            return True
        return False

    def _fuel_result(self, source, start, parsed_total, printed_total, expected, parsed_count, issues, terminator_ok=False, unparsed=False, matched=True):
        added = len(self.records) - start
        complete = adapter_complete(parsed_count, expected, parsed_total, printed_total, issues, terminator_ok=terminator_ok, unparsed=unparsed)
        return AdapterResult(matched, added, parsed_total, printed_total, expected, parsed_count, complete, issues)

    def _note_fingerprint(self, source, mon, hint, amount, tax=''):
        fp = line_fingerprint(mon, hint, amount=amount, tax=tax)
        self.detail_fingerprints['context:' + source['id']].append(fp)
        return fp

    def fuel(self, source, content):
        filename = source['filename']
        mon = month(filename)
        if mon not in self.months:
            return empty_adapter_result(False)
        comp = company(filename) or company(content)
        s = text(content)
        start = len(self.records)
        role = document_role_of(source)
        if role == 'cover':
            printed = extract_printed_total(s)
            locator = '請求書(鑑)'
            found = re.search(r'ご請求(?:金額|額)|請求(?:書)?(?:合計|金額)|合計金額|総合計', s)
            if found:
                locator = f'鑑:{found.group(0)}'
            issues = []
            if printed is None:
                issues.append(self.issue('control', source['id'], locator, '請求書鑑から請求総額を読めません', month=mon, category='fuel'))
            else:
                self.add_source_control(
                    source, locator, mon, 'fuel', printed, 'source_total', 'invoice_total', comp,
                    extraction_method='invoice_header_adapter:v1',
                )
            return AdapterResult(True, 0, 0, printed, 0, 0, printed is not None and not issues, issues)

        if filename.lower().endswith('.csv'):
            preamble, table = split_csv_preamble(content)
            reader = csv.DictReader(io.StringIO(table or s))
            if not reader.fieldnames:
                return empty_adapter_result(False)
            printed_total = extract_printed_total(preamble)
            printed_count = extract_printed_count(preamble)
            if printed_total is not None:
                self.add_source_control(
                    source, 'CSV鑑相当:請求合計', mon, 'fuel', printed_total, 'source_total', 'invoice_total', comp,
                    extraction_method='csv_header_total:v1',
                )
            issues = []
            parsed_total = 0
            parsed_count = 0
            line_sum = 0
            expected_rows = 0
            for n, row in enumerate(reader, 2):
                row = {compact(k): v for k, v in row.items() if k}
                expected_rows += 1
                hint = next((text(row.get(k)) for k in ('車番', '車両番号', '実車番・届先') if text(row.get(k))), '')
                billed = parse_money(row.get('合計金額', row.get('金額')))
                if billed.status == 'value' and month(row.get('給油日付')) in (mon, None):
                    line_sum += billed.amount
                parsed = billed
                if parsed.status == 'blank':
                    continue
                if parsed.status != 'value':
                    issues.append(self.issue(
                        'read', source['id'], f'CSV行{n}', '金額欄の抽出が未完了です。未確認のまま0円として確定しません',
                        raw_text=parsed.raw_text, reason_code=parsed.reason or 'non_numeric',
                        month=month(row.get('給油日付')) or mon, category='fuel',
                    ))
                    continue
                amount = parsed.amount
                if '合計金額' in row:
                    tax = 0
                    diesel = 0
                else:
                    tax_parsed = parse_money(row.get('参考消費税', 0))
                    diesel_parsed = parse_money(row.get('軽油税', 0))
                    tax = tax_parsed.amount if tax_parsed.status == 'value' else 0
                    diesel = diesel_parsed.amount if diesel_parsed.status == 'value' else 0
                charged = amount + tax + diesel
                before = len(self.records)
                self.add(
                    source, f'CSV行{n}', month(row.get('給油日付')) or mon, 'fuel', parse_money(charged),
                    self.match(hint, comp), comp, vehicle_hint=hint,
                    evidence='金額＋明示参考消費税＋軽油税。請求総額との差は別途照合',
                )
                if len(self.records) > before:
                    parsed_total += charged
                    parsed_count += 1
                    self._note_fingerprint(source, mon, hint, charged, tax=tax + diesel)
            if line_sum or expected_rows:
                self.add_source_control(
                    source, 'CSV請求合計列総和', mon, 'fuel', line_sum, 'sum_of_lines', 'category_total', comp,
                    extraction_method='csv_line_sum:v1',
                )
            expected = printed_count if printed_count is not None else None
            terminator_ok = not issues and expected_rows >= 0 and printed_total is not None
            return self._fuel_result(
                source, start, parsed_total, printed_total, expected, parsed_count, issues,
                terminator_ok=terminator_ok, unparsed=bool(issues), matched=True,
            )

        if '太陽' in filename:
            issues = []
            parsed_total = 0
            parsed_count = 0
            subtotal = None
            sub_locator = ''
            matched_line = False
            for n, line in enumerate(s.splitlines(), 1):
                m = re.search(r'^\s*([\d,]+)\s*カード車番[:：]\s*(\d{3,4}).*車両合計金額', line)
                if m:
                    matched_line = True
                    before = len(self.records)
                    self.add(source, f'抽出行{n}', mon, 'fuel', parse_money(m[1]), self.match(m[2], comp), comp,
                             vehicle_hint=m[2], evidence='原本の車両合計金額。参考税込額')
                    if len(self.records) > before:
                        amount = parse_money(m[1]).amount or 0
                        parsed_total += amount
                        parsed_count += 1
                        self._note_fingerprint(source, mon, m[2], amount)
                    continue
                sub = re.search(r'小計[^\d\-]*([+-]?[\d,]+)', line)
                if sub:
                    parsed = parse_money(sub[1])
                    if parsed.status == 'value':
                        subtotal = (subtotal or 0) + parsed.amount
                        sub_locator = f'抽出行{n}'
            if subtotal is not None:
                self.add_source_control(
                    source, sub_locator or '太陽小計', mon, 'fuel', subtotal, 'source_total', 'sheet_subtotal', comp,
                    extraction_method='taiyo_subtotal:v1',
                )
                if subtotal != parsed_total:
                    issues.append(self.issue(
                        'control', source['id'], sub_locator or '太陽小計',
                        '小計行の合計と明細加算が一致しません', month=mon, category='fuel',
                        printed_total=subtotal, parsed_total=parsed_total,
                    ))
            terminator_ok = subtotal is not None and not issues
            return self._fuel_result(
                source, start, parsed_total, subtotal, parsed_count if matched_line else None, parsed_count, issues,
                terminator_ok=terminator_ok, matched=matched_line,
            )

        if '車番別集計' in compact(s):
            issues = []
            parsed_total = 0
            parsed_count = 0
            subtotal = None
            sub_locator = ''
            active = False
            saw_end = False
            for n, line in enumerate(s.splitlines(), 1):
                if '車番別集計' in compact(line):
                    active = True
                    continue
                if active and '小計' in compact(line):
                    nums = [parse_money(x) for x in re.findall(r'[+-]?[\d,]+', line)]
                    values = [p.amount for p in nums if p.status == 'value']
                    if values:
                        subtotal = (subtotal or 0) + sum(values)
                        sub_locator = f'抽出行{n}'
                    active = False
                    saw_end = True
                    continue
                m = re.fullmatch(r'\s*(\d{3,4})\s+([\d,]+)\s+([\d,]+)\s*', line)
                if active and m:
                    goods = parse_money(m[2])
                    tax = parse_money(m[3])
                    combined = parse_money((goods.amount or 0) + (tax.amount or 0)) if goods.status == 'value' and tax.status == 'value' else goods if goods.status != 'value' else tax
                    before = len(self.records)
                    self.add(source, f'車番別集計 {m[1]}', mon, 'fuel', combined, self.match(m[1], comp), comp,
                             vehicle_hint=m[1], evidence='原本の車番別商品代＋明示参考消費税')
                    if len(self.records) > before and combined.status == 'value':
                        parsed_total += combined.amount
                        parsed_count += 1
                        self._note_fingerprint(source, mon, m[1], combined.amount, tax=tax.amount if tax.status == 'value' else '')
            if subtotal is not None:
                self.add_source_control(
                    source, sub_locator or '車番別小計', mon, 'fuel', subtotal, 'source_total', 'sheet_subtotal', comp,
                    extraction_method='plate_subtotal:v1',
                )
                if subtotal != parsed_total:
                    issues.append(self.issue(
                        'control', source['id'], sub_locator or '車番別小計',
                        '小計行の合計と明細加算が一致しません', month=mon, category='fuel',
                        printed_total=subtotal, parsed_total=parsed_total,
                    ))
            terminator_ok = saw_end and (subtotal is None or subtotal == parsed_total)
            return self._fuel_result(
                source, start, parsed_total, subtotal, parsed_count if parsed_count else None, parsed_count, issues,
                terminator_ok=terminator_ok, matched=True,
            )

        if '宇佐美' in filename or '御買上明細' in filename:
            issues = []
            parsed_total = 0
            parsed_count = 0
            printed_total = 0
            tax_total = 0
            printed_any = False
            blocks = list(re.finditer(r'^\s*\d{5}\s+(\d{4})\s+\d+\s*$', s, re.M))
            if not blocks:
                return empty_adapter_result(False)
            valid_fuel_blocks = 0
            for i, m in enumerate(blocks):
                block = s[m.end():blocks[i + 1].start() if i + 1 < len(blocks) else len(s)]
                plate4 = m[1]
                locator = f'文字位置{m.start()}'
                has_fuel_detail = any(
                    _is_usami_fuel_detail_line(line)
                    for line in block.splitlines()
                )
                if plate4 == '0000' and '御入金' in block and not has_fuel_detail:
                    self.decision_log.append(dict(
                        action='exclude_payment_page',
                        source_ref='context:' + source['id'],
                        locator=locator,
                        month=mon,
                        reason='入金ページ(給油明細なし)として除外',
                    ))
                    continue
                valid_fuel_blocks += 1
                sub = re.search(r'小計\s+([\d,]+)', block)
                diesel = re.search(r'軽油引取税[^\n]*?([\d,]+)\s*$', block, re.M)
                tax = re.search(r'参考消費税[^\n]*?\(\s*([\d,]+)\)', block)
                printed = re.search(r'自動車用潤滑油[^\n]*?\s([\d,]+)\s*$', block, re.M)
                if not sub or not printed:
                    ocr_block = (self.usami_ocr_adoptions or {}).get(source['id'], {}).get(m[1])
                    if (
                        ocr_block is not None
                        and getattr(ocr_block, 'adopted', False)
                        and ocr_block.charged_amount is not None
                        and ocr_block.printed_total is not None
                    ):
                        charged = ocr_block.charged_amount
                        printed_any = True
                        printed_total += ocr_block.printed_total
                        ocr_locator = ocr_block.source_locator or locator
                        if ocr_block.tax is None:
                            issues.append(self.issue(
                                'read', source['id'], ocr_locator,
                                '参考消費税が読めないブロックがあります',
                                month=mon, category='fuel', reason_code='tax_unreadable',
                            ))
                        else:
                            tax_total += ocr_block.tax
                        before = len(self.records)
                        self.add(
                            source, ocr_locator, mon, 'fuel', parse_money(charged),
                            self.match(m[1], comp), comp, vehicle_hint=m[1],
                            evidence='OCR承認済み宇佐美ブロック。小計＋軽油税を印字合計と照合し、明示参考消費税を加算',
                            ocr_run_id=ocr_block.run_id, ocr_page=ocr_block.page,
                            ocr_block_id=ocr_block.block_id, context_file_id=source.get('id'),
                        )
                        if len(self.records) > before:
                            parsed_total += charged
                            parsed_count += 1
                            self._note_fingerprint(source, mon, m[1], charged, tax=ocr_block.tax or '')
                        continue
                    missing = []
                    if not sub:
                        missing.append('小計')
                    if not printed:
                        missing.append('印字合計')
                    issues.append(self.issue(
                        'read', source['id'], locator,
                        '宇佐美ブロックの' + '・'.join(missing) + 'が欠落しています',
                        month=mon, category='fuel', reason_code='partial_block',
                    ))
                    continue
                sub_parsed = parse_money(sub[1])
                diesel_parsed = parse_money(diesel[1]) if diesel else MoneyParseResult('value', 0, '', 'int')
                printed_parsed = parse_money(printed[1])
                if sub_parsed.status != 'value' or diesel_parsed.status != 'value' or printed_parsed.status != 'value':
                    issues.append(self.issue(
                        'read', source['id'], locator, '宇佐美ブロックの金額を数値として読み取れません',
                        month=mon, category='fuel', reason_code='non_numeric',
                    ))
                    continue
                amount = sub_parsed.amount + diesel_parsed.amount
                if printed_parsed.amount != amount:
                    issues.append(self.issue('control', source['id'], locator, '車番小計と印字合計が一致しません'))
                    continue
                printed_any = True
                printed_total += printed_parsed.amount
                tax_parsed = parse_money(tax[1]) if tax else None
                if tax_parsed is None or tax_parsed.status != 'value':
                    tax_amount = 0
                    issues.append(self.issue(
                        'read', source['id'], locator,
                        '参考消費税が読めないブロックがあります',
                        month=mon, category='fuel', reason_code='tax_unreadable',
                    ))
                else:
                    tax_amount = tax_parsed.amount
                    tax_total += tax_amount
                charged = amount + (tax_amount or 0)
                before = len(self.records)
                self.add(source, f'{locator} 車番小計', mon, 'fuel', parse_money(charged), self.match(m[1], comp), comp,
                         vehicle_hint=m[1], evidence='小計＋軽油税を印字合計と照合し、明示参考消費税を加算')
                if len(self.records) > before:
                    parsed_total += charged
                    parsed_count += 1
                    self._note_fingerprint(source, mon, m[1], charged, tax=tax_amount or '')
            expected = valid_fuel_blocks
            printed_value = (printed_total + tax_total) if printed_any else None
            if printed_value is not None:
                self.add_source_control(
                    source, '宇佐美印字合計+明示参考消費税', mon, 'fuel', printed_value, 'source_total', 'invoice_total', comp,
                    extraction_method='usami_block_printed_plus_tax:v1',
                )
            terminator_ok = parsed_count == expected and not issues
            return self._fuel_result(
                source, start, parsed_total, printed_value, expected, parsed_count, issues,
                terminator_ok=terminator_ok, unparsed=bool(issues), matched=True,
            )

        return empty_adapter_result(False)

    def finish(self, docs):
        if not self.vehicles:
            raise ValueError('対象車両を原本から特定できません。計算は未実施です。車両台帳の読取方法を確認してください')
        dispositions={d['source_ref']:d for d in self.dispositions}
        for source,raw,content in docs:
            ref='context:'+source['id']
            if ref not in dispositions:
                dispositions[ref]=dict(source_ref=ref,status='included',evidence='原本を自動解析。未対応箇所は確認事項に保持')
        present={(a['vehicle_id'],r['month'],r['category']) for r in self.records for a in r['allocations']}
        blocked=set()
        for issue in self.issues:
            if issue.get('kind') == 'read' and issue.get('status') == 'unresolved':
                month_key = issue.get('month')
                category = issue.get('category')
                if month_key and category:
                    for vid in self.vehicles:
                        blocked.add((vid, month_key, category))
            if issue.get('kind') == 'allocation' and issue.get('status') == 'unresolved':
                record = next((r for r in self.records if r['id'] == issue.get('record_id')), None)
                if record:
                    for vid in self.vehicles:
                        blocked.add((vid, record['month'], record['category']))
            if issue.get('kind') == 'insurance_basis' and issue.get('status') == 'unresolved':
                month_key = issue.get('month')
                if month_key:
                    for vid in self.vehicles:
                        blocked.add((vid, month_key, 'insurance'))
        for r in self.records:
            if not r.get('allocations'):
                for vid in self.vehicles:
                    blocked.add((vid, r['month'], r['category']))
        if self.zero:
            for vid,v in self.vehicles.items():
                for mon in self.months:
                    for cat in CATEGORIES:
                        if (vid,mon,cat) not in present and (vid,mon,cat) not in blocked:
                            self.records.append(dict(id=f'zero:{vid}:{mon}:{cat}',company=v['company'],month=mon,category=cat,amount=0,
                                tax_basis='non_taxable' if cat in ('payroll','insurance') else 'inclusive',quality='assumed_zero',
                                source_ref='policy:missing-zero',source_locator='案件の不足0円指示',
                                evidence='真正の欠損のみ。読取失敗・未配賦は0円で解消しない',
                                zero_reason_code='authorized_missing',
                                allocations=[dict(vehicle_id=vid,ratio=1)]))
        drop_assumed_zero_when_actual(self.records, self.decision_log)
        if not any(r['quality']=='actual' for r in self.records):
            self.issue('empty',docs[0][0]['id'],'','実額明細を抽出できていません。全ゼロを合格にしません')
        return dict(schema='vehicle-profit-input-v1',rounding='yen_half_up',basis_note='原本表記を維持。税込リースへ税を再加算しない。税基準混在は暫定損益として表示。会社負担料率は推測しない。',
                    months=self.months,vehicles=list(self.vehicles.values()),records=self.records,required_categories=list(CATEGORIES),
                    auto_extraction=dict(version=VERSION,issues=self.issues,recoveries=self.recoveries,
                                         invoices=[dict(
                                             source_ref=inv.source_ref, vendor=inv.vendor, company=inv.company,
                                             billing_month=inv.billing_month, invoice_number=inv.invoice_number,
                                             document_role=inv.document_role, printed_total=inv.printed_total,
                                             extraction_complete=inv.extraction_complete,
                                         ) for inv in self.invoice_documents]),
                    missing_policy='zero' if self.zero else 'unknown',provisional_numeric=self.zero,
                    source_dispositions=list(dispositions.values()),source_controls=list(self.source_controls),
                    assignments=list(self.employee_assignments),
                    decision_log=list(self.decision_log),
                    scope_evidence='原本の車両台帳と月次明細から対象集合を抽出')

    def apply_invoice_relation(self, incoming: InvoiceDocument):
        group = [
            inv for inv in self.invoice_documents
            if inv.vendor and inv.vendor == incoming.vendor
            and inv.company and inv.company == incoming.company
            and inv.billing_month and inv.billing_month == incoming.billing_month
        ]
        if not group:
            self.remember_invoice(incoming)
            return incoming, 'included', None
        relations = [(classify_invoice_relation(incoming, other), other) for other in group]
        exact = next((other for rel, other in relations if rel == 'exact_duplicate'), None)
        if not exact:
            incoming_fps = set(incoming.detail_fingerprints)
            incoming_total = incoming.printed_total
            if incoming_total is None:
                incoming_total = self.source_amount_total(incoming.source_ref)
            for other in group:
                other_fps = set(other.detail_fingerprints)
                if not incoming_fps or incoming_fps != other_fps:
                    continue
                other_total = other.printed_total
                if other_total is None:
                    other_total = self.source_amount_total(other.source_ref)
                if incoming_total == other_total:
                    exact = other
                    break
        if exact:
            self.remember_invoice(incoming)
            return incoming, 'excluded', exact
        if incoming.document_role == 'cover':
            self.remember_invoice(incoming)
            return incoming, 'control', group[0]
        cover_other = next((other for other in group if other.document_role == 'cover'), None)
        if cover_other is not None:
            self.remember_invoice(incoming)
            return incoming, 'included', cover_other
        complement = next((other for rel, other in relations if rel == 'same_invoice_complement'), None)
        if complement:
            incoming_is_cover = incoming.document_role == 'cover' or not incoming.detail_fingerprints
            other_is_cover = complement.document_role == 'cover' or not complement.detail_fingerprints
            self.remember_invoice(incoming)
            if incoming_is_cover and not other_is_cover:
                return incoming, 'control', complement
            if other_is_cover and not incoming_is_cover:
                return incoming, 'included', complement
            keep_incoming = len(incoming.detail_fingerprints) >= len(complement.detail_fingerprints)
            if keep_incoming:
                self.drop_source_records(complement.source_ref)
                for d in self.dispositions:
                    if d.get('source_ref') == complement.source_ref:
                        d.update(status='excluded', relation='same_invoice_complement', duplicate_of=incoming.source_ref,
                                 evidence='同一請求の相補文書。明細精度の高い方を計上し、他方は control 証拠')
                return incoming, 'included', complement
            return incoming, 'control', complement
        partial = next((other for rel, other in relations if rel == 'partial_overlap'), None)
        if partial:
            self.remember_invoice(incoming)
            return incoming, 'partial', partial
        different = next((other for rel, other in relations if rel == 'different_invoice'), None)
        if different:
            self.remember_invoice(incoming)
            return incoming, 'included', different
        unknown = relations[0][1]
        self.remember_invoice(incoming)
        return incoming, 'unknown', unknown

    def record_disposition(self, source, status, evidence, relation='', counterpart=None, extra=None):
        item = dict(source_ref='context:' + source['id'], status=status, evidence=evidence)
        if relation:
            item['relation'] = relation
        if counterpart is not None:
            item['duplicate_of'] = counterpart.source_ref
            item['compared'] = dict(
                invoice_number=counterpart.invoice_number,
                printed_total=counterpart.printed_total,
                fingerprint_count=len(counterpart.detail_fingerprints),
            )
        if extra:
            item.update(extra)
        self.dispositions.append(item)
        return item


def _invoice_from_result(source, content, result: AdapterResult, fingerprints, issues):
    mon = month(source['filename']) or ''
    role = document_role_of(source)
    printed = result.printed_total
    if printed is None and role == 'cover':
        printed = extract_printed_total(content)
    if printed is None and result.parsed_detail_count:
        printed = result.parsed_total
    return InvoiceDocument(
        source_ref='context:' + source['id'],
        vendor=source_vendor(source, content),
        company=company(source['filename']) or company(content),
        billing_month=mon,
        invoice_number=extract_invoice_number(content, source['filename']),
        invoice_date=None,
        printed_total=printed,
        detail_count_printed=result.expected_detail_count,
        document_role=role,
        detail_fingerprints=list(fingerprints),
        extraction_complete=result.complete,
        extraction_issues=list(issues),
    )


def ocr_adoption_signature(manager, pid, docs):
    """OCR採用署名。フラグ無効または対象run無しなら空文字。

    各 source_id ごとに source_id:run_id:review_decision:review_signature を集め、
    ソートして digest した短い文字列を返す。承認・却下・再実行で値が変わる。
    """
    from app.capability_registry import ocr_feature_enabled
    if not ocr_feature_enabled():
        return ''
    from app.ocr_invoice_adapter import latest_ocr_run
    from app.ocr_store import OcrStore
    from app.vehicle_workflow import digest
    store = OcrStore(manager.memory.path)
    entries = []
    for item in docs or []:
        source = item[0] if isinstance(item, (list, tuple)) else item
        sid = source.get('id') if isinstance(source, dict) else getattr(source, 'id', str(source))
        if not sid:
            continue
        run = latest_ocr_run(store, pid, sid)
        if not run:
            continue
        review = store.get_review(run['run_id'])
        decision = str((review or {}).get('decision') or '')
        sig = str((review or {}).get('signature') or '')
        entries.append(f"{sid}:{run['run_id']}:{decision}:{sig}")
    if not entries:
        return ''
    entries.sort()
    return digest(entries)[:16]


def current_ocr_adoption_signature(manager, pid):
    """軽量に sources のみから現在の OCR 採用署名を計算する。"""
    from app.capability_registry import ocr_feature_enabled
    if not ocr_feature_enabled():
        return ''
    from app.vehicle_workflow import sources
    snapshot = sources(manager, pid)
    return ocr_adoption_signature(manager, pid, snapshot)


def adopt_ocr_for_normalization(payload, *, run_id='', review=None, context_file_id=''):
    """passed または人間承認済みのOCR結果だけを正規化入力候補にする。

    前月請求・入金・繰越、needs_review/failed/未承認は入れない。
    出典(context_file_id・run_id・page・block_id)を保持する。
    """
    from app.ocr_invoice_adapter import (
        adapt_ocr_invoice, adoptable_charge_dicts, is_ocr_adoptable,
    )
    if not payload:
        return []
    run = {'status': payload.get('status'), 'run_id': run_id or payload.get('run_id')}
    if not is_ocr_adoptable(run, review):
        return []
    adapted = adapt_ocr_invoice(payload)
    if adapted.file_absent or adapted.force_stop_downstream:
        return []
    source = payload.get('source') if isinstance(payload.get('source'), dict) else {}
    ctx = context_file_id or source.get('context_file_id') or ''
    used_run = run_id or payload.get('run_id') or adapted.run_id
    rows = []
    for item in adoptable_charge_dicts(adapted):
        rows.append({
            **item,
            'context_file_id': ctx,
            'ocr_run_id': used_run,
            'ocr_page': item.get('page'),
            'ocr_block_id': item.get('block_id'),
        })
    return rows


def collect_ocr_adoptions(manager, pid, docs):
    """flag 有効時のみ呼ぶ。未承認・failed は返さない。"""
    from app.ocr_invoice_adapter import is_ocr_adoptable, latest_ocr_run, payload_from_ocr_bundle
    from app.ocr_store import OcrStore
    store = OcrStore(manager.memory.path)
    found = {}
    for item in docs or []:
        source = item[0] if isinstance(item, (list, tuple)) else item
        run = latest_ocr_run(store, pid, source['id'])
        if not run:
            continue
        review = store.get_review(run['run_id'])
        if not is_ocr_adoptable(run, review):
            continue
        payload = payload_from_ocr_bundle(store.get_bundle(run['run_id']))
        if not payload:
            continue
        payload['run_id'] = run['run_id']
        found[source['id']] = {'payload': payload, 'run_id': run['run_id'], 'review': review}
    return found


def apply_ocr_adoption(ex, source, content, adoption):
    """正規抽出が不十分な原本へ、採用可能なOCR結果だけを載せる。既存経路の成功結果は上書きしない。"""
    from app.ocr_invoice_adapter import (
        adapt_ocr_invoice, current_month_source_control, is_ocr_adoptable,
        ocr_state_to_parse_money, to_adapter_result, to_invoice_document,
    )
    payload = (adoption or {}).get('payload') or {}
    review = (adoption or {}).get('review')
    run_id = (adoption or {}).get('run_id') or payload.get('run_id') or ''
    if not is_ocr_adoptable({'status': payload.get('status'), 'run_id': run_id}, review):
        return None
    adapted = adapt_ocr_invoice(payload)
    adapted.run_id = adapted.run_id or run_id
    mon = month(source.get('filename') or '')
    comp = company(source.get('filename') or '') or company(content)
    locator_base = f'ocr:{run_id}'
    if adapted.file_absent:
        return None
    for issue in adapted.issues:
        if issue.get('kind') == 'read' or issue.get('code') in {'unreadable', 'blank_purchase'}:
            ex.issue(
                'read', source['id'],
                f"{locator_base}:p{issue.get('page') or ''}:{issue.get('block_id') or ''}",
                issue.get('message') or 'OCR読取不能を0円として確定しません',
                reason_code='unreadable', month=mon, category='fuel',
            )
    if adapted.force_stop_downstream:
        return None
    for line in adapted.vehicle_lines:
        amount_field = line.line_amount
        if amount_field is None:
            continue
        parsed = ocr_state_to_parse_money(amount_field)
        locator = f'{locator_base}:p{amount_field.page}:{amount_field.block_id}'
        if parsed.status in ('invalid', 'out_of_range'):
            ex.issue(
                'read', source['id'], locator,
                'OCR読取不能を0円として確定しません',
                raw_text=parsed.raw_text, reason_code=parsed.reason or 'unreadable',
                month=mon, category='fuel',
            )
            continue
        if parsed.status == 'blank' or mon not in ex.months:
            continue
        hint = (line.vehicle_number.value if line.vehicle_number else '') or ''
        ex.add(
            source, locator, mon, 'fuel', parsed, ex.match(hint, comp), comp,
            vehicle_hint=hint,
            evidence='OCR当月明細。前月請求・入金・繰越は含めない',
            ocr_run_id=run_id, ocr_page=amount_field.page, ocr_block_id=amount_field.block_id,
            context_file_id=source.get('id'),
        )
        if parsed.status == 'value':
            ex._note_fingerprint(source, mon, hint, parsed.amount)
    control = current_month_source_control(adapted, source, mon=mon or '', category='fuel')
    if control:
        ex.add_control(**control)
    invoice = to_invoice_document(
        adapted, source, content,
        fingerprints=list(ex.detail_fingerprints.get('context:' + source['id'], [])),
    )
    return {'result': to_adapter_result(adapted), 'invoice': invoice, 'adapted': adapted}


def extract(docs, months, zero=False, ocr_adoptions=None):
    ex = Extractor(months, zero)
    if ocr_adoptions:
        from app.ocr_usami_adapter import usami_adoptions_from_ocr
        ex.usami_ocr_adoptions = usami_adoptions_from_ocr(ocr_adoptions)
    ex.read_books(docs)
    fuel_docs = [
        item for item in docs
        if Path(item[0]['filename']).suffix.lower() not in ('.xlsx', '.xlsm')
    ]
    fuel_docs.sort(key=lambda d: (
        0 if document_role_of(d[0]) == 'cover' else 1,
        0 if d[0]['filename'].lower().endswith('.csv') else 1,
        d[0]['filename'],
    ))
    for source, raw, content in fuel_docs:
        original = (content or '').split('[未検証の文字コード復元候補:', 1)[0]
        result = ex.fuel(source, original)
        recovered = False
        if not result.matched and not result.records_added and document_role_of(source) != 'cover':
            repaired = repair_encoding(original)
            before = len(ex.records)
            if repaired != original:
                result = ex.fuel(source, repaired)
                recovered = result.matched or result.records_added > 0 or result.printed_total is not None
                ex.recoveries.append(dict(
                    source_ref='context:' + source['id'], problem='文字符号化と読取精度の矛盾',
                    principles=['分離', '事前処置'],
                    candidates=[dict(adapter='original_text', passed=False), dict(adapter='reversible_cp932', passed=bool(recovered))],
                    checks=dict(reversible=True, parsed_records=len(ex.records) - before),
                    status='encoding_repaired' if recovered else 'development_required',
                ))
                original = repaired
            if not result.matched and not result.records_added and result.printed_total is None:
                adopted = (ocr_adoptions or {}).get(source['id'])
                applied = apply_ocr_adoption(ex, source, original, adopted) if adopted else None
                if applied:
                    result = applied['result']
                    fingerprints = list(ex.detail_fingerprints.get('context:' + source['id'], []))
                    invoice = applied['invoice']
                    kept, status, counterpart = ex.apply_invoice_relation(invoice)
                    evidence = 'OCR当月請求のみ採用。前月請求・入金・繰越は当月費用に入れない'
                    ex.record_disposition(source, 'included', evidence)
                    continue
                ex.issue('adapter', source['id'], '', '月次金額の抽出が未完了です。原本は保存済みです',
                         development=dict(module='vehicle_auto', acceptance='原本合計と車両別金額が一致する抽出器', owner='開発工程'))
                continue
        fingerprints = list(ex.detail_fingerprints.get('context:' + source['id'], []))
        invoice = _invoice_from_result(source, original, result, fingerprints, result.issues)
        kept, status, counterpart = ex.apply_invoice_relation(invoice)
        if status == 'excluded':
            ex.drop_source_records(invoice.source_ref)
            ex.record_disposition(
                source, 'excluded',
                '同一請求の完全重複。後着は除外し、先行原本を照合参照として保持',
                relation='exact_duplicate', counterpart=counterpart,
                extra=dict(compared_fingerprints=len(invoice.detail_fingerprints)),
            )
            continue
        if status == 'control':
            ex.drop_source_records(invoice.source_ref)
            evidence = '同一請求の相補文書。明細は計上せず請求総額の control として保持'
            ex.record_disposition(source, 'included', evidence, relation='same_invoice_complement', counterpart=counterpart)
            continue
        if status == 'partial':
            missing = sorted(set(counterpart.detail_fingerprints) - set(invoice.detail_fingerprints))
            extra_fps = sorted(set(invoice.detail_fingerprints) - set(counterpart.detail_fingerprints))
            diff = None
            if invoice.printed_total is not None and counterpart.printed_total is not None:
                diff = invoice.printed_total - counterpart.printed_total
            if len(invoice.detail_fingerprints) < len(counterpart.detail_fingerprints):
                ex.drop_source_records(invoice.source_ref)
            elif len(counterpart.detail_fingerprints) < len(invoice.detail_fingerprints):
                ex.drop_source_records(counterpart.source_ref)
            ex.issue(
                'overlap', source['id'], '',
                '同一業者・会社・月の請求が部分一致しています。自動除外せず確定を阻害します',
                month=invoice.billing_month, category='fuel', relation='partial_overlap',
                duplicate_of=counterpart.source_ref, difference=diff,
                missing_fingerprints=missing, extra_fingerprints=extra_fps,
            )
            ex.record_disposition(
                source, 'included',
                '部分重複。多い方を正とし欠け側は照合待ち。自動除外しない',
                relation='partial_overlap', counterpart=counterpart,
                extra=dict(difference=diff, missing_fingerprints=missing, extra_fingerprints=extra_fps),
            )
            continue
        if status == 'unknown':
            ex.issue(
                'overlap', source['id'], '',
                '請求関係を比較できません。二重加算の恐れを隠さず確定不可とします',
                month=invoice.billing_month, category='fuel', relation='unknown',
                duplicate_of=counterpart.source_ref if counterpart else None,
            )
            ex.record_disposition(
                source, 'included',
                '比較不能。除外せず二重の恐れを保持',
                relation='unknown', counterpart=counterpart,
            )
            continue
        evidence = '原本を自動解析。未対応箇所は確認事項に保持'
        relation = ''
        if counterpart and status == 'included':
            if classify_invoice_relation(invoice, counterpart) == 'different_invoice':
                relation = 'different_invoice'
                evidence = '同一業者・会社・月でも別請求として両方保持'
            elif classify_invoice_relation(invoice, counterpart) == 'same_invoice_complement':
                relation = 'same_invoice_complement'
                evidence = '同一請求の相補文書。明細精度の高い方を計上'
        ex.record_disposition(source, 'included', evidence, relation=relation, counterpart=counterpart if relation else None)
        if not result.complete and document_role_of(source) != 'cover' and not result.records_added:
            ex.issue('adapter', source['id'], '', '月次金額の抽出が未完了です。原本は保存済みです',
                     development=dict(module='vehicle_auto', acceptance='原本合計と車両別金額が一致する抽出器', owner='開発工程'))
    return ex.finish(docs)


def drop_assumed_zero_when_actual(records, decision_log=None, allocated_only=True):
    """同一車両×月×費目に actual が付いたら assumed_zero を削除する。未配賦 actual だけでは全車両 zero を消さない。"""
    actual_keys = set()
    for record in records:
        if record.get('quality') != 'actual':
            continue
        allocations = record.get('allocations') or []
        if allocated_only and not allocations:
            continue
        for item in allocations:
            vid = item.get('vehicle_id')
            if vid:
                actual_keys.add((vid, record.get('month'), record.get('category')))
    removed = []
    kept = []
    for record in records:
        if record.get('quality') != 'assumed_zero':
            kept.append(record)
            continue
        hit = False
        for item in record.get('allocations') or []:
            key = (item.get('vehicle_id'), record.get('month'), record.get('category'))
            if key in actual_keys:
                hit = True
                break
        if hit:
            removed.append(record)
        else:
            kept.append(record)
    records[:] = kept
    if decision_log is not None and removed:
        decision_log.append(dict(
            action='drop_assumed_zero',
            keys=[dict(vehicle_id=(r.get('allocations') or [{}])[0].get('vehicle_id'), month=r.get('month'), category=r.get('category'), id=r.get('id')) for r in removed],
            reason='actual_present',
        ))
    return removed


def authorized_zero(mission):
    s='\n'.join([str(mission.get(k,'')) for k in ('goal','success_criteria','constraints_text')]+[x.get('message','') for x in mission.get('instruction_messages',[]) if x.get('kind')=='mission_instruction_user'])
    return bool(re.search(r'不足[^。\n]{0,40}(?:0|０|ゼロ)円',s))


def apply_adopted_function(function_id, payload, params=None):
    """採用済みアダプターの明示分岐。動的 exec / 任意コード実行はしない。"""
    params = params or {}
    if function_id in ('identity_passthrough', 'vehicle_extract_pipeline'):
        return payload
    if function_id == 'trial_amount_map':
        data = dict(payload or {})
        mapping = params.get('amounts') or {}
        records = [dict(r) for r in data.get('records') or []]
        for record in records:
            allocations = record.get('allocations') or [{}]
            key = '|'.join([
                str(allocations[0].get('vehicle_id') or record.get('vehicle_id') or ''),
                str(record.get('month') or ''),
                str(record.get('category') or ''),
            ])
            if key in mapping:
                record['amount'] = mapping[key]
        data['records'] = records
        return data
    raise ValueError('未登録のアダプター関数IDです（動的execは禁止）: ' + str(function_id))


def apply_adopted_adapters(pid, data, source_ref='', vendor='', format_signature=''):
    """人間採用済みかつ適用範囲が一致するアダプターだけを明示分岐で適用する。採用前は何もしない。"""
    from app.automatic_triz import adapters_for_prepare
    result = data
    for adapter in adapters_for_prepare(pid, source_ref=source_ref, vendor=vendor, format_signature=format_signature):
        function_id = adapter.get('function_id') or adapter.get('adapter_id')
        result = apply_adopted_function(function_id, result, adapter.get('params') or {})
    return result


def prepare(manager,pid,mission):
    from app.vehicle_workflow import sources, digest, resolve, write_json, requested_months, INPUT_PATH, requirements_hash
    from app.context_files import extract_context_file
    snapshot=sources(manager,pid); docs=[]
    for source in snapshot:
        item=manager.memory.get_context_file(pid,source['id']);raw=item.get('original_data');content=item.get('content','')
        if raw and Path(source['filename']).suffix.lower() not in ('.xlsx','.xlsm'):
            try:content=extract_context_file(source['filename'],bytes(raw)).content
            except Exception:pass
        docs.append((source,bytes(raw) if raw else None,content))
    months=requested_months(mission);zero=authorized_zero(mission)
    from app.capability_registry import ocr_feature_enabled
    from app.executable_recipes import apply_during_prepare
    ocr_adoptions = collect_ocr_adoptions(manager, pid, docs) if ocr_feature_enabled() else None
    ocr_adoption_hash = current_ocr_adoption_signature(manager, pid)
    data,recipe_fields=apply_during_prepare(
        manager,pid,docs,months,zero,snapshot,REVISION,'zero' if zero else 'unknown',
        ocr_adoptions=ocr_adoptions,
    )
    data['source_snapshot']=snapshot
    data=apply_adopted_adapters(pid, data)
    envelope=dict(data=data,confirmed=False,mode=VERSION,extractor_revision=REVISION,
                  source_hash=digest(snapshot),requirements_hash=requirements_hash(mission),
                  ocr_adoption_hash=ocr_adoption_hash,
                  assignments=[],decisions=[],decision_log=list(data.get('decision_log') or []))
    envelope.update(recipe_fields)
    target=resolve(manager,pid,INPUT_PATH)
    old=None
    if target.exists():
        from app.vehicle_workflow import read_json
        old=read_json(target);write_json(target.parent/'history'/(digest(old)+'.json'),old)
    from app.vehicle_service import reuse_decisions
    reuse_decisions(envelope, old, snapshot)
    write_json(target,envelope)
    write_json(target.parent/'automatic_proof.json',dict(
        input_hash=digest(envelope),source_hash=digest(snapshot),requirements_hash=requirements_hash(mission),
        recipe_id=envelope.get('recipe_id') or '',recipe_version=envelope.get('recipe_version'),
        recipe_applied=bool(envelope.get('recipe_applied')),recipe_rejected=bool(envelope.get('recipe_rejected')),
        recipe_rule_results=envelope.get('recipe_rule_results') or {},
        recipe_reuse_demonstration=bool(envelope.get('recipe_reuse_demonstration')),
        recipe_learning_success=bool(envelope.get('recipe_learning_success')),
    ))
    return envelope


def evidence_issues(data,snapshot):
    issues=[x['message']+' ('+x['source_ref']+' '+x['locator']+')' for x in data['auto_extraction']['issues'] if x['status']=='unresolved']
    known={'context:'+s['id'] for s in snapshot}
    for r in data['records']:
        if r['quality']=='assumed_zero':
            if data.get('missing_policy')!='zero' or r['amount']!=0:issues.append('0円補完方針が不正です')
        elif r['source_ref'] not in known or not r.get('source_locator'):issues.append('明細の原本参照が不正です')
    return list(dict.fromkeys(issues))
