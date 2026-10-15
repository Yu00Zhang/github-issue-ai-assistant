from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from lib.github_api import fetch_issue


class GetIssueTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        envelope = fetch_issue(tool_parameters, self.runtime.credentials)
        # Exactly one SDK JSON message; the Workflow reads json[0].
        yield self.create_json_message(envelope)
