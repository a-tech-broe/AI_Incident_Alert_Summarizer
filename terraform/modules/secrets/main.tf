resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name                    = "${var.name_prefix}/${replace(each.key, "_", "-")}"
  description             = each.value.description
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(var.tags, { Integration = each.key })
}

# A placeholder version so the Lambda fails with a clear "not yet configured"
# error rather than ResourceNotFoundException on a fresh environment. The real
# value is written out of band; ignore_changes keeps Terraform from reverting it.
resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each = var.secrets

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = jsonencode({ value = "REPLACE_ME", managed_by = "terraform-placeholder" })

  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}
