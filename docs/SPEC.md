# AI_SRE
Excellent choice. Building Phase 1 (AI Alert Summarizer) first gives you immediate value and creates a solid foundation for the more advanced features.

Project Goal

When Grafana detects an alert:

1. Grafana sends a webhook.
2. Amazon EventBridge receives the event.
3. AWS Lambda processes the alert.
4. Lambda gathers context from:
    * Grafana API (alert details)
    * Splunk REST API (related logs)
    * CloudWatch (metrics)
    * ECS (service health)
5. Lambda sends the collected context to Amazon Bedrock.
6. Bedrock returns a concise incident summary.
7. Lambda posts the summary to Slack.

Example Slack message:
🚨 Incident Summary

Service: payment-api

Severity: Critical

Alert:
CPU > 90%

AI Analysis

Root Cause (85% confidence)
Recent deployment caused an increase in thread utilization.

Evidence
• ECS Task 3 restarting
• 17 NullPointerExceptions
• ALB 5xx increased 400%
• Deployment completed 8 minutes ago

Recommended Actions
1. Roll back deployment
2. Restart unhealthy task
3. Check DB connection pool

Estimated Business Impact
Checkout latency increased.

Overall Architecture
             Grafana Alert
                    │
                    ▼
            Amazon EventBridge
                    │
                    ▼
              Lambda (Python)
                    │
        ┌───────────┼─────────────┐
        ▼           ▼             ▼
    CloudWatch   ECS API     Splunk API
        │           │             │
        └───────────┼─────────────┘
                    ▼
             Amazon Bedrock
                    │
                    ▼
             Slack Notification

AWS Services
Service

Purpose

EventBridge

Receive Grafana alerts

Lambda

AI orchestration

Bedrock

LLM reasoning

IAM

Least-privilege access

CloudWatch

Metrics

ECS

Service status

Secrets Manager

Splunk and Slack secrets

S3

Lambda deployment artifacts

CloudWatch Logs

Lambda logging



ai-incident-summarizer/
│
├── terraform/
│   ├── modules/
│   │
│   ├── lambda/
│   ├── iam/
│   ├── eventbridge/
│   ├── bedrock/
│   ├── secrets/
│   ├── cloudwatch/
│   ├── s3/
│   │
│   ├── main.tf
│   ├── providers.tf
│   ├── outputs.tf
│   ├── variables.tf
│   └── terraform.tfvars
│
├── lambda/
│   ├── app.py
│   ├── prompt.py
│   ├── slack.py
│   ├── grafana.py
│   ├── ecs.py
│   ├── splunk.py
│   ├── cloudwatch.py
│   └── requirements.txt
│
├── .github/
│
├── README.md


Terraform Resources

We’ll provision:

S3 Bucket

Stores the Lambda deployment package.

⸻

IAM Role

Permissions for:

* Bedrock
* ECS
* CloudWatch
* Secrets Manager
* Logs

⸻

Lambda

Python 3.13

Memory

512 MB

Timeout

60 seconds

⸻

EventBridge Rule

Matches incoming Grafana webhook events.

⸻

Secrets Manager

* Splunk Token
* Slack Webhook
* Grafana API Token

⸻

CloudWatch Log Group

Retention

30 days

⸻

Development Phases

Phase 1

Infrastructure

* Terraform
* Lambda
* IAM
* S3
* EventBridge
* Secrets

No AI yet.

⸻

Phase 2

Lambda receives alerts.

⸻

Phase 3

Lambda calls:

* ECS
* CloudWatch
* Grafana

⸻

Phase 4

Lambda integrates with Splunk.

⸻

Phase 5

Bedrock prompt engineering.

⸻

Phase 6

Slack notifications.

⸻

Phase 7

GitHub Actions deployment.

⸻

GitHub Actions

We’ll also build a production-style CI/CD pipeline:
terraform fmt

↓

terraform validate

↓

terraform plan

↓

Approval

↓

terraform apply

↓

Package Lambda

↓

Upload to S3

↓

Update Lambda

Technologies You’ll Demonstrate

This project will showcase:

* Terraform module design
* AWS Lambda
* Amazon Bedrock
* EventBridge
* ECS APIs
* CloudWatch APIs
* Splunk REST API integration
* Secrets Manager
* GitHub Actions with OIDC
* IAM least privilege
* Production logging and monitoring
* AI prompt engineering for incident response

What we’ll build first

We’ll start by creating a production-ready Terraform foundation with reusable modules for:

1. VPC (optional if using existing networking)
2. S3 (Lambda artifacts)
3. IAM (Lambda execution role)
4. Lambda function
5. EventBridge rule and target
6. Secrets Manager
7. CloudWatch Log Group

Then we’ll implement the Lambda in Python, followed by the AI integration.

This approach mirrors how a production DevOps/SRE team would deliver the solution incrementally, keeping each stage deployable and testable.