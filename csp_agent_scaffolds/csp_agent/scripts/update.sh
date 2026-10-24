#!/usr/bin/env bash
# scripts/update.sh: put the latest code live, in one command (as root):
#   /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/update.sh
# Gets the code from GitHub, updates the database tables, restarts web and
# worker safely (scripts/restart.sh) and shows a short check.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
REPO=/home/shiwangi
cd "$REPO" || exit 1
export GIT_SSH_COMMAND="ssh -o HostName=github.com -o User=git -i /home/kapil/.ssh/csp_agreement_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=/home/kapil/.ssh/known_hosts"
echo "== getting the code =="
git -c safe.directory="$REPO" pull || { echo "git pull failed: nothing changed"; exit 1; }
git -c safe.directory="$REPO" log --oneline -1 | cut -c1-80
chmod +x "$APP"/scripts/*.sh
cd "$APP" || exit 1
echo "== database tables =="
.venv/bin/python -m scripts.migrate_schema 2>&1 | tail -1
echo "== restarting =="
"$APP/scripts/restart.sh" all
echo "== check =="
curl -s -m 30 -o /dev/null -w "  site from the internet: %{http_code}\n" https://indev.eko.in/csp-agreement-agent/upload
tail -1 logs/keep_alive.log | sed 's/^/  last keep-alive: /'
