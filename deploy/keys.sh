#!/usr/bin/env bash
# Manage API keys for the deployed MCP server. Changes take effect within about a minute; no redeploy.
#
#   deploy/keys.sh add NAME      mint a key for NAME (a person or client) and print it once
#   deploy/keys.sh revoke NAME   stop NAME's key from working
#   deploy/keys.sh list          labels and creation dates
#
# The keys live in one SSM Parameter Store SecureString, /eis-graph/mcp/api-keys, as
# {"NAME": {"sha256": "<hex digest of the key>", "created": "<UTC time>"}}. Only digests are stored: a key is shown
# when it is minted and can't be recovered after, so a lost key is revoked and re-minted.
set -euo pipefail
export AWS_REGION=us-east-2 AWS_PAGER="" AWS_CLI_AUTO_PROMPT=off
PARAM=/eis-graph/mcp/api-keys
CMD=${1:-}
NAME=${2:-}

current() {  # the key list; {} only if the parameter doesn't exist yet, any other error aborts
  local out
  if out=$(aws ssm get-parameter --name "$PARAM" --with-decryption --query Parameter.Value --output text 2>&1); then
    printf '%s' "$out"
  elif [[ $out == *ParameterNotFound* ]]; then
    echo '{}'
  else
    echo "can't read $PARAM: $out" >&2
    return 1
  fi
}

save() {  # value on stdin; a standard-tier parameter holds 4 KB, roughly 30 keys
  local value
  value=$(cat)
  [[ $value == \{* ]] || { echo "refusing to save an empty or malformed key list" >&2; exit 1; }
  if [ ${#value} -gt 4096 ]; then
    echo "key list would exceed Parameter Store's 4 KB standard limit; revoke unused keys first" >&2
    exit 1
  fi
  aws ssm put-parameter --name "$PARAM" --type SecureString --overwrite --tier Standard \
    --description "EIS graph MCP server API key digests (managed by deploy/keys.sh)" --value "$value" >/dev/null
}

case "$CMD" in
  add)
    [[ $NAME =~ ^[A-Za-z0-9._-]+$ ]] || { echo "usage: deploy/keys.sh add NAME (letters, digits, . _ -)"; exit 1; }
    KEY="eisg_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    SHA=$(printf %s "$KEY" | sha256sum | cut -d' ' -f1)
    current | NAME="$NAME" SHA="$SHA" python3 -c '
import json, os, sys, datetime
d = json.load(sys.stdin)
n = os.environ["NAME"]
if n in d:
    sys.exit(n + " already has a key; revoke it first")
d[n] = {"sha256": os.environ["SHA"],
        "created": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
print(json.dumps(d, separators=(",", ":")))' | save
    echo "API key for $NAME (shown only now; active within a minute):"
    echo "  $KEY"
    ;;
  revoke)
    [ -n "$NAME" ] || { echo "usage: deploy/keys.sh revoke NAME"; exit 1; }
    current | NAME="$NAME" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
n = os.environ["NAME"]
if d.pop(n, None) is None:
    sys.exit("no key named " + n)
print(json.dumps(d, separators=(",", ":")))' | save
    echo "revoked $NAME (stops working within a minute)"
    ;;
  list)
    current | python3 -c '
import json, sys
d = json.load(sys.stdin)
for name, e in sorted(d.items()):
    print(name.ljust(24), "created", e.get("created", "?"))
print(f"{len(d)} key(s)")'
    ;;
  *)
    sed -n 2,7p "$0"; exit 1 ;;
esac
