# Slash command guide (Beta)

AgentSeal can be used from the terminal UI. Start it with:

```bash
agentseal
```

Then type slash commands into the input box.

## Recommended first run

```text
/help
/token paste
/token test
/hf paste
/hf test
/audit 10
/pro 10
/auto multi-swe-bench 10
```

Tokens are optional, but GitHub and HuggingFace tokens make some workflows stronger. See [`TOKENS.md`](TOKENS.md).

## Command reference

| Command | Example | What it does | Token needed? | Output |
|---|---|---|---|---|
| `/help` | `/help` | Shows the command menu and token hints. | No | TUI help text |
| `/audit` | `/audit 10` | Audits bundled SWE-bench Verified instances. With no number, runs the full bundled set. | GitHub optional | JSON, Markdown, HTML report |
| `/pro` | `/pro 10` | Audits bundled SWE-bench Pro instances. With no number, runs the full bundled set. | GitHub optional | JSON, Markdown, HTML report |
| `/auto` | `/auto` | Lists known remote benchmark loaders. | No | List of benchmark aliases |
| `/auto <name> [N]` | `/auto multi-swe-bench 10` | Discovers/downloads a supported benchmark, caches it, normalizes rows, then audits it. | HF may be needed for gated datasets; GitHub optional for evidence | JSON, Markdown, HTML report |
| `/wizard` | `/wizard` | Opens a local file browser for custom `.parquet`, `.jsonl`, or `.json` datasets. | No, unless GitHub evidence search is desired | JSON, Markdown, HTML report |
| `/token` | `/token` | Shows GitHub-token status. | No | Status message |
| `/token paste` | `/token paste` | Reads a GitHub token from clipboard, redacts it, and stores it for the current user. | User supplies token | Token status |
| `/token test` | `/token test` | Calls GitHub to verify the token and rate-limit status. | GitHub token | API status |
| `/token file <path>` | `/token file C:\token.txt` | Reads a GitHub token from a local text file. | User supplies token file | Token status |
| `/token clear` | `/token clear` | Removes the saved GitHub token. | No | Status message |
| `/hf` | `/hf` | Shows HuggingFace-token status. | No | Status message |
| `/hf paste` | `/hf paste` | Reads a HuggingFace token from clipboard and stores it. | User supplies token | Token status |
| `/hf test` | `/hf test` | Calls HuggingFace to verify the token. | HF token | API status |
| `/hf clear` | `/hf clear` | Removes the saved HF token. | No | Status message |
| `/status` | `/status` | Shows GitHub API/rate-limit status when possible. | GitHub optional | Status message |
| `/report` | `/report` | Opens or lists existing reports. | No | Report list/view |
| `/reports` | `/reports` | Lists recent generated reports. | No | Report list |
| `/open` | `/open` | Opens the latest report in your browser. | No | Browser tab/window |
| `/open <name>` | `/open swebench_pro` | Opens a named matching report. | No | Browser tab/window |
| `/copy` | `/copy md` | Copies a recent report path to clipboard. | No | Clipboard path |
| `/copy <fmt> <name>` | `/copy json custom_data` | Copies a specific report path by format/name. | No | Clipboard path |
| `/stop` | `/stop` | Requests that the current audit stop. | No | Stop signal/status |
| `/new` | `/new` | Clears the current screen/log and starts fresh. | No | Clean TUI view |
| `/history` | `/history` | Shows previous report history. | No | Report history |
| `/history clear` | `/history clear` | Deletes saved local report history entries. | No | Confirmation/status |
| `/theme` | `/theme` | Switches or lists visual themes. | No | Theme state |
| `/clear` | `/clear` | Clears the visible log. | No | Clean log |
| `/esc` | `/esc` | Closes the wizard/file-browser panel. | No | UI panel closes |
| `/quit` | `/quit` | Exits AgentSeal. | No | Program exits |

## Command behavior notes

### `/audit` and `/pro`

These use bundled beta datasets from the installed release wheel:

- `/audit` → SWE-bench Verified style audit.
- `/pro` → SWE-bench Pro style audit.

The number argument is a sample size:

```text
/audit 10
/pro 10
```

For a serious run, start with a sample first, inspect the report, then run a larger audit.

### `/auto`

`/auto` is for known remote benchmark families or dataset names. It first loads benchmark rows, then activates CodeSeal/Bloom/local checks after rows are available.

Examples:

```text
/auto
/auto multi-swe-bench 10
/auto swe-bench 20
/auto humaneval 50
/auto mbpp 50
/auto bigcodebench 25
/auto owner/dataset-name 10
```

Public datasets may work without an HF token. Gated/private HuggingFace datasets require `/hf paste` and successful access on your HuggingFace account.

### `/wizard`

`/wizard` is for local custom datasets. Supported beta formats:

- `.parquet`
- `.jsonl`
- `.json`

AgentSeal tries to detect common columns such as:

- `instance_id`
- `repo`
- `patch`
- `test_patch`
- `problem_statement`
- `base_commit`
- `pull_number`, `pr_number`, `html_url`, or related source metadata when present

If your dataset does not contain PR metadata, AgentSeal can still perform local text/content checks, but exact source-PR evidence may be weaker.

## Good beta workflow

```text
/token paste
/token test
/hf paste
/hf test
/audit 10
/pro 10
/auto multi-swe-bench 10
/wizard
/reports
/open
```

Read the report before trusting the result. A strong report should have clear evidence classes, no broken clickable links, and no claim that a specific model memorized an item.
