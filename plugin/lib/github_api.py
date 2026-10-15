"""Small, bounded, read-only GitHub REST client; no Dify runtime dependency here."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


API_ORIGIN = "https://api.github.com"
API_VERSION = "2022-11-28"
ISSUE_BODY_LIMIT = 20_000
COMMENT_BODY_LIMIT = 2_000
DEFAULT_MAX_COMMENTS = 10
URL_PATTERN = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})/issues/(?P<number>[1-9][0-9]{0,18})/?",
    re.ASCII,
)


class GitHubError(Exception):
    """A deliberately safe error; never include exception text or API response bodies."""

    def __init__(
        self, code: str, message: str, retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class IssueReference:
    owner: str
    repo: str
    number: int

    @property
    def api_path(self) -> str:
        # Components contain only the ASCII characters allowed by URL_PATTERN.
        return f"/repos/{self.owner}/{self.repo}/issues/{self.number}"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"


def parse_issue_url(value: Any) -> IssueReference:
    match = URL_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if not match or match["repo"] in {".", ".."}:
        raise GitHubError(
            "INVALID_ISSUE_URL",
            "Enter a GitHub issue URL: https://github.com/OWNER/REPO/issues/NUMBER. "
            "Query strings, fragments, credentials and other hosts are not accepted.",
        )
    return IssueReference(match["owner"], match["repo"], int(match["number"]))


def parse_max_comments(value: Any = DEFAULT_MAX_COMMENTS) -> int:
    # Dify number parameters arrive as int or float. Do not silently coerce strings,
    # booleans, NaN, Infinity, fractional values or out-of-range values.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 30
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise GitHubError("INVALID_MAX_COMMENTS", "max_comments must be an integer from 0 to 30.")
    return int(value)


def normalize_token(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise GitHubError("INVALID_CREDENTIALS", "The optional GitHub token must be a string.")
    token = value.strip()
    if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise GitHubError(
            "INVALID_CREDENTIALS", "The GitHub token contains invalid characters. Re-enter it in the provider credential form."
        )
    return token


def _integer_header(value: str | None) -> int | None:
    if value is not None and re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    return None


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value:
        seconds = _integer_header(value)
        if seconds is not None:
            return seconds
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = _integer_header(response.headers.get("x-ratelimit-reset"))
        if reset is not None:
            return max(0, math.ceil(reset - time.time()))
    return None


def _response_error(response: httpx.Response) -> GitHubError:
    status = response.status_code
    retry_after = _retry_after(response)
    api_message = ""
    if status in (403, 429):
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                api_message = payload["message"].lower()
        except (ValueError, UnicodeError):
            pass
    rate_limited = status == 429 or (
        status == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
            or "rate limit" in api_message
            or "abuse detection" in api_message
        )
    )
    if rate_limited:
        return GitHubError(
            "RATE_LIMITED", "GitHub API rate limit reached. Wait before retrying.",
            True, retry_after if retry_after is not None else 60,
        )
    if status == 401:
        return GitHubError("UNAUTHORIZED", "GitHub rejected the token. Update the provider credential.")
    if status == 403:
        return GitHubError(
            "FORBIDDEN", "GitHub denied access. Check repository access, token Issues read permission and organization authorization."
        )
    if status == 404:
        return GitHubError(
            "NOT_FOUND", "The issue was not found or the credential cannot access this repository."
        )
    if status == 410:
        return GitHubError("GONE", "This issue or its comments are no longer available on GitHub.")
    if status == 422:
        return GitHubError("VALIDATION_FAILED", "GitHub could not validate this request. Check the issue URL.")
    if status in (301, 302, 303, 307, 308):
        return GitHubError(
            "REDIRECT_NOT_ALLOWED", "GitHub returned a redirect. Open the issue in GitHub and submit its current URL."
        )
    if status >= 500:
        return GitHubError(
            "GITHUB_UNAVAILABLE", "GitHub is temporarily unavailable. Try again later.", True, retry_after
        )
    return GitHubError("UNEXPECTED_STATUS", "GitHub returned an unexpected response. Try again later.")


class GitHubAPI:
    def __init__(self, token: str = "") -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-issue-ai-assistant/0.1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(
            base_url=API_ORIGIN,
            headers=headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self.rate_meta: dict[str, int] = {}

    def __enter__(self) -> GitHubAPI:
        return self

    def __exit__(self, *_: Any) -> None:
        self.client.close()

    def get_json(self, path: str, params: dict[str, int] | None = None) -> tuple[Any, bool]:
        # Only callers within this module construct paths. Reject even accidental
        # absolute URLs so that the Authorization header cannot leave api.github.com.
        if not re.fullmatch(r"/(?:user|repos/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*(?:/comments)?)", path):
            raise GitHubError("INVALID_ISSUE_URL", "The issue API path is invalid.")
        try:
            response = self.client.get(path, params=params)
        except httpx.TimeoutException:
            raise GitHubError("TIMEOUT", "GitHub did not respond in time. Please retry.", True) from None
        except httpx.HTTPError:
            raise GitHubError("NETWORK_ERROR", "Unable to reach GitHub. Please retry.", True) from None
        for header, key in (
            ("x-ratelimit-remaining", "rate_limit_remaining"),
            ("x-ratelimit-reset", "rate_limit_reset"),
        ):
            value = _integer_header(response.headers.get(header))
            if value is not None:
                self.rate_meta[key] = value
        if response.status_code != 200:
            raise _response_error(response)
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise GitHubError("INVALID_RESPONSE", "GitHub returned an unreadable JSON response.", True) from None
        # Link headers are never followed: a single page is sufficient for <=30.
        has_next = bool(re.search(r'\brel\s*=\s*"?next\b', response.headers.get("link", "")))
        return payload, has_next

    def validate_user(self) -> None:
        payload, _ = self.get_json("/user")
        if not isinstance(payload, dict) or not isinstance(payload.get("login"), str):
            raise GitHubError("INVALID_RESPONSE", "GitHub returned an invalid credential validation response.", True)


def _invalid_shape() -> GitHubError:
    return GitHubError("INVALID_RESPONSE", "GitHub returned an unexpected issue or comment format.", True)


def _string(payload: dict[str, Any], key: str, nullable: bool = False) -> str:
    value = payload.get(key)
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        raise _invalid_shape()
    return value


def _author(payload: dict[str, Any]) -> str:
    user = payload.get("user")
    if user is None:
        return "[deleted]"
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise _invalid_shape()
    return user["login"]


def _response_reference(value: str) -> IssueReference:
    try:
        return parse_issue_url(value)
    except GitHubError:
        raise _invalid_shape() from None


def _issue(payload: Any, reference: IssueReference) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise _invalid_shape()
    if "pull_request" in payload:
        raise GitHubError("PULL_REQUEST_NOT_SUPPORTED", "This URL resolves to a pull request. Submit a GitHub issue URL.")
    if type(payload.get("number")) is not int or payload["number"] != reference.number:
        raise _invalid_shape()
    if payload.get("state") not in ("open", "closed"):
        raise _invalid_shape()
    total = payload.get("comments")
    if type(total) is not int or total < 0:
        raise _invalid_shape()
    actual_url = _response_reference(_string(payload, "html_url"))
    if (actual_url.owner.lower(), actual_url.repo.lower(), actual_url.number) != (
        reference.owner.lower(), reference.repo.lower(), reference.number
    ):
        raise _invalid_shape()
    labels = payload.get("labels")
    if not isinstance(labels, list):
        raise _invalid_shape()
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
        else:
            raise _invalid_shape()
    body = _string(payload, "body", nullable=True)
    return {
        "number": reference.number,
        "title": _string(payload, "title"),
        "body": body[:ISSUE_BODY_LIMIT],
        "state": payload["state"],
        "labels": names,
        "author": _author(payload),
        "html_url": actual_url.html_url,
        "created_at": _string(payload, "created_at"),
        "updated_at": _string(payload, "updated_at"),
        "comments": total,
    }, len(body) > ISSUE_BODY_LIMIT


def _comment(payload: Any, reference: IssueReference) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] <= 0:
        raise _invalid_shape()
    identifier = payload["id"]
    html_url = _string(payload, "html_url")
    suffix = f"#issuecomment-{identifier}"
    if not html_url.endswith(suffix):
        raise _invalid_shape()
    actual_url = _response_reference(html_url[:-len(suffix)])
    if (actual_url.owner.lower(), actual_url.repo.lower(), actual_url.number) != (
        reference.owner.lower(), reference.repo.lower(), reference.number
    ):
        raise _invalid_shape()
    body = _string(payload, "body", nullable=True)
    return {
        "id": identifier,
        "body": body[:COMMENT_BODY_LIMIT],
        "author": _author(payload),
        "html_url": actual_url.html_url + suffix,
        "created_at": _string(payload, "created_at"),
        "updated_at": _string(payload, "updated_at"),
    }, len(body) > COMMENT_BODY_LIMIT


def _redact(value: Any, token: str) -> Any:
    """Defense in depth in case an upstream response echoes the supplied secret."""
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, token) for key, item in value.items()}
    return value


def fetch_issue(parameters: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
    """Always returns one safe envelope, including when comments fail after issue fetch."""
    meta: dict[str, Any] = {
        "schema_version": "1.0",
        "authenticated": False,
        "max_comments": None,
        "comments_total": None,
        "comments_returned": 0,
        "comments_incomplete": True,
        "comments_order": "oldest_first",
        "issue_body_truncated": False,
        "truncated_comment_ids": [],
        "warning_codes": [],
    }
    envelope: dict[str, Any] = {"ok": False, "issue": None, "comments": [], "meta": meta, "error": None}
    token = ""
    api: GitHubAPI | None = None
    try:
        reference = parse_issue_url(parameters.get("issue_url"))
        maximum = parse_max_comments(parameters.get("max_comments", DEFAULT_MAX_COMMENTS))
        meta["max_comments"] = maximum
        token = normalize_token(credentials.get("github_token"))
        meta["authenticated"] = bool(token)
        with GitHubAPI(token) as api:
            payload, _ = api.get_json(reference.api_path)
            issue, truncated = _issue(payload, reference)
            envelope["issue"] = issue
            meta["issue_body_truncated"] = truncated
            meta["comments_total"] = issue["comments"]
            has_next = False
            if maximum > 0 and issue["comments"] > 0:
                payload, has_next = api.get_json(
                    reference.api_path + "/comments", {"per_page": maximum, "page": 1}
                )
                if not isinstance(payload, list):
                    raise _invalid_shape()
                selected: list[dict[str, Any]] = []
                truncated_ids: list[int] = []
                for entry in payload[:maximum]:
                    comment, is_truncated = _comment(entry, reference)
                    selected.append(comment)
                    if is_truncated:
                        truncated_ids.append(comment["id"])
                if len({comment["id"] for comment in selected}) != len(selected):
                    raise _invalid_shape()
                # GitHub documents ascending ID order; preserve a deterministic
                # order if a mocked or future server returns a different order.
                selected.sort(key=lambda comment: comment["id"])
                envelope["comments"] = selected
                meta["truncated_comment_ids"] = truncated_ids
                if len(payload) > maximum:
                    meta["warning_codes"].append("API_RETURNED_EXTRA_COMMENTS")
                    has_next = True
            meta["comments_returned"] = len(envelope["comments"])
            meta["comments_incomplete"] = has_next or issue["comments"] > len(envelope["comments"])
            if meta["comments_incomplete"]:
                meta["warning_codes"].append("COMMENTS_LIMITED")
            if len(envelope["comments"]) > issue["comments"]:
                meta["warning_codes"].append("COMMENTS_CHANGED_DURING_FETCH")
            if truncated:
                meta["warning_codes"].append("ISSUE_BODY_TRUNCATED")
            if meta["truncated_comment_ids"]:
                meta["warning_codes"].append("COMMENT_BODIES_TRUNCATED")
            envelope["ok"] = True
    except GitHubError as error:
        envelope["error"] = error.as_dict()
    except Exception:
        # A stable envelope also covers unexpected failures without exposing
        # exception strings, request headers, response bodies or stack traces.
        envelope["error"] = GitHubError(
            "INTERNAL_ERROR", "The issue could not be processed. Please try again."
        ).as_dict()
    finally:
        if api is not None:
            meta.update(api.rate_meta)
    return _redact(envelope, token)
