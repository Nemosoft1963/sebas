"""単位2A: reconcile_sources と classify_invoice_relation の純粋関数試験。"""
from app.vehicle_auto import AdapterResult, InvoiceDocument, classify_invoice_relation
from app.vehicle_reconciliation import reconcile_sources


def rec(**overrides):
    item = dict(
        id='r1',
        source_ref='context:s',
        company='関東',
        month='2026-01',
        category='fuel',
        tax_basis='inclusive',
        amount=0,
        source_locator='CSV行2',
        quality='actual',
        allocations=[{'vehicle_id': '足立101か1234', 'ratio': 1}],
    )
    item.update(overrides)
    return item


def ctrl(**overrides):
    item = dict(
        source_ref='context:s',
        company='関東',
        month='2026-01',
        category='fuel',
        tax_basis='inclusive',
        invoice_id=None,
        control_kind='invoice_total',
        origin='source_total',
        amount=1000,
        source_locator='請求書(鑑)!B20',
        extraction_method='test',
        status='available',
    )
    item.update(overrides)
    return item


def invoice(**overrides):
    item = dict(
        source_ref='context:a',
        vendor='usami',
        company='関東',
        billing_month='2026-01',
        invoice_number='INV-1',
        invoice_date='2026-01-31',
        printed_total=1000,
        detail_count_printed=2,
        document_role='detail',
        detail_fingerprints=['2026-01-02|1234|軽油|10|100', '2026-01-03|1234|軽油|20|200'],
        extraction_complete=True,
        extraction_issues=[],
    )
    item.update(overrides)
    return InvoiceDocument(**item)


def _row(rows, **parts):
    matches = [
        r for r in rows
        if all(r.get(k) == v for k, v in parts.items())
    ]
    assert matches, (parts, rows)
    return matches[0]


def test_source_total_matches_line_sum():
    rows = reconcile_sources(
        [rec(amount=600, source_locator='CSV行2'), rec(id='r2', amount=400, source_locator='CSV行3')],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, source_ref='context:s', category='fuel')
    assert row['source_total'] == 1000
    assert row['included_total'] == 1000
    assert row['excluded_total'] == 0
    assert row['difference'] == 0
    assert row['status'] == 'matched'
    assert row['unknown_components'] == 0


def test_missing_line_is_mismatched_with_difference():
    rows = reconcile_sources(
        [rec(amount=600, source_locator='CSV行2')],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, category='fuel')
    assert row['included_total'] == 600
    assert row['difference'] == 400
    assert row['status'] == 'mismatched'


def test_duplicate_line_is_detected():
    rows = reconcile_sources(
        [
            rec(amount=600, source_locator='CSV行2'),
            rec(id='r2', amount=400, source_locator='CSV行3'),
            rec(id='r3', amount=400, source_locator='CSV行3'),
        ],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, category='fuel')
    assert row['included_total'] == 1400
    assert row['difference'] == -400
    assert row['duplicate_identities'] >= 1
    assert row['status'] == 'mismatched'


def test_grounded_exclusion_matches_and_ungrounded_exclusion_fails():
    grounded = dict(
        source_ref='context:s',
        company='関東',
        month='2026-01',
        category='fuel',
        tax_basis='inclusive',
        amount=100,
        source_locator='CSV行9',
        reason='対象外車両',
        approver='human',
    )
    matched = reconcile_sources(
        [rec(amount=900, source_locator='CSV行2')],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [grounded],
        [],
    )
    row = _row(matched, category='fuel')
    assert row['included_total'] == 900
    assert row['excluded_total'] == 100
    assert row['difference'] == 0
    assert row['status'] == 'matched'

    ungrounded = dict(
        source_ref='context:s',
        company='関東',
        month='2026-01',
        category='fuel',
        tax_basis='inclusive',
        amount=100,
    )
    failed = reconcile_sources(
        [rec(amount=900, source_locator='CSV行2')],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [ungrounded],
        [],
    )
    bad = _row(failed, category='fuel')
    assert bad['excluded_total'] == 0
    assert bad['status'] != 'matched'
    assert bad['ungrounded_exclusions'] == 1


def test_unavailable_control_is_not_a_pass():
    rows = reconcile_sources(
        [rec(amount=1000)],
        [ctrl(status='unavailable', origin='none', amount=None, reason_code='no_independent_total',
              reason='明細のみで請求総額欄がない')],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, category='fuel')
    assert row['status'] == 'unavailable'
    assert row['source_total'] is None
    assert row['status'] != 'matched'

    empty = reconcile_sources(
        [rec(amount=1000)],
        [],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    assert _row(empty, category='fuel')['status'] == 'unavailable'


def test_sum_of_lines_origin_is_not_a_pass():
    rows = reconcile_sources(
        [rec(amount=600), rec(id='r2', amount=400, source_locator='CSV行3')],
        [ctrl(origin='sum_of_lines', amount=1000, control_kind='category_total')],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, category='fuel')
    assert row['origin'] == 'sum_of_lines'
    assert row['source_total'] is None
    assert row['status'] != 'matched'
    assert row['status'] == 'unavailable'


def test_read_issue_makes_key_incomplete_even_if_difference_is_zero():
    rows = reconcile_sources(
        [rec(amount=600), rec(id='r2', amount=400, source_locator='CSV行3')],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [dict(
            kind='read',
            source_ref='context:s',
            company='関東',
            month='2026-01',
            category='fuel',
            tax_basis='inclusive',
            raw_text='要確認',
            reason_code='non_numeric',
            status='unresolved',
        )],
    )
    row = _row(rows, category='fuel')
    assert row['difference'] == 0
    assert row['unknown_components'] >= 1
    assert row['status'] == 'incomplete'
    assert row['status'] != 'matched'


def test_unallocated_amount_is_included_not_treated_as_missing():
    rows = reconcile_sources(
        [
            rec(amount=600, allocations=[{'vehicle_id': '足立101か1234', 'ratio': 1}]),
            rec(id='r2', amount=400, source_locator='CSV行3', allocations=[]),
        ],
        [ctrl(amount=1000)],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    row = _row(rows, category='fuel')
    assert row['included_total'] == 1000
    assert row['unallocated_total'] == 400
    assert row['difference'] == 0
    assert row['status'] == 'matched'


def test_distinct_source_ref_or_invoice_id_are_not_mixed():
    rows = reconcile_sources(
        [
            rec(amount=500, source_ref='context:a', source_locator='A1'),
            rec(id='r2', amount=800, source_ref='context:b', source_locator='B1'),
        ],
        [
            ctrl(amount=500, source_ref='context:a', source_locator='合計A'),
            ctrl(amount=800, source_ref='context:b', source_locator='合計B'),
        ],
        [
            dict(source_ref='context:a', status='included', evidence='a'),
            dict(source_ref='context:b', status='included', evidence='b'),
        ],
        [],
        [],
    )
    assert _row(rows, source_ref='context:a')['status'] == 'matched'
    assert _row(rows, source_ref='context:b')['status'] == 'matched'
    assert len(rows) == 2

    invoiced = reconcile_sources(
        [
            rec(amount=100, invoice_id='INV-1', source_locator='C1'),
            rec(id='r2', amount=200, invoice_id='INV-2', source_locator='C2'),
        ],
        [
            ctrl(amount=100, invoice_id='INV-1'),
            ctrl(amount=200, invoice_id='INV-2'),
        ],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    assert _row(invoiced, invoice_id='INV-1')['status'] == 'matched'
    assert _row(invoiced, invoice_id='INV-2')['status'] == 'matched'
    mixed = reconcile_sources(
        [
            rec(amount=100, invoice_id='INV-1', source_locator='C1'),
            rec(id='r2', amount=200, invoice_id='INV-2', source_locator='C2'),
        ],
        [ctrl(amount=300, invoice_id='INV-1')],
        [dict(source_ref='context:s', status='included', evidence='明細')],
        [],
        [],
    )
    assert _row(mixed, invoice_id='INV-1')['included_total'] == 100
    assert _row(mixed, invoice_id='INV-2')['status'] == 'unavailable'


def test_classify_invoice_relation_exact_duplicate():
    a = invoice()
    b = invoice(source_ref='context:b')
    assert classify_invoice_relation(a, b) == 'exact_duplicate'


def test_classify_invoice_relation_same_invoice_complement():
    a = invoice(document_role='detail')
    b = invoice(
        source_ref='context:cover',
        document_role='cover',
        detail_fingerprints=[],
        detail_count_printed=None,
    )
    assert classify_invoice_relation(a, b) == 'same_invoice_complement'


def test_classify_invoice_relation_different_invoice():
    a = invoice(invoice_number='INV-1', printed_total=1000)
    b = invoice(
        source_ref='context:b',
        invoice_number='INV-2',
        printed_total=2500,
        detail_fingerprints=['2026-01-10|5678|軽油|5|2500'],
    )
    assert classify_invoice_relation(a, b) == 'different_invoice'


def test_classify_invoice_relation_partial_overlap():
    a = invoice(
        invoice_number=None,
        printed_total=1000,
        detail_fingerprints=['line-a', 'line-shared'],
    )
    b = invoice(
        source_ref='context:b',
        invoice_number=None,
        printed_total=1500,
        detail_fingerprints=['line-shared', 'line-b'],
    )
    assert classify_invoice_relation(a, b) == 'partial_overlap'


def test_classify_invoice_relation_unknown():
    a = invoice(
        invoice_number=None,
        printed_total=None,
        detail_fingerprints=[],
        document_role='unknown',
        extraction_complete=False,
    )
    b = invoice(
        source_ref='context:b',
        invoice_number=None,
        printed_total=None,
        detail_fingerprints=[],
        document_role='unknown',
        extraction_complete=False,
    )
    assert classify_invoice_relation(a, b) == 'unknown'


def test_adapter_result_one_record_is_not_complete():
    result = AdapterResult(
        matched=True,
        records_added=1,
        parsed_total=400,
        printed_total=1000,
        expected_detail_count=3,
        parsed_detail_count=1,
        complete=False,
        issues=[{'kind': 'partial', 'message': '印字総額と一致しない'}],
    )
    assert result.matched and result.records_added == 1
    assert result.complete is False
