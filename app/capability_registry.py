"""Code-owned capabilities. A check is not permission to invoke an operation."""
import importlib.util
import os
from pathlib import Path

from app.upgrade_store import utcnow

OCR_FEATURE_FLAG_ENV = 'LOCALSAPORTER_OCR_ENABLED'
OCR_CAPABILITY_IDS = (
    'source.ocr_pdf_fallback',
    'source.extract_pdf_table',
    'source.validate_ocr',
    'source.review_ocr',
    'source.publish_approved_ocr',
)
CAPABILITY_ALIASES = {
    'source.extract_table': 'source.extract_pdf_table',
}
PDF_TEXT_CONSTRAINT = '文字PDFの抽出。OCR・一般的な表復元は保証しない'

DEFINITIONS = {
    'vehicle.source_normalize': ('vehicle_auto','registered_sources','対応形式を原本参照付きで抽出。未知形式・対応不明は未解決として保持'),
    'vehicle.monthly_profit': ('vehicle_profit','ledger','Decimal計算と車両別月次Excel生成'),
    'vehicle.recalculate': ('vehicle_profit','xlsx','LibreOfficeによる独立再計算'),
    'source.extract_pdf_text': ('context_files', 'pdf', PDF_TEXT_CONSTRAINT),
    'source.read_excerpt': ('source_retrieval', 'text', '保存された原本版のunitだけを読む'),
    'source.extract_table': ('context_files', 'pdf', '一般的なPDF表復元は未実装'),
    'source.ocr_pdf_fallback': ('ocr_client', 'pdf', 'PDFをページ画像化し、レイアウト分割後にOCRする。実行検証前は利用しない'),
    'source.extract_pdf_table': ('ocr_schema', 'pdf', 'OCR結果から表セルと行列構造を返す。実行検証前は利用しない'),
    'source.validate_ocr': ('ocr_schema', 'ocr', '合計、日付、表、反復、通常抽出との整合を検査する。実行検証前は利用しない'),
    'source.review_ocr': ('ocr_schema', 'ocr', 'UIで根拠画像と抽出値を人間確認する。実行検証前は利用しない'),
    'source.publish_approved_ocr': ('ocr_schema', 'ocr', '承認済み結果だけを後続計算とRAGへ公開する。実行検証前は利用しない'),
    'web.collect_public': ('public_web_research', 'https', '許可された公開調査のみ。取得成功は別途検証'),
    'table.profile': ('tabular_data', 'table', '登録表データの構造確認'),
    'table.transform': ('tabular_data', 'table', '許可された宣言型の集計・加工'),
    'workspace.write_text': ('workspace_files', 'text', '契約で許可された出力先のみ'),
    'source.hash_text': ('source_retrieval', 'text', '抽出本文の同一性'),
    'source.hash_original': ('source_retrieval', 'bytes', '保持されている原本バイト列の同一性'),
}


def ocr_feature_enabled() -> bool:
    """OPS-06: OCR能力のfeature flag。既定は無効で従来抽出へ戻せる。"""
    value = os.environ.get(OCR_FEATURE_FLAG_ENV, '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def ocr_compose_present() -> bool:
    return (Path(__file__).resolve().parents[1] / 'docker-compose.ocr.yml').is_file()


def ocr_runtime_validated() -> bool:
    """イメージや compose があるだけでは実行検証済みにしない。"""
    return False


def resolve_capability_id(capability: str) -> str:
    return CAPABILITY_ALIASES.get(capability, capability)


class CapabilityRegistry:
    def __init__(self, workspace=None, researcher=None, table_executor=None):
        self.workspace, self.researcher, self.table_executor = workspace, researcher, table_executor

    def check(self, capabilities, context_files, *, web_allowed=False):
        results = []
        for capability in capabilities:
            state, reason = 'available', 'definition_checked'
            definition = DEFINITIONS.get(capability)
            if capability in TRIZ_CAPABILITIES and capability not in DEFINITIONS:
                c=TRIZ_CAPABILITIES[capability]
                definition=('triz_adapters',c['domain'],c['description'])
            if definition is None:
                state, reason = 'unknown', 'unregistered'
            elif capability in TRIZ_CAPABILITIES and capability not in DEFINITIONS and not TRIZ_CAPABILITIES[capability]['available']:
                state,reason='unavailable','adapter_not_connected'
            elif capability.startswith('vehicle.') and importlib.util.find_spec('openpyxl') is None:
                state,reason='unavailable','not_configured'
            elif capability=='vehicle.recalculate' and not (__import__('shutil').which('libreoffice') or __import__('shutil').which('soffice')):
                state,reason='unavailable','not_configured'
            elif capability == 'source.extract_table':
                state, reason = 'unavailable', 'unsupported_operation'
            elif capability in OCR_CAPABILITY_IDS:
                if not ocr_feature_enabled():
                    state, reason = 'unavailable', 'feature_disabled'
                else:
                    state, reason = 'configured', 'definition_checked'
            elif capability == 'source.extract_pdf_text' and importlib.util.find_spec('pypdf') is None:
                state, reason = 'unavailable', 'not_configured'
            elif capability == 'web.collect_public':
                if self.researcher is None:
                    state, reason = 'unavailable', 'not_configured'
                elif not web_allowed:
                    state, reason = 'unavailable', 'approval_required'
            elif capability.startswith('table.') and self.table_executor is None:
                state, reason = 'unavailable', 'not_configured'
            elif capability == 'workspace.write_text' and self.workspace is None:
                state, reason = 'unavailable', 'not_configured'
            elif capability == 'source.hash_original' and not any(x.get('original_data') and x.get('source') != 'web' for x in context_files):
                state, reason = 'unknown', 'original_not_available'
            configured = definition is not None
            validated = False
            if capability in OCR_CAPABILITY_IDS:
                configured = True
                validated = ocr_runtime_validated()
            constraints = definition[2] if definition else '未登録。未実装とは断定しない'
            if capability == 'source.extract_pdf_text' and not ocr_runtime_validated():
                constraints = PDF_TEXT_CONSTRAINT
            results.append({'capability_id': capability, 'definition_version': '1', 'state': state,
                            'implemented':definition is not None and capability!='source.extract_table' and (capability not in TRIZ_CAPABILITIES or capability in DEFINITIONS or TRIZ_CAPABILITIES[capability]['available']),'input_compatible':'unchecked','validated':validated,
                            'configured': configured,
                            'triz_connected':TRIZ_CAPABILITIES.get(capability,{}).get('available',False),
                            'reason_code': reason, 'check_scope': 'configuration_only_no_network',
                            'checked_at': utcnow(), 'constraints': constraints})
        return results

TRIZ_CAPABILITIES={
 'document.rewrite':{'domain':'documents','available':True,'description':'登録テキストをローカルAIで再構成し、原本とは別のMarkdown成果物を作る。内容の正しさは人が確認。'},
 'research.synthesize':{'domain':'research','available':True,'description':'保存済み資料の原文引用と解釈を区別した報告書を作り、引用の完全一致を検査。新規Web取得は行わない。'},
 'code.patch':{'domain':'software','available':True,'description':'登録Python原本の完全一致箇所だけを置換したコピーと差分を作り、AST構文検査。コードは実行しない。'},
 'workflow.design':{'domain':'workflow','available':True,'description':'工程・確認・停止・復旧を備える業務手順書を作る。現場での改善効果は未測定。'},
 'operations.diagnose':{'domain':'operations','available':True,'description':'登録ログの引用に基づく診断仮説と試験計画を作る。実システムは変更しない。'},
 'browser.interact':{'domain':'browser','available':False,'description':'ブラウザ操作の実行コネクタ未接続。操作計画と試験方法を保存できる。'},
 'web.collect_public':{'domain':'research','available':False,'description':'TRIZからの公開Web取得は未接続。公開調査の許可と接続が必要。'},
 'code.run_tests':{'domain':'software','available':False,'description':'隔離コード実行コネクタ未接続。別環境の実行テスト証拠を人が登録する。'},
 'system.apply':{'domain':'operations','available':False,'description':'実システム修復の実行コネクタ未接続。'},
 'workflow.rollout':{'domain':'workflow','available':False,'description':'現場での業務変更・測定の実行コネクタ未接続。'},
 'general.action':{'domain':'general','available':False,'description':'専用実行コネクタ未接続。計画を保存する。'}}
