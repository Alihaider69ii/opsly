"""Module boundaries Phase 4 promised, enforced by reading imports (no code is executed).

* The AI agent, RAG and the chat code do not call Shopify. Phase 5 will let them read the imported
  tables through ``services/commerce``, never through the integration.
* The read service for commerce data does not depend on the integration that fills it.
* The Shopify integration does not depend on the AI layer.
* No application code branches on a vendor name (providers are data in the catalog).
"""

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def python_files(package: str):
    return sorted((APP / package).rglob("*.py"))


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def offenders(package: str, forbidden: str):
    return [
        f"{path.relative_to(APP)} imports {module}"
        for path in python_files(package)
        for module in imported_modules(path)
        if module == forbidden or module.startswith(forbidden + ".")
    ]


@pytest.mark.parametrize(
    "package",
    ["services/ai", "services/chat", "services/rag", "api/public_chat", "api/chat"],
)
def test_ai_and_chat_code_never_import_the_shopify_integration(package):
    assert offenders(package, "app.services.integrations") == []
    assert offenders(package, "app.api.integrations") == []


def test_the_commerce_read_service_does_not_depend_on_the_integration_that_fills_it():
    assert offenders("services/commerce", "app.services.integrations") == []


def test_the_shopify_integration_does_not_depend_on_the_ai_layer():
    assert offenders("services/integrations", "app.services.ai") == []
    assert offenders("services/integrations", "app.services.chat") == []


def test_the_secret_manager_depends_on_no_service_so_any_integration_can_reuse_it():
    modules = set(imported_modules(APP / "core" / "secrets.py"))
    assert not [m for m in modules if m.startswith("app.services")], modules


def test_configuration_reads_only_pure_data_modules_from_services():
    """``core/config.py`` may read the provider catalog and the scope list (plain data that import
    nothing from the app); it must not pull in any service logic."""
    modules = {
        m for m in imported_modules(APP / "core" / "config.py") if m.startswith("app.services")
    }
    assert modules <= {
        "app.services.ai.catalog",
        "app.services.email.catalog",
        "app.services.integrations.shopify.scopes",
        "app.services.integrations.shopify.versioning",
        "app.services.messaging.catalog",
    }, modules


def test_no_application_code_branches_on_an_ai_vendor_name():
    """Vendor differences are fields of the provider catalog, not ``if provider == "openai"``."""
    vendor = re.compile(
        r"""(?:==|!=|\bin\b|\bis\b)\s*\(?\s*["'](openai|anthropic|openrouter|groq|together|deepseek|mistral|gemini)["']"""
        r"""|["'](openai|anthropic|openrouter|groq|together|deepseek|mistral|gemini)["']\s*(?:==|!=)"""
    )
    allowed = {APP / "services" / "ai" / "catalog.py"}
    found = [
        f"{path.relative_to(APP)}:{number}: {line.strip()}"
        for path in sorted(APP.rglob("*.py"))
        if path not in allowed
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if vendor.search(line)
    ]
    assert found == []
