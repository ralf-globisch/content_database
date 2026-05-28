#!/usr/bin/env bash
# Bootstrap script for an EC2 instance in eu-west-1 to run the content catalogue pipeline.
# Run once after launching a fresh instance:
#   make ec2-setup EC2_HOST=ubuntu@<ip>
#
# Prerequisites (done before running this script):
#   1. Launch an EC2 instance in eu-west-1 (Ubuntu 22.04 LTS recommended)
#      - Attach an EBS volume at /dev/sdf (will be mounted at /data)
#      - Recommended: t3.large (2 vCPU, 8 GB) spot instance
#      - No key pair needed; no inbound security group rules needed
#   2. Attach an IAM role with:
#        s3:ListBucket + s3:GetObject  on  arn:aws:s3:::bitmovin-api-eu-west1-ci-input
#        AmazonSSMManagedInstanceCore  (managed policy — enables SSM Session Manager)
#   3. Add to ~/.ssh/config on your local machine:
#        Host i-*
#            User ubuntu
#            ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
#            StrictHostKeyChecking no
#   4. Run: make ec2-setup EC2_HOST=<instance-id>  (instance ID, not IP)

set -euo pipefail

DATA_DIR=/data
REPO_DIR=/opt/content-database
EBS_DEVICE=/dev/sdf   # adjust if AWS renames it to nvme1n1

echo "==> Updating packages"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    docker.io \
    make \
    rsync \
    python3 python3-pip \
    unzip \
    screen

echo "==> Installing AWS CLI v2"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp/
sudo /tmp/aws/install --update
rm -rf /tmp/awscliv2.zip /tmp/aws/

echo "==> Installing Python dependencies"
pip3 install --quiet boto3 duckdb

# Allow ubuntu user to run docker without sudo
sudo usermod -aG docker ubuntu

echo "==> Formatting and mounting EBS volume at $DATA_DIR"
sudo mkdir -p "$DATA_DIR"

if mountpoint -q "$DATA_DIR"; then
    echo "  $DATA_DIR is already mounted, skipping format/mount."
else
    # Detect device name (AWS may use nvme or xvd naming)
    if [ -b /dev/nvme1n1 ]; then
        EBS_DEVICE=/dev/nvme1n1
    elif [ -b /dev/xvdf ]; then
        EBS_DEVICE=/dev/xvdf
    fi

    if ! blkid "$EBS_DEVICE" | grep -q ext4; then
        echo "  Formatting $EBS_DEVICE as ext4..."
        sudo mkfs.ext4 -L content-db "$EBS_DEVICE"
    fi

    sudo mount "$EBS_DEVICE" "$DATA_DIR"
    echo "  Mounted $EBS_DEVICE at $DATA_DIR"
fi

# Persist mount across reboots
if ! grep -q "$DATA_DIR" /etc/fstab; then
    echo "LABEL=content-db $DATA_DIR ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
fi

sudo chown ubuntu:ubuntu "$DATA_DIR"

echo "==> Starting Docker"
sudo systemctl enable docker
sudo systemctl start docker

echo "==> Creating repo directory at $REPO_DIR"
sudo mkdir -p "$REPO_DIR"
sudo chown ubuntu:ubuntu "$REPO_DIR"

echo ""
echo "======================================================================"
echo "Setup complete. Next steps:"
echo ""
echo "  1. Deploy the code (from your local machine):"
echo "       make ec2-deploy EC2_HOST=ubuntu@<ip>"
echo ""
echo "  2. Build the Docker image on the EC2 instance:"
echo "       ssh ubuntu@<ip> 'cd $REPO_DIR && make build'"
echo ""
echo "  3. Run the interactive init wizard:"
echo "       make cloud-init EC2_HOST=ubuntu@<ip>"
echo ""
echo "  4. Or run a full scan in a screen session:"
echo "       ssh ubuntu@<ip> 'screen -dmS scan bash -c \"cd $REPO_DIR && make cloud-run\"'"
echo "       ssh ubuntu@<ip> 'screen -r scan'   # to attach and watch progress"
echo ""
echo "  5. After scan completes, pull the DB to local:"
echo "       make db-pull EC2_HOST=ubuntu@<ip>"
echo ""
echo "  6. Tear down when done (find instance ID first):"
echo "       aws ec2 describe-instances --filters 'Name=ip-address,Values=<ip>' \\"
echo "         --query 'Reservations[].Instances[].InstanceId' --output text"
echo ""
echo "       # Stop (EBS preserved, restartable):"
echo "       make ec2-stop  EC2_HOST=ubuntu@<ip> EC2_INSTANCE_ID=i-0abc123"
echo ""
echo "       # Terminate permanently:"
echo "       make ec2-terminate  EC2_HOST=ubuntu@<ip> EC2_INSTANCE_ID=i-0abc123"
echo ""
echo "  IAM role check:"
echo "       aws sts get-caller-identity   # should return the instance role ARN"
echo "======================================================================"
