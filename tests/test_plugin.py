"""Offline integration tests using the real Dify SDK and httpx MockTransport."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
sys.path.insert(0, str(PLUGIN))

# Import the real SDK before HTTP clients, as its runtime applies gevent patches.
import dify_plugin
from dify_plugin.config.config import DifyPluginEnv, InstallMethod
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.entities.tool import ToolInvokeMessage, ToolProviderConfiguration
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

import copy
import importlib.metadata
import json
import os
import tomllib
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import yaml

from lib.github_api import GitHubAPI, GitHubError, fetch_issue
from provider.github_issue import GitHubIssueProvider
from tools.get_issue import GetIssueTool


ISSUE_URL = "https://github.com/example/project/issues/42"
ISSUE_PATH = "/repos/example/project/issues/42"
SECRET = "github_pat_test_secret_never_return"
REAL_CLIENT = httpx.Client


def issue_payload(**overrides):
    payload = {
        "number": 42,
        "title": "Crash on startup",
        "body": "Steps to reproduce the crash.",
        "state": "open",
        "labels": [{"name": "bug"}],
        "user": {"login": "reporter"},
        "html_url": ISSUE_URL,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "comments": 2,
    }
    payload.update(overrides)
    return payload


def comment_payload(identifier=100, **overrides):
    payload = {
        "id": identifier,
        "body": "I can reproduce this.",
        "user": {"login": "maintainer"},
        "html_url": ISSUE_URL + f"#issuecomment-{identifier}",
        "created_at": "2026-10-02T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class PluginTests(unittest.TestCase):
    @contextmanager
    def http(self, replies):
        """Mock the HTTP boundary; SDK Tool and Provider classes remain real."""
        requests = []
        options = []
        queue = list(replies)

        def handler(request):
            requests.append(request)
            if not queue:
                self.fail("Unexpected additional HTTP request")
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            if callable(response):
                return response(request)
            return response

        def client_factory(**kwargs):
            options.append(kwargs.copy())
            return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        with patch("lib.github_api.httpx.Client", side_effect=client_factory):
            yield requests, options
        self.assertEqual(queue, [], "Expected mocked API response was never consumed")

    def invoke(self, **parameters):
        return fetch_issue({"issue_url": ISSUE_URL, **parameters}, {})

    def assert_envelope(self, result):
        self.assertEqual(set(result), {"ok", "issue", "comments", "meta", "error"})
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["comments"], list)
        self.assertIsInstance(result["meta"], dict)
        self.assertLessEqual(len(result["comments"]), 30)
        if result["ok"]:
            self.assertIsNone(result["error"])
            self.assertIsInstance(result["issue"], dict)
        else:
            self.assertEqual(set(result["error"]), {"code", "message", "retryable", "retry_after_seconds"})
            self.assertIsInstance(result["error"]["retryable"], bool)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_public_issue_normalization_and_fixed_origin(self):
        with self.http([
            httpx.Response(200, json=issue_payload()),
            httpx.Response(200, json=[comment_payload(101), comment_payload(100)]),
        ]) as (requests, options):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["issue"]["labels"], ["bug"])
        self.assertEqual(result["issue"]["author"], "reporter")
        self.assertEqual([entry["id"] for entry in result["comments"]], [100, 101])
        self.assertFalse(result["meta"]["comments_incomplete"])
        self.assertEqual(result["meta"]["comments_returned"], 2)
        self.assertEqual(result["meta"]["comments_order"], "oldest_first")
        self.assertEqual([request.url.host for request in requests], ["api.github.com"] * 2)
        self.assertEqual([request.method for request in requests], ["GET", "GET"])
        self.assertEqual(requests[0].url.path, ISSUE_PATH)
        self.assertEqual(dict(requests[1].url.params), {"per_page": "10", "page": "1"})
        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(requests[0].headers["x-github-api-version"], "2022-11-28")
        self.assertIs(options[0]["follow_redirects"], False)
        self.assertIs(options[0]["trust_env"], False)
        self.assertEqual(options[0]["timeout"].connect, 5.0)

    def test_authenticated_header_and_upstream_secret_redaction(self):
        with self.http([httpx.Response(200, json=issue_payload(comments=0, body=f"echo {SECRET}"))]) as (requests, _):
            result = fetch_issue({"issue_url": ISSUE_URL}, {"github_token": SECRET})
        self.assert_envelope(result)
        self.assertTrue(result["meta"]["authenticated"])
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(result["issue"]["body"], "echo [REDACTED]")

    def test_empty_token_credential_validation_never_uses_network(self):
        with patch("lib.github_api.httpx.Client") as client:
            for credentials in ({}, {"github_token": ""}, {"github_token": "   "}):
                GitHubIssueProvider().validate_credentials(credentials)
        client.assert_not_called()

    def test_supplied_token_validation_calls_user(self):
        with self.http([httpx.Response(200, json={"login": "example-user"})]) as (requests, _):
            GitHubIssueProvider().validate_credentials({"github_token": SECRET})
        self.assertEqual(requests[0].url.path, "/user")
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {SECRET}")

    def test_provider_validation_rejects_invalid_token_without_leak(self):
        with self.http([httpx.Response(401, json={"message": SECRET})]):
            with self.assertRaises(ToolProviderCredentialValidationError) as caught:
                GitHubIssueProvider().validate_credentials({"github_token": SECRET})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("rejected", str(caught.exception))

    def test_provider_handles_unreadable_user_payload_and_timeout(self):
        responses = [httpx.Response(200, json=[]), httpx.ReadTimeout(SECRET)]
        for response in responses:
            with self.subTest(response=type(response).__name__):
                with self.http([response]):
                    with self.assertRaises(ToolProviderCredentialValidationError) as caught:
                        GitHubIssueProvider().validate_credentials({"github_token": SECRET})
                self.assertNotIn(SECRET, str(caught.exception))

    def test_invalid_urls_fail_before_http(self):
        invalid = [
            None, "", "http://github.com/example/project/issues/42",
            "https://github.com.evil.test/example/project/issues/42",
            "https://evil.test/example/project/issues/42",
            "https://api.github.com/repos/example/project/issues/42",
            "https://token@github.com/example/project/issues/42",
            "https://github.com@evil.test/example/project/issues/42",
            "https://github.com:443/example/project/issues/42",
            "https://github.com/example/project/issues/42?anything=1",
            "https://github.com/example/project/issues/42#issuecomment-100",
            "https://github.com/example/project/pull/42",
            "https://github.com/example/project/issues/0",
            "https://github.com/example/project/issues/-1",
            "https://github.com/example/project/issues/1.5",
            "https://github.com/example/project/issues/042",
            "https://github.com/example/project/issues/42/extra",
            "https://github.com/example/%2e%2e/issues/42",
            "https://github.com/example/../issues/42",
            "https://github.com/example/project/issues/42\n",
            "https://github.com/example/project/issues/42\\evil",
            " https://github.com/example/project/issues/42",
            "https://github.com/example//project/issues/42",
            "https://github.com/example/project/issues/" + "9" * 5000,
        ]
        with patch("lib.github_api.httpx.Client") as client:
            for value in invalid:
                with self.subTest(value=str(value)[:100]):
                    result = self.invoke(issue_url=value)
                    self.assert_envelope(result)
                    self.assertEqual(result["error"]["code"], "INVALID_ISSUE_URL")
        client.assert_not_called()

    def test_invalid_comment_limits_fail_before_http(self):
        invalid = [None, True, False, -1, 31, 1.5, "10", "", float("nan"), float("inf"), {}, 10 ** 1000]
        with patch("lib.github_api.httpx.Client") as client:
            for value in invalid:
                with self.subTest(value=str(value)[:100]):
                    result = self.invoke(max_comments=value)
                    self.assert_envelope(result)
                    self.assertEqual(result["error"]["code"], "INVALID_MAX_COMMENTS")
        client.assert_not_called()

    def test_invalid_token_characters_fail_before_http(self):
        with patch("lib.github_api.httpx.Client") as client:
            for value in (123, "token\r\nInjected: yes", "has space", "非ASCII", "x" * 4097):
                result = fetch_issue({"issue_url": ISSUE_URL}, {"github_token": value})
                self.assertEqual(result["error"]["code"], "INVALID_CREDENTIALS")
        client.assert_not_called()

    def test_zero_comments_skips_request_and_discloses_omission(self):
        with self.http([httpx.Response(200, json=issue_payload(comments=15))]) as (requests, _):
            result = self.invoke(max_comments=0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["comments"], [])
        self.assertEqual(result["meta"]["comments_total"], 15)
        self.assertTrue(result["meta"]["comments_incomplete"])
        self.assertIn("COMMENTS_LIMITED", result["meta"]["warning_codes"])

    def test_no_comments_skips_request_and_is_complete(self):
        with self.http([httpx.Response(200, json=issue_payload(comments=0))]) as (requests, _):
            result = self.invoke()
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        self.assertFalse(result["meta"]["comments_incomplete"])

    def test_maximum_thirty_is_one_page_and_never_follows_link(self):
        with self.http([
            httpx.Response(200, json=issue_payload(comments=300)),
            httpx.Response(200, json=[comment_payload(100 + n) for n in range(30)], headers={
                "Link": '<https://evil.test/steal>; rel="next"',
            }),
        ]) as (requests, _):
            result = self.invoke(max_comments=30.0)
        self.assert_envelope(result)
        self.assertEqual(len(result["comments"]), 30)
        self.assertTrue(result["meta"]["comments_incomplete"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].url.params["per_page"], "30")

    def test_truncation_boundaries_and_deleted_authors(self):
        with self.http([
            httpx.Response(200, json=issue_payload(body="a" * 20001, user=None)),
            httpx.Response(200, json=[
                comment_payload(100, body="b" * 2001, user=None),
                comment_payload(101, body="c" * 2000),
            ]),
        ]):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertEqual(len(result["issue"]["body"]), 20000)
        self.assertEqual(len(result["comments"][0]["body"]), 2000)
        self.assertTrue(result["meta"]["issue_body_truncated"])
        self.assertEqual(result["meta"]["truncated_comment_ids"], [100])
        self.assertEqual(result["issue"]["author"], "[deleted]")
        self.assertEqual(result["comments"][0]["author"], "[deleted]")
        self.assertIn("ISSUE_BODY_TRUNCATED", result["meta"]["warning_codes"])
        self.assertIn("COMMENT_BODIES_TRUNCATED", result["meta"]["warning_codes"])

    def test_null_bodies_become_empty_strings(self):
        with self.http([
            httpx.Response(200, json=issue_payload(body=None, comments=1)),
            httpx.Response(200, json=[comment_payload(body=None)]),
        ]):
            result = self.invoke()
        self.assertTrue(result["ok"])
        self.assertEqual(result["issue"]["body"], "")
        self.assertEqual(result["comments"][0]["body"], "")

    def test_pull_request_api_response_is_rejected(self):
        with self.http([httpx.Response(200, json=issue_payload(pull_request={}))]) as (requests, _):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertEqual(result["error"]["code"], "PULL_REQUEST_NOT_SUPPORTED")
        self.assertEqual(len(requests), 1)
        self.assertIsNone(result["issue"])

    def test_http_errors_are_safe_and_actionable(self):
        cases = [
            (401, "UNAUTHORIZED", False), (403, "FORBIDDEN", False),
            (404, "NOT_FOUND", False), (410, "GONE", False),
            (422, "VALIDATION_FAILED", False), (429, "RATE_LIMITED", True),
            (500, "GITHUB_UNAVAILABLE", True), (503, "GITHUB_UNAVAILABLE", True),
            (418, "UNEXPECTED_STATUS", False),
        ]
        for status, code, retryable in cases:
            with self.subTest(status=status):
                with self.http([httpx.Response(status, json={"message": SECRET})]):
                    result = fetch_issue({"issue_url": ISSUE_URL}, {"github_token": SECRET})
                self.assert_envelope(result)
                self.assertEqual(result["error"]["code"], code)
                self.assertIs(result["error"]["retryable"], retryable)

    def test_permission_403_is_distinct_from_primary_and_secondary_limits(self):
        cases = [
            ({}, {"message": "Resource not accessible"}, "FORBIDDEN", None),
            ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "110"}, {}, "RATE_LIMITED", 10),
            ({"Retry-After": "35"}, {}, "RATE_LIMITED", 35),
            ({}, {"message": "You have exceeded a secondary rate limit."}, "RATE_LIMITED", 60),
        ]
        for headers, payload, code, delay in cases:
            with self.subTest(code=code, headers=headers):
                with self.http([httpx.Response(403, headers=headers, json=payload)]), patch("lib.github_api.time.time", return_value=100):
                    result = self.invoke()
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["error"]["retry_after_seconds"], delay)

    def test_rate_limit_retry_after_http_date(self):
        with self.http([httpx.Response(429, headers={"Retry-After": "Wed, 01 Jan 2020 00:00:00 GMT"})]):
            result = self.invoke()
        self.assertEqual(result["error"]["retry_after_seconds"], 0)

    def test_rate_limit_malformed_headers_do_not_crash(self):
        with self.http([httpx.Response(429, headers={"Retry-After": "invalid", "X-RateLimit-Reset": "bad"})]):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertEqual(result["error"]["retry_after_seconds"], 60)

    def test_rate_metadata_uses_latest_valid_numeric_headers(self):
        with self.http([
            httpx.Response(200, json=issue_payload(comments=1), headers={"X-RateLimit-Remaining": "59", "X-RateLimit-Reset": "200"}),
            httpx.Response(200, json=[comment_payload()], headers={"X-RateLimit-Remaining": "58", "X-RateLimit-Reset": "200"}),
        ]):
            result = self.invoke()
        self.assertEqual(result["meta"]["rate_limit_remaining"], 58)
        self.assertEqual(result["meta"]["rate_limit_reset"], 200)

    def test_redirect_never_sends_token_to_destination(self):
        with self.http([httpx.Response(302, headers={"Location": "https://evil.test/steal"})]) as (requests, _):
            result = fetch_issue({"issue_url": ISSUE_URL}, {"github_token": SECRET})
        self.assert_envelope(result)
        self.assertEqual(result["error"]["code"], "REDIRECT_NOT_ALLOWED")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "api.github.com")

    def test_transport_failures_never_expose_raw_exception(self):
        for failure, code in [(httpx.ReadTimeout(SECRET), "TIMEOUT"), (httpx.ConnectError(SECRET), "NETWORK_ERROR")]:
            with self.subTest(code=code):
                with self.http([failure]):
                    result = fetch_issue({"issue_url": ISSUE_URL}, {"github_token": SECRET})
                self.assert_envelope(result)
                self.assertEqual(result["error"]["code"], code)
                self.assertTrue(result["error"]["retryable"])

    def test_malformed_json_is_safe(self):
        with self.http([httpx.Response(200, content=("broken " + SECRET).encode())]):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertEqual(result["error"]["code"], "INVALID_RESPONSE")

    def test_unexpected_issue_shapes_do_not_crash(self):
        invalid = [
            [], None, {}, issue_payload(number=True), issue_payload(number=43),
            issue_payload(state="unknown"), issue_payload(comments="2"),
            issue_payload(labels=[{}]), issue_payload(user="invalid"),
            issue_payload(body={}), issue_payload(html_url="https://evil.test/issue/42"),
            issue_payload(html_url="https://github.com/another/project/issues/42"),
        ]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:100]):
                with self.http([httpx.Response(200, json=payload)]):
                    result = self.invoke()
                self.assert_envelope(result)
                self.assertEqual(result["error"]["code"], "INVALID_RESPONSE")

    def test_comment_failure_retains_issue_but_prevents_success(self):
        with self.http([
            httpx.Response(200, json=issue_payload()),
            httpx.Response(403, json={"message": "Permission denied"}),
        ]):
            result = self.invoke()
        self.assert_envelope(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["issue"]["number"], 42)
        self.assertEqual(result["error"]["code"], "FORBIDDEN")
        self.assertTrue(result["meta"]["comments_incomplete"])

    def test_invalid_comments_are_atomic(self):
        invalid = [
            {}, [comment_payload(id=True)], [comment_payload(body={})],
            [comment_payload(html_url="https://evil.test/#issuecomment-100")],
            [comment_payload(), comment_payload()],
            [comment_payload(100), {"id": 101}],
        ]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:100]):
                with self.http([httpx.Response(200, json=issue_payload()), httpx.Response(200, json=payload)]):
                    result = self.invoke()
                self.assert_envelope(result)
                self.assertEqual(result["error"]["code"], "INVALID_RESPONSE")
                self.assertEqual(result["comments"], [])

    def test_sdk_emits_exactly_one_json_message_on_success_and_failure(self):
        self.assertTrue(Path(dify_plugin.__file__).is_file())
        self.assertTrue(issubclass(GetIssueTool, dify_plugin.Tool))
        tool = GetIssueTool.from_credentials({})
        with self.http([httpx.Response(200, json=issue_payload(comments=0))]):
            messages = list(tool.invoke({"issue_url": ISSUE_URL}))
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], ToolInvokeMessage)
        self.assertEqual(messages[0].type, ToolInvokeMessage.MessageType.JSON)
        self.assert_envelope(messages[0].message.json_object)
        errors = list(tool.invoke({"issue_url": "invalid"}))
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0].message.json_object["ok"])

    def test_sdk_parses_provider_and_tool_yaml(self):
        original = Path.cwd()
        try:
            os.chdir(PLUGIN)
            provider_yaml = yaml.safe_load(Path("provider/github_issue.yaml").read_text(encoding="utf-8"))
            configuration = ToolProviderConfiguration.model_validate(copy.deepcopy(provider_yaml))
        finally:
            os.chdir(original)
        self.assertEqual(configuration.identity.name, "github_issue")
        self.assertEqual(len(configuration.tools), 1)
        self.assertEqual(configuration.tools[0].identity.name, "get_issue")
        credential = configuration.credentials_schema[0]
        self.assertEqual(credential.name, "github_token")
        self.assertFalse(credential.required)
        parameters = {parameter.name: parameter for parameter in configuration.tools[0].parameters}
        self.assertEqual(parameters["max_comments"].default, 10)
        self.assertEqual(parameters["max_comments"].form.value, "llm")
        self.assertEqual(parameters["issue_url"].form.value, "llm")
        self.assertTrue(parameters["issue_url"].required)

    def test_environment_example_matches_actual_sdk_settings(self):
        config = DifyPluginEnv(_env_file=PLUGIN / ".env.example")
        self.assertEqual(config.INSTALL_METHOD, InstallMethod.Remote)
        self.assertEqual(config.REMOTE_INSTALL_URL, "debug.dify.ai:5003")
        self.assertEqual(dify_plugin.Plugin._get_remote_install_host_and_port(config), ("debug.dify.ai", 5003))
        self.assertEqual(config.REMOTE_INSTALL_KEY, "replace_with_your_workspace_debug_key")

    def test_sdk_loads_whole_plugin_manifest_sources_and_assets(self):
        original = Path.cwd()
        try:
            os.chdir(PLUGIN)
            registration = PluginRegistration(DifyPluginEnv(_env_file=None))
        finally:
            os.chdir(original)
        self.assertEqual(registration.configuration.author, "issueassistant")
        self.assertEqual(registration.configuration.name, "github_issue_assistant")
        self.assertEqual(registration.configuration.version, "0.1.0")
        self.assertEqual(len(registration.tools_mapping), 1)
        self.assertEqual(registration.tools_configuration[0].identity.name, "github_issue")
        self.assertIn("icon.svg", [asset.filename for asset in registration.files])

    def test_sdk_supports_separate_remote_host_and_port(self):
        config = DifyPluginEnv(
            _env_file=None, REMOTE_INSTALL_URL=None,
            REMOTE_INSTALL_HOST="debug.example.test", REMOTE_INSTALL_PORT=5010,
        )
        self.assertEqual(dify_plugin.Plugin._get_remote_install_host_and_port(config), ("debug.example.test", 5010))

    def test_dependency_pins_stay_synchronized(self):
        project = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = (PLUGIN / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(project["project"]["dependencies"], requirements)
        self.assertEqual(importlib.metadata.version("dify-plugin"), "0.10.2")
        self.assertEqual(importlib.metadata.version("httpx"), "0.28.1")

    def test_client_rejects_absolute_api_paths_before_network(self):
        with self.http([]):
            with GitHubAPI(SECRET) as api:
                with self.assertRaises(GitHubError) as caught:
                    api.get_json("https://evil.test/steal")
        self.assertEqual(caught.exception.code, "INVALID_ISSUE_URL")


if __name__ == "__main__":
    unittest.main()
