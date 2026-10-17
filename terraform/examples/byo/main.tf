# A Flint cluster on machines you already have: one or more control-plane
# servers and any number of KVM hosts, reached over SSH. You bring Postgres
# (any managed one works) and an S3-compatible bucket.
#
#   terraform init && terraform apply
#   export FLINT_URL=$(terraform output -raw api_url) FLINT_API_KEY=$(terraform output -raw admin_token)
#   flint cluster hosts
#
# For more than one server, put a TCP load balancer in front of their API
# (9300) and host (9301) ports and set api_url/host_endpoint to it.

terraform {
  required_version = ">= 1.5"
}

module "pki" {
  source        = "../../modules/flint-pki"
  host_endpoint = local.host_endpoint
}

locals {
  host_endpoint = coalesce(var.host_endpoint, "https://${var.servers[0].address}:9301")
  api_url       = coalesce(var.api_url, "http://${var.servers[0].address}:9300")
}

module "server_config" {
  source                  = "../../modules/flint-cloudinit"
  role                    = "server"
  flint_ref               = var.flint_ref
  database_url            = var.database_url
  store_url               = var.store_url
  store_region            = var.store_region
  store_access_key_id     = var.store_access_key_id
  store_secret_access_key = var.store_secret_access_key
  host_endpoint           = local.host_endpoint
  admin_token             = module.pki.admin_token
  join_secret             = module.pki.join_secret
  pki = {
    cp_ca_cert_pem    = module.pki.cp_ca_cert_pem
    cp_ca_key_pem     = module.pki.cp_ca_key_pem
    hosts_ca_cert_pem = module.pki.hosts_ca_cert_pem
    hosts_ca_key_pem  = module.pki.hosts_ca_key_pem
  }
}

module "host_config" {
  source                  = "../../modules/flint-cloudinit"
  for_each                = { for h in var.hosts : h.address => h }
  role                    = "host"
  flint_ref               = var.flint_ref
  backend                 = var.backend
  store_url               = var.store_url
  store_region            = var.store_region
  store_access_key_id     = var.store_access_key_id
  store_secret_access_key = var.store_secret_access_key
  join_token              = module.pki.join_token
  advertise_addr          = each.value.address
  zone                    = each.value.zone
}

resource "terraform_data" "server" {
  for_each         = { for s in var.servers : s.address => s }
  triggers_replace = [module.server_config.env_file, var.flint_ref]

  connection {
    type        = "ssh"
    host        = each.value.address
    user        = var.ssh_user
    private_key = var.ssh_private_key
  }
  provisioner "remote-exec" {
    inline = ["sudo mkdir -p /etc/flint /opt/flint && sudo chown $(id -u) /etc/flint /opt/flint"]
  }
  provisioner "file" {
    content     = module.server_config.env_file
    destination = "/etc/flint/flint.env"
  }
  provisioner "file" {
    content     = module.pki.cp_ca_cert_pem
    destination = "/etc/flint/cp-ca.crt"
  }
  provisioner "file" {
    content     = module.pki.cp_ca_key_pem
    destination = "/etc/flint/cp-ca.key"
  }
  provisioner "file" {
    content     = module.pki.hosts_ca_cert_pem
    destination = "/etc/flint/hosts-ca.crt"
  }
  provisioner "file" {
    content     = module.pki.hosts_ca_key_pem
    destination = "/etc/flint/hosts-ca.key"
  }
  provisioner "file" {
    content     = module.server_config.install_script
    destination = "/opt/flint/install.sh"
  }
  provisioner "remote-exec" {
    inline = [
      "sudo chown -R root:root /etc/flint /opt/flint",
      module.server_config.install_command,
    ]
  }
}

resource "terraform_data" "host" {
  for_each         = { for h in var.hosts : h.address => h }
  depends_on       = [terraform_data.server]
  triggers_replace = [module.host_config[each.key].env_file, var.flint_ref]

  connection {
    type        = "ssh"
    host        = each.value.address
    user        = var.ssh_user
    private_key = var.ssh_private_key
  }
  provisioner "remote-exec" {
    inline = ["sudo mkdir -p /etc/flint /opt/flint && sudo chown $(id -u) /etc/flint /opt/flint"]
  }
  provisioner "file" {
    content     = module.host_config[each.key].env_file
    destination = "/etc/flint/flint.env"
  }
  provisioner "file" {
    content     = module.host_config[each.key].install_script
    destination = "/opt/flint/install.sh"
  }
  provisioner "remote-exec" {
    inline = [
      "sudo chown -R root:root /etc/flint /opt/flint",
      module.host_config[each.key].install_command,
    ]
  }
}
