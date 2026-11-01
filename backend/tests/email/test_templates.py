import pytest

from app.services.email.templates import (
    InvalidTemplate,
    extract_placeholders,
    render,
    render_template,
    validate_variables,
)


def test_extract_placeholders_finds_dotted_paths():
    assert extract_placeholders("Hi {{customer.first_name}}, order {{order.number}} shipped") == {
        "customer.first_name",
        "order.number",
    }


def test_render_substitutes_known_values():
    out = render("Hi {{customer.first_name}}", {"customer": {"first_name": "Sam"}}, escape_html=False)
    assert out == "Hi Sam"


def test_unknown_placeholder_fails_safely_to_empty_string_never_left_literal():
    out = render("Hi {{customer.nickname}}!", {"customer": {"first_name": "Sam"}}, escape_html=False)
    assert out == "Hi !"
    assert "{{" not in out


def test_html_context_escapes_substituted_values_but_text_context_does_not():
    context = {"customer": {"first_name": "<script>alert(1)</script>"}}
    html_out = render("{{customer.first_name}}", context, escape_html=True)
    text_out = render("{{customer.first_name}}", context, escape_html=False)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "<script>" in text_out  # plain text has no markup to escape against


def test_structured_values_are_never_substituted_directly():
    out = render("{{customer}}", {"customer": {"first_name": "Sam"}}, escape_html=False)
    assert out == ""  # a dict/list value resolves to nothing, not str(dict)


def test_render_template_returns_all_three_rendered_fields():
    subject, html_out, text_out = render_template(
        subject="Hi {{customer.first_name}}",
        body_html="<p>{{customer.first_name}}</p>",
        body_text="Hi {{customer.first_name}}",
        context={"customer": {"first_name": "Sam"}},
    )
    assert subject == "Hi Sam"
    assert html_out == "<p>Sam</p>"
    assert text_out == "Hi Sam"


def test_validate_variables_accepts_a_fully_declared_template():
    validate_variables(
        ["customer.first_name", "order.number"],
        subject="Hi {{customer.first_name}}",
        body_html="<p>Order {{order.number}}</p>",
        body_text=None,
    )  # no raise


def test_validate_variables_rejects_an_undeclared_placeholder():
    with pytest.raises(InvalidTemplate):
        validate_variables(
            ["customer.first_name"],
            subject="Hi {{customer.first_name}}",
            body_html="<p>Secret: {{internal.admin_notes}}</p>",
            body_text=None,
        )


def test_render_tolerates_missing_or_empty_text_without_raising():
    assert render(None, {}, escape_html=False) == ""
    assert render("", {}, escape_html=False) == ""
