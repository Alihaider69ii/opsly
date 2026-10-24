import base64

import pytest

from app.services.integrations.shopify import scopes
from app.services.integrations.shopify.errors import InvalidShopDomain
from app.services.integrations.shopify.validation import (
    is_valid_shop_domain,
    normalize_shop_domain,
    oauth_query_message,
    parse_gid,
    parse_query_string,
    sign_oauth_query,
    sign_webhook_body,
    to_int_id,
    verify_oauth_hmac,
    verify_webhook_hmac,
)

SECRET = "shpss_test_secret"


# --- shop domains -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my-store.myshopify.com", "my-store.myshopify.com"),
        ("  My-Store.MyShopify.com  ", "my-store.myshopify.com"),
        ("https://my-store.myshopify.com", "my-store.myshopify.com"),
        ("https://my-store.myshopify.com/", "my-store.myshopify.com"),
        ("http://my-store.myshopify.com", "my-store.myshopify.com"),  # we always use https anyway
        ("my-store.myshopify.com/", "my-store.myshopify.com"),
        ("https://admin.shopify.com/store/My-Store", "my-store.myshopify.com"),
        ("https://admin.shopify.com/store/my-store/", "my-store.myshopify.com"),
        ("a.myshopify.com", "a.myshopify.com"),
        ("my-store.myshopify.com\r\n", "my-store.myshopify.com"),  # surrounding whitespace trimmed
        ("store123.myshopify.com", "store123.myshopify.com"),
    ],
)
def test_legitimate_store_addresses_are_normalized(raw, expected):
    assert normalize_shop_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # not a myshopify.com store at all
        "",
        "   ",
        "my-store",
        "myshopify.com",
        ".myshopify.com",
        "evil.com",
        "shopify.com",
        "my-store.shopify.com",
        # lookalikes and suffix tricks
        "my-store.myshopify.com.evil.com",
        "my-store.myshopify.com.",
        "evil.com/my-store.myshopify.com",
        "evil.com#my-store.myshopify.com",
        "evil.commyshopify.com",
        "evilmyshopify.com",
        "a.b.myshopify.com",
        "evil.com.myshopify.com",
        # credentials, ports, paths, queries
        "https://evil.com@my-store.myshopify.com",
        "https://my-store.myshopify.com@evil.com",
        "https://user:pass@my-store.myshopify.com",
        "https://my-store.myshopify.com:8443",
        "my-store.myshopify.com:443",
        "https://my-store.myshopify.com/admin",
        "my-store.myshopify.com/admin",
        "https://my-store.myshopify.com?next=//evil.com",
        "https://my-store.myshopify.com#frag",
        # internal and special hosts
        "localhost",
        "https://localhost",
        "127.0.0.1",
        "http://127.0.0.1:8000",
        "169.254.169.254",
        "http://169.254.169.254/latest/meta-data",
        "[::1]",
        "0.0.0.0",
        "10.0.0.1",
        "metadata.google.internal",
        # characters that have no business in a hostname
        "-bad.myshopify.com",
        "my_store.myshopify.com",
        "my store.myshopify.com",
        "my-store.myshopify.com\nHost: evil.com",
        "my-store.myshopify.com\r\nHost: evil.com",
        "my\\store.myshopify.com",
        "%6dy-store.myshopify.com",
        "ｍｙ-store.myshopify.com",  # fullwidth characters
        "my-\u212aore.myshopify.com",  # Kelvin sign: lower-cases to a plain ASCII "k"
        "\u0130stanbul.myshopify.com",  # dotted capital I
        "my-störe.myshopify.com",
        "ftp://my-store.myshopify.com",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://admin.shopify.com/foo",
        "https://admin.shopify.com/store/",
        "https://admin.shopify.com/store/a.b",
        "x" * 70 + ".myshopify.com",
        "a" * 300,
    ],
)
def test_everything_else_is_rejected(raw):
    with pytest.raises(InvalidShopDomain):
        normalize_shop_domain(raw)
    assert not is_valid_shop_domain(raw)


def test_non_strings_are_rejected():
    for bad in (None, 123, b"x.myshopify.com", ["a.myshopify.com"]):
        with pytest.raises(InvalidShopDomain):
            normalize_shop_domain(bad)  # type: ignore[arg-type]


# --- ids -----------------------------------------------------------------------------------------


def test_graphql_ids_are_parsed_only_for_the_expected_type():
    assert parse_gid("gid://shopify/Product/123", "Product") == 123
    assert parse_gid("gid://shopify/Product/123?foo=bar", "Product") == 123
    assert parse_gid("gid://shopify/Order/9007199254740993", "Order") == 9007199254740993
    for bad in (
        "gid://shopify/Product/123",  # wrong expected type below
        "gid://shopify/Product/abc",
        "gid://shopify/Product/",
        "gid://shopify/Product/-1",
        "gid://other/Product/1",
        "Product/1",
        "123",
        "",
        None,
        123,
        "gid://shopify/Product/" + "9" * 30,
    ):
        assert parse_gid(bad, "Variant") is None
    assert parse_gid("gid://shopify/Product/abc", "Product") is None
    assert parse_gid("gid://shopify/Product/" + "9" * 30, "Product") is None


def test_numeric_ids_from_webhook_payloads():
    assert to_int_id(123) == 123 and to_int_id("456") == 456
    assert to_int_id(2**62) == 2**62
    for bad in (
        0,
        -5,
        True,
        False,
        2**63,
        "abc",
        "12 3",
        "-1",
        "",
        None,
        1.5,
        [1],
        "9" * 20,
        "１２",
    ):
        assert to_int_id(bad) is None


# --- OAuth callback signature ------------------------------------------------------------------------


def signed(**params) -> dict[str, str]:
    query = {
        "code": "abc",
        "shop": "s.myshopify.com",
        "state": "xyz",
        "timestamp": "1700000000",
        **params,
    }
    query["hmac"] = sign_oauth_query(query, SECRET)
    return query


def test_a_correctly_signed_callback_verifies():
    assert verify_oauth_hmac(signed(), SECRET)


def test_the_signed_message_is_sorted_and_excludes_the_hmac():
    query = {"z": "1", "a": "2", "hmac": "ignored"}
    assert oauth_query_message(query) == "a=2&z=1"


def test_any_change_to_any_parameter_breaks_the_signature():
    base = signed()
    for key in ("code", "shop", "state", "timestamp"):
        assert not verify_oauth_hmac({**base, key: base[key] + "x"}, SECRET), key
    assert not verify_oauth_hmac({**base, "extra": "1"}, SECRET)  # an added parameter
    without = {k: v for k, v in base.items() if k != "state"}
    assert not verify_oauth_hmac(without, SECRET)  # a removed parameter


def test_wrong_secret_missing_or_malformed_hmac_all_fail():
    good = signed()
    assert not verify_oauth_hmac(good, "other-secret")
    assert not verify_oauth_hmac(good, "")
    assert not verify_oauth_hmac({k: v for k, v in good.items() if k != "hmac"}, SECRET)
    assert not verify_oauth_hmac({**good, "hmac": ""}, SECRET)
    assert not verify_oauth_hmac({**good, "hmac": "zz"}, SECRET)
    assert not verify_oauth_hmac({**good, "hmac": good["hmac"][:-1]}, SECRET)


def test_signature_comparison_is_case_insensitive_on_hex_only():
    good = signed()
    assert verify_oauth_hmac({**good, "hmac": good["hmac"].upper()}, SECRET)


def test_duplicate_query_parameters_are_refused():
    with pytest.raises(InvalidShopDomain):
        parse_query_string("shop=a.myshopify.com&shop=evil.myshopify.com&hmac=x")
    assert parse_query_string("a=1&b=&c=%20x") == {"a": "1", "b": "", "c": " x"}


# --- webhook signature --------------------------------------------------------------------------------


def test_webhook_signature_round_trip_and_rejections():
    body = b'{"id": 123, "title": "x"}'
    header = sign_webhook_body(body, SECRET)
    assert verify_webhook_hmac(body, header, SECRET)
    assert not verify_webhook_hmac(body + b" ", header, SECRET)  # body changed by one byte
    assert not verify_webhook_hmac(body, header, "other")
    assert not verify_webhook_hmac(body, None, SECRET)
    assert not verify_webhook_hmac(body, "", SECRET)
    assert not verify_webhook_hmac(body, "not base64 !!!", SECRET)
    assert not verify_webhook_hmac(body, header[:-2], SECRET)
    assert not verify_webhook_hmac(body, header, "")
    # the signature is over the raw bytes: re-serialized JSON with the same meaning does not verify
    assert not verify_webhook_hmac(b'{"title":"x","id":123}', header, SECRET)
    # hex is not an accepted encoding (Shopify uses base64)
    assert not verify_webhook_hmac(body, base64.b64decode(header).hex(), SECRET)


# --- scopes ---------------------------------------------------------------------------------------------


def test_scopes_are_centralized_minimal_and_read_only():
    assert scopes.DEFAULT_SCOPES == ("read_products", "read_customers", "read_orders")
    assert all(s.startswith("read_") for s in scopes.DEFAULT_SCOPES)  # nothing that can write
    assert "read_all_orders" not in scopes.DEFAULT_SCOPES  # needs separate approval; not needed yet
    assert scopes.REQUIRED_SCOPES == {"read_products"}
    assert all(info.why for info in scopes.SCOPES)  # every scope is justified


def test_scope_parsing_and_coverage():
    granted = scopes.parse_scopes(" read_products, read_orders ,,")
    assert granted == {"read_products", "read_orders"}
    assert scopes.covers(granted, "read_products")
    assert not scopes.covers(granted, "read_customers")
    assert scopes.covers(granted, "")  # topics that need no scope
    # Shopify: a write scope implies the matching read scope
    assert scopes.covers(frozenset({"write_customers"}), "read_customers")
    assert not scopes.covers(frozenset({"read_customers"}), "write_customers")
    assert scopes.scope_string({"b", "a"}) == "a,b"
    assert scopes.parse_scopes(None) == frozenset()


def test_every_webhook_topic_names_a_known_scope():
    known = {i.scope for i in scopes.SCOPES} | {""}
    assert set(scopes.WEBHOOK_TOPIC_SCOPES.values()) <= known
