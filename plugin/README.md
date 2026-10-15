# GitHub Issue Assistant Tool plugin

A Python Tool plugin using the official Dify Plugin SDK. `get_issue` retrieves one issue from the GitHub REST API and a bounded sample of its oldest comments. The surrounding Dify Workflow can summarize the issue, suggest triage labels, and draft a human-reviewed reply. This plugin itself performs only GET requests.

This is an independent integration, not an official GitHub or Dify product.

## Requirements

- Python 3.12 or later; runner target Python 3.12.
- Official `dify-plugin==0.10.2` and `httpx==0.28.1`, pinned identically in `requirements.txt` and `pyproject.toml`.
- Dify 1.14.0 or later with custom Tool plugin installation enabled.
- The official Dify plugin CLI to create an installable `.difypkg`.

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r plugin\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

No test requires a GitHub token, Dify account, or network connection. The tests import the actual installed Dify SDK and mock only the HTTP boundary.

## Credentials

Leave the optional `github_token` credential blank for public issues. Without a token, credential validation succeeds without making a network request. With a token, validation calls `GET https://api.github.com/user`. Invalid credentials fail explicitly; they are never silently retried anonymously.

For private repositories, create a fine-grained personal access token restricted to the required repositories with **Issues: read-only**, subject to any organization approval or SSO requirements. Enter it only in the Dify provider credential form. Do not put it in workflow inputs, screenshots, the README, `.env`, or GitHub. `/user` checks token validity; the actual issue request separately checks repository access.

## Build and install

From the repository root, build an installable package with the official CLI:

```powershell
dify plugin package ./plugin
```

In Dify's Tool Plugins page, choose installation from a local package and upload the `.difypkg`. Open the provider's credential settings and save an empty token for public issues, or supply a token for repositories it can access. Add **GitHub Issue → Get GitHub issue** as a Workflow Tool node. Custom package policies and menu labels vary by deployment.

For development, optional remote debugging uses `plugin/.env.example`. Copy it to `plugin/.env`, then enter your workspace's debug address and key using `INSTALL_METHOD=remote`, `REMOTE_INSTALL_URL=HOST:PORT`, and `REMOTE_INSTALL_KEY`. From `plugin/`, run `..\.venv\Scripts\python.exe -m main`. Keep the process running during debugging. SDK 0.10.2 also supports separate `REMOTE_INSTALL_HOST` and `REMOTE_INSTALL_PORT` when `REMOTE_INSTALL_URL` is unset.

Keep `.env` local. `.difyignore` excludes local environment files, virtual environments, and caches from packages. No Dify model, storage, or tool invocation permissions are requested by the plugin.

## Inputs and output

| Parameter | Type | Behavior |
| --- | --- | --- |
| `issue_url` | required string | `https://github.com/OWNER/REPO/issues/POSITIVE_NUMBER`; an optional trailing slash is accepted. |
| `max_comments` | optional number | Integer 0–30; default 10. Zero skips comments. |

The URL is validated before network activity. Only exact `https://github.com` URLs are accepted; userinfo, ports, query strings, fragments, percent encoding, path injection, Enterprise hosts and pull request URLs are rejected. The input URL is never fetched directly. Validated components construct requests to the fixed `https://api.github.com` origin with redirects disabled. An issue API response containing `pull_request` is also rejected.

For example, run the Tool with this public issue and a ten-comment limit:

```json
{
  "issue_url": "https://github.com/langgenius/dify/issues/44141",
  "max_comments": 10
}
```

For an accessible issue, expect `ok=true`, `error=null`, a matching `issue.html_url`, and at most ten comments. Compare the returned title and body with GitHub. Set `max_comments` to `0` to fetch only the issue. Public issue content and comment counts can change.

The Tool emits exactly one SDK JSON message. Dify exposes it as the first element of the Tool node's `json` array: `json[0]`.

| Envelope field | Contents |
| --- | --- |
| `ok` | Boolean success flag. Check this before using issue data. |
| `issue` | Issue object, or null when retrieval fails before an issue is available. |
| `comments` | Array of selected comments. |
| `meta` | Authentication usage, comment counts, retrieval limits, truncation flags, and warnings. |
| `error` | Null on success; otherwise `code`, `message`, `retryable`, and nullable `retry_after_seconds`. |

Issue fields are `number`, `title`, `body`, `state`, `labels`, `author`, `html_url`, `created_at`, `updated_at`, and `comments` (the total comment count). Labels are strings. Each comment contains `id`, `body`, `author`, `html_url`, `created_at`, and `updated_at`. Null bodies become empty strings; deleted authors are represented as `[deleted]`.

Important metadata includes `comments_total`, `comments_returned`, `comments_incomplete`, `issue_body_truncated`, `truncated_comment_ids`, and `warning_codes`. `authenticated` indicates whether a token was supplied. Optional `rate_limit_remaining` and `rate_limit_reset` values come from valid numeric GitHub response headers.

Issue bodies are capped at 20,000 characters; comment bodies at 2,000 characters each. Comments use the first API page, in ascending comment ID order, with `per_page=max_comments`. The Tool makes at most two API calls per invocation and never follows pagination links. It skips the comment request when the limit or reported comment count is zero.

`comments_incomplete` concerns missing comments, including when a zero limit omits existing comments. Body truncation has separate flags: all reported comments can be returned while an individual body is still shortened. GitHub data can change between requests, so counts describe a snapshot.

On failure, `ok` is false and `error` contains `code`, a safe `message`, `retryable`, and `retry_after_seconds` (integer or null). `issue` may remain available if only the comment request failed, but the Workflow must branch on `ok` and route failures to its error output instead of sending partial data to the model. No automatic retries are performed.

For example, `https://example.com/issue/1` returns `INVALID_ISSUE_URL` before any HTTP request. An expired token produces `UNAUTHORIZED`; it does not fall back to anonymous access.

| Error | Action |
| --- | --- |
| `INVALID_ISSUE_URL`, `INVALID_MAX_COMMENTS` | Correct the input; validation happens before any request. |
| `INVALID_CREDENTIALS`, `UNAUTHORIZED` | Re-enter a valid credential in the provider form. |
| `FORBIDDEN` | Review repository access, Issues read permission, and organization authorization. |
| `RATE_LIMITED` | Wait the indicated delay. `Retry-After` and rate reset headers are respected; fallback is 60 seconds. |
| `NOT_FOUND`, `GONE` | Confirm the issue exists and the credential can access it. |
| `VALIDATION_FAILED` | Check the submitted issue URL. |
| `REDIRECT_NOT_ALLOWED` | Copy the current URL after opening it in GitHub. |
| `PULL_REQUEST_NOT_SUPPORTED` | Use an issue rather than a pull request. |
| `TIMEOUT`, `NETWORK_ERROR`, `GITHUB_UNAVAILABLE`, `INVALID_RESPONSE` | Retry later; inspect `retryable` and optional delay. |
| `UNEXPECTED_STATUS`, `INTERNAL_ERROR` | Check deployment health before retrying. |

Repository content is untrusted. Treat it as evidence in the model prompt and disclose incomplete or truncated content. A published app uses its workspace's configured credential; visitors do not automatically inherit their own GitHub permissions. Use an empty or public-only credential for a public app. Restrict access to apps using private-repository credentials. See [PRIVACY.md](PRIVACY.md) for data handling.

## Primary sources

- [Dify Tool plugin development guide](https://docs.dify.ai/en/develop-plugin/dev-guides-and-walkthroughs/tool-plugin)
- [Official Python SDK release 0.10.2](https://github.com/langgenius/dify-plugin-sdks/releases/tag/v0.10.2)
- [Official CLI Python main.py template](https://github.com/langgenius/dify-plugin-daemon/blob/main/cmd/commandline/plugin/templates/python/main.py)
- [SDK environment configuration](https://github.com/langgenius/dify-plugin-sdks/blob/v0.10.2/src/dify_plugin/config/config.py)
- [GitHub: get an issue](https://docs.github.com/en/rest/issues/issues#get-an-issue)
- [GitHub: list issue comments](https://docs.github.com/en/rest/issues/comments#list-issue-comments)
- [GitHub: REST API troubleshooting](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api)
- [GitHub: supported API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)

The Tool explicitly uses GitHub REST API version `2022-11-28`.
