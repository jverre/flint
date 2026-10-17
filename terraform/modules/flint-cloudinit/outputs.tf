output "user_data" {
  description = "cloud-init user data that installs and starts the node."
  value       = "#cloud-config\n${yamlencode(local.cloud_config)}"
  sensitive   = true
}

output "env_file" {
  description = "Contents of /etc/flint/flint.env, for machines provisioned without cloud-init."
  value       = local.env_file
  sensitive   = true
}

output "install_script" {
  value = local.install_script
}

output "install_command" {
  description = "Run on the machine after writing env_file (and the CA files on servers)."
  value       = "sudo env FLINT_ROLE=${var.role} FLINT_REF=${var.flint_ref} FLINT_REPO=${var.flint_repo} FLINT_BACKEND=${var.backend} bash /opt/flint/install.sh"
}

output "pki_files" {
  description = "CA files a server needs (path, content, permissions)."
  value       = local.pki_files
  sensitive   = true
}
