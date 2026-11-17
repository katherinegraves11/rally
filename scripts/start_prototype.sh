#!/bin/zsh
# Start both sides of the Rally prototype on this Mac:
#   club dashboard  http://localhost:8517
#   player app      http://localhost:8600
# Both share runtime/ (your demo data is kept between restarts; use Reset in the dashboard to start over).
# Stop them with: scripts/stop_prototype.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p runtime
nohup .venv/bin/streamlit run app.py --server.port 8517 --server.headless true > runtime/dashboard.log 2>&1 &
RALLY_DASHBOARD_URL=http://localhost:8517 nohup .venv/bin/uvicorn player_app.main:app --port 8600 --host 127.0.0.1 \
  > runtime/player_app.log 2>&1 &
echo "Club dashboard: http://localhost:8517"
echo "Player app:     http://localhost:8600"
