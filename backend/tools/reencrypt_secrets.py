"""Check, and finish, an encryption-key rotation for the secrets stored in the database.

BizFlow encrypts Shopify tokens with ``SECRET_ENCRYPTION_KEY`` and names the key in each stored
envelope, so a new key can be introduced without downtime (see ``app/core/secrets.py``). The old key
must stay configured (``SECRET_ENCRYPTION_PREVIOUS_KEYS``) until nothing is encrypted with it any
more. Token refresh re-encrypts active stores within an hour, but nothing else proves that, and a
key that is retired too early makes every remaining token unreadable. This tool tells you.

    cd backend
    python -m tools.reencrypt_secrets            # report only: how many secrets use which key
    python -m tools.reencrypt_secrets --apply    # re-encrypt every secret under the current key

Exit status is 0 when every stored secret is readable and encrypted under the current key, 1 when
some still use an old key (safe to rerun with ``--apply``) or cannot be read, 2 when the encryption
key is not configured. Only key ids and counts are printed, never a token, a ciphertext or a key.
"""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.config import get_settings
from app.core.secrets import SecretError, SecretManager
from app.db.database import SessionLocal
from app.db.models import ShopifyConnection
from app.services.integrations.shopify.auth import SessionFactory, token_context

logger = logging.getLogger(__name__)

# (context kind, column on ShopifyConnection)
SECRET_COLUMNS = (
    ("access_token", "access_token_encrypted"),
    ("refresh_token", "refresh_token_encrypted"),
)


@dataclass
class Report:
    by_key: Counter[str] = field(default_factory=Counter)
    needs_rotation: int = 0
    rotated: int = 0
    unreadable: int = 0

    @property
    def remaining(self) -> int:
        return self.needs_rotation - self.rotated

    @property
    def clean(self) -> bool:
        return self.remaining == 0 and self.unreadable == 0


def reencrypt(session_factory: SessionFactory, secrets: SecretManager, *, apply: bool) -> Report:
    """One short transaction (and row lock) per connection, so a running worker is never blocked
    for long and a token refreshed meanwhile is read again under the lock."""
    report = Report()
    with session_factory() as db:
        connection_ids = list(db.scalars(select(ShopifyConnection.id)))
    for connection_id in connection_ids:
        with session_factory() as db:
            connection = db.scalar(
                select(ShopifyConnection)
                .where(ShopifyConnection.id == connection_id)
                .with_for_update()
            )
            if connection is None:
                continue
            changed = False
            for kind, column in SECRET_COLUMNS:
                stored = getattr(connection, column)
                if not stored:
                    continue
                try:
                    report.by_key[secrets.key_id_of(stored)] += 1
                    if not secrets.needs_rotation(stored):
                        secrets.decrypt(stored, context=token_context(kind, connection.id))
                        continue
                    report.needs_rotation += 1
                    if not apply:
                        continue
                    replacement = secrets.rotate(stored, context=token_context(kind, connection.id))
                except SecretError:
                    report.unreadable += 1
                    continue
                setattr(connection, column, replacement)
                report.rotated += 1
                changed = True
            if changed:
                db.commit()
    return report


def render(report: Report, secrets: SecretManager, *, apply: bool) -> str:
    lines = [f"current key id: {secrets.current_key_id}"]
    for key_id, count in sorted(report.by_key.items()):
        marker = "current" if key_id == secrets.current_key_id else "OLD"
        lines.append(f"  stored secrets under key '{key_id}' ({marker}): {count}")
    if not report.by_key:
        lines.append("  no stored secrets")
    if apply:
        lines.append(f"re-encrypted under the current key: {report.rotated}")
    lines.append(f"still under an old key: {report.remaining}")
    lines.append(f"unreadable (wrong/missing key or tampered): {report.unreadable}")
    if report.clean:
        lines.append("OK: every stored secret is readable and uses the current key.")
        lines.append("It is now safe to remove retired keys from SECRET_ENCRYPTION_PREVIOUS_KEYS.")
    elif report.unreadable:
        lines.append(
            "ACTION: some secrets cannot be read. If a previous key was removed too early, put it "
            "back in SECRET_ENCRYPTION_PREVIOUS_KEYS; affected stores must otherwise reconnect."
        )
    else:
        lines.append(
            "ACTION: run again with --apply to re-encrypt them; do not retire old keys yet."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="re-encrypt (default: report only)")
    args = parser.parse_args(argv)
    try:
        secrets = SecretManager.from_settings(get_settings())
    except SecretError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    report = reencrypt(SessionLocal, secrets, apply=args.apply)
    print(render(report, secrets, apply=args.apply))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
