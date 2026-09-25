#!/bin/zsh
cd "${0:A:h}" || exit 1
python3 app.py &
server_pid=$!
trap 'kill $server_pid 2>/dev/null' EXIT INT TERM
sleep 1
if kill -0 $server_pid 2>/dev/null; then
  open 'http://127.0.0.1:8766'
  wait $server_pid
else
  wait $server_pid
fi
