variable "servers" {
  description = "Control-plane machines (any Linux)."
  type        = list(object({ address = string }))
}

variable "hosts" {
  description = "VM hosts: Linux with /dev/kvm (bare metal or nested virtualization). zone is the failure domain."
  type        = list(object({ address = string, zone = string }))
}

variable "ssh_user" {
  type    = string
  default = "ubuntu"
}

variable "ssh_private_key" {
  type      = string
  sensitive = true
}

variable "database_url" {
  description = "postgres://user:pass@host:5432/flint?sslmode=require"
  type        = string
  sensitive   = true
}

variable "store_url" {
  description = "s3://bucket/prefix, with ?endpoint=https://... for non-AWS stores."
  type        = string
}

variable "store_region" {
  type    = string
  default = "us-east-1"
}

variable "store_access_key_id" {
  type      = string
  sensitive = true
}

variable "store_secret_access_key" {
  type      = string
  sensitive = true
}

variable "api_url" {
  description = "Public API URL (your load balancer). Default: the first server, port 9300."
  type        = string
  default     = ""
}

variable "host_endpoint" {
  description = "URL hosts reach the control plane on (your load balancer). Default: the first server, port 9301."
  type        = string
  default     = ""
}

variable "backend" {
  type    = string
  default = "firecracker"
}

variable "flint_ref" {
  type    = string
  default = "main"
}
