output "cp_ca_cert_pem" {
  value = tls_self_signed_cert.ca["cp"].cert_pem
}

output "cp_ca_key_pem" {
  value     = tls_private_key.ca["cp"].private_key_pem
  sensitive = true
}

output "hosts_ca_cert_pem" {
  value = tls_self_signed_cert.ca["hosts"].cert_pem
}

output "hosts_ca_key_pem" {
  value     = tls_private_key.ca["hosts"].private_key_pem
  sensitive = true
}

output "admin_token" {
  description = "Bootstrap admin API token (FLINT_API_KEY for the CLI)."
  value       = local.admin_token
  sensitive   = true
}

output "join_secret" {
  value     = random_password.join.result
  sensitive = true
}

output "join_token" {
  description = "Reusable host join token (FLINT_JOIN_TOKEN on hosts)."
  value       = local.join_token
  sensitive   = true
}
