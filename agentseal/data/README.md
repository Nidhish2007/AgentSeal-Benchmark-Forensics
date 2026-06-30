# AgentSeal local artifacts

The beta wheel published in GitHub Releases includes the local audit artifacts:

- `codeseal_model.sqlite` — CodeSeal content-overlap index
- `stack_v2.bloom` — Stack v2 repository-membership Bloom filter
- `swebench_verified.parquet` — bundled SWE-bench Verified sample data
- `swebench_pro.parquet` — bundled SWE-bench Pro data

These artifacts are intentionally not committed into the normal Git history because
`codeseal_model.sqlite` is larger than GitHub's normal 100 MiB file limit. Use one
of these options:

1. Recommended for beta users: install the wheel from the GitHub Release.
2. For source development with artifacts: copy the artifact files from an installed
   wheel into this directory.
3. For maintainers: track large artifacts with Git LFS or publish them as release assets.
