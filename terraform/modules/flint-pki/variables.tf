variable "host_endpoint" {
  description = "URL hosts use to reach the control plane's host listener, e.g. https://flint-hosts.internal:9301. Its host name must be what the servers' FLINT_HOST_ENDPOINT says."
  type        = string
}
