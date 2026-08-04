# Bedrock has no "provision the model" resource — access is granted per account
# in the console and the model is called at runtime. This module therefore owns
# the parts of Bedrock that *are* declarative: the guardrail, and the resource
# ARNs the execution role needs permission on.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  # Cross-region inference profiles ("us.", "eu.", "apac." prefixes) route to a
  # foundation model in one of several regions. Permission is needed on both the
  # profile and the underlying model, hence the wildcard region below.
  is_inference_profile = can(regex("^(us|eu|apac|us-gov)\\.", var.model_id))
  base_model_id        = local.is_inference_profile ? regex("^(?:us|eu|apac|us-gov)\\.(.+)$", var.model_id)[0] : var.model_id

  foundation_model_arn = "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/${local.base_model_id}"

  inference_profile_arn = local.is_inference_profile ? format(
    "arn:%s:bedrock:%s:%s:inference-profile/%s",
    data.aws_partition.current.partition,
    data.aws_region.current.name,
    data.aws_caller_identity.current.account_id,
    var.model_id,
  ) : null

  invoke_resource_arns = compact([local.foundation_model_arn, local.inference_profile_arn])
}

# ---------------------------------------------------------------------------
# Guardrail
#
# Alert payloads are attacker-influenced in the general case: a pod name, a log
# line or a Grafana annotation can carry instructions aimed at the model. The
# guardrail is the backstop behind the prompt-level delimiting in prompt.py.
# ---------------------------------------------------------------------------

resource "aws_bedrock_guardrail" "this" {
  count = var.enable_guardrail ? 1 : 0

  name                      = "${var.name_prefix}-guardrail"
  description               = "Constrains incident summarization output and filters hostile content arriving in alert payloads."
  blocked_input_messaging   = "This alert payload was blocked by content policy and was not summarized."
  blocked_outputs_messaging = "The generated summary was blocked by content policy."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE" # PROMPT_ATTACK only supports input filtering
    }

    filters_config {
      type            = "MISCONDUCT"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
  }

  # Telemetry routinely carries connection strings and tokens. Redact them
  # rather than block, so the summary still reaches the responder.
  sensitive_information_policy_config {
    pii_entities_config {
      type   = "AWS_ACCESS_KEY"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "AWS_SECRET_KEY"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "PASSWORD"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "EMAIL"
      action = "ANONYMIZE"
    }
  }

  tags = var.tags
}

resource "aws_bedrock_guardrail_version" "this" {
  count = var.enable_guardrail ? 1 : 0

  guardrail_arn = aws_bedrock_guardrail.this[0].guardrail_arn
  description   = "Managed by Terraform."

  lifecycle {
    create_before_destroy = true
  }
}
