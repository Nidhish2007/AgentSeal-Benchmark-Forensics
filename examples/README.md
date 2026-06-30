# Examples

Generated reports are intentionally not committed in this beta repository because real reports may contain third-party source snippets and live evidence URLs.

To create a local example after installation:

```bash
agentseal audit --sample 10 --out ./agentseal_reports/example.json
agentseal report ./agentseal_reports/example.json --format html
```

Review generated reports before publishing them.
