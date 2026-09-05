"""A deliberately narrow, read-only GitHub App REST client.

The client only uses GitHub's Git data and repository metadata endpoints.  The
one POST is the GitHub-required exchange of an App JWT for a short-lived,
contents-read installation token.  It intentionally has no generic request
method and no repository mutation methods.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx
import jwt


DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_TREE_ENTRIES = 100_000
DEFAULT_MAX_TREE_REQUESTS = 2_000
DEFAULT_MAX_TREE_DEPTH = 64


class GitHubIntegrationError(Exception):
    """Base exception for a GitHub integration failure.

    Error messages deliberately omit request headers and response bodies so an
    installation token or App JWT cannot be accidentally logged by callers.
    """


class GitHubConfigurationError(GitHubIntegrationError):
    """Raised when client credentials or an API argument is unsafe/invalid."""


class GitHubPayloadError(GitHubIntegrationError):
    """Raised when a successful GitHub response is missing required fields."""


class GitHubTransportError(GitHubIntegrationError):
    """Raised when GitHub cannot be reached."""


class GitHubTimeoutError(GitHubTransportError):
    """Raised when a GitHub request exceeds its configured timeout."""


class GitHubAPIError(GitHubIntegrationError):
    """A non-success response from GitHub's REST API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


class GitHubAuthenticationError(GitHubAPIError):
    """GitHub rejected the App JWT or installation token."""


class GitHubNotFoundError(GitHubAPIError):
    """The selected repository, ref, tree, or blob was not found."""


class GitHubRateLimitError(GitHubAPIError):
    """GitHub's primary or secondary rate limit was reached."""


class GitHubTreeLimitExceeded(GitHubIntegrationError):
    """A complete tree walk would exceed the configured safe entry bound."""


@dataclass(frozen=True, slots=True)
class InstallationAccessToken:
    """A short-lived installation token returned by GitHub.

    The token field is hidden from ``repr`` to reduce accidental credential
    exposure in logs.  Callers should keep it in memory only.
    """

    token: str = field(repr=False)
    expires_at: datetime
    permissions: Mapping[str, str]
    repository_selection: str


@dataclass(frozen=True, slots=True)
class RepositoryMetadata:
    id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool
    html_url: str | None


@dataclass(frozen=True, slots=True)
class BranchReference:
    """The immutable Git commit and root tree behind a named branch."""

    name: str
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    path: str
    mode: str
    type: str
    sha: str
    size: int | None
    url: str | None


@dataclass(frozen=True, slots=True)
class GitTree:
    sha: str
    entries: tuple[GitTreeEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class GitBlob:
    sha: str
    content: bytes
    size: int
    url: str | None

    def text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        """Decode a source blob only when the caller explicitly needs text."""

        return self.content.decode(encoding, errors)


class GitHubReadOnlyClient:
    """Authenticate a GitHub App and retrieve repository source data safely.

    ``http_client`` and ``base_url`` are injectable for GitHub Enterprise and
    mock-transport tests.  An injected client is never closed by this class.
    All public source-data methods require an explicit installation token,
    which keeps token lifetime and persistence decisions outside this client.
    """

    def __init__(
        self,
        app_id: str | int,
        private_key_pem: str | bytes,
        *,
        base_url: str = DEFAULT_GITHUB_API_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        api_version: str = DEFAULT_GITHUB_API_VERSION,
    ) -> None:
        self._app_id = self._validate_nonempty_text(str(app_id), "app_id")
        self._private_key_pem = self._validate_private_key(private_key_pem)
        self._base_url = self._validate_base_url(base_url)
        self._api_version = self._validate_nonempty_text(api_version, "api_version")
        self._timeout = self._validate_timeout(timeout)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "GitHubReadOnlyClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close only the HTTP client created internally by this instance."""

        if self._owns_client:
            await self._client.aclose()

    def create_app_jwt(self, *, now: datetime | None = None) -> str:
        """Create a GitHub App JWT signed with RS256.

        GitHub requires an ``iss`` claim containing the app ID and accepts JWTs
        whose expiry is no more than ten minutes in the future.  Backdating
        ``iat`` by one minute tolerates small clock skew.
        """

        issued_now = now or datetime.now(timezone.utc)
        if issued_now.tzinfo is None:
            raise GitHubConfigurationError("JWT timestamp must be timezone-aware")

        now_seconds = int(issued_now.timestamp())
        claims = {
            "iat": now_seconds - 60,
            "exp": now_seconds + 540,
            "iss": self._app_id,
        }
        try:
            encoded = jwt.encode(claims, self._private_key_pem, algorithm="RS256")
        except Exception as exc:  # PyJWT intentionally hides crypto backend details.
            raise GitHubConfigurationError("unable to sign GitHub App JWT") from exc
        return encoded

    async def create_installation_token(
        self,
        installation_id: int,
        *,
        repository_ids: Sequence[int] | None = None,
    ) -> InstallationAccessToken:
        """Exchange an App JWT for a short-lived, contents-read token.

        This is the sole POST in this client because GitHub mandates it for App
        installation authentication.  The requested token is restricted to
        read-only contents access; any write permission in GitHub's response is
        rejected rather than returned to the caller.
        """

        installation_id = self._validate_positive_int(installation_id, "installation_id")
        body: dict[str, Any] = {"permissions": {"contents": "read"}}
        if repository_ids is not None:
            body["repository_ids"] = self._validate_repository_ids(repository_ids)

        payload = await self._request_json(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers=self._app_auth_headers(),
            json_body=body,
        )
        token = self._require_text(payload, "token")
        expires_at = self._parse_timestamp(self._require_text(payload, "expires_at"))
        if expires_at <= datetime.now(timezone.utc):
            raise GitHubAuthenticationError(
                "GitHub returned an already-expired installation token",
                status_code=200,
            )
        permissions = self._parse_permissions(payload.get("permissions"))
        self._ensure_read_only_permissions(permissions)
        repository_selection = self._require_text(payload, "repository_selection")
        if repository_selection != "selected":
            raise GitHubAuthenticationError(
                "GitHub installation token is not limited to selected repositories",
                status_code=200,
            )
        return InstallationAccessToken(
            token=token,
            expires_at=expires_at,
            permissions=permissions,
            repository_selection=repository_selection,
        )

    async def get_app_installation(self, installation_id: int) -> Mapping[str, Any]:
        """Read one installation's metadata using the App JWT.

        Installation state normally arrives by webhook, which needs a publicly
        reachable URL. That is a reasonable production assumption and a poor
        first-run one: until a tunnel exists, a correctly installed App cannot
        be connected at all, and the failure looks identical to a broken
        install. This is the same object the `installation` webhook carries, so
        it can seed the connection directly and let webhooks keep it current.

        Read-only: a plain GET, with no side effect on GitHub.
        """

        installation_id = self._validate_positive_int(installation_id, "installation_id")
        return await self._request_json(
            "GET",
            f"/app/installations/{installation_id}",
            headers=self._app_auth_headers(),
        )

    # GitHub documentation uses both names in different contexts.  Keeping the
    # alias avoids consumers guessing whether "access" is part of the name.
    create_installation_access_token = create_installation_token

    async def list_installation_repositories(
        self,
        installation_token: str | InstallationAccessToken,
        *,
        per_page: int = 100,
        max_pages: int = 100,
    ) -> tuple[RepositoryMetadata, ...]:
        """List every repository visible to an installation, with a hard bound."""

        per_page = self._validate_page_size(per_page)
        max_pages = self._validate_positive_int(max_pages, "max_pages")
        headers = self._installation_auth_headers(installation_token)

        repositories: list[RepositoryMetadata] = []
        expected_total: int | None = None
        for page in range(1, max_pages + 1):
            payload = await self._request_json(
                "GET",
                "/installation/repositories",
                headers=headers,
                params={"per_page": per_page, "page": page},
            )
            if expected_total is None:
                expected_total = self._require_nonnegative_int(payload, "total_count")

            items = payload.get("repositories")
            if not isinstance(items, list):
                raise GitHubPayloadError("GitHub response field 'repositories' must be a list")
            repositories.extend(self._repository_from_payload(item) for item in items)

            if len(repositories) >= expected_total:
                return tuple(repositories[:expected_total])
            if not items:
                raise GitHubPayloadError(
                    "GitHub repository pagination ended before the advertised total"
                )

        raise GitHubTreeLimitExceeded(
            "installation repository pagination exceeded its configured maximum pages"
        )

    async def get_repository_metadata(
        self,
        owner: str,
        repository: str,
        installation_token: str | InstallationAccessToken,
    ) -> RepositoryMetadata:
        """Fetch a repository and its configured default branch."""

        payload = await self._request_json(
            "GET",
            self._repository_path(owner, repository),
            headers=self._installation_auth_headers(installation_token),
        )
        return self._repository_from_payload(payload)

    # A concise alias for callers that already know the returned shape.
    get_repository = get_repository_metadata

    async def get_branch(
        self,
        owner: str,
        repository: str,
        branch: str,
        installation_token: str | InstallationAccessToken,
    ) -> BranchReference:
        """Get a branch's immutable commit SHA and root tree SHA."""

        payload = await self._request_json(
            "GET",
            f"{self._repository_path(owner, repository)}/branches/{self._quote_segment(branch, 'branch')}",
            headers=self._installation_auth_headers(installation_token),
        )
        commit = self._require_mapping(payload.get("commit"), "commit")
        commit_detail = self._require_mapping(commit.get("commit"), "commit.commit")
        tree = self._require_mapping(commit_detail.get("tree"), "commit.commit.tree")
        response_name = self._require_text(payload, "name")
        if response_name != branch:
            raise GitHubPayloadError("GitHub branch name did not match the requested branch")
        return BranchReference(
            name=response_name,
            commit_sha=self._require_text(commit, "sha"),
            tree_sha=self._require_text(tree, "sha"),
        )

    async def resolve_ref(
        self,
        owner: str,
        repository: str,
        ref: str,
        installation_token: str | InstallationAccessToken,
    ) -> str:
        """Resolve a branch, tag, or commit ref to an immutable commit SHA."""

        payload = await self._request_json(
            "GET",
            f"{self._repository_path(owner, repository)}/commits/{self._quote_segment(ref, 'ref')}",
            headers=self._installation_auth_headers(installation_token),
        )
        return self._require_text(payload, "sha")

    async def get_recursive_tree(
        self,
        owner: str,
        repository: str,
        tree_sha: str,
        installation_token: str | InstallationAccessToken,
    ) -> GitTree:
        """Fetch GitHub's recursive tree result, preserving its truncation flag."""

        return await self._get_tree(
            owner,
            repository,
            tree_sha,
            installation_token,
            recursive=True,
        )

    async def get_complete_tree(
        self,
        owner: str,
        repository: str,
        tree_sha: str,
        installation_token: str | InstallationAccessToken,
        *,
        max_entries: int = DEFAULT_MAX_TREE_ENTRIES,
        max_tree_requests: int = DEFAULT_MAX_TREE_REQUESTS,
        max_tree_depth: int = DEFAULT_MAX_TREE_DEPTH,
    ) -> GitTree:
        """Fetch a complete source tree without silently accepting truncation.

        GitHub's recursive tree endpoint may truncate large repositories.  On a
        truncated response this method walks non-recursive tree objects and
        returns fully prefixed paths, bounded by ``max_entries`` so one unusually
        large repository cannot exhaust the service.
        """

        max_entries = self._validate_positive_int(max_entries, "max_entries")
        max_tree_requests = self._validate_positive_int(
            max_tree_requests, "max_tree_requests"
        )
        max_tree_depth = self._validate_positive_int(max_tree_depth, "max_tree_depth")
        recursive_tree = await self.get_recursive_tree(
            owner, repository, tree_sha, installation_token
        )
        if not recursive_tree.truncated:
            if len(recursive_tree.entries) > max_entries:
                raise GitHubTreeLimitExceeded(
                    f"GitHub tree contains more than the configured {max_entries} entries"
                )
            return recursive_tree

        headers = self._installation_auth_headers(installation_token)
        collected: list[GitTreeEntry] = []
        active_tree_shas: set[str] = set()
        tree_request_count = 0

        async def walk(current_tree_sha: str, prefix: str = "", depth: int = 0) -> None:
            nonlocal tree_request_count
            if depth > max_tree_depth:
                raise GitHubTreeLimitExceeded(
                    f"GitHub tree walk exceeded the configured depth of {max_tree_depth}"
                )
            if current_tree_sha in active_tree_shas:
                raise GitHubPayloadError("GitHub tree walk contained a cyclic tree reference")
            active_tree_shas.add(current_tree_sha)
            try:
                tree_request_count += 1
                if tree_request_count > max_tree_requests:
                    raise GitHubTreeLimitExceeded(
                        "GitHub tree walk exceeded the configured tree-object request limit"
                    )
                tree = await self._get_tree(
                    owner,
                    repository,
                    current_tree_sha,
                    installation_token,
                    recursive=False,
                    headers=headers,
                )
                if tree.truncated:
                    raise GitHubTreeLimitExceeded(
                        "GitHub truncated a non-recursive tree during the complete tree walk"
                    )
                for entry in tree.entries:
                    full_path = f"{prefix}/{entry.path}" if prefix else entry.path
                    collected.append(
                        GitTreeEntry(
                            path=full_path,
                            mode=entry.mode,
                            type=entry.type,
                            sha=entry.sha,
                            size=entry.size,
                            url=entry.url,
                        )
                    )
                    if len(collected) > max_entries:
                        raise GitHubTreeLimitExceeded(
                            f"GitHub tree walk exceeded the configured {max_entries} entries"
                        )
                    if entry.type == "tree":
                        await walk(entry.sha, full_path, depth + 1)
            finally:
                active_tree_shas.remove(current_tree_sha)

        await walk(tree_sha)
        return GitTree(sha=tree_sha, entries=tuple(collected), truncated=False)

    async def get_blob(
        self,
        owner: str,
        repository: str,
        blob_sha: str,
        installation_token: str | InstallationAccessToken,
    ) -> GitBlob:
        """Fetch and base64-decode a Git blob without writing to GitHub."""

        payload = await self._request_json(
            "GET",
            f"{self._repository_path(owner, repository)}/git/blobs/{self._quote_segment(blob_sha, 'blob_sha')}",
            headers=self._installation_auth_headers(installation_token),
        )
        encoding = self._require_text(payload, "encoding")
        if encoding.lower() != "base64":
            raise GitHubPayloadError(f"unsupported GitHub blob encoding: {encoding}")
        encoded_content = self._require_text(payload, "content")
        try:
            content = base64.b64decode("".join(encoded_content.splitlines()), validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubPayloadError("GitHub blob content was not valid base64") from exc

        size = self._require_nonnegative_int(payload, "size")
        if len(content) != size:
            raise GitHubPayloadError("GitHub blob size did not match decoded content")
        response_sha = self._require_text(payload, "sha")
        if response_sha != blob_sha:
            raise GitHubPayloadError("GitHub blob SHA did not match the requested object")
        return GitBlob(
            sha=response_sha,
            content=content,
            size=size,
            url=self._optional_text(payload.get("url")),
        )

    async def _get_tree(
        self,
        owner: str,
        repository: str,
        tree_sha: str,
        installation_token: str | InstallationAccessToken,
        *,
        recursive: bool,
        headers: Mapping[str, str] | None = None,
    ) -> GitTree:
        payload = await self._request_json(
            "GET",
            f"{self._repository_path(owner, repository)}/git/trees/{self._quote_segment(tree_sha, 'tree_sha')}",
            headers=headers or self._installation_auth_headers(installation_token),
            params={"recursive": "1"} if recursive else None,
        )
        items = payload.get("tree")
        if not isinstance(items, list):
            raise GitHubPayloadError("GitHub response field 'tree' must be a list")
        entries = tuple(self._tree_entry_from_payload(item) for item in items)
        truncated = payload.get("truncated", False)
        if not isinstance(truncated, bool):
            raise GitHubPayloadError("GitHub response field 'truncated' must be a boolean")
        response_sha = self._require_text(payload, "sha")
        if response_sha != tree_sha:
            raise GitHubPayloadError("GitHub tree SHA did not match the requested object")
        return GitTree(
            sha=response_sha,
            entries=entries,
            truncated=truncated,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        # The client has no generic write transport.  POST is reserved solely
        # for GitHub's documented installation-token exchange above.
        if method not in {"GET", "POST"}:
            raise GitHubConfigurationError("GitHub client permits only GET and token-exchange POST")
        if method == "POST" and re.fullmatch(r"/app/installations/[1-9][0-9]*/access_tokens", path) is None:
            raise GitHubConfigurationError("GitHub client permits POST only for installation token exchange")

        request_headers = {**self._base_headers(), **headers}
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=request_headers,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise GitHubTimeoutError("GitHub request timed out") from exc
        except httpx.RequestError as exc:
            raise GitHubTransportError("GitHub request failed before receiving a response") from exc

        if not response.is_success:
            self._raise_response_error(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubPayloadError("GitHub returned a non-JSON success response") from exc
        if not isinstance(payload, Mapping):
            raise GitHubPayloadError("GitHub returned a JSON response with an unexpected shape")
        return payload

    def _raise_response_error(self, response: httpx.Response) -> None:
        payload: Mapping[str, Any] | None = None
        try:
            candidate = response.json()
            if isinstance(candidate, Mapping):
                payload = candidate
        except ValueError:
            pass

        provider_message = ""
        if payload is not None and isinstance(payload.get("message"), str):
            provider_message = payload["message"]
        request_id = response.headers.get("x-github-request-id")
        retry_after = self._parse_retry_after(response.headers.get("retry-after"))
        message = f"GitHub API returned HTTP {response.status_code}"
        kwargs = {
            "status_code": response.status_code,
            "request_id": request_id,
            "retry_after_seconds": retry_after,
        }

        if response.status_code == 401:
            raise GitHubAuthenticationError(message, **kwargs)
        if response.status_code == 404:
            raise GitHubNotFoundError(message, **kwargs)
        rate_limited = (
            response.status_code == 429
            or response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in provider_message.lower()
        )
        if rate_limited:
            raise GitHubRateLimitError(message, **kwargs)
        raise GitHubAPIError(message, **kwargs)

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self._api_version,
            "User-Agent": "pulsegraph-github-integration/1.0",
        }

    def _app_auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.create_app_jwt()}"}

    @staticmethod
    def _installation_auth_headers(
        installation_token: str | InstallationAccessToken,
    ) -> dict[str, str]:
        token = (
            installation_token.token
            if isinstance(installation_token, InstallationAccessToken)
            else installation_token
        )
        if not isinstance(token, str) or not token.strip():
            raise GitHubConfigurationError("installation token must be a non-empty string")
        return {"Authorization": f"Bearer {token.strip()}"}

    def _repository_path(self, owner: str, repository: str) -> str:
        return f"/repos/{self._quote_segment(owner, 'owner')}/{self._quote_segment(repository, 'repository')}"

    @staticmethod
    def _quote_segment(value: str, field_name: str) -> str:
        return quote(GitHubReadOnlyClient._validate_nonempty_text(value, field_name), safe="")

    @staticmethod
    def _validate_nonempty_text(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GitHubConfigurationError(f"{field_name} must be a non-empty string")
        if value != value.strip():
            raise GitHubConfigurationError(f"{field_name} must not contain surrounding whitespace")
        return value

    @staticmethod
    def _validate_private_key(private_key_pem: str | bytes) -> str | bytes:
        if isinstance(private_key_pem, bytes):
            if not private_key_pem.strip():
                raise GitHubConfigurationError("GitHub App private key must not be empty")
            return private_key_pem
        if isinstance(private_key_pem, str) and private_key_pem.strip():
            # .env files commonly store PEM line breaks as literal "\\n".
            # PyJWT requires actual line-feed characters when parsing the key.
            return private_key_pem.replace("\\n", "\n")
        raise GitHubConfigurationError("GitHub App private key must be PEM text or bytes")

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        value = GitHubReadOnlyClient._validate_nonempty_text(base_url, "base_url").rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GitHubConfigurationError("base_url must be an absolute HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GitHubConfigurationError("base_url must not contain credentials, a query, or a fragment")
        return value

    @staticmethod
    def _validate_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
        if isinstance(timeout, httpx.Timeout):
            return timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise GitHubConfigurationError("timeout must be greater than zero")
        return httpx.Timeout(float(timeout))

    @staticmethod
    def _validate_positive_int(value: object, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GitHubConfigurationError(f"{field_name} must be a positive integer")
        return value

    @classmethod
    def _validate_repository_ids(cls, repository_ids: Sequence[int]) -> list[int]:
        if isinstance(repository_ids, (str, bytes)) or not repository_ids:
            raise GitHubConfigurationError("repository_ids must be a non-empty sequence of IDs")
        if len(repository_ids) > 500:
            raise GitHubConfigurationError("GitHub allows at most 500 repository IDs per token")
        return [cls._validate_positive_int(item, "repository_id") for item in repository_ids]

    @staticmethod
    def _validate_page_size(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise GitHubConfigurationError("per_page must be an integer from 1 through 100")
        return value

    @staticmethod
    def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise GitHubPayloadError(f"GitHub response field '{field_name}' must be an object")
        return value

    @staticmethod
    def _require_text(payload: Mapping[str, Any], field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise GitHubPayloadError(f"GitHub response field '{field_name}' must be a non-empty string")
        return value

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise GitHubPayloadError("GitHub response optional text field must be a string")
        return value

    @staticmethod
    def _require_nonnegative_int(payload: Mapping[str, Any], field_name: str) -> int:
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GitHubPayloadError(
                f"GitHub response field '{field_name}' must be a non-negative integer"
            )
        return value

    @classmethod
    def _repository_from_payload(cls, payload: object) -> RepositoryMetadata:
        data = cls._require_mapping(payload, "repository")
        owner = cls._require_mapping(data.get("owner"), "owner")
        private = data.get("private")
        if not isinstance(private, bool):
            raise GitHubPayloadError("GitHub response field 'private' must be a boolean")
        return RepositoryMetadata(
            id=cls._require_nonnegative_int(data, "id"),
            owner=cls._require_text(owner, "login"),
            name=cls._require_text(data, "name"),
            full_name=cls._require_text(data, "full_name"),
            default_branch=cls._require_text(data, "default_branch"),
            private=private,
            html_url=cls._optional_text(data.get("html_url")),
        )

    @classmethod
    def _tree_entry_from_payload(cls, payload: object) -> GitTreeEntry:
        data = cls._require_mapping(payload, "tree entry")
        size_value = data.get("size")
        if size_value is not None and (isinstance(size_value, bool) or not isinstance(size_value, int)):
            raise GitHubPayloadError("GitHub tree entry field 'size' must be an integer or null")
        return GitTreeEntry(
            path=cls._require_text(data, "path"),
            mode=cls._require_text(data, "mode"),
            type=cls._require_text(data, "type"),
            sha=cls._require_text(data, "sha"),
            size=size_value,
            url=cls._optional_text(data.get("url")),
        )

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubPayloadError("GitHub token expiry was not ISO-8601") from exc
        if parsed.tzinfo is None:
            raise GitHubPayloadError("GitHub token expiry must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_permissions(value: object) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise GitHubPayloadError("GitHub response field 'permissions' must be an object")
        permissions: dict[str, str] = {}
        for permission, level in value.items():
            if not isinstance(permission, str) or not isinstance(level, str):
                raise GitHubPayloadError("GitHub token permissions must map strings to strings")
            permissions[permission] = level
        return permissions

    @staticmethod
    def _ensure_read_only_permissions(permissions: Mapping[str, str]) -> None:
        normalized_permissions = {
            permission: level.lower() for permission, level in permissions.items()
        }
        if normalized_permissions != {"metadata": "read", "contents": "read"}:
            raise GitHubAuthenticationError(
                "GitHub installation token permissions are not exactly Metadata/Contents read-only",
                status_code=200,
            )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
