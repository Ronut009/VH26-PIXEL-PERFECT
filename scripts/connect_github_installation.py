"""Connect an already-installed GitHub App without waiting for a webhook.

    .venv/Scripts/python.exe scripts/connect_github_installation.py <installation-id>

Installation state normally arrives by webhook, and that is the right steady
state - GitHub tells PulseGraph when an App is installed, suspended, or has its
repository selection changed. It is a poor *first* run, though: until a public
tunnel exists, a correctly installed App cannot be connected at all, and the
symptom is indistinguishable from a broken installation.

This asks GitHub directly for the same object the `installation` webhook
carries, stores it through exactly the same code path, and then syncs the
repository list. Webhooks keep it current afterwards; nothing here replaces
them.

Everything it does is read-only against GitHub: one GET for the installation,
one token exchange, one GET for the repositories.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiosqlite  # noqa: E402

from src.config import settings  # noqa: E402
from src.github_integration.client import (  # noqa: E402
    GitHubIntegrationError,
    GitHubReadOnlyClient,
)
from src.github_integration.router import _metadata_payload  # noqa: E402
from src.github_integration.store import (  # noqa: E402
    GitHubStoreError,
    get_installation,
    replace_installation_repositories,
    upsert_installation_from_webhook,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: connect_github_installation.py <installation-id>")
        print("  the id is in the URL of github.com/settings/installations/<id>")
        return 2

    installation_id = int(sys.argv[1])

    if not settings.github_app_is_configured:
        print("[fail] GITHUB_APP_CLIENT_ID and GITHUB_APP_PRIVATE_KEY are not set in .env")
        return 1

    client = GitHubReadOnlyClient(
        settings.github_app_issuer,
        settings.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n"),
        timeout=settings.GITHUB_REQUEST_TIMEOUT_SECONDS,
        api_version=settings.GITHUB_API_VERSION,
    )

    try:
        try:
            installation = await client.get_app_installation(installation_id)
        except GitHubIntegrationError as exc:
            print(f"[fail] GitHub rejected the installation lookup: {exc}")
            if "401" in str(exc):
                # A 401 here reads like a bad key, and almost never is. GitHub
                # says "A JSON web token could not be decoded" when it cannot
                # resolve the issuer to an App - the signature is never even
                # checked - so the usual cause is the issuer, not the key.
                print()
                print("  A 401 on an App JWT is usually the issuer, not the key.")
                print(f"  Current issuer: {settings.github_app_issuer}")
                if not settings.GITHUB_APP_ID:
                    print("  This is the Client ID. GitHub documents both the Client ID")
                    print("  and the numeric App ID, but only the App ID is accepted")
                    print("  everywhere.")
                    print()
                    print("  Fix: copy the App ID (a number) from")
                    print(f"       github.com/settings/apps/{settings.GITHUB_APP_SLUG}")
                    print("       General tab -> 'App ID', then set GITHUB_APP_ID in .env")
                print()
                print("  Ruled out already if you ran the diagnostics: key parses and")
                print("  signs, matches the downloaded PEM, and the clock is in sync.")
            return 1

        selection = installation.get("repository_selection")
        account = (installation.get("account") or {}).get("login", "?")
        print(f"  installation {installation_id} on @{account}")
        print(f"  repository_selection: {selection}")
        print(f"  permissions: {dict(installation.get('permissions') or {})}")

        if selection != "selected":
            # PulseGraph fails closed on a broader installation than the
            # read-only MVP needs. This is a deliberate least-privilege stance,
            # not a limitation to work around.
            print()
            print("[fail] This App is installed on ALL repositories.")
            print("       PulseGraph only accepts an installation scoped to selected")
            print("       repositories - it is a read-only tool and asking for more")
            print("       access than it needs undermines that claim.")
            print()
            print(f"       Fix: github.com/settings/installations/{installation_id}")
            print("            Repository access -> Only select repositories")
            print("            -> pick the repo(s) -> Save, then re-run this.")
            return 1

        db_path = str(ROOT / settings.DATABASE_PATH)
        connection = await aiosqlite.connect(db_path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                stored_id, applied = await upsert_installation_from_webhook(
                    connection, {"installation": installation}, "created"
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

            record = await get_installation(connection, stored_id)
            print(f"  stored installation status: {record['status']}")
            if record["status"] != "active":
                print(f"[fail] installation is not usable: {record['status']}")
                return 1

            token = await client.create_installation_token(stored_id)
            repositories = await client.list_installation_repositories(token)

            await connection.execute("BEGIN IMMEDIATE")
            try:
                repository_ids = await replace_installation_repositories(
                    connection,
                    installation_id=stored_id,
                    expected_state_revision=int(record["state_revision"]),
                    # Reuse the router's adapter rather than a second copy:
                    # two shapes for the same persistence contract is how
                    # they drift apart.
                    repositories=[_metadata_payload(r) for r in repositories],
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        finally:
            await connection.close()

        print()
        print(f"[ ok ] connected {len(repository_ids)} repository(ies):")
        for repository in repositories:
            print(f"         {repository.full_name} (default branch {repository.default_branch})")
        print()
        print("  Restart the backend so it picks up the connection, then open")
        print("  the dashboard -> Code Investigation.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
