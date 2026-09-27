import io,json
import pytest
from openpyxl import Workbook
from app.vehicle_auto import AdapterResult,Extractor,extract,repair_encoding,month,parse_money,evidence_issues,REVISION
from app.vehicle_profit import calculate
from app.vehicle_workflow import validate_evidence


def book(rows):
    b=Workbook();s=b.active
    for r in rows:s.append(r)
    stream=io.BytesIO();b.save(stream);return stream.getvalue()


def ledger(rows=None):
    return ({'id':'s','filename':'関東月次.xlsx','quality':'readable'},book(rows or [
        ['車両番号','年月','売上','総支給額','会社負担社会保険','燃料費','税込リース'],
        ['足立101か1234','2026-01',1000,200,30,100,50],
        ['足立101か1234','2026-02',2000,300,40,200,60]]),'')


def test_automatic_ledger_no_manual_json_and_zero_policy():
    # 真正欠損（空の費目）のみ仮定ゼロ。読取失敗・未配賦の解消には使わない。
    data=extract([ledger()],['2026-01','2026-02'],True)
    result=calculate(data)
    assert [r['profit'] for r in result['rows']]==[620,1400]
    assert not data['auto_extraction']['issues']
    assert sum(r['quality']=='assumed_zero' for r in data['records'])==4
    assert all(r.get('source_locator') for r in data['records'])


def test_no_silent_zero_without_authorization():
    data=extract([ledger()],['2026-01','2026-02'],False)
    assert all(r['profit'] is None for r in calculate(data)['rows'])


def test_unknown_vehicle_money_preserved_and_no_suffix_collision():
    docs=[ledger([['車両番号','年月','売上'],['足立101か1234','2026-01',100],['足立102か1234','2026-01',200]]),
        ({'id':'f','filename':'関東202601請求明細.csv'},None,'給油日付,車番,金額,参考消費税,軽油税\n20260115,1234,100,10,20')]
    data=extract(docs,['2026-01'],True)
    r=next(r for r in data['records'] if r['category']=='fuel' and r['quality']=='actual')
    assert r['amount']==130 and not r['allocations']
    assert any(x['kind']=='allocation' for x in data['auto_extraction']['issues'])
    assert all(r['profit'] is not None for r in calculate(data)['rows']) # provisional numeric only
    assert calculate(data)['warnings']


def test_recovery_candidate_does_not_parse_original_and_appended_copy_twice():
    original='＜ 車 番 別 集 計 ＞\n1234 1000 100\n＊ 車 番 別 小 計 ＊ 1000 100'
    garbled=original.encode('cp932').decode('latin1')
    doc=({'id':'f','filename':'関東202601ENEOS.pdf'},None,garbled+'\n[未検証の文字コード復元候補: 原本照合が必要]\n'+original)
    data=extract([ledger(),doc],['2026-01'],True)
    actual=[r for r in data['records'] if r['source_ref']=='context:f']
    assert len(actual)==1 and actual[0]['amount']==1100
    assert data['auto_extraction']['recoveries'][0]['status']=='encoding_repaired'
    assert data['auto_extraction']['recoveries'][0]['status']!='business_recovered'
    assert data['auto_extraction']['recoveries'][0]['status']!='recovered'


USAMI_LINE='00001 1234 1\n品目\n小計 100\n軽油引取税 0\n参考消費税 (0)\n自動車用潤滑油 100\n'
USAMI_OTHER='00002 5678 1\n品目\n小計 250\n軽油引取税 0\n参考消費税 (0)\n自動車用潤滑油 250\n'


def test_csv_pdf_duplicate_is_not_double_counted():
    # 同一明細集合・同一合計のCSVとPDFは完全重複。一方 excluded。燃料合計は一回分。
    csv_body='給油日付,車番,金額\n20260102,1234,100'
    data=extract([ledger(),
                  ({'id':'csv','filename':'燃料/202601/請求明細_関東.csv'},None,csv_body),
                  ({'id':'pdf','filename':'燃料/202601/御買上明細_関東.pdf'},None,USAMI_LINE)],['2026-01'],True)
    fuel=[r for r in data['records'] if r['category']=='fuel' and r['quality']=='actual' and r['source_ref']!='context:s']
    assert sum(r['amount'] for r in fuel)==100
    assert len(fuel)==1
    disp={d['source_ref']:d for d in data['source_dispositions']}
    assert disp['context:pdf']['status']=='excluded'
    assert disp['context:pdf'].get('relation')=='exact_duplicate'
    assert disp['context:pdf'].get('duplicate_of')=='context:csv'
    assert disp['context:csv']['status']=='included'
    assert any(c.get('origin')=='source_total' and c.get('source_ref')=='context:pdf' for c in data['source_controls'])


def test_empty_input_not_a_success():
    with pytest.raises(ValueError,match='対象車両'):extract([],['2026-01'],True)


def test_months_accept_japanese_and_filename():
    assert month('令和 8年 7月')=='2026-07'
    assert month('燃料/202601/請求書')=='2026-01'
    assert month('2026-07-01 00:00:00')=='2026-07'


@pytest.mark.asyncio
async def test_automatic_prepare_and_decision_invalidates_changed_source(tmp_path):
    from test_vehicle_workflow import setup
    from app.vehicle_auto import prepare
    from app.vehicle_service import state,decide
    manager,pid=setup(tmp_path)
    src,raw,_=ledger()
    manager.memory.add_context_file(pid,src['filename'],'月次原本',len(raw),source='upload',data=raw)
    mission=await manager.generate_plan(pid)
    data=prepare(manager,pid,mission)
    status=state(manager,pid)
    assert status['prepared'] and status['vehicles']==1
    assert not status['stale']
    manager.memory.add_context_file(pid,'extra.txt','追加',6,source='upload')
    assert state(manager,pid)['stale']
    with pytest.raises(ValueError,match='原本'):
        decide(manager,pid,mission['plan_version'],status['input_hash'],'unknown','',False,'確認済み')

def test_signed_csv_amount_and_blank_registration_fallback():
    data=extract([ledger(),({'id':'fuel','filename':'関東202601Wing.csv'},None,
        '給油日付,車両番号,実車番・届先,合計金額,軽油税\n20260110, ,1234,+0000120,10')],['2026-01'],True)
    r=next(r for r in data['records'] if r['source_ref']=='context:fuel')
    assert r['amount']==120 and r['allocations'][0]['vehicle_id']=='足立101か1234'


def test_formula_without_cached_value_is_not_silently_treated_as_absent():
    data=extract([ledger([['車両番号','年月','売上'],['足立101か1234','2026-01','=10+20']])],['2026-01'],True)
    assert any(i['kind']=='read' and '数式セル' in i['message'] for i in data['auto_extraction']['issues'])

def test_registration_variants_preserve_full_target_set_and_old_number_note():
    from app.vehicle_auto import plate
    assert plate('足立430あ73-57')=='足立430あ7357'
    assert plate('足立481れ5492.（7474）')=='足立481れ5492'
    d=extract([ledger([['車両番号','年月','売上'],['足立430あ73-57','2026-01',100],['足立481れ5492.（7474）','2026-01',200]])],['2026-01'],True)
    assert len(d['vehicles'])==2
    assert [r['profit'] for r in calculate(d)['rows']]==[100,200]
    assert '7474' in d['vehicles'][1]['label']


def test_numeric_zero_is_actual_amount_not_assumed_zero():
    data=extract([ledger([['車両番号','年月','売上','総支給額','会社負担社会保険','燃料費','税込リース'],
                         ['足立101か1234','2026-01',0,0,0,0,0]])],['2026-01'],True)
    actual=[r for r in data['records'] if r['category']=='revenue']
    assert len(actual)==1
    assert actual[0]['quality']=='actual' and actual[0]['amount']==0 and actual[0]['source_locator']
    assert all(r['quality']!='assumed_zero' or r['category'] not in ('revenue','payroll','insurance','fuel','lease') for r in data['records'])
    assert not any(i['kind']=='read' and actual[0]['source_locator'] in i.get('locator','') for i in data['auto_extraction']['issues'])


def test_string_zero_is_actual_amount():
    data=extract([ledger([['車両番号','年月','売上'],['足立101か1234','2026-01','0']])],['2026-01'],True)
    actual=[r for r in data['records'] if r['category']=='revenue']
    assert len(actual)==1 and actual[0]['quality']=='actual' and actual[0]['amount']==0 and actual[0]['source_locator']


def test_blank_category_uses_assumed_zero_only_when_authorized():
    blank=ledger([['車両番号','年月','売上','総支給額','会社負担社会保険','燃料費','税込リース'],
                  ['足立101か1234','2026-01',None,200,30,100,50]])
    unauthorized=extract([blank],['2026-01'],False)
    assert all(r['profit'] is None for r in calculate(unauthorized)['rows'])
    assert not any(r['category']=='revenue' for r in unauthorized['records'])
    authorized=extract([blank],['2026-01'],True)
    zeros=[r for r in authorized['records'] if r['category']=='revenue']
    assert len(zeros)==1 and zeros[0]['quality']=='assumed_zero' and zeros[0]['amount']==0


def test_non_numeric_confirmation_is_read_issue_not_zero():
    data=extract([ledger([['車両番号','年月','売上','燃料費'],['足立101か1234','2026-01',1000,'要確認']])],['2026-01'],True)
    reads=[i for i in data['auto_extraction']['issues'] if i['kind']=='read']
    assert reads
    assert any(i.get('locator') and ('要確認' in str(i.get('raw_text','')) or i.get('reason_code')=='non_numeric') for i in reads)
    assert not any(r['category']=='fuel' and r['amount']==0 and r['quality'] in ('actual','assumed_zero') for r in data['records'])
    snapshot=[{'id':'s','quality':'readable'}]
    assert evidence_issues(data,snapshot)
    assert validate_evidence(data,snapshot,True)


def test_formula_without_cached_value_is_not_assumed_zero():
    data=extract([ledger([['車両番号','年月','売上'],['足立101か1234','2026-01','=10+20']])],['2026-01'],True)
    assert any(i['kind']=='read' and '数式セル' in i['message'] for i in data['auto_extraction']['issues'])
    assert not any(r['category']=='revenue' and r['quality']=='assumed_zero' for r in data['records'])


def test_unreadable_fuel_does_not_complete_under_zero_policy():
    data=extract([ledger([['車両番号','年月','売上','燃料費'],['足立101か1234','2026-01',1000,'要確認']])],['2026-01'],True)
    assert not any(r['category']=='fuel' and r['quality'] in ('actual','assumed_zero') and r['amount']==0 for r in data['records'])
    snapshot=[{'id':'s','quality':'readable'}]
    issues=validate_evidence(data,snapshot,True)
    assert issues
    assert evidence_issues(data,snapshot)


def test_boolean_and_out_of_range_are_not_recorded_as_amounts():
    assert parse_money(True).status=='invalid' and parse_money(True).reason=='boolean'
    assert parse_money(False).status=='invalid'
    huge=parse_money(10**13)
    assert huge.status=='out_of_range'
    data=extract([ledger([['車両番号','年月','売上'],['足立101か1234','2026-01',True]])],['2026-01'],True)
    assert not any(r['category']=='revenue' and r['quality']=='actual' for r in data['records'])
    assert any(i['kind']=='read' and i.get('reason_code')=='boolean' for i in data['auto_extraction']['issues'])
    data2=extract([ledger([['車両番号','年月','売上'],['足立101か1234','2026-01',10**13]])],['2026-01'],True)
    assert not any(r['category']=='revenue' and r['quality']=='actual' for r in data2['records'])
    assert any(i['kind']=='read' and i.get('reason_code')=='out_of_range' for i in data2['auto_extraction']['issues'])
    assert REVISION=='20260927.2'


def test_same_month_different_invoices_are_both_kept():
    data=extract([ledger(),
                  ({'id':'a','filename':'燃料/202601/御買上明細_関東.pdf'},None,USAMI_LINE),
                  ({'id':'b','filename':'燃料/202601/請求明細_関東.csv'},None,'給油日付,車番,金額\n20260115,5678,250')],['2026-01'],True)
    fuel=[r for r in data['records'] if r['category']=='fuel' and r['quality']=='actual' and r['source_ref']!='context:s']
    assert sorted(r['amount'] for r in fuel)==[100,250]
    disp={d['source_ref']:d for d in data['source_dispositions']}
    assert disp['context:a']['status']=='included' and disp['context:b']['status']=='included'
    assert 'different_invoice' in {disp['context:a'].get('relation'), disp['context:b'].get('relation')}
    assert not any(d.get('status')=='excluded' and d['source_ref'] in ('context:a','context:b') for d in data['source_dispositions'])


def test_partial_overlap_is_not_auto_excluded():
    csv_body='給油日付,車番,金額\n20260102,1234,100'
    pdf=USAMI_LINE+USAMI_OTHER
    data=extract([ledger(),
                  ({'id':'csv','filename':'燃料/202601/請求明細_関東.csv'},None,csv_body),
                  ({'id':'pdf','filename':'燃料/202601/御買上明細_関東.pdf'},None,pdf)],['2026-01'],True)
    disp={d['source_ref']:d for d in data['source_dispositions']}
    assert disp['context:csv']['status']=='included'
    assert disp['context:pdf']['status']=='included'
    assert disp['context:pdf'].get('relation')=='partial_overlap'
    assert disp['context:pdf'].get('duplicate_of')=='context:csv'
    overlaps=[i for i in data['auto_extraction']['issues'] if i.get('relation')=='partial_overlap' or i.get('kind')=='overlap']
    assert overlaps
    fuel=[r for r in data['records'] if r['category']=='fuel' and r['quality']=='actual' and r['source_ref']!='context:s']
    assert {r['amount'] for r in fuel}=={100,250}


def test_cover_invoice_is_source_total_control_not_unconditional_excluded():
    data=extract([ledger(),
                  ({'id':'cover','filename':'燃料/202601/請求書(鑑)_関東.pdf'},None,'ご請求金額 1000円\n明細はありません')],['2026-01'],True)
    disp=next(d for d in data['source_dispositions'] if d['source_ref']=='context:cover')
    assert disp['status']!='excluded'
    assert not any(r['source_ref']=='context:cover' and r['category']=='fuel' and r['quality']=='actual' for r in data['records'])
    controls=[c for c in data['source_controls'] if c['source_ref']=='context:cover']
    assert controls
    assert all(c.get('origin')=='source_total' and c.get('status')=='available' and c.get('amount')==1000 for c in controls)
    assert all(c.get('category')=='fuel' for c in controls)


def test_usami_partial_block_is_not_complete():
    text='00001 1234 1\n小計 100\n軽油引取税 0\n参考消費税 (0)\n自動車用潤滑油 100\n00002 5678 1\n品目のみ\n'
    ex=Extractor(['2026-01'])
    result=ex.fuel({'id':'u','filename':'燃料/202601/宇佐美_関東.pdf'},text)
    assert isinstance(result,AdapterResult)
    assert result.matched
    assert result.records_added==1
    assert result.expected_detail_count==2
    assert result.parsed_detail_count==1
    assert result.complete is False
    assert any(i.get('reason_code')=='partial_block' or '欠落' in i.get('message','') for i in result.issues)
    assert any(i.get('kind')=='read' for i in ex.issues)


def test_taiyo_and_plate_subtotal_recorded_as_control():
    taiyo='1000 カード車番: 1234 車両合計金額\n小計 1000\n'
    ex=Extractor(['2026-01'])
    result=ex.fuel({'id':'t','filename':'燃料/202601/太陽_関東.pdf'},taiyo)
    assert result.records_added==1 and result.parsed_total==1000
    assert any(c.get('origin')=='source_total' and c.get('control_kind')=='sheet_subtotal' and c.get('amount')==1000 for c in ex.source_controls)
    plate='＜車番別集計＞\n1234 1000 100\n＊車番別小計＊ 900 50'
    ex2=Extractor(['2026-01'])
    result2=ex2.fuel({'id':'p','filename':'燃料/202601/ENEOS_関東.pdf'},plate)
    assert result2.records_added==1 and result2.parsed_total==1100
    assert any(c.get('origin')=='source_total' and c.get('amount')==950 for c in ex2.source_controls)
    assert any('小計行' in i.get('message','') for i in result2.issues)
    assert result2.complete is False


def test_excel_total_row_becomes_source_total_control():
    data=extract([ledger([['車両番号','年月','売上','燃料費'],
                          ['足立101か1234','2026-01',1000,100],
                          ['合計','2026-01',1000,100]])],['2026-01'],True)
    controls=[c for c in data['source_controls'] if c.get('origin')=='source_total']
    assert controls
    assert any(c.get('category')=='revenue' and c.get('amount')==1000 for c in controls)
    assert any(c.get('category')=='fuel' and c.get('amount')==100 for c in controls)
    assert not any(v['id']=='合計' for v in data['vehicles'])


def test_csv_line_sum_is_not_independent_source_total():
    data=extract([ledger(),({'id':'fuel','filename':'関東202601請求明細.csv'},None,
                            '給油日付,車番,金額\n20260102,1234,100\n20260103,1234,40')],['2026-01'],True)
    csv_controls=[c for c in data['source_controls'] if c['source_ref']=='context:fuel']
    assert csv_controls
    assert all(c.get('origin')=='sum_of_lines' for c in csv_controls)
    assert not any(c.get('origin')=='source_total' for c in csv_controls)
    assert sum(c.get('amount') or 0 for c in csv_controls)==140


def test_cover_and_csv_complement_keeps_csv_lines_and_cover_control():
    data=extract([ledger(),
                  ({'id':'cover','filename':'燃料/202601/請求書(鑑)_関東.pdf'},None,'ご請求金額 100円'),
                  ({'id':'csv','filename':'燃料/202601/請求明細_関東.csv'},None,'給油日付,車番,金額\n20260102,1234,100')],['2026-01'],True)
    fuel=[r for r in data['records'] if r['source_ref']=='context:csv' and r['category']=='fuel']
    assert len(fuel)==1 and fuel[0]['amount']==100
    assert not any(r['source_ref']=='context:cover' and r['quality']=='actual' for r in data['records'])
    cover=next(c for c in data['source_controls'] if c['source_ref']=='context:cover')
    assert cover['origin']=='source_total' and cover['amount']==100
    disp={d['source_ref']:d for d in data['source_dispositions']}
    assert disp['context:cover']['status']=='included'
    assert disp['context:csv']['status']=='included'


USAMI_PAYMENT_PAGE = (
    "00001 0000 2\n"
    "テスト運輸（有） 御中 2026/01/31\n"
    "1 25 宇佐美本社 御入金（振込） 500,000\n"
    "入金計 500,000\n"
    "品名 無鉛ハイオク レギュラー 軽油 灯油 自動車用潤滑油 合計\n"
)


def test_usami_payment_page_excluded_not_partial_block():
    # (a) 入金ページを含む宇佐美テキストで partial_block が出ず、正常ブロックが抽出される
    text = USAMI_PAYMENT_PAGE + USAMI_LINE
    docs = [
        ledger(),
        ({'id': 'u1', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, text),
    ]
    data = extract(docs, ['2026-01'], True)
    issues = data['auto_extraction']['issues']
    assert not any(i.get('reason_code') == 'partial_block' for i in issues)

    # (b) 入金ページを除外した記録が残る (source_ref・月)
    payment_logs = [
        entry for entry in data.get('decision_log', [])
        if entry.get('action') == 'exclude_payment_page'
    ]
    assert len(payment_logs) == 1
    assert payment_logs[0]['source_ref'] == 'context:u1'
    assert payment_logs[0]['month'] == '2026-01'
    assert '入金ページ' in payment_logs[0]['reason']

    # (c) 入金額が費用に入らない (fuel の actual 合計は USAMI_LINE の 100 のみ)
    fuel = [r for r in data['records'] if r['category'] == 'fuel' and r['quality'] == 'actual' and r['source_ref'] == 'context:u1']
    assert sum(r['amount'] for r in fuel) == 100
    assert not any(r['amount'] == 500000 for r in fuel)
    assert not any(r['amount'] == 0 and r['quality'] == 'actual' for r in fuel)

    # (d) 車番0000で「御入金」が無い、または明細行があるブロックは従来どおり partial_block
    no_payment_text = (
        "00001 0000 1\n"
        "品目のみ\n"
    )
    ex_np = Extractor(['2026-01'])
    res_np = ex_np.fuel({'id': 'unp', 'filename': '燃料/202601/宇佐美_関東.pdf'}, no_payment_text)
    assert any(i.get('reason_code') == 'partial_block' for i in res_np.issues)

    with_detail_text = (
        "00001 0000 1\n"
        "1111 0000 1 15 支店名 軽油 10 100 1000\n"
        "御入金 500,000\n"
    )
    ex_wd = Extractor(['2026-01'])
    res_wd = ex_wd.fuel({'id': 'uwd', 'filename': '燃料/202601/宇佐美_関東.pdf'}, with_detail_text)
    assert any(i.get('reason_code') == 'partial_block' for i in res_wd.issues)

    # (e) 入金ページのあるファイルと無いファイルが混在しても他のファイルに影響しない
    docs_mixed = [
        ledger(),
        ({'id': 'u_jan', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, USAMI_PAYMENT_PAGE + USAMI_LINE),
        ({'id': 'u_feb', 'filename': '燃料/202602/宇佐美_関東.pdf'}, None, USAMI_OTHER),
    ]
    data_mixed = extract(docs_mixed, ['2026-01', '2026-02'], True)
    assert not any(i.get('reason_code') == 'partial_block' for i in data_mixed['auto_extraction']['issues'])
    fuel_jan = [r for r in data_mixed['records'] if r['source_ref'] == 'context:u_jan' and r['category'] == 'fuel' and r['quality'] == 'actual']
    fuel_feb = [r for r in data_mixed['records'] if r['source_ref'] == 'context:u_feb' and r['category'] == 'fuel' and r['quality'] == 'actual']
    assert sum(r['amount'] for r in fuel_jan) == 100
    assert sum(r['amount'] for r in fuel_feb) == 250


def test_usami_source_control_aligns_with_tax_and_reconciliation():
    from app.vehicle_workflow import source_reconciliation_report
    # (a) & (e) 2ブロックの宇佐美テキストで ΣP+ΣT が統制値になり、明細合計と一致し matched、extraction_method も検証
    usami_2blocks = (
        "00001 1234 1\n品目\n小計 1000\n軽油引取税 100\n参考消費税 (100)\n自動車用潤滑油 1100\n"
        "00002 1234 1\n品目\n小計 2000\n軽油引取税 200\n参考消費税 (200)\n自動車用潤滑油 2200\n"
    )
    docs_a = [
        ledger(),
        ({'id': 'u_a', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, usami_2blocks),
    ]
    data_a = extract(docs_a, ['2026-01'], True)
    fuel_a = [r for r in data_a['records'] if r['source_ref'] == 'context:u_a' and r['category'] == 'fuel' and r['quality'] == 'actual']
    assert sum(r['amount'] for r in fuel_a) == 3600
    controls_a = [c for c in data_a['source_controls'] if c['source_ref'] == 'context:u_a']
    assert len(controls_a) == 1
    ctrl_a = controls_a[0]
    assert ctrl_a['amount'] == 3600
    assert ctrl_a['source_locator'] == '宇佐美印字合計+明示参考消費税'
    assert ctrl_a['extraction_method'] == 'usami_block_printed_plus_tax:v1'
    assert ctrl_a['control_kind'] == 'invoice_total'
    assert ctrl_a['origin'] == 'source_total'

    issues_recon, summary, rows = source_reconciliation_report(data_a, [{'id': 's', 'filename': '関東月次.xlsx'}, {'id': 'u_a', 'filename': '燃料/202601/宇佐美_関東.pdf'}])
    assert summary['matched'] >= 1
    assert summary['mismatched'] == 0
    u_row = next(r for r in rows if r['source_ref'] == 'context:u_a')
    assert u_row['status'] == 'matched'
    assert u_row['difference'] == 0


def test_usami_missing_tax_row_creates_control_issue_and_not_matched():
    from app.vehicle_workflow import source_reconciliation_report
    # (b) 参考消費税の行が無いブロックがあると issue が出て照合は matched にならない
    usami_no_tax = (
        "00001 1234 1\n品目\n小計 1000\n軽油引取税 100\n自動車用潤滑油 1100\n"
    )
    docs_b = [
        ledger(),
        ({'id': 'u_b', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, usami_no_tax),
    ]
    data_b = extract(docs_b, ['2026-01'], True)
    tax_issues = [
        i for i in data_b['auto_extraction']['issues']
        if '参考消費税' in i.get('message', '')
    ]
    assert tax_issues
    assert any(i.get('reason_code') == 'tax_unreadable' for i in tax_issues)

    issues_recon, summary, rows = source_reconciliation_report(data_b, [{'id': 's', 'filename': '関東月次.xlsx'}, {'id': 'u_b', 'filename': '燃料/202601/宇佐美_関東.pdf'}])
    u_row = next(r for r in rows if r['source_ref'] == 'context:u_b')
    assert u_row['status'] != 'matched'


def test_usami_ocr_adopted_block_includes_tax_in_source_control():
    # (c) OCR採用ブロック経由でも同様に統制値に ΣT が入る
    from tests.test_ocr_usami import usami_payload, approved_review, adoption_bundle, PARTIAL_TEXT_9901, COMPLETE_TEXT_9902
    payload = usami_payload(status="needs_review")
    docs_c = [
        ledger(),
        ({'id': 'u_c', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, PARTIAL_TEXT_9901 + COMPLETE_TEXT_9902),
    ]
    data_c = extract(
        docs_c, ['2026-01'], True,
        ocr_adoptions={'u_c': adoption_bundle(payload, approved_review())},
    )
    ctrl_c = next(c for c in data_c['source_controls'] if c['source_ref'] == 'context:u_c')
    assert ctrl_c['amount'] == 180150 + 68850
    assert ctrl_c['source_locator'] == '宇佐美印字合計+明示参考消費税'
    assert ctrl_c['extraction_method'] == 'usami_block_printed_plus_tax:v1'
    fuel_c = [r for r in data_c['records'] if r['source_ref'] == 'context:u_c' and r['category'] == 'fuel']
    assert sum(r['amount'] for r in fuel_c) == 180150 + 68850


def test_usami_identity_failure_block_excluded_from_source_control():
    # (d) 恒等式(S+D==P)が成り立たないブロックは従来どおり採用されず、統制値にも入らない
    usami_identity_fail = (
        "00001 1234 1\n品目\n小計 1000\n軽油引取税 100\n参考消費税 (100)\n自動車用潤滑油 1200\n"
        "00002 1234 1\n品目\n小計 2000\n軽油引取税 200\n参考消費税 (200)\n自動車用潤滑油 2200\n"
    )
    docs_d = [
        ledger(),
        ({'id': 'u_d', 'filename': '燃料/202601/宇佐美_関東.pdf'}, None, usami_identity_fail),
    ]
    data_d = extract(docs_d, ['2026-01'], True)
    fuel_d = [r for r in data_d['records'] if r['source_ref'] == 'context:u_d' and r['category'] == 'fuel']
    assert len(fuel_d) == 1
    assert fuel_d[0]['amount'] == 2400
    ctrl_d = next(c for c in data_d['source_controls'] if c['source_ref'] == 'context:u_d')
    assert ctrl_d['amount'] == 2400
    assert any('車番小計と印字合計が一致しません' in i.get('message', '') for i in data_d['auto_extraction']['issues'])
