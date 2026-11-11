#!/usr/bin/env bash
# Runs on the container host. Usage: ci-build.sh <ref-name> <sha>
# Env: RELEASE_TOKEN, FORGEJO_URL, REPOSITORY  (clone + publish release for v* tags)
#      DIST_ROOT (default /config/image-renamer-py-dist)
set -euo pipefail
ref_name="$1"; sha="$2"
app="image-renamer-py"
dist_root="${DIST_ROOT:-/config/$app-dist}"
build_dir="$(mktemp -d /tmp/$app.XXXXXX)"
trap 'rm -rf "$build_dir"' EXIT

git clone -q "${FORGEJO_URL/https:\/\//https://oauth2:${RELEASE_TOKEN}@}/$REPOSITORY.git" "$build_dir"
git -C "$build_dir" checkout -q "$sha"

version=""; [[ "$ref_name" == v* ]] && version="${ref_name#v}"
for platform in linux windows; do
    image="$app-$platform"
    docker build -t "$image" -f "$build_dir/build/Dockerfile.$platform" "$build_dir/build"
    docker run --rm -v "$build_dir":/src -e APP_VERSION="${version:-dev}" "$image"
done
out="$build_dir/dist"
(cd "$out" && sha256sum ImageRenamer-* > SHA256SUMS)

label="${version:-$(git -C "$build_dir" rev-parse --short HEAD)}"
mkdir -p "$dist_root/$label"; cp "$out"/* "$dist_root/$label/"
echo "Artifacts saved to $dist_root/$label"
[[ -z "$version" || -z "${RELEASE_TOKEN:-}" ]] && exit 0

api="$FORGEJO_URL/api/v1/repos/$REPOSITORY"; auth="Authorization: token $RELEASE_TOKEN"
release_id="$(curl -fsS -H "$auth" "$api/releases/tags/$ref_name" 2>/dev/null | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2 || true)"
if [[ -z "$release_id" ]]; then
    release_id="$(curl -fsS -H "$auth" -H 'Content-Type: application/json' \
        -d "{\"tag_name\":\"$ref_name\",\"name\":\"Image Renamer $version\"}" \
        "$api/releases" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)"
fi
for f in "$out"/*; do
    curl -fsS -H "$auth" -F "attachment=@$f" "$api/releases/$release_id/assets?name=$(basename "$f")" > /dev/null
    echo "Uploaded $(basename "$f")"
done
echo "Release published: $FORGEJO_URL/$REPOSITORY/releases/tag/$ref_name"
