#!/usr/bin/env bash
# Rehearse the OMNI-29 box locally — no AWS, no credentials, no money.
#
# Runs the instance's user_data and scripts/aws_bootstrap.sh on an Ubuntu 24.04
# container, then the runbook's commands as `ubuntu` through a login shell
# (the way an SSM session is told to work), with the gauntlet's docker sandbox
# on a real Linux daemon. A full stub-proposer cycle must pass every gate
# (`[cycle] feature_step`, or `verified_awaiting_human_merge` for a feature that
# ends in one step), the loop must outlive a killed session when started in tmux
# (OMNI-139), and the operator console must answer.
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
#   - the SSM session that starts the loop is a stand-in: a pty made by `script`
#     with a tmux client on it, killed the way a dropped session ends. It shows
#     that tmux and the loop outlive their client's terminal, not how ssm-agent
#     behaves; an agent restart (which may take tmux with it) is not modelled.
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
# main.py reports "[cycle] <status>: ..." (OMNI-123 replaced "cycle status:",
# which this check went on grepping for). A cycle that passed every gate is a
# committed feature step since staged delivery (OMNI-130), or a PR when the
# feature ends after one step. The same set as episodic.ACCEPTED_OUTCOMES,
# minus "promoted", which only a human merge produces.
status=$(sed -nE 's/^\[cycle\] ([a-z_]+):.*/\1/p' /tmp/cycle.log | head -1)
case "$status" in
  feature_step|verified_awaiting_human_merge)
    check "full cycle in the docker sandbox ($status)" ok ;;
  *)
    check "full cycle in the docker sandbox" no "${status:-no [cycle] line} — tail of /tmp/cycle.log:"
    # The container is removed on exit unless KEEP=1, so show the evidence here.
    tail -20 /tmp/cycle.log | sed 's/^/      /' ;;
esac

# OMNI-139: a loop started in tmux outlives the session that started it. The
# stand-in for an SSM session is a process group of its own holding a pty with a
# tmux client attached, as the shell ssm-agent runs is. Killing it closes the
# pty, which hangs the client up the way a dropped session does. The runbook's
# own commands are typed into the tmux shell: `new-session -A -s sis`, then the
# loop with its `python -u` and `tee -i`. Afterwards tmux and the loop must be
# where they were, and Ctrl-C from a "new session" must stop the loop cleanly.
export TERM=xterm-256color
[ -n "$(command -v tmux || true)" ] && check "tmux installed by the bootstrap" ok \
  || check "tmux installed by the bootstrap" no "not found"
rm -f runtime/loop.log /tmp/sess.in
mkfifo /tmp/sess.in
setsid script -qfc 'tmux new-session -A -s sis' /dev/null < /tmp/sess.in > /dev/null 2>&1 &
session=$!
exec 3> /tmp/sess.in
for _ in $(seq 1 20); do tmux has-session -t sis 2>/dev/null && break; sleep 0.5; done
sleep 1
attached=$(tmux list-clients -t sis 2>/dev/null | wc -l)
server=$(tmux display-message -p -t sis '#{pid}' 2>/dev/null || true)
[ "$attached" -eq 1 ] && check "a tmux client is attached inside the stand-in session" ok \
  || check "a tmux client is attached inside the stand-in session" no "$attached clients"
tmux send-keys -t sis 'cd ~/omnibase' Enter
tmux send-keys -t sis "export SIS_SANDBOX=docker; poetry run python -u main.py --loop --loop-max-cycles 10 --loop-interval-seconds 1 2>&1 | tee -i -a runtime/loop.log" Enter
for _ in $(seq 1 90); do grep -q 'server loop starting' runtime/loop.log 2>/dev/null && break; sleep 1; done
kill -KILL "$session" 2>/dev/null; kill -HUP -- "-$session" 2>/dev/null; exec 3>&-
sleep 3
after=$(tmux list-clients -t sis 2>/dev/null | wc -l)
server_after=$(tmux display-message -p -t sis '#{pid}' 2>/dev/null || true)
if [ "$after" -eq 0 ] && [ -n "$server" ] && [ "$server" = "$server_after" ] && pgrep -f 'main.py --loop' >/dev/null; then
  check "the session was killed and tmux and the loop are still there" ok
else
  check "the session was killed and tmux and the loop are still there" no "clients $attached->$after, server $server->$server_after, loop $(pgrep -f 'main.py --loop' | wc -l) process(es)"
  tail -15 runtime/loop.log 2>/dev/null | sed 's/^/      /'
fi
tmux send-keys -t sis C-c
for _ in $(seq 1 180); do grep -q '^\[loop\] stopped' runtime/loop.log 2>/dev/null && break; sleep 1; done
if grep -q '^\[loop\] stopped.*interrupted' runtime/loop.log 2>/dev/null; then
  check "Ctrl-C from a new session stops the loop cleanly, and the log says why" ok
else
  check "Ctrl-C from a new session stops the loop cleanly, and the log says why" no "no '[loop] stopped … interrupted' line — tail of runtime/loop.log:"
  tail -15 runtime/loop.log 2>/dev/null | sed 's/^/      /'
fi
tmux capture-pane -p -S - -t sis 2>/dev/null | grep -q '^\[loop\] stopped' \
  && check "the output is still in the tmux pane" ok \
  || check "the output is still in the tmux pane" no "tmux session or output missing"
tmux kill-session -t sis 2>/dev/null || true

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
