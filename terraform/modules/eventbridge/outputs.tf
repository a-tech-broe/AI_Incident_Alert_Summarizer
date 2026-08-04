output "event_bus_name" {
  description = "Name of the alerts bus. Producers target this bus in PutEvents."
  value       = aws_cloudwatch_event_bus.this.name
}

output "event_bus_arn" {
  description = "ARN of the alerts bus."
  value       = aws_cloudwatch_event_bus.this.arn
}

output "rule_name" {
  description = "Name of the routing rule."
  value       = aws_cloudwatch_event_rule.this.name
}

output "rule_arn" {
  description = "ARN of the routing rule."
  value       = aws_cloudwatch_event_rule.this.arn
}

output "event_pattern" {
  description = "Rendered event pattern, useful when configuring producers."
  value       = aws_cloudwatch_event_rule.this.event_pattern
}
