# Token setup guide (Beta)

AgentSeal can run local checks without tokens when benchmark rows are already available. Tokens make remote workflows stronger.

## What each token is used for

| Token | Needed for | Not needed for |
|---|---|---|
| GitHub token | Higher GitHub API limits, PR/diff fetches, code-search evidence, exact public evidence URLs | Local CodeSeal/Bloom checks on already-loaded rows |
| HuggingFace token | Gated/private HF datasets, accepted-license datasets, private datasets you can access | Bundled SWE-bench Verified/Pro data, local files, public HF datasets |

Never commit tokens, paste them into GitHub issues, or include them in screenshots.

## GitHub token

### Why use it?

A GitHub token gives AgentSeal a higher authenticated API limit and improves public evidence verification. It helps AgentSeal fetch source metadata and preserve exact evidence URLs when GitHub allows access.

### Recommended permissions

For public beta audits, use the least-privilege token that works for your target data.

Recommended starting point:

- Fine-grained token when possible.
- Read-only access only.
- Select only repositories you need if private repositories are involved.
- Set an expiration date.

GitHub supports fine-grained and classic personal access tokens. GitHub recommends fine-grained tokens when possible, while noting that fine-grained tokens do not support every task that classic tokens support. GitHub also warns that personal access tokens act like passwords and should be kept secure.

### How to create a GitHub token

1. Open GitHub.
2. Click your profile picture.
3. Open **Settings**.
4. Open **Developer settings**.
5. Open **Personal access tokens**.
6. Choose **Fine-grained tokens** when possible.
7. Click **Generate new token**.
8. Give it a clear name such as `agentseal-beta-audit`.
9. Set an expiration.
10. Choose the minimum read-only access needed.
11. Generate and copy the token once.

If a fine-grained token does not work for a specific GitHub API path, create a classic token with the minimum scopes you need. For public-source auditing, avoid write/admin scopes.

### Add it to AgentSeal

Inside the TUI:

```text
/token paste
/token test
```

Other accepted forms:

```text
/token github_pat_...
/token file C:\Users\you\token.txt
```

PowerShell environment variable:

```powershell
$env:GITHUB_TOKEN="github_pat_..."
agentseal
```

macOS/Linux environment variable:

```bash
export GITHUB_TOKEN="github_pat_..."
agentseal
```

### Remove it

```text
/token clear
```

If a token is accidentally exposed, revoke it from GitHub immediately.

## HuggingFace token

### Why use it?

A HuggingFace token is needed when `/auto` tries to access a gated/private dataset or a dataset that requires you to accept terms on HuggingFace.

### Recommended permissions

Use a read-only token for AgentSeal dataset downloads unless you specifically need write access for another workflow. HuggingFace documents that user access tokens can have read or write permissions, and recommends read tokens when write access is not needed.

### How to create a HuggingFace token

1. Open HuggingFace.
2. Open your profile/settings.
3. Go to **Access Tokens**.
4. Click **New token**.
5. Give it a name such as `agentseal-beta-audit`.
6. Select read-only access.
7. Generate and copy the token.

If a dataset is gated, also open that dataset page in your browser and accept the dataset terms with the same HuggingFace account.

### Add it to AgentSeal

Inside the TUI:

```text
/hf paste
/hf test
```

PowerShell:

```powershell
$env:HF_TOKEN="hf_..."
agentseal
```

macOS/Linux:

```bash
export HF_TOKEN="hf_..."
agentseal
```

### Remove it

```text
/hf clear
```

## Token troubleshooting

### `/token test` fails

Check:

- The token was copied fully.
- It was not line-wrapped or truncated.
- It has not expired.
- Your organization does not require SSO approval.
- Your organization allows the selected token type.

### `/hf test` works but `/auto` still fails

Check:

- The dataset is public or you have accepted its terms.
- You are logged into the same HuggingFace account that created the token.
- The token has read access.
- The dataset ID is correct.

### The tool downloads without an HF token

That can be normal. Public HuggingFace datasets may download without authentication. HF token is required for gated/private datasets, not for every `/auto` run.

## Official references

- GitHub: Managing your personal access tokens — https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub: Authenticating to the REST API — https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api
- HuggingFace: User access tokens — https://huggingface.co/docs/hub/en/security-tokens
- HuggingFace Hub quickstart — https://huggingface.co/docs/huggingface_hub/en/quick-start
