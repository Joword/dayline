#!/usr/bin/env bash
# CI review pipeline for one member tool (plan §8.3). CI-system neutral: call it from a
# GitLab / Gitea / Jenkins job that has docker, python3 and the Dayline backend installed
# (`pip install ./backend` provides `dl`).
#
#   tools/ci/review.sh tools/<name>            pull request: lint, dependency scan, build,
#                                              run hardened, contract test + examples
#   tools/ci/review.sh tools/<name> --submit   after merge: also push the image and submit
#                                              tool.yaml + digest + tools/list for review
#
# --submit needs DAYLINE_URL (e.g. https://dayline.corp/v1) and DAYLINE_API_TOKEN (the service
# token). Artifacts go to $OUT_DIR (default: out/): <name>.check.json, <name>.definition.json.
#
# CI checks the contract; it does not enforce network_egress. Production does (egress proxy).
set -euo pipefail

DIR=${1:?usage: tools/ci/review.sh tools/<name> [--submit]}
DIR=${DIR%/}
SUBMIT=${2:-}
OUT=${OUT_DIR:-out}
mkdir -p "$OUT"

field() {
  python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' \
    "$DIR/tool.yaml" "$1"
}

# 0. the manifest itself (same validation as the server)
python3 -c 'import sys, pathlib; from app.tools.manifest import load_manifest; load_manifest(pathlib.Path(sys.argv[1]))' "$DIR"
NAME=$(field name)
VERSION=$(field version)
LANGUAGE=$(field language)
IMAGE=$(field image)
IMAGE=${IMAGE%%@*}
TAG="$IMAGE:$VERSION-${GIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
echo "== $NAME v$VERSION ($LANGUAGE) -> $TAG"

# 1. lint + known-vulnerability scan of the dependencies
case "$LANGUAGE" in
  python)
    ruff check "$DIR"
    pip-audit --strict -r "$DIR/requirements.txt"
    ;;
  typescript | javascript)
    (cd "$DIR" && npm ci --no-audit --no-fund && npx tsc --noEmit && npm audit --omit=dev --audit-level=high)
    ;;
  *)
    echo "!! no automated lint for language '$LANGUAGE': the reviewer checks it by hand" >&2
    ;;
esac

# 2. build
docker build --pull -t "$TAG" "$DIR"

# 3. run it the way production does (read-only, no capabilities, non-root, limits)
TOKEN="dlk_ci$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
CID=$(docker run -d --rm \
  -e DAYLINE_TOOL_NAME="$NAME" -e DAYLINE_TOOL_TOKEN="$TOKEN" -e PORT=8080 \
  --read-only --tmpfs /tmp:size=64m --cap-drop ALL --security-opt no-new-privileges:true \
  --user 65532:65532 --memory 256m --pids-limit 128 \
  -p 127.0.0.1::8080 "$TAG")
trap 'docker logs "$CID" > "$OUT/$NAME.container.log" 2>&1 || true; docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
PORT=$(docker port "$CID" 8080/tcp | head -n1 | sed 's/.*://')
URL="http://127.0.0.1:$PORT/mcp"
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL" || true)
  [ "$code" = "401" ] && break  # up, and refusing calls without the tool token
  sleep 1
done
[ "$code" = "401" ] || { echo "!! the tool must answer 401 without its token (got '$code')" >&2; exit 1; }

# 4. contract test: tools/list rules, schema checks, examples/*.json
dl tools check "$DIR" --url "$URL" --token "$TOKEN" --write-definition "$OUT/$NAME.definition.json" --json \
  | tee "$OUT/$NAME.check.json"

[ "$SUBMIT" = "--submit" ] || exit 0

# 5. publish the image and register this exact build for developer review
: "${DAYLINE_URL:?DAYLINE_URL is required for --submit}" "${DAYLINE_API_TOKEN:?DAYLINE_API_TOKEN is required for --submit}"
docker push "$TAG"
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$TAG")
DIGEST=${DIGEST##*@}
python3 - "$DIR/tool.yaml" "$OUT/$NAME.definition.json" "$DIGEST" > "$OUT/$NAME.submit.json" <<'EOF'
import json, sys, yaml
manifest = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
definition = json.load(open(sys.argv[2], encoding="utf-8"))
print(json.dumps({"manifest": manifest, "definition": definition, "image_digest": sys.argv[3]}, ensure_ascii=False))
EOF
curl -fsS -X POST "${DAYLINE_URL%/}/internal/tools/submit" \
  -H "Authorization: Bearer $DAYLINE_API_TOKEN" -H "X-Dayline-Client: ci" -H "Content-Type: application/json" \
  --data-binary @"$OUT/$NAME.submit.json" | tee "$OUT/$NAME.submitted.json"
echo
echo "== submitted $NAME $DIGEST; a developer reviews it with: dl tools show $NAME"
