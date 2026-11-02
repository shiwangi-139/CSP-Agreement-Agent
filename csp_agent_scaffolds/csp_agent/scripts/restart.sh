#!/usr/bin/env bash
# scripts/restart.sh [web|worker|all]   (default: all)
# Restarts this project's web and/or worker safely: stops only the Python
# program, never its restart loop (scripts/run_forever.sh), which starts it
# again 5 s later with the current code. If a program isn't running at all,
# keep_alive.sh starts it. Waits until the site answers, then says so.
# Our programs are found by scripts/our_pids.sh: the web by app.main:app on
# this project's port 9440, however it was started (by hand too: the old rule,
# "first word is our .venv Python", missed such a web, so an update never
# replaced it); the worker by app.worker from this project.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
PY="$APP/.venv/bin/python"
WHAT="${1:-all}"
cd "$APP" || exit 1

. "$APP/scripts/our_pids.sh"
stop() {    # stop "web" or "worker"
    pids=$("$1"_pids)
    [ -z "$pids" ] && { echo "$1: not running"; return; }
    for p in $pids; do echo "stopping $1 (pid $p, by $(ps -o user= -p $p), running since $(ps -o lstart= -p $p))"; done
    stop_pids $pids
}
started() {
    for p in $("$1"_pids); do echo "$1 now running: pid $p, since $(ps -o lstart= -p $p)"; done
}
[ "$WHAT" = all ] || [ "$WHAT" = web ] && stop web
[ "$WHAT" = all ] || [ "$WHAT" = worker ] && stop worker
sleep 7                               # run_forever.sh starts them again after 5 s
"$APP/scripts/keep_alive.sh"          # starts anything not running at all
for i in $(seq 1 30); do
    if curl -s -m 5 -o /dev/null http://127.0.0.1:9440/; then
        echo "web is answering again (after ~$((i * 2 + 7)) s)"
        started web; started worker; exit 0
    fi
    sleep 2
done
echo "web did not answer within 60 s: see tail -30 $APP/logs/web.log"; exit 1
