#!/usr/bin/env bash
# Paper-trading supervisor: relaunch the bot if the process ever dies, so
# long unattended runs keep producing logs for research. Each relaunch
# opens a new session_*.log and restores positions from state.json.
# Ctrl-C stops it for real. Live mode is refused on purpose: a live bot
# that crashed should be looked at by a human, not blindly restarted.
cd "$(dirname "$0")"

for arg in "$@"; do
    if [ "$arg" = "--live" ]; then
        echo "refusing to supervise --live; run 'python -m bot.main --live' directly" >&2
        exit 1
    fi
done

trap 'exit 0' INT

while true; do
    .venv/bin/python -m bot.main "$@"
    echo "bot exited (code $?); restarting in 10s — Ctrl-C to stop"
    sleep 10
done
