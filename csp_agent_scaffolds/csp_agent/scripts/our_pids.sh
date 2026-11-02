# scripts/our_pids.sh: sourced by restart.sh and keep_alive.sh.
# Finds THIS project's programs and only them, never tmux or run_forever.sh
# (whose command lines mention the same words):
#   web     a Python / uvicorn running app.main:app on this project's port
#           9440 (the one Nginx sends the site to), however and by whomever
#           it was started: by hand, by run_forever.sh, as "uvicorn ...";
#   worker  a Python running app.worker from this project (its own .venv
#           Python, or this folder as its working folder).
# APP and PY must be set by the script that sources this file.

_cmd()   { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null; }
_first() { tr '\0' '\n' < "/proc/$1/cmdline" 2>/dev/null | head -1; }

web_pids() {
    for p in $(pgrep -f "app.main:app"); do
        case "$(basename "$(_first "$p")")" in python*|uvicorn) ;; *) continue ;; esac
        _cmd "$p" | grep -q -- "9440" && echo "$p"
    done
}

worker_pids() {
    for p in $(pgrep -f "app.worker"); do
        case "$(basename "$(_first "$p")")" in python*) ;; *) continue ;; esac
        { [ "$(_first "$p")" = "$PY" ] || [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$APP" ]; } && echo "$p"
    done
}

# Stop these programs: ask first (TERM); one still there after 15 s (hung)
# is made to stop (KILL). Says when it can't (another user's program: run
# this as root or as that user).
stop_pids() {
    for p in "$@"; do
        if ! kill "$p" 2>/dev/null; then
            echo "cannot stop pid $p (it belongs to $(ps -o user= -p "$p")): run this as root or as that user"
        fi
    done
    for _ in $(seq 1 15); do
        alive=""; for p in "$@"; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
        [ -z "$alive" ] && return 0
        sleep 1
    done
    for p in $alive; do
        echo "pid $p did not stop in 15 s (hung): forcing it"; kill -KILL "$p" 2>/dev/null
    done
}
