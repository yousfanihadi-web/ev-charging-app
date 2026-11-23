# Smart EV Charging: CI/CD pipeline

Flow: code change -> git push -> GitHub Actions -> unit tests + security scan -> build zip -> S3 -> deploy latest build to EC2 -> verify live site.

## Files
- app.py, static/index.html: the app (Flask + SQLite, see earlier notes)
- VERSION: shown at the bottom of the app. Change it to prove a deployment.
- tests/test_app.py: unit tests (pipeline fails if they fail)
- .github/workflows/ci-cd.yml: the pipeline (test, security, build, deploy, verify)
- deploy/setup-ec2.sh: one-time server setup (paste into EC2 "User data")

## One-time AWS setup (about 15 minutes)
1. **S3 bucket**: create a private bucket, e.g. `ev-builds-yourname`.
2. **IAM role for EC2** (name it `ev-ec2-role`): attach `AmazonSSMManagedInstanceCore` and this inline policy (replace BUCKET):
   `{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::BUCKET/builds/*"}]}`
3. **EC2 instance**: Amazon Linux 2023, t3.micro, default VPC with a public IP. Attach `ev-ec2-role`. Security group: allow HTTP (port 80) from anywhere. Under Advanced details, paste the whole `deploy/setup-ec2.sh` into **User data**.
4. **IAM user for GitHub** (`github-deployer`): attach `AmazonSSMFullAccess` and this inline policy (replace BUCKET), then create an access key:
   `{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::BUCKET/builds/*"}]}`

## GitHub setup
Create a repository, push this folder to the `main` branch, then add these secrets (Settings -> Secrets and variables -> Actions):

| Secret | Value |
|---|---|
| AWS_ACCESS_KEY_ID | access key of github-deployer |
| AWS_SECRET_ACCESS_KEY | secret key of github-deployer |
| AWS_REGION | e.g. us-east-1 |
| S3_BUCKET | your bucket name |
| EC2_INSTANCE_ID | e.g. i-0123456789abcdef0 |
| EC2_HOST | the EC2 public DNS or IP |

The first push runs the pipeline and does the first deployment. Open `http://EC2_HOST`.

## Demo: a change going live
1. Edit `VERSION` to `1.0.1` and change some visible text in `static/index.html`.
2. `git add . && git commit -m "v1.0.1" && git push`
3. Watch Actions: test, security, build, deploy. Show the zip in S3 under `builds/`.
4. Refresh the live site: the new version and text appear.
To show a failing pipeline, break a test and push: the build and deploy jobs do not run.

## Run locally
    pip install -r requirements.txt pytest
    pytest
    python app.py        # http://localhost:5000
