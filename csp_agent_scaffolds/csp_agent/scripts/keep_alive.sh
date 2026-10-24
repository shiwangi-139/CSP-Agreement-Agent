#!/usr/bin/env bash
# scripts/keep_alive.sh
# Safety net for THIS project only: makes sure the CSP agent's web (dashboard
# + upload page, port 9440) and worker (reminders, sending, sheet, Gmail) are
# running, and starts the missing one. Inside its window,
# scripts/run_forever.sh restarts the program 5 s after it stops. It never
# stops or touches anything else on the server. Run from cron every 5
# minutes and at reboot (crontab -e):
#
#   */5 * * * * /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/keep_alive.sh >> /home/shiwangi/csp_agent_scaffolds/csp_agent/logs/cron.log 2>&1
#   @reboot     sleep 60 && /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/keep_alive.sh
#
# It checks the real programs, not tmux sessions: the web must answer on
# port 9440, and a worker must run from this folder. Its tmux has its own
# socket in run/ (not the shared /tmp), so cron and a terminal see the same
# windows:  tmux -S /home/shiwangi/csp_agent_scaffolds/csp_agent/run/tmux.sock ls
# Every run writes one line to logs/keep_alive.log.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
PY="$APP/.venv/bin/python"
LOG="$APP/logs/keep_alive.log"
SOCK="$APP/run/tmux.sock"
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

cd "$APP" || exit 1
mkdir -p "$APP/logs" "$APP/run"
T="tmux -S $SOCK"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

ours() {    # PIDs of this project's processes matching $1
    for p in $(pgrep -f "$1"); do
        [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$APP" ] && echo "$p"
    done
}

web=up worker=up
if ! curl -s -m 10 -o /dev/null http://127.0.0.1:9440/; then
    web=down
    if [ -n "$(ours 'uvicorn app.main:app')" ]; then
        say "web not answering on 9440 but its process is running: left alone (may be starting)"
    else
        $T kill-session -t web 2>/dev/null
        $T new -d -s web "$APP/scripts/run_forever.sh web $PY -m uvicorn app.main:app --host 127.0.0.1 --port 9440"
        say "web was down: started"
    fi
fi
if [ -z "$(ours 'python -m app.worker')" ]; then
    worker=down
    $T kill-session -t worker 2>/dev/null
    $T new -d -s worker "$APP/scripts/run_forever.sh worker $PY -m app.worker"
    say "worker was down: started"
fi
say "check: web $web, worker $worker"
