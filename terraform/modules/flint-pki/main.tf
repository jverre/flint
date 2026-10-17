# Cluster credentials, generated before any machine exists: the two CAs
# (control plane, hosts), the bootstrap admin token and a reusable host join
# secret. With these, hosts can be created in the same apply as the control
# plane, or by an autoscaling group, and still join it securely.

terraform {
  required_version = ">= 1.5"
  required_providers {
    tls    = { source = "hashicorp/tls", version = ">= 4.0" }
    random = { source = "hashicorp/random", version = ">= 3.5" }
  }
}

locals {
  cas = {
    cp    = "flint control-plane CA"
    hosts = "flint hosts CA"
  }
}

resource "tls_private_key" "ca" {
  for_each    = local.cas
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  for_each              = local.cas
  private_key_pem       = tls_private_key.ca[each.key].private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24 * 365 * 10
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]
  subject {
    common_name  = each.value
    organization = "flint"
  }
}

resource "random_password" "admin" {
  length  = 40
  special = false
}

resource "random_password" "join" {
  length  = 40
  special = false
}

locals {
  admin_token = "flint_admin_${random_password.admin.result}"
  # The token format flint-cluster join reads (pki.JoinToken): it pins the
  # control-plane CA, so a host can't be tricked into joining an impostor.
  join_token = "flintjoin1.${base64encode(jsonencode({
    s  = var.host_endpoint
    i  = "static"
    k  = random_password.join.result
    ca = tls_self_signed_cert.ca["cp"].cert_pem
  }))}"
}
