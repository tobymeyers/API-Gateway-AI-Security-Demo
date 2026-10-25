#!/usr/bin/env bash
# Packages the two Lambdas needing external dependencies (the orchestrator
# - anthropic[bedrock] - and the JWT authorizer - PyJWT[crypto]) into
# infra/build/*.zip. The three service Lambdas and the trigger-agent Lambda
# are pure boto3 and are defined inline in infra/template.yaml, so they
# need no build step.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/infra/build"

# --platform/--only-binary force manylinux wheels regardless of the host
# OS - without this, building on macOS (or anything non-Linux) bundles
# Rust-backed deps (pydantic-core/jiter, PyJWT's cryptography backend)
# compiled for the wrong platform, and the Lambda fails at import time.
# Matches both functions' default architecture (x86_64) in template.yaml -
# switch to manylinux2014_aarch64 if you ever set Architectures: [arm64].
build_package() {
  local name="$1"
  local pkg_dir="$BUILD_DIR/${name}_pkg"

  rm -rf "$pkg_dir"
  mkdir -p "$pkg_dir"

  echo "Installing $name dependencies for manylinux2014_x86_64/py3.12..."
  python3 -m pip install \
    --upgrade \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    -r "$ROOT_DIR/functions/$name/requirements.txt" \
    -t "$pkg_dir"

  for f in "$ROOT_DIR/functions/$name"/*.py; do
    cp "$f" "$pkg_dir/"
  done

  rm -f "$BUILD_DIR/$name.zip"
  (cd "$pkg_dir" && zip -r -q "$BUILD_DIR/$name.zip" .)
  echo "Built $BUILD_DIR/$name.zip"
}

build_package orchestrator
cp "$ROOT_DIR/openapi/gateway-openapi.yaml" "$BUILD_DIR/orchestrator_pkg/"
(cd "$BUILD_DIR/orchestrator_pkg" && zip -r -q "$BUILD_DIR/orchestrator.zip" gateway-openapi.yaml)

build_package jwt_authorizer
