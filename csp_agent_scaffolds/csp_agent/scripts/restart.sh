#!/usr/bin/env bash
# scripts/restart.sh [web|worker|all]   (default: all)
# Restarts this project's web and/or worker safely: stops only the Python
# program, never its restart loop (scripts/run_forever.sh), which starts it
# again 5 s later with the current code. If a program isn't running at all,
# keep_alive.sh starts it. Waits until the site answers, then says so.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
WHAT="${1:-all}"
cd "$APP" || exit 1

stop() {    # $1 = what the Python command line contains
    for p in $(pgrep -f "$1"); do
        [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$APP" ] || continue
        case "$(readlink /proc/$p/exe 2>/dev/null)" in *python*) kill "$p" && echo "stopped $1 (pid $p)";; esac
    done
}
[ "$WHAT" = all ] || [ "$WHAT" = web ] && stop "uvicorn app.main:app"
[ "$WHAT" = all ] || [ "$WHAT" = worker ] && stop "python -m app.worker"
sleep 2
"$APP/scripts/keep_alive.sh"          # starts anything not running at all
for i in $(seq 1 30); do
    if curl -s -m 5 -o /dev/null http://127.0.0.1:9440/; then
        echo "web is answering again (after ~$((i * 2 + 2)) s)"; exit 0
    fi
    sleep 2
done
echo "web did not answer within 60 s: see tail -30 $APP/logs/web.log"; exit 1
