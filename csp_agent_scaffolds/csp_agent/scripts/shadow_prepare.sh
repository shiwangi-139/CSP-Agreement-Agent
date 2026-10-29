#!/usr/bin/env bash
# scripts/shadow_prepare.sh [ref]: get candidate code for the shadow test
# (scripts/shadow_test.py) into /home/shiwangi/csp_shadow, without touching
# the live code (as root):
#   /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/shadow_prepare.sh             # latest on GitHub
#   /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/shadow_prepare.sh 1a2b3c4     # a given commit
REPO=/home/shiwangi
SHADOW=$REPO/csp_shadow
REF=${1:-origin/main}
cd "$REPO" || exit 1
export GIT_SSH_COMMAND="ssh -o HostName=github.com -o User=git -i /home/kapil/.ssh/csp_agreement_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=/home/kapil/.ssh/known_hosts"
G="git -c safe.directory=$REPO -c safe.directory=$SHADOW"
$G fetch -q origin || { echo "git fetch failed: nothing changed"; exit 1; }
grep -qx "/csp_shadow/" .git/info/exclude 2>/dev/null || echo "/csp_shadow/" >> .git/info/exclude
if [ -d "$SHADOW" ]; then
  $G -C "$SHADOW" checkout -q --detach "$REF" || exit 1
else
  $G worktree add -q --detach "$SHADOW" "$REF" || exit 1
fi
$G -C "$SHADOW" log --oneline -1 | cut -c1-80
echo "candidate ready; now run (inside tmux):"
echo "  cd /home/shiwangi/csp_agent_scaffolds/csp_agent && nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.shadow_test --candidate $SHADOW/csp_agent_scaffolds/csp_agent"
