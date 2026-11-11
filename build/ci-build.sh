#!/usr/bin/env bash
# Runs on the container host. Usage: ci-build.sh <ref-name> <sha>
# Env: RELEASE_TOKEN, FORGEJO_URL, REPOSITORY  (clone + publish release for v* tags)
#      GH_TOKEN (optional) + GH_REPOSITORY (default mabayhan/image-renamer-py):
#        push main + the tag to GitHub and publish the same release there
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
    docker run --rm -v "$build_dir":/src -e APP_VERSION="${version:-dev}" \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$image"
done
out="$build_dir/dist"
(cd "$out" && sha256sum ImageRenamer-* > SHA256SUMS)

label="${version:-$(git -C "$build_dir" rev-parse --short HEAD)}"
mkdir -p "$dist_root/$label"; cp "$out"/* "$dist_root/$label/"
echo "Artifacts saved to $dist_root/$label"
[[ -z "$version" ]] && exit 0

if [[ -n "${RELEASE_TOKEN:-}" ]]; then
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
fi

# ---- GitHub mirror: push code + tag, publish the release with the same binaries ----
if [[ -n "${GH_TOKEN:-}" ]]; then
    gh_repo="${GH_REPOSITORY:-mabayhan/image-renamer-py}"
    basic="$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)"
    git -C "$build_dir" -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $basic" \
        push "https://github.com/$gh_repo.git" "refs/remotes/origin/main:refs/heads/main" "refs/tags/$ref_name"
    gh_api="https://api.github.com/repos/$gh_repo"
    gh_auth=(-H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json")
    gh_id="$(curl -fsS "${gh_auth[@]}" "$gh_api/releases/tags/$ref_name" 2>/dev/null | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*' || true)"
    if [[ -z "$gh_id" ]]; then
        gh_id="$(curl -fsS "${gh_auth[@]}" -d "{\"tag_name\":\"$ref_name\",\"name\":\"Image Renamer $version\",\"generate_release_notes\":true}" \
            "$gh_api/releases" | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*')"
    fi
    for f in "$out"/*; do
        curl -fsS "${gh_auth[@]}" -H 'Content-Type: application/octet-stream' --data-binary "@$f" \
            "https://uploads.github.com/repos/$gh_repo/releases/$gh_id/assets?name=$(basename "$f")" > /dev/null
        echo "Uploaded $(basename "$f") to GitHub"
    done
    echo "GitHub release published: https://github.com/$gh_repo/releases/tag/$ref_name"
else
    echo "GH_TOKEN not set - skipping GitHub mirror"
fi
