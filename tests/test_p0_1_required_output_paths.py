"""P0-1: 必須成果物パスの決定的抽出と完全一致の受入試験。

外部AI・実通信は使わない。汎用の固定達成条件と実関数で再現する。
"""
import pytest

from app.structured_planning import (
    REQUIRED_OUTPUT_PATH_MISMATCH,
    REQUIRED_OUTPUT_PATH_UNKNOWN,
    compile_plan,
    compile_task,
    contract_of,
    extract_required_output_paths,
    validate_plan,
    verify_outputs,
)


def _proposal(extra=None):
    row = {"title": "疑似資料", "scope": "疑似の内容を整理する。本文を作る。",
           "headings": ["概要", "詳細"], "depends_on": []}
    if extra:
        row.update(extra)
    return row


def _plan_with_paths(criterion, paths):
    outputs = [{"path": path, "required_headings": ["概要", "詳細"]} for path in paths]
    task = compile_task(1, criterion, _proposal({"outputs": outputs}), [])
    return compile_plan([criterion], [task], goal="疑似P0-1")


def test_extract_single_and_double_and_none():
    single = extract_required_output_paths("`result/guide.md` に手順書を保存する")
    assert single["status"] == "explicit"
    assert single["paths"] == ["result/guide.md"]
    double = extract_required_output_paths("result/guide.md と result/faq_list.csv を保存する")
    assert double["status"] == "explicit"
    assert double["paths"] == ["result/guide.md", "result/faq_list.csv"]
    none = extract_required_output_paths("市場を定義し資料を作る")
    assert none == {"status": "none", "paths": [], "reasons": ["no_explicit_path"]}


def test_extract_unknown_contexts():
    for text, reason in [
        ("result/guide.md に保存しない", "negation_context"),
        ("例: result/guide.md を作る", "example_context"),
        ("たとえば result/guide.md を作る", "example_context"),
        ("result/guide.md 以外を作る", "exclusion_context"),
        ("result/a.md ではなく result/b.md を作る", "contradictory_paths"),
        ("result/a.md と result/b.md のいずれかを作る", "contradictory_paths"),
    ]:
        got = extract_required_output_paths(text)
        assert got["status"] == "unknown", text
        assert got["paths"] == []
        assert reason in got["reasons"], text


def test_extract_invalid_and_too_many_and_non_target_extension():
    for text in ["../x.md を result/guide.md に保存する", "result/guide.md と result/../x.md を保存する",
                 "/abs.md と result/guide.md を保存する", "result\\a.md を保存する",
                 "https://example.com/a.md を result/guide.md に保存する",
                 "result/a.md と http://example.com/result/b.md を保存する",
                 "result/a md.md を保存する", "result/a:b.md を保存する",
                 "result//a.md を保存する", "result/a~1.md を保存する"]:
        got = extract_required_output_paths(text)
        assert got["status"] == "unknown", text
        assert got["paths"] == []
        assert got["reasons"] == ["invalid_path_format"], text
    many = " ".join(f"result/a{i}.md" for i in range(6))
    got = extract_required_output_paths(many + " を保存する")
    assert got["status"] == "unknown"
    assert got["paths"] == []
    assert got["reasons"] == ["too_many_paths"]
    pdf_only = extract_required_output_paths("result/guide.pdf に保存する")
    assert pdf_only["status"] == "none"
    mixed = extract_required_output_paths("result/guide.pdf と市場定義")
    assert mixed["status"] == "none"


def test_compile_task_accepts_matching_and_rejects_others():
    criterion = "`result/guide.md` に手順書を保存する"
    task = compile_task(1, criterion, _proposal({"outputs": [
        {"path": "result/guide.md", "required_headings": ["概要", "詳細"]}]}), [])
    assert [out["path"] for out in contract_of(task)["outputs"]] == ["result/guide.md"]
    default_task = compile_task(1, criterion, _proposal(), [])
    assert [out["path"] for out in contract_of(default_task)["outputs"]] == ["result/guide.md"]
    with pytest.raises(ValueError):
        compile_task(1, criterion, _proposal({"outputs": [
            {"path": "result/sc01.md", "required_headings": ["概要", "詳細"]}]}), [])
    with pytest.raises(ValueError):
        compile_task(1, criterion, _proposal({"outputs": [
            {"path": "result/guide.md", "required_headings": ["概要", "詳細"]},
            {"path": "result/extra.md", "required_headings": ["概要", "詳細"]}]}), [])
    count_criterion = "result/guide.md と result/faq_list.md の2ファイル、合計2ファイルで保存する"
    with pytest.raises(ValueError):
        compile_task(1, count_criterion, _proposal({"outputs": [
            {"path": "result/guide.md", "required_headings": ["概要", "詳細"]}]}), [])


def test_compile_task_without_explicit_path_keeps_legacy():
    legacy = compile_task(1, "市場を定義し資料を作る", _proposal(), [])
    assert [out["path"] for out in contract_of(legacy)["outputs"]] == ["result/sc01.md"]
    with pytest.raises(ValueError, match=REQUIRED_OUTPUT_PATH_UNKNOWN):
        compile_task(1, "例: result/guide.md を作る", _proposal(), [])


def test_validate_plan_mismatch_unknown_and_none():
    good = _plan_with_paths("`result/guide.md` に手順書を保存する", ["result/guide.md"])
    checked = validate_plan(good, {"SC01"})
    assert checked["passed"] is True
    bad_paths = _plan_with_paths("`result/guide.md` に手順書を保存する", ["result/guide.md"])
    bad_task = next(t for t in bad_paths["tasks"] if t["task_key"] == "SC01")
    contract = contract_of(bad_task)
    contract["outputs"] = [{"path": "result/other.md", "required_headings": ["概要", "詳細"],
                            "minimum_characters": 600}]
    bad_task["acceptance_criteria"] = __import__("json").dumps(contract, ensure_ascii=False)
    bad = validate_plan(bad_paths, {"SC01"})
    assert bad["passed"] is False
    assert any(REQUIRED_OUTPUT_PATH_MISMATCH in issue for issue in bad["issues"])
    unknown_paths = _plan_with_paths("`result/guide.md` に手順書を保存する", ["result/guide.md"])
    unknown_task = next(t for t in unknown_paths["tasks"] if t["task_key"] == "SC01")
    unknown_contract = contract_of(unknown_task)
    unknown_contract["criterion"] = "例: result/guide.md を作る"
    unknown_task["acceptance_criteria"] = __import__("json").dumps(unknown_contract, ensure_ascii=False)
    unknown = validate_plan(unknown_paths, {"SC01"})
    assert unknown["passed"] is False
    assert any(REQUIRED_OUTPUT_PATH_UNKNOWN in issue for issue in unknown["issues"])
    legacy = compile_plan(["市場を定義し資料を作る"], [compile_task(1, "市場を定義し資料を作る", _proposal(), [])])
    assert validate_plan(legacy, {"SC01"})["passed"] is True


def test_validate_plan_single_and_double_files():
    single = _plan_with_paths("`result/guide.md` に手順書を保存する", ["result/guide.md"])
    assert validate_plan(single, {"SC01"})["passed"] is True
    two_criterion = ["`result/guide.md` に手順書を保存する", "`result/faq_list.md` を保存する"]
    lone = compile_task(1, two_criterion[0], _proposal({"outputs": [
        {"path": "result/guide.md", "required_headings": ["概要", "詳細"]}]}), [])
    tasks = compile_plan(two_criterion, [lone, compile_task(2, two_criterion[1], _proposal({"outputs": [
        {"path": "result/faq_list.md", "required_headings": ["概要", "詳細"]}]}), [])])["tasks"]
    checked_two = validate_plan({"tasks": tasks}, {"SC01", "SC02"})
    assert checked_two["passed"] is True, checked_two["issues"]
    wrong_plan = compile_plan(
        ["`result/guide.md` に手順書を保存する", "資料を作る"],
        [lone, compile_task(2, "資料を作る", _proposal(), [])])["tasks"]
    tampered = wrong_plan[2]
    tampered_contract = contract_of(tampered)
    tampered_contract["criterion"] = "`result/faq_list.md` を保存する"
    tampered_contract["outputs"] = [{"path": "result/wrong.md", "required_headings": ["概要", "詳細"],
                                     "minimum_characters": 600}]
    tampered["acceptance_criteria"] = __import__("json").dumps(tampered_contract, ensure_ascii=False)
    assert validate_plan({"tasks": wrong_plan}, {"SC01", "SC02"})["passed"] is False


def test_rebuild_keeps_required_paths_and_rejects_mismatch():
    criterion = "`result/guide.md` に手順書を保存する"
    task = compile_task(1, criterion, _proposal({"outputs": [
        {"path": "result/guide.md", "required_headings": ["概要", "詳細"]}]}), [])
    assert [out["path"] for out in contract_of(task)["outputs"]] == ["result/guide.md"]
    plan = compile_plan([criterion], [task], goal="疑似P0-1")
    kept = next(t for t in plan["tasks"] if t["task_key"] == "SC01")
    assert kept["title"].strip() != ""
    assert validate_plan(plan, {"SC01"})["passed"] is True
    tampered = __import__("copy").deepcopy(plan)
    target = next(t for t in tampered["tasks"] if t["task_key"] == "SC01")
    contract = contract_of(target)
    contract["criterion"] = criterion
    contract["outputs"] = [{"path": "result/other.md", "required_headings": ["概要", "詳細"],
                            "minimum_characters": 600}]
    target["acceptance_criteria"] = __import__("json").dumps(contract, ensure_ascii=False)
    bad = validate_plan(tampered, {"SC01"})
    assert bad["passed"] is False
    assert any(REQUIRED_OUTPUT_PATH_MISMATCH in issue for issue in bad["issues"])


def test_body_path_string_alone_does_not_pass_existence(tmp_path):
    criterion = "`result/guide.md` に手順書を保存する"
    task = compile_task(1, criterion, _proposal({"outputs": [
        {"path": "result/guide.md", "required_headings": ["概要", "詳細"]}]}), [])
    headings = contract_of(task)["outputs"][0]["required_headings"]
    body = "\n".join(f"## {heading}\n本文です。result/guide.md を参照。" for heading in headings)
    body += "\n" + ("本文の追記。" * 200)
    target = tmp_path / "guide.md"

    def resolve(path):
        assert path == "result/guide.md"
        return target

    assert any("読み取り失敗" in item for item in verify_outputs(task, resolve))
    target.write_text("\n".join(f"## {heading}\n" + ("具体的な内容。" * 60) for heading in headings),
                      encoding="utf-8")
    assert verify_outputs(task, resolve) == []
