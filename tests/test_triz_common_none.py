"""validate_problem が None を文字列 'None' に変換しないことの検証。"""
import pytest

from app.triz_common import FIELDS, validate_problem


def base_values(**overrides):
    values = {key: f"{key}の内容" for key in FIELDS}
    values.update(overrides)
    return values


def test_physical_none_becomes_empty_string():
    """physical は空を許す項目。None は '' として扱われ、'None' にならない。"""
    problem = validate_problem(base_values(physical=None))
    assert problem["physical"] == ""


def test_physical_missing_key_is_empty_string():
    """キー自体が無い場合も従来どおり空文字。"""
    values = base_values()
    del values["physical"]
    problem = validate_problem(values)
    assert problem["physical"] == ""


@pytest.mark.parametrize("field", [f for f in FIELDS if f != "physical"])
def test_required_field_none_raises(field):
    """physical 以外の必須項目が None のときは従来どおり ValueError。"""
    with pytest.raises(ValueError):
        validate_problem(base_values(**{field: None}))


def test_no_field_contains_literal_none_string():
    """どの項目も、None 由来の文字列 'None' を持たない。"""
    problem = validate_problem(base_values(physical=None))
    assert "None" not in problem.values()


def test_normal_strings_are_unchanged():
    """通常の文字列はそのまま保持される。"""
    values = base_values(goal="目標テキスト", physical="物理矛盾テキスト")
    problem = validate_problem(values)
    assert problem["goal"] == "目標テキスト"
    assert problem["physical"] == "物理矛盾テキスト"
    assert set(problem) == set(FIELDS)


def test_surrounding_whitespace_is_stripped():
    """前後の空白除去は従来どおり。"""
    problem = validate_problem(base_values(goal="  目標  ", physical="\n 物理 \t"))
    assert problem["goal"] == "目標"
    assert problem["physical"] == "物理"


def test_whitespace_only_required_field_raises():
    """空白のみの必須項目は従来どおり ValueError。"""
    with pytest.raises(ValueError):
        validate_problem(base_values(goal="   "))


def test_whitespace_only_physical_is_allowed():
    """physical が空白のみでも従来どおり許容される。"""
    problem = validate_problem(base_values(physical="   "))
    assert problem["physical"] == ""


def test_length_limit_is_unchanged():
    """2500文字上限は従来どおり(2500はOK、2501はValueError)。"""
    assert validate_problem(base_values(goal="あ" * 2500))["goal"] == "あ" * 2500
    with pytest.raises(ValueError):
        validate_problem(base_values(goal="あ" * 2501))
    with pytest.raises(ValueError):
        validate_problem(base_values(physical="あ" * 2501))


def test_non_string_values_are_stringified():
    """None 以外の非文字列は従来どおり文字列化される。"""
    problem = validate_problem(base_values(goal=123, physical=0))
    assert problem["goal"] == "123"
    assert problem["physical"] == "0"
