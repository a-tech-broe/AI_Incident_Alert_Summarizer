config {
  call_module_type = "local"
  force            = false
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.48.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

# The ruleset validates runtimes against a list baked in at release time, so a
# runtime newer than the pinned plugin reads as invalid. Keep this version
# ahead of the runtimes in use, or the next Python release breaks CI again.

# Module variables are documented for consumers even when the root stack does
# not currently set them; an unused declaration is not a defect here.
rule "terraform_unused_declarations" {
  enabled = false
}

rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_naming_convention" {
  enabled = true
  format  = "snake_case"
}
