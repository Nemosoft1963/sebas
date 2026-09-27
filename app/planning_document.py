"""Contract-owned headings for local planning documents; no model file operations."""
from __future__ import annotations

import re

from app.structured_planning import contract_of, decode_object


def planning_output(task: dict) -> dict | None:
    contract = contract_of(task) or {}
    outputs = contract.get("outputs", [])
    if (task.get("mode") != "local" or contract.get("final_verification")
            or contract.get("public_web_research", {}).get("required")
            or contract.get("action_requirements") or len(outputs) != 1):
        return None
    output = outputs[0]
    if (not output.get("path", "").endswith(".md")
            or not output.get("required_headings")
            or (contract.get("document_mode") != "planning"
                and not re.search(r"計画|planning|\bplan\b", contract.get("criterion", ""), re.I))):
        return None
    return output


def section_schema(output: dict) -> dict:
    sections = {f"section_{i}": {"type": "string", "minLength": 40, "maxLength": 400}
                for i, _ in enumerate(output["required_headings"], 1)}
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "sections": {"type": "object", "properties": sections,
                         "required": list(sections), "additionalProperties": False},
            "missing_inputs": {"type": "array", "items": {"type": "string"}},
        }, "required": ["sections", "missing_inputs"],
    }


def render_sections(task: dict, output: dict, response: str, context_files: list[dict] | None = None) -> str:
    payload = decode_object(response)
    if set(payload) != {"sections", "missing_inputs"}:
        raise ValueError("計画本文はsectionsとmissing_inputsだけを返してください")
    missing = payload["missing_inputs"]
    if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
        raise ValueError("missing_inputsの形式が不正です")
    if missing and (context_files is None or planning_output(task) is None):
        raise ValueError("計画作成に必要な未取得資料: " + "、".join(missing))
    sections = payload["sections"]
    keys = [f"section_{i}" for i, _ in enumerate(output["required_headings"], 1)]
    if not isinstance(sections, dict) or set(sections) != set(keys):
        raise ValueError("計画本文の必須項目が不足または契約外です")
    parts = ["# " + re.sub(r"[\r\n]+", " ", task["title"])]
    for key, heading in zip(keys, output["required_headings"]):
        body = sections[key]
        if not isinstance(body, str):
            raise ValueError(f"{heading}: 本文は文字列で指定してください")
        body = body.strip()
        if len(body) > 400:
            raise ValueError(f"{heading}: 本文は400文字以内で要点をまとめてください")
        meaningful = re.sub(r"要確認|未確認|未定|TBD|TODO|[\W_]", "", body, flags=re.I)
        if len(meaningful) < 30:
            raise ValueError(f"{heading}: 空欄・仮置きだけでは完了できません")
        if re.search(r"実施しました|実行しました|取得しました|検索しました|収集しました|ダウンロードしました|申請中|提出済み|承認済み|から引用しています|重複を排除しました", body):
            raise ValueError(f"{heading}: 今回実行していない操作の完了表現があります。今後の手順として書いてください")
        if "事例" in heading and not re.search(r"仮説|提案例|例示|検証予定", body):
            raise ValueError(f"{heading}: 計画段階の事例は仮説・提案例と明記してください")
        parts.append(f"## {heading}\n\n{body}")
    if missing:
        from app.context_files import extraction_quality
        parts.append("## 実行前に解消する資料条件\n\n計画書の作成と実作業の完了は別です。以下の条件を解消するまで、該当資料による計算・納品を完了扱いにしません。")
        for name in dict.fromkeys(missing):
            matches = [x for x in context_files if x.get('source') != 'memo' and x.get('filename') == name]
            state = ('登録原本なし（名称一致なし）' if not matches else
                     '読取品質の確認が必要' if any(extraction_quality(str(x.get('content', ''))) != 'readable' for x in matches)
                     else '登録済み・内容または提示範囲の確認が必要')
            safe_name = re.sub(r'[\r\n]+', ' ', name)[:500]
            parts.append(f"- {safe_name}: {state}。担当: 資料確認担当。手順: 原本と抽出内容を照合し、必要なら再抽出する。完了条件: 対象項目の根拠・読取結果を記録し、未確認を解消する。")
    document = "\n\n".join(parts) + "\n"
    if len(document.strip()) < output.get("minimum_characters", 0):
        raise ValueError("計画本文の最低文字数が不足しています")
    return document


def capability_context(context_files: list[dict], collector_available: bool) -> str:
    lines = [
        "# バックエンドが観測した機能と資料",
        "公開Web取得器の設定: " + ("あり" if collector_available else "なし"),
        "取得器の設定は任意URLの取得成功を保証しません。新規取得は調査工程で行います。",
        "以下は取得済み資料です。未取得の版と、機能そのものの不存在を区別してください。",
    ]
    from app.context_files import extraction_quality
    for item in context_files:
        if item.get('source') != 'memo' and extraction_quality(str(item.get('content', ''))) != 'readable':
            lines.append('読取未確認: context:' + str(item.get('id', '')) + '。登録済みですが内容を計算根拠にせず、原本照合・再抽出を計画に含めてください。')
        if item.get("source") != "web":
            continue
        content = str(item.get("content", ""))
        metadata = re.findall(r"(?m)^- (?:URL|取得日時\(UTC\)|本文SHA256): .+$", content)
        lines.append("context:" + str(item["id"]) + "\n" + "\n".join(metadata))
    lines.extend([
        "本文SHA256は保存された抽出本文のハッシュです。PDFバイナリのハッシュとは区別します。",
        "現在の仕事は計画書作成です。未取得項目は確認手順・担当・完了条件を計画に記載できます。",
        "必須原本が本当に足りず計画を作れない場合だけ、missing_inputsに具体的な資料名を記載します。",
        "実績・効果・制度数値は原本refを添え、原本にない事例は仮説・例示と明記します。",
        "外部AIは助言・レビュー担当です。実施承認を出すのは人間です。",
    ])
    return "\n".join(lines)

def public_research_plan_sections(task: dict, output: dict, context_files: list[dict]) -> dict | None:
    """A procedural research plan, never a claim of current grant eligibility."""
    criterion = (contract_of(task) or {}).get("criterion", "" ).strip()
    if (not re.search(r"公募要領.*調査計画", task.get("title", ""))
            or not re.fullmatch(r"(?:まずは)?計画を(?:詳細に)?作成してください[。！!]*", criterion)):
        return None
    web_files = [x for x in context_files if x.get("source") == "web"
                 and re.search(r"(?m)^- URL: https://", str(x.get("content", "")))
                 and re.search(r"(?m)^- 本文SHA256: [0-9a-f]{64}\s*$", str(x.get("content", "")))
                 and re.search(r"(?m)^- 取得日時\(UTC\): \S+", str(x.get("content", "")))]
    if not web_files:
        return None
    references = "、".join("context:" + str(x["id"]) for x in web_files[:2])
    bodies = {
        "調査手順とスケジュール": (
            "着手日をD0とする実施案。担当者はD0に登録原本と対象公募回を照合し、D1に公式ページ・PDFの版と改訂を確認する。"
            "D2に要件一覧と未確認事項を整理し、D3に事例・構成案を検討、D4にレビュー、D5に責任者の承認を得る。"
            "これは所要日数の仮置きであり申請期限ではない。公式期限と担当者の稼働を確認して日程を確定する。"
        ),
        "公式情報収集項目一覧": (
            "調査担当者は、公募回・版、発行主体、公開日・更新日、対象者、対象経費、補助率・上限、申請期限、必要添付、"
            "申請経路、改訂情報を一覧化する。各行に原本ref、該当ページまたは見出し、確認日、確定・未確認の区分を付す。"
            "過去版の数値を現行条件へ転用しない。未確認の行は不足資料、確認担当、確認方法を記載し、全行の根拠照合を完了条件とする。"
        ),
        "外部検索タスク詳細": (
            "公開情報調査の担当者は、公式ポータルの公募回一覧と改訂履歴を起点に、対象回の公式PDFへ辿る。"
            "新規取得が必要な資料は調査工程に回し、取得URL、日時、抽出本文SHA256を登録する。"
            "GビズID等の未確認手続きも公式案内を確認する項目として管理する。取得失敗はURLと理由を残し、"
            "既存機能の不存在とは区別する。完了条件は、対象版の一次資料と必要項目の対応が確認できることとする。"
        ),
        "事例とシステム構成図": (
            "仮説として、利用者→業務画面→権限制御→ローカルAI処理→成果物・監査記録という構成案を検討する。"
            "設計担当者は登録された既存システム資料と照合し、実在する機能、追加開発、未確認の接続を区別する。"
            "事例は公開された原本の該当箇所を確認できるものを採用し、それ以外は提案例と明記する。"
            "効果・採択実績を作らず、各接続の入出力、担当、実証方法を図に併記できることを完了条件とする。"
        ),
        "レビューと承認フロー": (
            "担当者が根拠と版を自己点検し、設計レビュー担当が構成・不足資料・実証方法を確認する。"
            "外部AIのレビューは明示許可がある場合だけ公開情報の範囲で行い、社内原本や個人情報を送らない。"
            "指摘ごとに修正内容と再確認結果を記録し、最終的な実施承認は人間の責任者が行う。"
            "申請送信や契約はこの計画作成では行わず、別途承認を得る。未解決事項と承認者・承認日を追跡可能にする。"
        ),
        "根拠と未確認事項": (
            "参照候補となる取得済みWeb原本: " + references + "。"
            "これらの存在は現行版・対象適合性の確認完了を意味しない。調査担当者は対象回、改訂、制度条件、"
            "手続き詳細を原本と照合する。本文SHA256とPDFファイル自体のハッシュを混同しない。"
            "不足事項は必要資料・確認先・担当・解消条件の一覧で管理し、未確認の条件は断定しない。"
        ),
        "実施状態と次の行動": (
            "この成果物は今後の調査・設計の実施計画であり、新規検索、PDF取得、申請、レビュー依頼を行った報告ではない。"
            "次に人間の責任者が対象公募回、担当者、仮日程を確認する。その後、調査担当者が登録原本の版を照合し、"
            "不足項目を調査工程へ引き継ぐ。各工程は証拠と完了条件が揃ってから完了扱いとし、資料不足や承認待ちは明示する。"
        ),
    }
    headings = output["required_headings"]
    if set(headings) != set(bodies):
        return None
    return {"sections": {f"section_{i}": bodies[h] for i, h in enumerate(headings, 1)},
            "missing_inputs": []}

def validate_document_references(document: str, context_files: list[dict]) -> None:
    """Reject invented identifiers even if the model omitted source_references."""
    allowed = {str(item['id']) for item in context_files
               if item.get('id') and item.get('source') != 'memo'}
    cited = set(re.findall(r"context:([A-Za-z0-9_-]+)", document))
    if cited - allowed:
        raise ValueError('本文に存在しない登録原本参照があります: ' + ', '.join(sorted(cited - allowed)))


def preparation_sections(task: dict, output: dict, context_files: list[dict]) -> dict | None:
    """Only observed inventory and future checks; never infer accounting facts."""
    contract = contract_of(task) or {}
    if (task.get('task_key') != 'SC00' or contract.get('document_mode') != 'planning'
            or contract.get('criterion_ids')):
        return None
    headings = output['required_headings']
    allowed = set(contract.get('source_refs', []))
    files = [item for item in context_files if item.get('id') and item.get('source') != 'memo'
             and 'context:' + str(item['id']) in allowed]
    refs = '、'.join('context:' + str(item['id']) for item in files[:3]) or '登録原本なし'
    inventory = (
        f'バックエンドの登録目録で参照できる原本は{len(files)}件。参照例: {refs}。'
        'これは登録状態の確認であり、本文の正確性や計算条件の確認完了を意味しない。'
        '担当者は原本の名称・対象期間・版・必要な項目を照合し、採用する資料と理由を記録する。'
    )
    bodies = {
        '原本の確認結果': inventory,
        '不足資料': '担当者は達成条件に必要な入力項目と登録目録を対応付け、未登録・判読不能・版の不一致を区別する。不足資料の有無や具体的な名称は本文照合後に確定し、推測では断定しない。不足が判明した場合は確認先、入手手順、確認担当、再開条件を記録し、未確認の値を補って計算を進めない。',
        '入力条件と対象範囲': '対象期間、金額単位、税込・税抜、集計キー、配賦基準は依頼条件と原本の記載を照合して確定する。原本にない条件は未確認として扱う。税込と明記された金額の再度の税込変換を行わず、売上と原価の一致を一般的な正常条件にしない。各集計を対応する原本と個別に照合する。',
        'ニーズ探索の質問票': '質問票には現状の業務、困っている場面、利用者、既存の対処方法、制約、改善を判断する指標を設ける。顧客層や回答内容は仮定で確定しない。担当者は質問の目的と回答の確認方法を対応付け、未回答の欄を明示する。顧客への接触や送信は承認を得てから行い、架空の面談結果を記入しない。',
        '検証手順と承認待ち': '担当者は入力の読み取り、条件の照合、試行、独立した期待結果との比較、成果物確認の順序を設計する。差異がある場合は該当する原本と工程へ戻り、原因と修正内容を記録する。外部送信や契約など承認対象の操作は、具体的な対象と内容を示して承認を得るまで実行しない。',
        '根拠と未確認事項': '登録目録の存在と原本の内容が正しいことは別に扱う。本文から採用する事実には正確な原本IDと該当箇所を付け、引用内容を原本と照合する。過去の経験や生成した計画は今回の数値を裏付ける証拠にしない。未確認の条件、解消方法、担当者、次工程への移行条件を一覧で管理する。',
        '実施状態と次の行動': 'この成果物は登録状態に基づく準備手順の整理である。原本の内容照合、計算、集計、業務成果物の完成を証明するものではない。次に担当者が必要資料と入力条件を確認し、確認記録を残す。先行条件が満たされてから後続工程を開始し、各工程の実行結果と検証結果を分けて記録する。',
    }
    if '税込の明記' not in task.get('description', ''):
        bodies['入力条件と対象範囲'] = '担当者は依頼の対象、対象期間、入力の形式、必要な項目、対応付けの方法を原本と照合する。原本や依頼にない条件は未確認とし、確認先を明記する。対象外の業務を工程へ追加せず、入力変更時に再検証する範囲を定義する。必要条件が確認できたことを記録してから、後続工程の具体化へ進む。'
    if any(heading not in bodies for heading in headings):
        raise ValueError('準備工程に未対応の見出しがあります')
    return {'sections': {f'section_{i}': bodies[heading] for i, heading in enumerate(headings, 1)},
            'missing_inputs': []}
