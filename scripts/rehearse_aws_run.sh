#!/usr/bin/env bash
# Rehearse the OMNI-29 box locally — no AWS, no credentials, no money.
#
# Runs the instance's user_data and scripts/aws_bootstrap.sh on an Ubuntu 24.04
# container, then the runbook's commands as `ubuntu` through a login shell
# (the way an SSM session is told to work), with the gauntlet's docker sandbox
# on a real Linux daemon. A full stub-proposer cycle must reach
# verified_awaiting_human_merge, and the operator console must answer.
#
# Why this exists: the first rehearsal found two defects that no unit test and
# no read-through could see, each of which would have cost an EC2 lifecycle —
# the docker sandbox could not read its own temp dir on native Linux (Docker
# Desktop's file sharing ignores ownership, so every Mac run passed), and the
# box could not start the operator console (the `ui` group was not installed).
#
# Usage:  scripts/rehearse_aws_run.sh          (needs Docker; ~3 min when warm)
#         KEEP=1 scripts/rehearse_aws_run.sh   (leave the container for poking)
#
# Deliberate differences from EC2, each an environment gap, not a script edit:
#   - sudo is installed first (the AMI has it; the image doesn't);
#   - `systemctl` is a shim that starts dockerd (no systemd in a container);
#   - PID 1 delegates cgroup controllers (as docker:dind does) so the nested
#     dockerd can apply the sandbox's --memory/--cpus, which systemd does on EC2;
#   - the repo is copied from this working tree instead of cloned, so what is
#     rehearsed is what you are about to merge, not what is already on develop;
#   - it runs at the host's architecture (arm64 on Apple Silicon), which the
#     bootstrap supports; the EC2 box is x86_64.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
C=sis-rehearsal
VOL=sis-rehearsal-docker
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; [ "${KEEP:-0}" = 1 ] || { docker rm -f "$C" >/dev/null 2>&1; docker volume rm "$VOL" >/dev/null 2>&1; } || true' EXIT

say() { printf '[rehearsal] %s\n' "$*"; }
docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }

docker rm -f "$C" >/dev/null 2>&1 || true
docker volume rm "$VOL" >/dev/null 2>&1 || true
docker run -d --privileged --name "$C" --shm-size=2g -v "$VOL:/var/lib/docker" \
  ubuntu:24.04 bash -c '
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
    sed -e "s/ / +/g" -e "s/^/+/" < /sys/fs/cgroup/cgroup.controllers \
      > /sys/fs/cgroup/cgroup.subtree_control || :
    exec sleep infinity' >/dev/null
sleep 2
say "box up: $(docker exec "$C" uname -m); cgroups delegated: $(docker exec "$C" cat /sys/fs/cgroup/cgroup.subtree_control)"

cat > "$WORK/systemctl" <<'EOF'
#!/bin/bash
# Rehearsal shim: no systemd here. "enable --now docker" starts dockerd.
if [[ "$*" == *docker* ]]; then
  (dockerd >/var/log/dockerd.log 2>&1 &)
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done
  exit 1
fi
exit 0
EOF
docker exec "$C" bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo >/dev/null'
docker cp "$WORK/systemctl" "$C:/usr/local/sbin/systemctl"
docker exec "$C" chmod 755 /usr/local/sbin/systemctl

# The repo as the box would see it: tracked files, working-tree content.
(cd "$REPO" && git ls-files -z | COPYFILE_DISABLE=1 tar --null -T - -cf -) \
  | docker exec -i "$C" bash -c 'mkdir -p /home/ubuntu/omnibase && tar -xf - -C /home/ubuntu/omnibase 2>/dev/null'

# user_data, minus the clone (see infra/aws/main.tf).
say "user_data + bootstrap ..."
start=$(date +%s)
if ! docker exec "$C" bash -c '
    set -euo pipefail
    exec > /var/log/sis-bootstrap.log 2>&1
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y git
    chown -R ubuntu:ubuntu /home/ubuntu/omnibase
    bash /home/ubuntu/omnibase/scripts/aws_bootstrap.sh'; then
  say "BOOTSTRAP FAILED — tail of /var/log/sis-bootstrap.log:"
  docker exec "$C" tail -30 /var/log/sis-bootstrap.log
  exit 1
fi
say "bootstrap ok in $(( $(date +%s) - start ))s"

# "The run itself", minus real credentials, as ubuntu via a login shell.
cat > "$WORK/run.sh" <<'EOF'
#!/bin/bash
set -uo pipefail
cd ~/omnibase
fail=0
check() { if [ "$2" = ok ]; then echo "  ✓ $1"; else echo "  ✗ $1 — $3"; fail=1; fi; }

p=$(command -v poetry || true)
[ -n "$p" ] && check "poetry on PATH after sudo -iu ubuntu" ok || check "poetry on PATH" no "not found"
deps=$(poetry run python -c 'import ray, panel, boto3, anthropic, hypothesis; print("ok")' 2>&1 | tail -1)
[ "$deps" = ok ] && check "real + llm + ui dependencies import" ok || check "dependencies" no "$deps"

export SIS_SANDBOX=docker
poetry run python main.py > /tmp/cycle.log 2>&1
status=$(grep -oE "cycle status: [a-z_]+" /tmp/cycle.log | head -1)
[ "$status" = "cycle status: verified_awaiting_human_merge" ] \
  && check "full cycle in the docker sandbox ($status)" ok \
  || check "full cycle in the docker sandbox" no "${status:-no status} — see /tmp/cycle.log"

SIS_FRONTEND_AUTH=none timeout 90 poetry run python -m sis.frontend > /tmp/fe.log 2>&1 &
code=000
for _ in $(seq 1 60); do code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true); [ "$code" != 000 ] && break; sleep 1; done
[ "$code" = 200 ] && check "operator console answers on 127.0.0.1:8080" ok || check "operator console" no "HTTP $code — see /tmp/fe.log"
exit $fail
EOF
docker cp "$WORK/run.sh" "$C:/tmp/run.sh"
docker exec "$C" chmod 755 /tmp/run.sh
say "runbook as ubuntu:"
if docker exec "$C" sudo -iu ubuntu bash /tmp/run.sh 2>/dev/null; then
  say "PASS — the box would come up and run."
else
  say "FAIL — see above$( [ "${KEEP:-0}" = 1 ] && echo "; container '$C' kept for inspection")."
  exit 1
fi
