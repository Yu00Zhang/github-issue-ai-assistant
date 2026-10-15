from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from lib.github_api import GitHubAPI, GitHubError, normalize_token


class GitHubIssueProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        try:
            token = normalize_token(credentials.get("github_token"))
            if token:
                with GitHubAPI(token) as api:
                    api.validate_user()
        except GitHubError as error:
            raise ToolProviderCredentialValidationError(error.message) from None
        except Exception:
            # Do not let HTTP exceptions or headers enter Dify's error messages.
            raise ToolProviderCredentialValidationError(
                "Unable to validate the GitHub credential. Please try again."
            ) from None
