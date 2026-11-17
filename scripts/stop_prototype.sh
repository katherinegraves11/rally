#!/bin/zsh
# Stop the Rally dashboard (8517) and player app (8600). Demo data in runtime/ is kept.
# Only the servers listening on those ports are stopped (not tunnels connected to them).
for port in 8517 8600; do
  for pid in $(lsof -ti tcp:$port -sTCP:LISTEN); do kill $pid; done
done
echo "Stopped."
