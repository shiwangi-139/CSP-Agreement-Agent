#!/usr/bin/env bash
# scripts/keep_alive.sh
# Safety net for THIS project only: starts the CSP agent's own tmux windows
# "web" (dashboard + upload page, port 9440) and "worker" (the scheduler:
# reminders, sending, sheet, Gmail) if either is missing. Inside each window
# scripts/run_forever.sh restarts the program 5 s after it stops. It never
# stops or touches anything else on the server. Run from cron every 5
# minutes and at reboot (crontab -e):
#
#   */5 * * * * /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/keep_alive.sh
#   @reboot     sleep 60 && /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/keep_alive.sh
#
# What it did is written to logs/keep_alive.log.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
PY="$APP/.venv/bin/python"
LOG="$APP/logs/keep_alive.log"
export PATH=/usr/local/bin:/usr/bin:/bin

cd "$APP" || exit 1
mkdir -p "$APP/logs"

if ! tmux has-session -t web 2>/dev/null; then
    if ss -ltn | grep -q '127.0.0.1:9440 '; then
        echo "$(date '+%F %T') web window missing but port 9440 is in use: left alone" >> "$LOG"
    else
        tmux new -d -s web "$APP/scripts/run_forever.sh web $PY -m uvicorn app.main:app --host 127.0.0.1 --port 9440"
        echo "$(date '+%F %T') started web" >> "$LOG"
    fi
fi

if ! tmux has-session -t worker 2>/dev/null; then
    # Only one worker may run (two would double the jobs).
    running=""
    for p in $(pgrep -f "python -m app.worker"); do
        [ "$(readlink /proc/$p/cwd)" = "$APP" ] && running=1     # only this project's worker
    done
    if [ -n "$running" ]; then
        echo "$(date '+%F %T') worker window missing but a worker is running: left alone" >> "$LOG"
    else
        tmux new -d -s worker "$APP/scripts/run_forever.sh worker $PY -m app.worker"
        echo "$(date '+%F %T') started worker" >> "$LOG"
    fi
fi
