variable "name" {
  type    = string
  default = "flint"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "zones" {
  description = "At least two availability zones; hosts get one group per zone."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "servers" {
  description = "Control-plane replicas, spread across the zones."
  type        = number
  default     = 2
}

variable "server_instance_type" {
  type    = string
  default = "t3.small"
}

variable "host_instance_type" {
  description = "Needs /dev/kvm: a .metal type, or one with nested virtualization."
  type        = string
  default     = "c6i.metal"
}

variable "hosts_per_zone" {
  type    = number
  default = 1
}

variable "max_hosts_per_zone" {
  type    = number
  default = 4
}

variable "host_disk_gb" {
  type    = number
  default = 100
}

variable "backend" {
  type    = string
  default = "firecracker"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "db_multi_az" {
  description = "Highly available catalog (recommended for production)."
  type        = bool
  default     = true
}

variable "api_cidrs" {
  description = "Who may call the API (your backend, operators)."
  type        = list(string)
}

variable "internal_api" {
  description = "Keep the load balancer internal to the VPC."
  type        = bool
  default     = true
}

variable "flint_ref" {
  type    = string
  default = "main"
}

variable "destroy_snapshots_with_cluster" {
  description = "Let terraform destroy delete the bucket's snapshots and skip the final DB snapshot."
  type        = bool
  default     = false
}

variable "ubuntu_ami_id" {
  description = "Pinned Canonical Ubuntu AMI for this region. Promote an explicitly tested image; no automatic latest lookup."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]+$", var.ubuntu_ami_id))
    error_message = "Provide an explicit Ubuntu AMI ID."
  }
}
variable "host_kernel" {
  description = "Exact running kernel release of the approved host image, including flavour. Livepatch support must be verified for this exact kernel."
  type        = string
}
variable "hypervisor_version" {
  description = "Explicit hypervisor release, e.g. v46.0 for Cloud Hypervisor; never latest."
  type        = string
}
variable "canary_zones" {
  description = "Zones designated for canary hosts. Apply and rehearse their image changes before stable hosts."
  type        = list(string)
  default     = []
}

variable "canary_host" {
  description = "Candidate host image/kernel/VMM/source applied only to canary_zones. Promote these exact values to stable after maintenance rehearsals pass."
  type        = object({ ami_id = string, kernel = string, hypervisor_version = string, flint_ref = string })
  default     = null
}
