#!/usr/bin/env bash
# scripts/run_forever.sh NAME COMMAND...
# Runs COMMAND (this project's web or worker) and starts it again 5 seconds
# after it stops for any reason, writing its output to logs/NAME.log and each
# stop / start to logs/keep_alive.log. Started inside tmux by keep_alive.sh.
APP=/home/shiwangi/csp_agent_scaffolds/csp_agent
NAME="$1"; shift
cd "$APP" || exit 1
mkdir -p logs
while true; do
    echo "$(date '+%F %T') $NAME started" >> logs/keep_alive.log
    "$@" >> "logs/$NAME.log" 2>&1
    echo "$(date '+%F %T') $NAME stopped (exit code $?), restarting in 5 s" >> logs/keep_alive.log
    sleep 5
done
