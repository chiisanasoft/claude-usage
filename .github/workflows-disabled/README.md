# Disabled upstream workflows

These are upstream's workflows, kept verbatim but moved out of
`.github/workflows/` so GitHub does not run them here. Nothing about them is
broken — they just don't belong to a fork:

- **`tag-on-merge.yml`** — tags a release and publishes the VS Code extension
  `.vsix` from `CHANGELOG.md`. Releases of this project are the upstream
  maintainer's to cut, and this fork must not publish artifacts under a version
  number that means something different from upstream's.
- **`extension-ci.yml`** — tests the VS Code extension. The fork changes no
  TypeScript, so it only ever re-tests upstream's code.

`tests.yml` is still active: it covers the Python code this fork actually
changes.

To restore one, move it back into `.github/workflows/`.
