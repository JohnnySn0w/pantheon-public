---
surface: above-bar
headline: litellm 1.107.0 (PyPI, 2026-10-21) shipped a build-pipeline-injected credential stealer that targets ~/.claude and ~/.cursor configs; no litellm dependency found in Pantheon repo, but rotate-if-installed applies. Rest of the week is routine MCP CVE churn.
---

# mcp-supply-chain watch, window 2026-10-26 to 2026-11-02

## Summary

- One new in-class incident: **litellm 1.107.0 compromise** (see below). Not MCP-specific, but it is a post-trust AI-tooling supply-chain hit with credential exfiltration aimed at Claude/Cursor config dirs.
- OSV malware feed: 487 MAL ids touched the window across npm and PyPI; 24 matched the AI-tooling name regex (list below). Mostly typosquats and low-reach packages.
- NVD: 31 CVEs matched "MCP" published in-window. Mostly command-injection in small MCP servers. One notable: official MCP Python SDK audience-validation flaw.
- HN: little signal (see Instruments).

## Incident: litellm 1.107.0 (primary + secondary)

OSV record MAL-2026-2144 (originally the March TeamPCP compromise, 1.82.7/1.82.8) was modified 2026-10-29 to add 1.107.0. Primary record: <https://storage.googleapis.com/osv-vulnerabilities/PyPI/MAL-2026-2144.json>.

- Claim: 1.107.0 was uploaded 2026-10-21 and carries a stealer plus RCE backdoor absent from the git tag. Quote: "litellm 1.107.0, uploaded to PyPI on 2026-10-21 between 11:39 and 11:40 UTC, contains a credential stealer and a remote code execution backdoor that are not in the upstream source (the v1.107.0 git tag of BerriAI/litellm does not contain them)".
- Trigger is a `.pth` file, so it fires at every interpreter start without importing litellm. Quote: "the payload is triggered at every Python interpreter start, without importing litellm."
- Exfil targets include AI dev tools. Quote: "AI developer tool configurations (~/.cursor, ~/.claude, ~/.kiro, Windsurf)"; posted to "http://www.litellm.dev/healthcheck".
- Remediation per record: "Any environment where 1.107.0 was installed should be treated as compromised and its credentials rotated." Clean: 1.106.2 and 1.107.0rc1/rc2.
- Disagreement: a second OSV source (amazon-inspector) in the same record says 1.107.0 showed no malicious paths ("No install-time or import-time code paths were observed..."); the google-open-source-security and multiple news sources contradict it. Treat as inspector scanning a different artifact (sdist was clean per secondary coverage). [weak on the inspector side]
- Secondary corroboration (via search summary, not fetched in full): The Hacker News, BleepingComputer, StepSecurity, Datadog Security Labs, Endor Labs. They report Docker `latest` images also affected, a roughly two-hour PyPI exposure, and attacker use of a lookalike repo to run the release workflow. Datadog says no link to the March TeamPCP attack is established.
- Pantheon exposure: grep of pantheon-config tools/*.py, *.txt, *.toml found no litellm reference. Home-dir seats and other repos (gpu-ml VM210 venvs, laptop) not checked. [open question]

## OSV malware-feed hits (regex-matched, in-window published or modified)

Method: both `modified_id.csv` feeds fetched, MAL ids with CSV timestamp >= 2026-10-25 retained (487), JSON fetched (0 errors), filtered on the brief's regex, deduplicated by id+ecosystem+package. One withdrawn record flagged. Record URL pattern: `https://storage.googleapis.com/osv-vulnerabilities/<npm|PyPI>/<id>.json`.

New in-window (published 2026-10-25 or later):
- PyPI: MAL-2026-18296 zagents-message-bus (version 99.99.100, dependency-confusion pattern, amazon-inspector); 18327 zagents-framework; 18297 nebulallm; 18298/18299/18300 claudeopusfile, claudeopuseni, claudeopusenifile; 18427 skills-analyzer (malicious hidden in pinned dependency `is-it-ai`, kam193 campaign report).
- npm: MAL-2026-18335 ctf-agent; 18346 agentenact; 18425 local-commander-agent.

Published earlier, modified in-window (re-triage or added detail):
- npm: 18054 @decoopus/claude-setup (impersonates official Anthropic Claude Desktop setup CLI, "Claude Max 20x" banner), 18055 api-anthropic, 18059 claude-desktop-setup, 18060 codex-setup, 18099 formatting-subtitles-for-claude, 18192 agentic-awesome-skills, 18193 opencode-skills-collection, 18220 skillward, 16355 gemini-computer-use, 17515 monitoring-agent.
- PyPI: 17195 claudedashbord, 3429 openai-spellchecker, 3638 openai-spellcheckers, 2144 litellm.
- Withdrawn (retraction): npm MAL-2026-4365 `@atlisp/mcp`, withdrawn 2026-05-26, record modified 2026-10-28. Not a live threat.
- Note: 18192/18193/18220 are amazon-inspector keyword-pattern detections on skills collections / a scanner's own rule files; false-positive risk is high. [weak]

Pattern synthesis: the campaign shape this week is impersonation of Claude/Codex/Anthropic tooling (setup CLIs, `claude*` typosquats) rather than MCP-server rugpulls. No rugpull of a popular MCP server found.

## CVEs (NVD keyword "MCP", published 2026-10-26 to 2026-11-02)

Volume: **31 raw CVE ids across ~30 projects; largest single-project batch: 2 (Royal MCP WordPress plugin CVE-2026-111960/111961; AI Engine WordPress plugin CVE-2026-107745/110732).** Five-to-seven of the 31 are keyword noise (Linux kernel CVE-2026-99164, `open` CVE-2026-119025, LM Studio, Functype, etc). No single-project backlog batch, so no batch adjustment: raw = batch-adjusted. Week-over-week comparison not done (no prior-week count in hand). [open]

A cluster of ~9 ids in the CVE-2026-1175xx range (ai-sdlc plugin, DroidMind, ATXP Gemini CLI extension, Codefriar sf-mcp, esp-mcp, mcp-ffmpeg, Kali-GPT, nmap-mcp, hagent) share 8.5 scores and a "through commit/version X" phrasing, consistent with one CNA bulk-assigning across many projects. These are different projects, so not a single-project batch. Fixed-version status not checked per project (many say "through commit", i.e. likely unfixed). [weak: bulk-CNA inference]

Notable:
- **CVE-2026-116684, MCP Python SDK (official), CVSS 6.8.** Quote: "server-side bearer authentication configured through AuthSettings with token_verifier accepts a valid token issued for another service when the authorization server serves multiple resources and the TokenVerifier does not independently validate the aud audience." Affects 1.10.0 until 1.30.0 and 2.2.0; "fixed in versions 1.30.0 and 2.2.0". Published 2026-10-27. Pantheon exposure: only if we run an MCP server using SDK AuthSettings with multi-resource auth; not checked.
- CVE-2026-93328 Google Cloud MCP Toolbox for Databases, CVSS 9.3, path traversal + missing authz in cloud-storage source.
- CVE-2026-62863 MoLing MCP server, 9.6, SSE mode (fixed 0.5.0). CVE-2026-120338 Prefab UI sandbox, 10.0 (Pyodide sandbox). CVE-2026-85297 Google Cloud Run MCP bucket squatting (fixed 1.11.0). CVE-2026-115413 AWS API MCP Server OAuth audience validation (fixed 1.6.0).
- CVE-2026-61542 Gearboy embedded MCP server 8.6; CVE-2026-61546 AnkiMCP 8.8; CVE-2026-118378 Tabularis SQLite SQLi via MCP clients 8.5.
- Clients: no Claude Code or Cursor CVE found by NVD "Claude Code" keyword; only third-party orchestrator praktor (CVE-2026-62331, Telegram upload). Continue (CVE-2026-116109, 2.1, MCPManager) is low severity.
- Related HN item (not a CVE): "Breaking the AWS MCP Python Sandbox" writeup, 2026-10-27, 3 points, <https://xplo1t-sec.github.io/posts/breaking-the-aws-mcp-python-sandbox/> [single-source, not read].

## Not checked / negatives with instruments

- modelcontextprotocol/servers releases, OWASP MCP Top 10 updates: not fetched this run (budget); open.
- HN: query "MCP server vulnerability" returned HTTP 200 with zero hits since 2026-10-25; second query "MCP" returned 10 hits (all Show HN tool launches plus the AWS sandbox post). Queries "Model Context Protocol security" and "MCP malicious npm" returned empty with 200. Low HN signal is plausibly real, not an instrument failure, since "MCP" itself worked.
- Prior anchors (Postmark rugpull Sept 2025, Anthropic MCP RCE April 2026): no new incident of that exact shape found.

## Follow-ups

- Audit litellm install footprint — check gpu-ml VM210, laptop and seat venvs for litellm 1.107.0 (or `cook.pth` in site-packages); rotate credentials if found.
- Check MCP SDK pin — confirm any Pantheon MCP server using Python SDK AuthSettings is on >=1.30.0 / 2.2.0.
- Seed-source gap — add a fetch of modelcontextprotocol/servers releases and OWASP MCP Top 10 to the next run (skipped here).

## Sources

- <https://storage.googleapis.com/osv-vulnerabilities/PyPI/modified_id.csv> and <https://storage.googleapis.com/osv-vulnerabilities/npm/modified_id.csv>: primary OSV feeds.
- <https://storage.googleapis.com/osv-vulnerabilities/PyPI/MAL-2026-2144.json>: primary, litellm record (also cites BerriAI/litellm issue 24518, wiz.io, futuresearch.ai).
- Per-record JSONs under the same bucket for the MAL ids listed above: primary.
- <https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch=MCP&pubStartDate=2026-10-26T00:00:00.000&pubEndDate=2026-11-02T23:59:59.000>: primary NVD API (CVE list, scores).
- NVD CVE-2026-116684 record: primary, MCP Python SDK.
- <https://thehackernews.com/2026/10/litellm-suspects-github-token-leak-in.html>, <https://www.stepsecurity.io/blog/litellm-1-107-0-compromised-pypi-backdoored-wheels-credential-stealer>, <https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-docker/>, <https://www.endorlabs.com/learn/litellm-1-107-0-compromised-python-startup-hooks-steal-cloud-ai-and-proxy-credentials>: secondary, seen as search results only, not fetched.
- <https://hn.algolia.com/api/v1/search>: HN Algolia API, queries above.
