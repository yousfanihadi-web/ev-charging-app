#!/bin/bash
# ONE-TIME server setup. Paste this whole file into "User data" when launching the EC2
# instance (Amazon Linux 2023). It installs Python, creates the service, and the deploy script.
set -e
dnf install -y python3 python3-pip unzip
mkdir -p /opt/ev-app/data /opt/ev-app/releases
python3 -m venv /opt/ev-app/venv

cat > /opt/ev-app/deploy.sh <<'DEPLOY'
#!/bin/bash
# Called by the pipeline (through AWS SSM). Usage: deploy.sh <bucket> <region>
# Takes the LATEST build from S3 and makes it live.
set -euo pipefail
BUCKET=$1; REGION=${2:-us-east-1}
APP=${APP_DIR:-/opt/ev-app}
KEY=$(aws s3 cp "s3://$BUCKET/builds/latest.txt" - --region "$REGION" | tr -d '[:space:]')
echo "Deploying $KEY"
aws s3 cp "s3://$BUCKET/$KEY" /tmp/build.zip --region "$REGION"
REL="$APP/releases/$(date +%Y%m%d%H%M%S)"
mkdir -p "$REL"
unzip -q -o /tmp/build.zip -d "$REL"
"$APP/venv/bin/pip" install -q -r "$REL/requirements.txt"
ln -sfn "$REL" "$APP/current"
systemctl restart ev-app
echo "Deployed $KEY"
DEPLOY
chmod +x /opt/ev-app/deploy.sh

cat > /etc/systemd/system/ev-app.service <<'UNIT'
[Unit]
Description=Smart EV Charging app
After=network.target

[Service]
WorkingDirectory=/opt/ev-app/current
Environment=PORT=80
Environment=EV_DB=/opt/ev-app/data/ev.db
ExecStart=/opt/ev-app/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ev-app
