# Renders one Flint node's configuration as cloud-init user data (or as an
# env file plus install script for machines you provision another way).
# Pure Terraform: no cloud provider, so every cloud module and hand-rolled
# setup shares it.

terraform {
  required_version = ">= 1.5"
}

locals {
  server_env = {
    FLINT_DATABASE_URL  = var.database_url
    FLINT_LISTEN        = var.listen
    FLINT_HOST_LISTEN   = var.host_listen
    FLINT_HOST_ENDPOINT = var.host_endpoint
    FLINT_ADMIN_TOKEN   = var.admin_token
    FLINT_JOIN_SECRET   = var.join_secret
    FLINT_CP_CA_CERT    = var.pki == null ? "" : "/etc/flint/cp-ca.crt"
    FLINT_CP_CA_KEY     = var.pki == null ? "" : "/etc/flint/cp-ca.key"
    FLINT_HOSTS_CA_CERT = var.pki == null ? "" : "/etc/flint/hosts-ca.crt"
    FLINT_HOSTS_CA_KEY  = var.pki == null ? "" : "/etc/flint/hosts-ca.key"
  }
  host_env = {
    FLINT_HOST_IMAGE         = var.host_image
    FLINT_HOST_KERNEL        = var.host_kernel
    FLINT_HYPERVISOR_VERSION = var.hypervisor_version
    FLINT_ROLLOUT_GROUP      = var.rollout_group
    FLINT_JOIN_TOKEN         = var.join_token
    FLINT_ADVERTISE_ADDR     = var.advertise_addr
    FLINT_ZONE               = var.zone
    FLINT_BACKEND            = var.backend
    FLINT_PORT               = tostring(var.agent_port)
    FLINT_SNAPD_PORT         = tostring(var.snapd_port)
    FLINT_SNAPD_PEER_LISTEN  = ":${var.snapd_port}"
    FLINT_WATCHDOG_DEVICE    = var.watchdog ? "/dev/watchdog" : ""
    FLINT_MAX_VMS            = var.max_vms == 0 ? "" : tostring(var.max_vms)
  }
  shared_env = {
    FLINT_STORE_URL       = var.store_url
    AWS_REGION            = var.store_region
    AWS_ACCESS_KEY_ID     = var.store_access_key_id
    AWS_SECRET_ACCESS_KEY = var.store_secret_access_key
  }
  env = merge(local.shared_env, var.role == "server" ? local.server_env : local.host_env, var.extra_env)
  env_file = join("", [
    for k in sort(keys(local.env)) : "${k}=${local.env[k]}\n" if local.env[k] != ""
  ])

  pki_files = var.role == "server" && var.pki != null ? [
    { path = "/etc/flint/cp-ca.crt", content = var.pki.cp_ca_cert_pem, permissions = "0644" },
    { path = "/etc/flint/cp-ca.key", content = var.pki.cp_ca_key_pem, permissions = "0640" },
    { path = "/etc/flint/hosts-ca.crt", content = var.pki.hosts_ca_cert_pem, permissions = "0644" },
    { path = "/etc/flint/hosts-ca.key", content = var.pki.hosts_ca_key_pem, permissions = "0640" },
  ] : []

  install_script = file("${path.module}/files/install.sh")

  cloud_config = {
    write_files = concat([
      { path = "/etc/flint/flint.env", content = local.env_file, permissions = "0600" },
      { path = "/opt/flint/install.sh", content = local.install_script, permissions = "0755" },
    ], local.pki_files)
    runcmd = [
      ["env", "FLINT_ROLE=${var.role}", "FLINT_REF=${var.flint_ref}", "FLINT_REPO=${var.flint_repo}",
        "FLINT_BACKEND=${var.backend}", "FLINT_HOST_KERNEL=${var.host_kernel}",
      "FLINT_HYPERVISOR_VERSION=${var.hypervisor_version}", "/opt/flint/install.sh"],
    ]
  }
}
