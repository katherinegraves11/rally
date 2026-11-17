#!/bin/zsh
# Stop the Rally dashboard (8517) and player app (8600). Demo data in runtime/ is kept.
for port in 8517 8600; do pids=$(lsof -ti tcp:$port); [ -n "$pids" ] && kill $pids; done
echo "Stopped."
