#!/usr/bin/env bash
# Creates the SQS work queue + DLQ with a redrive policy and prints the IAM
# policy to attach to the VM's instance role. Run from a machine with AWS
# credentials for the target account. Idempotent.
set -euo pipefail
REGION="${PULSEOPS_AWS_REGION:-me-central-1}"
MAX_RECEIVE="${PULSEOPS_MAX_RECEIVE_COUNT:-5}"

DLQ_URL=$(aws sqs create-queue --region "$REGION" --queue-name pulseops-work-dlq \
  --attributes MessageRetentionPeriod=1209600 --query QueueUrl --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)
REDRIVE=$(printf '{"deadLetterTargetArn":"%s","maxReceiveCount":"%s"}' "$DLQ_ARN" "$MAX_RECEIVE")
ATTRS=$(python3 -c 'import json,sys; print(json.dumps({"VisibilityTimeout":"60","ReceiveMessageWaitTimeSeconds":"10","RedrivePolicy":sys.argv[1]}))' "$REDRIVE")
QUEUE_URL=$(aws sqs create-queue --region "$REGION" --queue-name pulseops-work \
  --attributes "$ATTRS" --query QueueUrl --output text)
QUEUE_ARN=$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$QUEUE_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

echo "Work queue: $QUEUE_URL"
echo "DLQ:        $DLQ_URL"
echo
echo "Attach this least-privilege policy to the instance role used by the VM:"
cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"
      ],
      "Resource": ["$QUEUE_ARN", "$DLQ_ARN"]
    }
  ]
}
POLICY
