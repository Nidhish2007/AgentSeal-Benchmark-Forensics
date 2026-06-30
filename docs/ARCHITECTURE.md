# Architecture overview

High-level audit flow:

```text
input dataset rows
-> normalize repo/instance metadata
-> fetch/parse source PR or patch where available
-> local checks: patch/problem/test overlap
-> CodeSeal content-overlap lookup
-> Stack v2 Bloom repository-membership lookup
-> optional independent GitHub/HF public-source verification
-> risk scoring
-> JSON/Markdown/HTML report generation
```

Evidence is deliberately split into different classes:

- source PR / benchmark construction evidence;
- CodeSeal local corpus evidence;
- Stack v2 Bloom probable repo-membership evidence;
- independently verified public-source links.

The report renderer should never present a placeholder or broken URL as clickable evidence.
