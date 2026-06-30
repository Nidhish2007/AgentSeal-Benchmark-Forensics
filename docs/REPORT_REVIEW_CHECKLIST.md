# Report review checklist

Before publishing a result, inspect the HTML and Markdown reports.

## Must pass

- No raw secret tokens appear anywhere.
- No `github.com/search?q=...` links are presented as evidence.
- No synthetic placeholder repo links are clickable evidence.
- Broken/404 evidence is rendered as `not linked`.
- CodeSeal/Bloom local corpus signals are separated from independent public evidence.
- The report does not say “proves memorized”.
- The report does not claim “all public repositories”.
- Limitations are visible.

## Recommended beta run

```text
/audit 10
/pro 10
/auto multi-swe-bench 10
```

Then open the generated HTML report and click every evidence link in the high-risk cases.
