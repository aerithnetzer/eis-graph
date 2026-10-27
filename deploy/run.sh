#!/bin/sh
# Lambda entry point (handler "run.sh"): the Lambda Web Adapter layer starts this, waits for the server to answer on
# $PORT, then forwards each function-URL request to it as plain HTTP.
export PYTHONPATH="$LAMBDA_TASK_ROOT:$PYTHONPATH"
exec python3 -m eisgraph.mcp_server --deployed
