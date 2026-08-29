#!/usr/bin/env bash
# One-time bootstrap for the OMNI-29 run box (docs/AWS_RUN.md).
#
# Invoked as root by the instance's user_data after it clones the repo to
# /home/ubuntu/omnibase; everything user-level runs as ubuntu. Logged by
# user_data to /var/log/sis-bootstrap.log. Idempotent enough to re-run by hand
# if a step fails partway.
set -euo pipefail

REPO_DIR=/home/ubuntu/omnibase

# --- system layer (root) --------------------------------------------------
# The lock timeout matters at boot: unattended-upgrades runs on the apt-daily
# timer at exactly the moment user_data does, and without waiting for the lock
# `set -e` aborts the bootstrap partway (see the same option in main.tf).
APT_WAIT="-o DPkg::Lock::Timeout=600"
apt-get $APT_WAIT update
DEBIAN_FRONTEND=noninteractive apt-get $APT_WAIT install -y docker.io git curl jq unzip
systemctl enable --now docker
usermod -aG docker ubuntu

# AWS CLI v2: not preinstalled on Ubuntu AMIs, needed for the secret fetch and
# the artifacts sync (docs/AWS_RUN.md "The run itself").
if ! command -v aws >/dev/null; then
    curl -sSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
    unzip -q /tmp/awscliv2.zip -d /tmp
    /tmp/aws/install
    rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# --- user layer (ubuntu) --------------------------------------------------
# Python 3.14 via uv: 24.04's apt does not carry 3.14, and the standard CPython
# build is required — NOT free-threaded; Ray ships no cp314t wheels (CLAUDE.md).
sudo -u ubuntu -H bash -s <<'AS_UBUNTU'
set -euo pipefail
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.14
uv tool install poetry
cd "$HOME/omnibase"
poetry env use "$(uv python find 3.14)"
poetry install --with real --with llm
AS_UBUNTU

# The kernel-enforced sandbox image. A real (non-stub) proposer REQUIRES
# SIS_SANDBOX=docker (M1) — and on EC2 the sandbox's --network none is also
# what keeps candidate code away from the IMDS credential endpoint.
sudo -u ubuntu docker build -t sis-gauntlet:latest \
    -f "$REPO_DIR/Dockerfile.gauntlet" "$REPO_DIR"

echo "sis bootstrap complete"
