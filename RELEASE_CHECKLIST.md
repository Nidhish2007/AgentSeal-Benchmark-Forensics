# Release checklist

Before making the repo public:

- [ ] Confirm repo name and owner in `pyproject.toml` URLs.
- [ ] Confirm license is MIT or replace `LICENSE` before publishing.
- [ ] Confirm no real tokens in repo history.
- [ ] Confirm no `.agentseal_search_cache` folder is committed.
- [ ] Confirm the wheel is uploaded as a Release asset, not normal Git file.
- [ ] Attach `release-assets/agentseal-5.0.0-1beta2fix1-py3-none-any.whl` to release `v5.0.0-beta.2`.
- [ ] Attach `release-assets/SHA256SUMS.txt`.
- [ ] In release notes, mark the project as beta.
- [ ] Run a 10-instance audit and review the report before launch.
