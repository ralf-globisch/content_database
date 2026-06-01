# Testing Guide

## AWS profile setup (required for all targets)

All make targets that touch AWS use the `enc-dev` profile by default. Set it in your shell before running anything:

```bash
export AWS_PROFILE=enc-dev
```

The `enc-dev` profile must be configured in `~/.aws/config` with a `role_arn` pointing to the role that has S3 and EC2 access. After external MFA authentication (e.g. via `mfa`), boto3 and the AWS CLI will assume the role automatically:

```ini
# ~/.aws/config
[profile enc-dev]
role_arn       = arn:aws:iam::ACCOUNT_ID:role/enc-dev-role
source_profile = default
region         = eu-west-1
```

Verify before running any target:
```bash
aws sts get-caller-identity --profile enc-dev
# Should return the assumed role ARN, not your base IAM user
```

---

## Local testing (no EC2 required)

Verifies all new pipeline logic before touching cloud infrastructure.

**1. Rebuild the Docker image** (picks up code changes):
```bash
make build
```

**2. Verify AWS credentials:**
```bash
make check-auth   # uses AWS_PROFILE=enc-dev from your shell
```

**3. Run the interactive init wizard** (requires Python deps on host):
```bash
pip install boto3 duckdb   # if not already installed
make init
```
Walk through a few prefixes — mark one `y`, one `n`, then `s` to start. Confirm it scans and writes to the DB.

**4. Check recorded prefix state:**
```bash
make list-prefixes
```
The `n` prefix should be marked ignored; the `y` prefix should have a `last_scanned_at` timestamp and `file_count`.

**5. Run inventory again — recently-scanned prefixes should be skipped:**
```bash
make inventory ARGS="--staleness-days 7"
```
Log output should say "Skipping N recently-scanned prefix(es)".

**6. Force a rescan of one prefix:**
```bash
make force-rescan PREFIX=<one-of-your-prefixes>/
```

**7. Run metadata on a small number of files:**
```bash
make metadata ARGS="--limit 20"
```

**8. Check summary and unknown extensions:**
```bash
make summary
make unknown-extensions
```

**9. Open the UI — verify the bucket selector appears in the sidebar:**
```bash
make ui-local
```

---

## Cloud (EC2) testing

Uses **SSM Session Manager** — no key pair, no open SSH port, no public IP required.
All `ssh`/`scp`/`rsync` calls in the Makefile tunnel transparently through SSM.

### Prerequisites

- `AWS_PROFILE=enc-dev` exported in your shell (see [AWS profile setup](#aws-profile-setup-required-for-all-targets) above)
- SSM session-manager-plugin installed locally:
  ```bash
  # macOS
  brew install --cask session-manager-plugin

  # Linux (deb)
  curl -sLO "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb"
  sudo dpkg -i session-manager-plugin.deb
  ```
- Add this block to `~/.ssh/config` (routes all `ssh i-0...` calls through SSM using the `enc-dev` profile):
  ```
  Host i-*
      User ubuntu
      IdentityFile ~/.ssh/id_ed25519
      ProxyCommand sh -c "aws ec2-instance-connect send-ssh-public-key --profile enc-dev --region eu-west-1 --instance-id %h --instance-os-user ubuntu --ssh-public-key file://${HOME}/.ssh/id_ed25519.pub; aws ssm start-session --profile enc-dev --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region eu-west-1"
      StrictHostKeyChecking no
  ```
  This injects your local public key via EC2 Instance Connect (60-second window) then tunnels SSH through SSM — no stored key pair, no open port 22.

With this config, `EC2_HOST` is just the instance ID — no IP address, no key pair needed.

---

### Step 1 — Create the IAM role (one-time)

```bash
# Trust policy
aws iam create-role \
  --profile enc-dev \
  --role-name content-catalogue-ec2 \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'

# S3 read access for the target bucket
aws iam put-role-policy \
  --profile enc-dev \
  --role-name content-catalogue-ec2 \
  --policy-name s3-read \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Action":["s3:ListBucket","s3:GetObject"],
      "Resource":[
        "arn:aws:s3:::bitmovin-api-eu-west1-ci-input",
        "arn:aws:s3:::bitmovin-api-eu-west1-ci-input/*"
      ]
    }]
  }'

# SSM access — allows Session Manager to connect without SSH port or key pair
aws iam attach-role-policy \
  --profile enc-dev \
  --role-name content-catalogue-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# Instance profile
aws iam create-instance-profile --profile enc-dev --instance-profile-name content-catalogue-ec2
aws iam add-role-to-instance-profile \
  --profile enc-dev \
  --instance-profile-name content-catalogue-ec2 \
  --role-name content-catalogue-ec2
```

---

### Step 2 — Launch a spot instance in eu-west-1

No key pair. No inbound security group rules. The instance doesn't need a public IP
(SSM traffic goes outbound over port 443 to the SSM endpoints).

```bash
# Get the latest Ubuntu 22.04 AMI (SSM agent pre-installed)
AMI=$(aws ec2 describe-images \
  --profile enc-dev \
  --region eu-west-1 \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
            "Name=state,Values=available" \
  --query "sort_by(Images,&CreationDate)[-1].ImageId" \
  --output text)
echo "AMI: $AMI"

# Launch — no --key-name, no inbound SG rules required
INSTANCE_ID=$(aws ec2 run-instances \
  --profile enc-dev \
  --region eu-west-1 \
  --image-id $AMI \
  --instance-type t3.large \
  --instance-market-options '{"MarketType":"spot"}' \
  --iam-instance-profile Name=content-catalogue-ec2 \
  --block-device-mappings '[
    {"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"DeleteOnTermination":true}},
    {"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":20,"VolumeType":"gp3","DeleteOnTermination":true}}
  ]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=content-catalogue}]' \
  --query "Instances[0].InstanceId" --output text)
echo "INSTANCE_ID=$INSTANCE_ID"
```

If you want to restrict outbound traffic, the instance needs HTTPS (443) to:
- `ssm.eu-west-1.amazonaws.com`
- `ssmmessages.eu-west-1.amazonaws.com`
- `ec2messages.eu-west-1.amazonaws.com`

---

### Step 3 — Wait for SSM to become available (~60s)

```bash
aws ec2 wait instance-running --profile enc-dev --region eu-west-1 --instance-ids $INSTANCE_ID

# Wait until SSM agent registers (usually 30-60s after running state)
aws ssm wait command-executed \
  --profile enc-dev \
  --command-id $(aws ssm send-command \
    --profile enc-dev \
    --instance-ids $INSTANCE_ID \
    --document-name "AWS-RunShellScript" \
    --parameters '{"commands":["echo ready"]}' \
    --query "Command.CommandId" --output text) \
  --instance-id $INSTANCE_ID 2>/dev/null || true

echo "SSM ready — connecting as: EC2_HOST=$INSTANCE_ID"
```

---

### Step 4 — Bootstrap, deploy, and build

`EC2_HOST` is the instance ID; the SSH config ProxyCommand routes it through SSM.

```bash
EC2_HOST=$INSTANCE_ID

make ec2-setup  EC2_HOST=$EC2_HOST      # installs Docker, mounts EBS (~2 min)
make ec2-deploy EC2_HOST=$EC2_HOST      # rsyncs the repo
ssh $EC2_HOST "cd /opt/content-database && make build"   # builds Docker image (~3 min)
```

---

### Step 5 — Run a limited test scan

`cloud-init` runs the interactive prefix wizard (inventory only). Metadata extraction is always a separate step afterwards.

```bash
# Option A — interactive wizard to pick which prefixes to scan
make cloud-init     EC2_HOST=$EC2_HOST
make cloud-metadata EC2_HOST=$EC2_HOST ARGS="--limit 20"

# Option B — target a known small prefix directly (skips the wizard)
make cloud-inventory EC2_HOST=$EC2_HOST ARGS="--force-prefix test/ --staleness-days 0"
make cloud-metadata  EC2_HOST=$EC2_HOST ARGS="--limit 20"

# Option C — run both inventory and metadata in one shot (use for full runs)
make cloud-run EC2_HOST=$EC2_HOST ARGS="--staleness-days 0 --limit 20"
```

---

### Step 6 — Pull the DB and verify locally

```bash
make db-pull EC2_HOST=$EC2_HOST
make summary
make ui-local
```

---

### Step 7 — Tear down

Both targets pull the DB first, then terminate (spot instances cannot be stopped, only terminated).
`AWS_PROFILE` must be set — either exported in your shell or passed on the command line.

```bash
# With AWS_PROFILE already exported:
make ec2-stop EC2_HOST=$EC2_HOST EC2_INSTANCE_ID=$INSTANCE_ID

# Or pass it explicitly:
make ec2-stop EC2_HOST=$EC2_HOST EC2_INSTANCE_ID=$INSTANCE_ID AWS_PROFILE=enc-dev
```
