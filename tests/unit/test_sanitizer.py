"""ui/render.py's HTML sanitising, against every bypass the regex version it replaced had.

Each of these actually got through the previous, regex-based filter. None of them require
the model to cooperate; the table-cell versions are the exact shape scripts/db output takes
once raw database content reaches an answer.

Checked by actually parsing the sanitiser's output and inspecting the tags and attributes it
contains, rather than searching the raw string for a keyword. A keyword search cannot tell
"a dangerous tag survived, live" apart from "the word describing one is sitting there as
plain, escaped, inert text", and the second of those is exactly what a working sanitiser
produces for input like this: not a real difference to fail a test over.
"""

from html.parser import HTMLParser

import pytest

from ui.render import _ALLOWED_ATTRIBUTES, _ALLOWED_TAGS, as_frame, escape_text, markdown_to_html


class _Inspector(HTMLParser):
    """Every tag and attribute an HTML parser actually sees in a string.

    Deliberately not a security parser of its own: it trusts Python's stdlib to tokenise the
    markup the same way a browser would, and only records what came out, so the test below
    can assert directly on what a real parser considers this document to contain.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []  # (tag, name, value)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            self.attrs.append((tag, name, value or ""))

    handle_startendtag = handle_starttag


def _inspect(html: str) -> _Inspector:
    parser = _Inspector()
    parser.feed(html)
    return parser


def _assert_safe(html: str) -> None:
    parsed = _inspect(html)
    unexpected_tags = [t for t in parsed.tags if t not in _ALLOWED_TAGS]
    assert not unexpected_tags, f"disallowed tag(s) survived: {unexpected_tags} in {html!r}"
    for tag, name, value in parsed.attrs:
        allowed_for_tag = _ALLOWED_ATTRIBUTES.get(tag, set())
        assert name in allowed_for_tag or name == "rel", (
            f"disallowed attribute {name!r} on <{tag}> in {html!r}"
        )
        if name == "href":
            assert value.split(":", 1)[0].lower() in ("http", "https", "mailto") or not value, (
                f"unsafe href scheme survived: {value!r} in {html!r}"
            )


PAYLOADS = [
    ("nested script", "<scr<script>ipt>alert(1)</scr</script>ipt>"),
    ("plain script", "<script>alert(1)</script>"),
    ("javascript href", '<a href="javascript:alert(1)">click</a>'),
    ("md javascript link", "[click](javascript:alert(1))"),
    ("img onerror, slash separators", "<img/src=x/onerror=alert(1)>"),
    ("video source onerror", "<video><source/onerror=alert(1)>"),
    ("nested object", "<obj<object>ect data=x>"),
    ("base tag", '<base href="http://evil.com/">'),
    ("mathml javascript href", '<math><maction xlink:href="javascript:alert(1)">x</maction></math>'),
    ("style attribute", '<div style="background:url(javascript:alert(1))">x</div>'),
    ("srcset attribute", '<img srcset="x" src="y">'),
    ("tab inside scheme", '<a href="jav&#9;ascript:alert(1)">x</a>'),
    ("leading space in scheme", "[c]( javascript:alert(1))"),
    ("data uri, markdown link", "[c](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)"),
    ("vbscript", "[c](vbscript:alert(1))"),
    ("iframe srcdoc", '<iframe srcdoc="<script>alert(1)</script>"></iframe>'),
    ("data uri raw href", '<a href="data:text/html,<script>alert(1)</script>">x</a>'),
]


@pytest.mark.parametrize("name,payload", PAYLOADS, ids=[n for n, _ in PAYLOADS])
def test_payload_is_neutralised(name, payload):
    _assert_safe(markdown_to_html(payload))


@pytest.mark.parametrize("name,payload", PAYLOADS, ids=[n for n, _ in PAYLOADS])
def test_payload_is_neutralised_inside_a_table_cell(name, payload):
    # This is the exact shape agent/nodes/answer.py builds from raw database rows: a
    # Markdown table with the value sitting in one cell.
    table = f"| value |\n|---|\n| {payload} |"
    _assert_safe(markdown_to_html(table))


def test_ordinary_answer_still_renders():
    out = markdown_to_html(
        "**13 customers** are from the USA.\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    )
    assert "<strong>13 customers</strong>" in out
    assert "<table>" in out
    assert "<td>1</td>" in out


def test_real_link_survives_with_a_safe_rel():
    out = markdown_to_html("[docs](https://example.com)")
    assert 'href="https://example.com"' in out
    assert "noopener" in out


def test_escape_text_strips_markup_entirely():
    assert escape_text("<b>bold</b>") == "bold"
    assert "&lt;" not in escape_text("<b>bold</b>")  # stripped, not encoded, by design


def test_as_frame_handles_duplicate_column_names():
    frame = as_frame({"columns": ["name", "name"], "rows": [("AC/DC", "Accept")]})
    assert frame is not None
    assert list(frame.columns) == ["name", "name (2)"]
    assert frame.iloc[0]["name"] == "AC/DC"
    assert frame.iloc[0]["name (2)"] == "Accept"


def test_as_frame_preserves_leading_zeros():
    frame = as_frame({"columns": ["postal_code"], "rows": [("01234",), ("00987",)]})
    assert frame["postal_code"].tolist() == ["01234", "00987"]


def test_as_frame_still_types_genuine_numbers():
    frame = as_frame({"columns": ["count"], "rows": [("5",), ("12",)]})
    assert frame["count"].tolist() == [5, 12]
    assert str(frame["count"].dtype) != "object"
