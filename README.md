# GitHub Issue AI Assistant

Turn a GitHub issue URL into an English triage report and a suggested maintainer reply. A custom Python Tool built with the official Dify Plugin SDK retrieves live GitHub data; a Dify Workflow validates the result, handles retrieval failures, and asks an LLM to analyze the available evidence.

[Try the Web App](https://udify.app/workflow/SSJaWzacNqR5QbQb) · [Download the plugin](github_issue_assistant-0.1.0.difypkg) · [Workflow YAML](workflow/github_issue_ai_assistant.yml)

## Features

- Retrieves the issue title, body, status, labels, author, and a bounded oldest-first comment sample through GitHub's REST API.
- Produces an English summary, category, advisory priority, missing information, next steps, reply draft, and scope limitations.
- Routes handled input/API errors to an error output without calling the LLM.
- Supports public issues without a token and optional secret credentials for authenticated reads.
- Performs only reads: it never posts a comment, changes labels, or modifies a repository.

## Architecture

```text
Issue URL → get_issue → Normalize JSON → Success?
                                         ├─ Yes → LLM → report
                                         └─ No  → error_message
```

The plugin `issueassistant/github_issue_assistant` version `0.1.0` exposes `get_issue` through provider `github_issue`. It constructs requests to the fixed `https://api.github.com` origin with redirects disabled. A supplied token is validated through `GET /user`; issue and comment retrieval use GitHub's Issues REST endpoints.

## Requirements

- Dify **1.14.0 or later**, with local Tool plugin installation enabled and support for Workflow DSL **0.7.0**.
- **OpenAI provider plugin 1.0.7** and **gpt-5**, with credentials, model availability, and quota configured in the destination workspace.
- Python **3.12** for local development; the official Dify Plugin CLI for rebuilding the package.

## Install and configure

1. Download [`github_issue_assistant-0.1.0.difypkg`](github_issue_assistant-0.1.0.difypkg). In Dify, open **Integrations → Tools → Tool Plugins** and install the local package **before importing the workflow**.
2. Create a provider credential. Leave the optional secret `github_token` blank for public issues. For private repositories, use a fine-grained token restricted to the necessary repositories with **Issues: read-only**.
3. Configure the model dependency, then import [`workflow/github_issue_ai_assistant.yml`](workflow/github_issue_ai_assistant.yml). The workflow references the bundled plugin package.
4. Restore the installed Tool, destination provider credential, and model bindings if Dify requests them. Credentials are not supplied by the YAML.
5. Check that Tool `issue_url` uses the workflow input and `max_comments` is `10`. Run the examples below before publishing your app.

A published app uses its configured service credential. Visitors may retrieve anything that credential can access. Use no token or public-only access for a public demo; keep private-repository credentials behind suitable app access controls. Never place tokens or model keys in workflow inputs, source code, or exported files.

## Usage

The workflow accepts one required `issue_url`, up to 256 characters:

```text
https://github.com/langgenius/dify/issues/44141
```

Expect an English report grounded in the retrieved issue and comments. Compare its source links and scope with GitHub before using the reply.

For an error example, enter `https://example.com/issue/1`. Expect `INVALID_ISSUE_URL`, correction guidance, and no LLM execution.

The Tool's optional `max_comments` accepts integers **0–30**, default **10**; zero skips comments. Its JSON envelope contains `ok`, `issue`, `comments`, `meta`, and `error`. See [plugin details](plugin/README.md) for its schema and error codes.

## Limits and errors

GitHub.com issues are supported; pull requests, Enterprise hosts, repository code inspection, and attachment retrieval are excluded. Comment counts and text are bounded, and long content can be truncated. Metadata preserves these limitations for the report.

Issue content is untrusted data. Prompts instruct the model to ignore embedded instructions, cite supplied sources, and distinguish facts from hypotheses. Priorities and replies are suggestions requiring human review.

The Tool sanitizes authentication, access, rate-limit, timeout, and upstream-response errors. Unexpected platform, plugin-loading, or Code exceptions may stop the workflow rather than reach its handled-error branch.

## Local development

From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\plugin\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The 33 plugin tests use the installed SDK with mocked HTTP responses. They do not require network access or credentials. To rebuild using the official CLI:

```powershell
dify plugin package .\plugin
```

Use the reported package path. Check supplied artifact hashes in [SHA256SUMS.txt](SHA256SUMS.txt).

## License

[MIT](LICENSE).
