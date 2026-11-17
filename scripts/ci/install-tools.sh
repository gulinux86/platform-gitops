#!/usr/bin/env bash
# Installs the tools CI uses, each pinned and checked against its release checksum,
# so a runner image update cannot change what fails. Bump a version with its sum.
#
# Usage: scripts/ci/install-tools.sh      (CI only — installs with sudo)
set -euo pipefail

KUBECONFORM_VERSION=v0.8.0
KUBECONFORM_SHA256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883

TMP=$(mktemp -d)

curl -sSfLo "$TMP/kubeconform.tgz" \
  "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz"
echo "$KUBECONFORM_SHA256  $TMP/kubeconform.tgz" | sha256sum -c -
tar -xzf "$TMP/kubeconform.tgz" -C "$TMP" kubeconform
sudo install "$TMP/kubeconform" /usr/local/bin/kubeconform

# PyYAML for check_tenants.py, hash-pinned, in a virtualenv: the runner's system
# Python is externally managed and its packages change with the image.
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --require-hashes -r scripts/ci/requirements.txt
echo "$TMP/venv/bin" >>"${GITHUB_PATH:-/dev/null}"

kubeconform -v
