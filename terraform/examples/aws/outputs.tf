output "api_url" {
  value = "http://${aws_lb.control_plane.dns_name}:9300"
}

output "admin_token" {
  value     = module.pki.admin_token
  sensitive = true
}

output "join_token" {
  description = "Join hosts from elsewhere (e.g. bare metal) with: FLINT_JOIN_TOKEN=<this> flint agent --advertise auto"
  value       = module.pki.join_token
  sensitive   = true
}

output "snapshot_bucket" {
  value = aws_s3_bucket.snapshots.bucket
}
