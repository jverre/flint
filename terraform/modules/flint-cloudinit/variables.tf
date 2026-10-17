variable "role" {
  description = "server (control plane) or host (runs VMs)."
  type        = string
  validation {
    condition     = contains(["server", "host"], var.role)
    error_message = "role must be server or host."
  }
}

variable "flint_ref" {
  description = "Git ref of Flint to install."
  type        = string
  default     = "main"
}

variable "flint_repo" {
  type    = string
  default = "https://github.com/jverre/flint"
}

# ── shared ────────────────────────────────────────────────────────────────

variable "store_url" {
  description = "Snapshot store: s3://bucket/prefix (add ?endpoint=https://... for R2, GCS interop, MinIO, Ceph) or file:///shared/mount."
  type        = string
}

variable "store_region" {
  type    = string
  default = ""
}

variable "store_access_key_id" {
  description = "Static S3 credentials; leave empty to use the instance's role."
  type        = string
  default     = ""
  sensitive   = true
}

variable "store_secret_access_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "extra_env" {
  description = "Additional FLINT_* settings for /etc/flint/flint.env."
  type        = map(string)
  default     = {}
}

# ── server ────────────────────────────────────────────────────────────────

variable "database_url" {
  description = "Control plane database: postgres://... (HA) or sqlite:///var/lib/flint/server/flint.db (one server)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "listen" {
  type    = string
  default = ":9300"
}

variable "host_listen" {
  type    = string
  default = ":9301"
}

variable "host_endpoint" {
  description = "URL hosts reach the host listener on (through the load balancer if there is one)."
  type        = string
  default     = ""
}

variable "admin_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "join_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "pki" {
  description = "Outputs of the flint-pki module (servers only). Without it, the first server generates the CAs."
  type = object({
    cp_ca_cert_pem    = string
    cp_ca_key_pem     = string
    hosts_ca_cert_pem = string
    hosts_ca_key_pem  = string
  })
  default   = null
  sensitive = true
}

# ── host ──────────────────────────────────────────────────────────────────

variable "join_token" {
  description = "Join token (flint-pki's join_token, or one from `flint cluster join-token`)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "advertise_addr" {
  description = "Address the control plane and other hosts reach this host on; auto = its primary address."
  type        = string
  default     = "auto"
}

variable "zone" {
  description = "Failure domain (e.g. the cloud zone). VM sets spread across zones."
  type        = string
  default     = ""
}

variable "backend" {
  type    = string
  default = "firecracker"
  validation {
    condition     = contains(["firecracker", "cloud-hypervisor"], var.backend)
    error_message = "backend must be firecracker or cloud-hypervisor."
  }
}

variable "agent_port" {
  type    = number
  default = 9400
}

variable "snapd_port" {
  type    = number
  default = 9501
}

variable "watchdog" {
  description = "Back fencing with the host watchdog (softdog if there's no hardware one). Recommended for durable VMs."
  type        = bool
  default     = true
}

variable "max_vms" {
  description = "Cap on VMs this host runs (0 = derive from memory)."
  type        = number
  default     = 0
}
