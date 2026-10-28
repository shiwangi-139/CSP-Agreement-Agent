#!/usr/bin/env bash
# scripts/restart.sh [web|worker|all]   (default: all)
# Restarts this project's web and/or worker safely: stops only the Python
# program, never its restart loop (scripts/run_forever.sh), which starts it
# again 5 s later with the current code. If a program isn't running at all,
# keep_alive.sh starts it. Waits until the site answers, then says so.
# Our programs are found by their command line, whose first word is this
# project's own Python ($APP/.venv/bin/python): after a reboot the system may
# not show a process's folder, so the folder can't be used to tell.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
PY="$APP/.venv/bin/python"
WHAT="${1:-all}"
cd "$APP" || exit 1

ours() {    # PIDs of this project's Python running $1 (not tmux / run_forever.sh, which mention it too)
    for p in $(pgrep -f "$PY $1"); do
        [ "$(tr '\0' '\n' < /proc/$p/cmdline 2>/dev/null | head -1)" = "$PY" ] && echo "$p"
    done
}
stop() {
    for p in $(ours "$1"); do
        echo "stopping $1 (pid $p, running since $(ps -o lstart= -p $p))"; kill "$p"
    done
}
started() {
    for p in $(ours "$1"); do echo "$1 now running: pid $p, since $(ps -o lstart= -p $p)"; done
}
[ "$WHAT" = all ] || [ "$WHAT" = web ] && stop "-m uvicorn app.main:app"
[ "$WHAT" = all ] || [ "$WHAT" = worker ] && stop "-m app.worker"
sleep 7                               # run_forever.sh starts them again after 5 s
"$APP/scripts/keep_alive.sh"          # starts anything not running at all
for i in $(seq 1 30); do
    if curl -s -m 5 -o /dev/null http://127.0.0.1:9440/; then
        echo "web is answering again (after ~$((i * 2 + 7)) s)"
        started "-m uvicorn app.main:app"; started "-m app.worker"; exit 0
    fi
    sleep 2
done
echo "web did not answer within 60 s: see tail -30 $APP/logs/web.log"; exit 1
