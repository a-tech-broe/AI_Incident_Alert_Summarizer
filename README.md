# AI_SRE

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
