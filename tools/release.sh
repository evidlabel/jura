#!/bin/bash
# make release VERSION=0.2.3a1 — bump, check, commit "release X", tag vX, then (asked) push main and the tag.
#
# Refuses unless: on main, no uncommitted changes, not behind origin/main, the tag is new,
# and lint + tests pass. The version lives in pyproject.toml, src/workbench/__init__.py,
# the README's two "running release" lines and uv.lock, and as semver (0.3.1a1 ->
# 0.3.1-alpha.1) in the window: app/Cargo.toml, app/tauri.conf.json, app/Cargo.lock.
# Pushing the tag builds that window (.github/workflows/app.yml) and attaches it to the
# release, which is where jura setup fetches it: one tag, one version, by design. Nothing leaves the machine until
# you answer y; answering n leaves the commit and tag local (push them later, or undo with
# `git tag -d vX && git reset --hard HEAD~1`).
set -euo pipefail
cd "$(dirname "$0")/.."

new="${1:-}"
old="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
die() { echo "release: $*" >&2; exit 1; }

[ -n "$new" ] || die "say which version: make release VERSION=X (now $old)"
[[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]] || die "$new is not a version like 0.2.3 or 0.2.3a1"
[ "$new" != "$old" ] || die "already at $old"
[ "$(git branch --show-current)" = main ] || die "release from main (on $(git branch --show-current))"
git diff --quiet && git diff --cached --quiet || die "commit or stash your changes first"
git rev-parse -q --verify "refs/tags/v$new" >/dev/null && die "tag v$new exists"
if git fetch -q origin main 2>/dev/null; then
  [ -z "$(git rev-list HEAD..origin/main)" ] || die "main is behind origin/main: pull first"
else
  echo "release: could not reach origin; not checked against origin/main" >&2
fi

echo "release: $old -> $new"
make -s lint
make -s test

sed -i "s/^version = \"$old\"/version = \"$new\"/" pyproject.toml
sed -i "s/^__version__ = \"$old\"/__version__ = \"$new\"/" src/workbench/__init__.py
sed -i "/running release\|^jura\.sh /s/$old/$new/" README.md
uv lock -q
app_new="$(.venv/bin/python -c "from workbench.setup import app_version; print(app_version('$new'))")"
sed -i "0,/^version = \".*\"/s//version = \"$app_new\"/" app/Cargo.toml
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$app_new\"/" app/tauri.conf.json
sed -i "/^name = \"workbench-app\"\$/{n;s/^version = \".*\"/version = \"$app_new\"/}" app/Cargo.lock
grep -q "version = \"$new\"" pyproject.toml && grep -q "\"$new\"" src/workbench/__init__.py \
  && [ "$(grep -c "$new" README.md)" -ge 2 ] \
  && grep -q "^version = \"$app_new\"" app/Cargo.toml && grep -q "\"version\": \"$app_new\"" app/tauri.conf.json \
  && grep -A1 '^name = "workbench-app"$' app/Cargo.lock | grep -q "\"$app_new\"" \
  || die "the version did not land everywhere: see git diff"

git add pyproject.toml src/workbench/__init__.py README.md uv.lock app/Cargo.toml app/tauri.conf.json app/Cargo.lock
git commit -q -m "release $new"
git tag "v$new"
echo "release: committed and tagged v$new (window $app_new)"
git --no-pager log --oneline -1

read -r -p "Push main and v$new to origin? [y/N] " ok
if [ "$ok" = y ] || [ "$ok" = Y ]; then
  git push origin main
  git push origin "v$new"
else
  echo "release: not pushed. Later: git push origin main && git push origin v$new"
fi
