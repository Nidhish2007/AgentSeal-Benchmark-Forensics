# Maintainer release guide

## Recommended GitHub layout

Push only the `repo/` source tree to GitHub. Do **not** commit the beta wheel into normal Git history.

Use GitHub Releases for the wheel:

1. Create a new public GitHub repository, for example `agentseal`.
2. Copy the contents of this `repo/` folder into your local repository.
3. Commit and push the source tree.
4. Create a release tag, for example `v5.0.0-beta.2`.
5. Upload `release-assets/agentseal-5.0.0-1beta2fix1-py3-none-any.whl` and `release-assets/SHA256SUMS.txt` as release assets.

## Why not commit the wheel or CodeSeal SQLite normally?

The wheel is larger than 100 MiB, and the CodeSeal SQLite artifact is larger than 100 MiB uncompressed.
GitHub blocks normal repository files over 100 MiB. Use Git LFS or GitHub Releases for large artifacts.

## Optional: Git LFS path

If you want full source + artifacts in the repository:

```bash
git lfs install
git lfs track "*.sqlite" "*.bloom" "*.parquet" "*.whl"
git add .gitattributes
```

Then copy the large artifacts into `agentseal/data/` and commit after Git LFS is active.

For public beta simplicity, Releases are cleaner.
