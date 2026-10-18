output "api_url" {
  value = local.api_url
}

output "admin_token" {
  value     = module.pki.admin_token
  sensitive = true
}

output "join_token" {
  description = "Join more hosts later with: FLINT_JOIN_TOKEN=<this> flint agent --advertise auto"
  value       = module.pki.join_token
  sensitive   = true
}

output "host_user_data" {
  description = "cloud-init user data per host, if you'd rather boot hosts from it than provision over SSH."
  value       = { for k, m in module.host_config : k => m.user_data }
  sensitive   = true
}
