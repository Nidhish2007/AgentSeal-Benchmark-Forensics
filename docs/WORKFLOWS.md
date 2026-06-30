# Workflows (Beta)

## 1. Fast local smoke test

Use this to confirm the install works.

```text
/help
/audit 10
/open
```

Expected result: a small report opens locally.

## 2. SWE-bench Verified audit

```text
/token paste
/token test
/audit 10
/open
```

Then run a larger audit if the sample report looks clean.

## 3. SWE-bench Pro audit

```text
/token paste
/token test
/pro 10
/open
```

Use this for a stricter beta report. Before publishing, inspect the Markdown or HTML output with the report checklist.

## 4. Multi-SWE-bench audit

```text
/hf paste
/hf test
/token paste
/token test
/auto multi-swe-bench 10
/open
```

Notes:

- `/auto` first downloads or loads benchmark rows.
- CodeSeal and Stack v2 Bloom activate after rows are loaded.
- Public datasets may download without HF token.
- Gated datasets require a HuggingFace token and accepted dataset terms.

## 5. Custom local dataset audit

```text
/wizard
```

Choose a local `.parquet`, `.jsonl`, or `.json` dataset.

AgentSeal works best when rows include fields like:

```text
instance_id
repo
patch
test_patch
problem_statement
base_commit
pull_number / pr_number / html_url
```

If PR/source fields are missing, the audit can still run, but source-link evidence may be weaker.

## 6. Report review before publishing

```text
/reports
/open
/copy md
```

Check:

- no secrets;
- no local private paths;
- no 404 evidence links;
- no overclaiming;
- corpus signals and public-source evidence are separated.
