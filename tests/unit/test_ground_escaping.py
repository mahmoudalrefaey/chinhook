"""agent/nodes/ground.py's value-matching helpers: identifier validation and ILIKE escaping."""

from agent.nodes.ground import _escape_ilike_value, _is_valid_identifier


def test_valid_identifiers_pass():
    assert _is_valid_identifier("customer") is True
    assert _is_valid_identifier("_private") is True
    assert _is_valid_identifier("table123") is True


def test_identifiers_with_quotes_or_injection_attempts_are_rejected():
    assert _is_valid_identifier('artist"; drop table artist; --') is False
    assert _is_valid_identifier('') is False
    assert _is_valid_identifier('has space') is False
    assert _is_valid_identifier('has-dash') is False


def test_ilike_escaping_preserves_a_plain_value():
    assert "AC/DC" in _escape_ilike_value("AC/DC")


def test_percent_sign_is_escaped_as_a_literal():
    escaped = _escape_ilike_value("100%")
    assert escaped == "100\\%"


def test_underscore_is_escaped_as_a_literal():
    escaped = _escape_ilike_value("AC_DC")
    assert escaped == "AC\\_DC"


def test_trailing_backslash_does_not_produce_a_dangling_escape():
    escaped = _escape_ilike_value("AC/DC\\")
    # The literal backslash must itself be escaped, so it can never be the last, dangling
    # character of the pattern, which is what previously made Postgres reject the query
    # outright with "pattern must not end with escape character".
    assert not escaped.endswith("\\") or escaped.endswith("\\\\")


def test_single_quote_is_doubled_for_the_sql_string_literal():
    escaped = _escape_ilike_value("O'Brien")
    assert "''" in escaped
