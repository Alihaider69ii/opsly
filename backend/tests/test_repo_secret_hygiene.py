"""No secret can reach the repository: the ignore rules, the env templates and the source files.

Works on the files git would actually commit (tracked, or untracked-but-not-ignored), so it reflects
what the next ``git add`` could publish.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2", ".pdf", ".ttf"}
LOCKFILES = {"package-lock.json"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def committable_files() -> list[Path]:
    result = git("ls-files", "--cached", "--others", "--exclude-standard")
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [REPO / line for line in result.stdout.splitlines() if line]


def readable(path: Path) -> str | None:
    if path.suffix.lower() in BINARY or path.name in LOCKFILES or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def in_tests(path: Path) -> bool:
    parts = path.relative_to(REPO).parts
    return "tests" in parts or "__tests__" in parts or path.name.endswith((".test.ts", ".test.tsx"))


# Formats a deliberately fake test value does not resemble.
UNIVERSAL = {
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "an AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "a Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "a GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{36}\b",
    "a Slack token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "a Stripe live key": r"\b[sr]k_live_[A-Za-z0-9]{16,}",
}
# Plausible-looking credentials: fine as obvious fixtures in tests, never in shipped files.
SHIPPED_ONLY = {
    "a Shopify token": r"\bshp(?:at|ca|ss|pa|rt)_[A-Za-z0-9]{16,}",
    "an API key (sk-...)": r"\bsk-[A-Za-z0-9_-]{16,}",
    "a JWT": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
}
# Documentation placeholders for URL credentials.
URL_PLACEHOLDER_USERS = {"user", "username", "app", "bizflow", "postgres"}
URL_PLACEHOLDER_PASSWORDS = {"pass", "password", "bizflow", "changeme", "change-me", "secret"}


def test_the_checkout_contains_files_to_scan():
    assert len(committable_files()) > 100


def test_no_file_that_could_be_committed_contains_a_credential():
    problems = []
    for path in committable_files():
        text = readable(path)
        if text is None:
            continue
        patterns = dict(UNIVERSAL) if in_tests(path) else {**UNIVERSAL, **SHIPPED_ONLY}
        for what, pattern in patterns.items():
            if re.search(pattern, text):
                problems.append(f"{path.relative_to(REPO)}: looks like {what}")
    assert problems == []


def test_urls_with_embedded_credentials_are_only_documentation_placeholders():
    problems = []
    for path in committable_files():
        text = readable(path)
        if text is None or in_tests(path):
            continue
        for match in re.finditer(r"://(?P<user>[^/\s:@]+):(?P<password>[^/\s@]{4,})@", text):
            user, password = match["user"].lower(), match["password"].lower()
            if "$" in password or "{" in password or "<" in password or "*" in password:
                continue  # a template, an interpolation or a mask
            if user in URL_PLACEHOLDER_USERS and password in URL_PLACEHOLDER_PASSWORDS:
                continue
            problems.append(f"{path.relative_to(REPO)}: {match['user']}:***@")
    assert problems == []


def test_no_environment_file_other_than_the_templates_can_be_committed():
    offenders = [
        str(p.relative_to(REPO))
        for p in committable_files()
        if p.name.startswith(".env") and not p.name.endswith(".example")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "backend/.env",
        "backend/.env.production",
        "frontend/.env.local",
        "widget/.env.local",
        "certs/server.pem",
        "deploy/private.key",
        "secrets/shopify.txt",
        "request-1234567890.json",
        "backend/bizflow.db",
        "backend/dev.sqlite3",
    ],
)
def test_secret_bearing_paths_are_ignored_by_git(path):
    assert git("check-ignore", "--quiet", path).returncode == 0, f"{path} is not git-ignored"


@pytest.mark.parametrize(
    "path",
    [".env.example", "backend/.env.example", "frontend/.env.example", ".env.production.example"],
)
def test_the_templates_are_not_ignored(path):
    assert git("check-ignore", "--quiet", path).returncode == 1


# --- the templates themselves ---------------------------------------------------------------------------------

SECRETISH = re.compile(r"(SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE|ENCRYPTION_KEY)", re.I)
NOT_A_CREDENTIAL = re.compile(r"(_MINUTES|_SECONDS|_BYTES|_LIMIT|_FIELD|_ID|_DIMENSIONS)$")
DEV_DEFAULTS = {"bizflow"}  # the docker-compose development database credentials


def template_assignments(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        yield number, name.strip(), value.strip().strip("\"'")


def templates() -> list[Path]:
    return [
        p for p in committable_files() if p.name.startswith(".env") and p.name.endswith(".example")
    ]


def test_there_are_templates_to_check():
    assert len(templates()) >= 4


@pytest.mark.parametrize("template", templates(), ids=lambda p: str(p.relative_to(REPO)))
def test_template_secrets_are_blank_never_filled_in(template):
    problems = []
    for number, name, value in template_assignments(template):
        if SECRETISH.search(name) and not NOT_A_CREDENTIAL.search(name):
            if not value or value in DEV_DEFAULTS or value.replace(".", "", 1).isdigit():
                continue  # blank, a dev default, or a plain number (a size or a lifetime)
            if not re.fullmatch(r"(<.*>|change-?me|your[-_].*|xxx+|\*+)", value, re.I):
                problems.append(f"{template.name}:{number} {name} has a value")
    assert problems == []


@pytest.mark.parametrize("template", templates(), ids=lambda p: str(p.relative_to(REPO)))
def test_templates_never_default_to_a_production_environment(template):
    for _number, name, value in template_assignments(template):
        if name == "ENVIRONMENT":
            assert value == "development"
        if name in {"LLM_PROVIDER", "EMBEDDING_PROVIDER"}:
            assert value in {"", "fake"}  # a real vendor must be a deliberate choice


def test_browser_visible_variables_hold_no_secret_names():
    """Anything prefixed NEXT_PUBLIC_ is compiled into the page for every visitor."""
    problems = []
    for path in committable_files():
        text = readable(path)
        if text is None or in_tests(path) or path.suffix == ".md":
            continue
        for name in re.findall(r"NEXT_PUBLIC_[A-Z0-9_]+", text):
            if SECRETISH.search(name):
                problems.append(f"{path.relative_to(REPO)}: {name}")
    assert problems == []


def test_the_frontend_and_widget_never_reference_server_side_secrets():
    server_only = re.compile(
        r"(SECRET_ENCRYPTION_KEY|SHOPIFY_CLIENT_SECRET|JWT_SECRET_KEY|LLM_API_KEY|"
        r"EMBEDDING_API_KEY|OPENAI_API_KEY|DATABASE_URL|REDIS_URL)"
    )
    problems = []
    for path in committable_files():
        parts = path.relative_to(REPO).parts
        if parts[0] not in {"frontend", "widget"} or in_tests(path) or path.suffix == ".md":
            continue
        text = readable(path)
        if text and server_only.search(text) and path.name != ".env.example":
            problems.append(str(path.relative_to(REPO)))
    assert problems == []
