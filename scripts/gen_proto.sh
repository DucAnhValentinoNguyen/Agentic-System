#!/bin/sh
# Regenerate gRPC stubs into services/agent-api/app/rpc (committed, so images need no build step).
set -e
cd "$(dirname "$0")/.."
uv run python -m grpc_tools.protoc -Iproto --python_out=services/agent-api/app/rpc \
  --grpc_python_out=services/agent-api/app/rpc proto/retrieval.proto
sed -i 's/^import retrieval_pb2 as/from . import retrieval_pb2 as/' services/agent-api/app/rpc/retrieval_pb2_grpc.py
