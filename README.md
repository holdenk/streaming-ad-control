# streaming-ad-control

# 1. Create unprivileged service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin streaming-ad-monitor

# 2. Deploy the app
sudo cp -r . /opt/streaming-ad-monitor
sudo python3 -m venv /opt/streaming-ad-monitor/venv
sudo /opt/streaming-ad-monitor/venv/bin/pip install -r /opt/streaming-ad-monitor/requirements.txt

# 3. Drop in your rules and secrets
sudo mkdir /etc/streaming-ad-monitor
sudo cp rules.example.yaml /etc/streaming-ad-monitor/rules.yaml  # then edit
sudo tee /etc/streaming-ad-monitor/env <<'EOF'
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL_LOGIN=...
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_ADS_ACCOUNT_ID=...
EOF
sudo chmod 640 /etc/streaming-ad-monitor/env
sudo chown root:streaming-ad-monitor /etc/streaming-ad-monitor/env

# 4. Enable and start
sudo cp contrib/streaming-ad-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now streaming-ad-monitor
