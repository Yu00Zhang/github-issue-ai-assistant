# Privacy policy

This plugin makes read-only HTTPS requests to `https://api.github.com` for the issue URL supplied by the user and, when requested, up to 30 of its oldest comments. It does not create issues, post comments, or modify repositories.

An optional GitHub token is received through Dify's provider credential mechanism and sent only in the Authorization header to GitHub's API. The plugin does not include the token in its output or logs. Credential validation uses `GET /user` when a token is supplied. Without a token, public issues are fetched anonymously. HTTP redirects and environment proxy settings are disabled.

Returned data includes the issue title, body, labels, author handle, dates, and GitHub URL, plus selected comment bodies, author handles, dates, and URLs. Issue bodies are capped at 20,000 characters and comment bodies at 2,000 characters each. Metadata identifies truncation and omitted comments; numeric GitHub rate-limit metadata may also be returned. The plugin returns sanitized errors instead of raw HTTP exceptions or API error responses.

Your Dify application may send the returned issue and comment data to its configured model provider. Dify, GitHub, and the model provider apply their own retention and privacy policies. Choose a model provider approved for the data being processed.

A published Web App uses the workspace's configured GitHub credential. Visitors do not automatically inherit their own GitHub permissions. Use an empty or public-only credential for a public demo, and restrict access to apps whose credential can read private repositories.

The plugin has no database, persistent cache, analytics, or telemetry endpoint, and it requests no Dify model, storage, or tool invocation permissions. Dify may independently retain provider credentials, workflow inputs, outputs, and logs according to workspace settings. The plugin's lack of persistent storage does not change those platform policies.

Issue and comment text is untrusted external content. The consuming application should treat it as evidence rather than instructions and preserve the supplied completeness and truncation flags.
