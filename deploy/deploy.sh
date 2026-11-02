#!/usr/bin/env bash
# Build the Lambda zip (arm64 deps + server code), upload the read-only graph to the private data bucket under a
# content-addressed prefix (the function copies it to /tmp at cold start), and deploy deploy/template.yaml.
#
#   deploy/deploy.sh [graph-dir]     graph-dir: a build-serving output (default data/full/serving)
#
# API keys are managed separately with deploy/keys.sh (Parameter Store), with no redeploy needed.
# The zip in S3 expires after a day (Lambda keeps its own copy, and a deploy's rollback can still find it), so
# nothing is left billing.
set -euo pipefail
export AWS_REGION=us-east-2 AWS_PAGER="" AWS_CLI_AUTO_PROMPT=off
cd "$(dirname "$0")/.."
GRAPH_DIR=${1:-data/full/serving}
STACK=eis-graph-mcp
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=eis-graph-deploy-$ACCOUNT-$AWS_REGION
DATA_BUCKET=eis-graph-data-$ACCOUNT-$AWS_REGION

[ -f "$GRAPH_DIR/evidence.sqlite" ] || { echo "no build-serving output in $GRAPH_DIR"; exit 1; }
aws ssm get-parameter --name /eis-graph/mcp/api-keys --query Parameter.Name --output text >/dev/null 2>&1 ||
  echo "warning: no API keys yet, so every request will be refused; create one with deploy/keys.sh add NAME"

rm -rf build/lambda build/eis-graph-mcp.zip
uv pip install --quiet --target build/lambda --python-platform aarch64-manylinux_2_28 --python-version 3.12 \
  --only-binary :all: "mcp>=2.3.0" "pyoxigraph==0.5.13" uvicorn
mkdir -p build/lambda/eisgraph
cp eisgraph/__init__.py eisgraph/mcp_server.py eisgraph/server.py eisgraph/ontology.py build/lambda/eisgraph/
cp deploy/run.sh build/lambda/run.sh && chmod 755 build/lambda/run.sh
find build/lambda -name __pycache__ -prune -exec rm -rf {} +
python3 - <<'EOF'  # stdlib zip (no `zip` binary needed); keeps file modes so run.sh stays executable
import os, zipfile
with zipfile.ZipFile("build/eis-graph-mcp.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, _, files in os.walk("build/lambda"):
        for f in files:
            path = os.path.join(root, f)
            z.write(path, os.path.relpath(path, "build/lambda"))
EOF
CODE_KEY=eis-graph-mcp-$(sha256sum build/eis-graph-mcp.zip | cut -c1-12).zip
echo "zip: $(du -h build/eis-graph-mcp.zip | cut -f1) -> s3://$BUCKET/$CODE_KEY"

if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint=$AWS_REGION >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":1}}]}'
fi
aws s3 cp --only-show-errors build/eis-graph-mcp.zip "s3://$BUCKET/$CODE_KEY"

# Same graph files -> same prefix, so redeploying unchanged data uploads nothing new.
GRAPH_HASH=$(cd "$GRAPH_DIR" && find . -type f ! -name LOCK -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
GRAPH_PREFIX=deploy/serving-$GRAPH_HASH
aws s3 sync --only-show-errors --exclude LOCK "$GRAPH_DIR" "s3://$DATA_BUCKET/$GRAPH_PREFIX"
echo "graph: $(du -sh "$GRAPH_DIR" | cut -f1) -> s3://$DATA_BUCKET/$GRAPH_PREFIX"

aws cloudformation deploy --stack-name "$STACK" --template-file deploy/template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides CodeBucket="$BUCKET" CodeKey="$CODE_KEY" GraphBucket="$DATA_BUCKET" GraphPrefix="$GRAPH_PREFIX"

URL=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='McpUrl'].OutputValue" --output text)
echo "MCP endpoint: $URL"
