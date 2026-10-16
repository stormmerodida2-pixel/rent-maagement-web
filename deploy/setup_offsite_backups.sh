#!/usr/bin/env bash
# One-off AWS setup for off-site backups. Run from a computer with AWS CLI access (an admin user), not
# the server. Safe to run again: it creates what's missing and re-applies the settings.
#
#   deploy/setup_offsite_backups.sh <instance-id> [server-region] [bucket-region]
#
# Creates, in a different region from the server:
#   - a private S3 bucket: encrypted, versioned, public access blocked, HTTPS only, and a lifecycle rule
#     that deletes copies after KEEP_DAYS (default 90);
#   - an IAM role for the server that can ONLY add files under backups/ (it can't list, read or delete,
#     so someone who breaks into the server can't wipe the copies);
#   - attaches the role to the EC2 instance and lets Docker containers use it (metadata hop limit 2).
# Then set BACKUP_S3_BUCKET and BACKUP_S3_REGION in .env_prod on the server (printed at the end).
set -euo pipefail

INSTANCE_ID=${1:?usage: $0 <instance-id> [server-region] [bucket-region]}
SERVER_REGION=${2:-ap-south-1}
BUCKET_REGION=${3:-eu-west-1}
KEEP_DAYS=${KEEP_DAYS:-90}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=${BUCKET:-rentipay-backups-$ACCOUNT}
ROLE=rentipay-backup-writer
PROFILE=rentipay-backup-writer

echo "Bucket s3://$BUCKET in $BUCKET_REGION; server $INSTANCE_ID in $SERVER_REGION"

# --- Bucket --------------------------------------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$BUCKET_REGION" \
    --create-bucket-configuration LocationConstraint="$BUCKET_REGION" > /dev/null
  echo "Created bucket."
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --output text --lifecycle-configuration "{
  \"Rules\": [{
    \"ID\": \"expire-old-backups\", \"Status\": \"Enabled\", \"Filter\": {\"Prefix\": \"backups/\"},
    \"Expiration\": {\"Days\": $KEEP_DAYS},
    \"NoncurrentVersionExpiration\": {\"NoncurrentDays\": 7},
    \"AbortIncompleteMultipartUpload\": {\"DaysAfterInitiation\": 1}
  }]}" > /dev/null
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"HttpsOnly\", \"Effect\": \"Deny\", \"Principal\": \"*\", \"Action\": \"s3:*\",
    \"Resource\": [\"arn:aws:s3:::$BUCKET\", \"arn:aws:s3:::$BUCKET/*\"],
    \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
  }]}" > /dev/null
echo "Bucket settings applied (private, encrypted, versioned, copies kept $KEEP_DAYS days)."

# --- Server role: upload only ----------------------------------------------------------------------------
if ! aws iam get-role --role-name "$ROLE" > /dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --description "Rentipay server: upload backups only" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /dev/null
  echo "Created role $ROLE."
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name upload-backups --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\", \"Action\": \"s3:PutObject\", \"Resource\": \"arn:aws:s3:::$BUCKET/backups/*\"}]}"
if ! aws iam get-instance-profile --instance-profile-name "$PROFILE" > /dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE" > /dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
  echo "Created instance profile; waiting for IAM to catch up…"
  sleep 15
fi

# --- Attach to the server ----------------------------------------------------------------------------
current=$(aws ec2 describe-iam-instance-profile-associations --region "$SERVER_REGION" \
  --filters Name=instance-id,Values="$INSTANCE_ID" Name=state,Values=associated \
  --query 'IamInstanceProfileAssociations[0].[AssociationId,IamInstanceProfile.Arn]' --output text)
if [ "$current" = "None" ] || [ -z "$current" ]; then
  aws ec2 associate-iam-instance-profile --region "$SERVER_REGION" --instance-id "$INSTANCE_ID" \
    --iam-instance-profile Name="$PROFILE" > /dev/null
  echo "Attached the role to $INSTANCE_ID."
elif [[ "$current" != *"/$PROFILE" ]]; then
  echo "The instance already has another role ($current). Add the upload-backups policy to that role instead." >&2
  exit 1
fi
# Containers are one network hop further from the instance metadata service than the host.
aws ec2 modify-instance-metadata-options --region "$SERVER_REGION" --instance-id "$INSTANCE_ID" \
  --http-tokens required --http-put-response-hop-limit 2 --http-endpoint enabled > /dev/null

echo
echo "Done. On the server, add to .env_prod and restart:"
echo "  BACKUP_S3_BUCKET=$BUCKET"
echo "  BACKUP_S3_REGION=$BUCKET_REGION"
echo "  docker compose --env-file .env_prod up -d"
echo "Then test with:  docker compose --env-file .env_prod exec scheduler python manage.py backup_db"
