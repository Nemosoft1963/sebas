"""presenters.js reviewRows: issues / provider_outcomes が配列以外でも落ちない。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app/static/presenters.js").read_text(encoding="utf-8")


def function_body(name):
    start = SOURCE.find("function " + name + "(")
    assert start >= 0, name
    depth = 0
    begun = False
    for index in range(start, len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
            begun = True
        elif SOURCE[index] == "}":
            depth -= 1
            if begun and depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(name)


UNKNOWN = "不明"
STATUS = {
    "passed": "合格",
    "not_passed": "指摘あり",
    "unverified": "未検証",
    "connection_failed": "接続できない",
}


def present(value):
    if value is None or value == "":
        return UNKNOWN
    if value is True:
        return "必須"
    if value is False:
        return "不要"
    if isinstance(value, list):
        return "、".join(present(x) for x in value) if value else UNKNOWN
    if isinstance(value, dict):
        return "、".join(k + ": " + present(value[k]) for k in value) if value else UNKNOWN
    return str(value)


def first(source, keys):
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source and source[key] is not None and source[key] != "":
            return source[key]
    return None


def status_presenter(input_value):
    if input_value is None or input_value == "":
        return UNKNOWN
    key = input_value.get("status") if isinstance(input_value, dict) else input_value
    return STATUS[key] if key in STATUS else UNKNOWN


def issue_text(issue):
    if issue and isinstance(issue, dict):
        return present(first(issue, ["text", "issue", "message", "detail"]))
    return present(issue)


def review_rows(data):
    reviews = first(data, ["reviews", "provider_reviews", "results"]) or []
    outcomes = data.get("provider_outcomes") or []
    if not isinstance(reviews, list):
        reviews = []
    if not isinstance(outcomes, list):
        outcomes = []
    rows = []
    for row in reviews:
        issues = row.get("issues") if isinstance(row, dict) else None
        if not isinstance(issues, list):
            issues = [issues] if issues else []
        rows.append({
            "ai": present(first(row, ["provider", "id", "label", "name"])),
            "verdict": status_presenter(first(row, ["verdict", "status", "outcome"])),
            "severity": present(first(row, ["severity", "importance", "priority"])),
            "issue": present([issue_text(item) for item in issues]),
            "proposal": present(first(row, ["suggestion", "proposal", "fix", "recommendation"])),
        })
    review_ids = [first(r, ["provider", "id"]) for r in reviews if isinstance(r, dict)]
    for row in outcomes:
        ident = first(row, ["provider", "id"])
        if ident in review_ids:
            continue
        rows.append({
            "ai": present(first(row, ["provider", "id", "label"])),
            "verdict": status_presenter(first(row, ["status", "outcome"])),
            "severity": UNKNOWN,
            "issue": present(first(row, ["error", "message"])),
            "proposal": UNKNOWN,
        })
    return rows


def test_review_rows_js_defends_non_array_issues_and_outcomes():
    body = function_body("reviewRows")
    assert "if(!Array.isArray(reviews))reviews=[]" in body
    assert "if(!Array.isArray(outcomes))outcomes=[]" in body
    assert "var issues=row.issues" in body
    assert "if(!Array.isArray(issues))issues=issues?[issues]:[]" in body
    assert "(row.issues||[]).map" not in body


def test_review_rows_accepts_issues_string_object_array_null():
    string_row = review_rows({"reviews": [{"provider": "a", "status": "not_passed", "issues": "工程不足"}]})
    assert string_row[0]["issue"] == "工程不足"

    object_row = review_rows({"reviews": [{"provider": "b", "status": "not_passed", "issues": {"text": "検証なし"}}]})
    assert object_row[0]["issue"] == "検証なし"

    array_row = review_rows({"reviews": [{"provider": "c", "status": "not_passed", "issues": [{"text": "入力欠落"}, "期限未記載"]}]})
    assert array_row[0]["issue"] == "入力欠落、期限未記載"

    null_row = review_rows({"reviews": [{"provider": "d", "status": "passed", "issues": None}]})
    assert null_row[0]["issue"] == UNKNOWN

    missing_row = review_rows({"reviews": [{"provider": "e", "status": "passed"}]})
    assert missing_row[0]["issue"] == UNKNOWN


def test_review_rows_accepts_non_array_provider_outcomes():
    rows = review_rows({
        "reviews": [{"provider": "a", "status": "passed", "issues": []}],
        "provider_outcomes": {"a": "success"},
    })
    assert len(rows) == 1
    assert rows[0]["ai"] == "a"
