# resolve_profile: optional workspace injection

`claudewheel/profile.py:27` — `resolve_profile(name)` hardcodes `Workspace.default()` (the sole
reader of `CLAUDEWHEEL_CONFIG_DIR`). Library consumers and their tests have no way to resolve
profiles against an injected/isolated workspace; test suites that exercise consumers end up
touching the real `~/.claudewheel` unless they monkeypatch.

Fix (additive, zero call-site breaks — all 16 known call sites pass a single positional arg):

    resolve_profile(name, *, workspace: Workspace | None = None)

with `None` → `Workspace.default()`. Add tests covering injected-workspace resolution. This is
the library-level seam; consumer test isolation builds on it.
