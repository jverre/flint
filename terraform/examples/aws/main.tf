# A highly available Flint cluster on AWS:
#
#   - control plane: an autoscaling group of servers across zones behind a
#     network load balancer (API on 9300, hosts on 9301), RDS Postgres;
#   - snapshots: an S3 bucket, reached with an instance role (no keys);
#   - hosts: one autoscaling group per zone, so every host knows its zone and
#     VM sets spread across them. Hosts need /dev/kvm: .metal instances
#     (default) or a type with nested virtualization.
#
#   terraform init && terraform apply
#   export FLINT_URL=$(terraform output -raw api_url) FLINT_API_KEY=$(terraform output -raw admin_token)
#   flint cluster hosts

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 5.0" }
    random = { source = "hashicorp/random", version = ">= 3.5" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "zones" {
  for_each = toset(var.zones)
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = [each.value]
  }
}


locals {
  subnets       = [for z in var.zones : data.aws_subnets.zones[z].ids[0]]
  host_endpoint = "https://${aws_lb.control_plane.dns_name}:9301"
  store_url     = "s3://${aws_s3_bucket.snapshots.bucket}/snapshots"
}

# ── snapshot store ────────────────────────────────────────────────────────

resource "aws_s3_bucket" "snapshots" {
  bucket_prefix = "${var.name}-snapshots-"
  force_destroy = var.destroy_snapshots_with_cluster
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_role" "node" {
  name_prefix = "${var.name}-node-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "snapshots" {
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.snapshots.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.snapshots.arn}/*" },
    ]
  })
}

resource "aws_iam_instance_profile" "node" {
  name_prefix = "${var.name}-node-"
  role        = aws_iam_role.node.name
}

# ── network ───────────────────────────────────────────────────────────────

resource "aws_security_group" "server" {
  name_prefix = "${var.name}-server-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description = "API"
    from_port   = 9300
    to_port     = 9300
    protocol    = "tcp"
    cidr_blocks = var.api_cidrs
  }
  ingress {
    description = "hosts (NLB preserves their addresses)"
    from_port   = 9301
    to_port     = 9301
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "host" {
  name_prefix = "${var.name}-host-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description     = "agent API, control plane only"
    from_port       = 9400
    to_port         = 9400
    protocol        = "tcp"
    security_groups = [aws_security_group.server.id]
  }
  ingress {
    description = "snapshot chunks between hosts"
    from_port   = 9501
    to_port     = 9501
    protocol    = "tcp"
    self        = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "control_plane" {
  name_prefix        = substr(var.name, 0, 6)
  load_balancer_type = "network"
  internal           = var.internal_api
  subnets            = local.subnets
}

resource "aws_lb_target_group" "control_plane" {
  for_each    = { api = 9300, hosts = 9301 }
  name_prefix = substr("${each.key}${var.name}", 0, 6)
  port        = each.value
  protocol    = "TCP"
  vpc_id      = data.aws_vpc.default.id
  health_check {
    protocol = "TCP"
  }
}

resource "aws_lb_listener" "control_plane" {
  for_each          = aws_lb_target_group.control_plane
  load_balancer_arn = aws_lb.control_plane.arn
  port              = each.value.port
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = each.value.arn
  }
}

# ── database ──────────────────────────────────────────────────────────────

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.server.id]
  }
}

resource "aws_db_instance" "catalog" {
  identifier_prefix      = "${var.name}-"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = var.db_instance_class
  allocated_storage      = 20
  db_name                = "flint"
  username               = "flint"
  password               = random_password.db.result
  multi_az               = var.db_multi_az
  vpc_security_group_ids = [aws_security_group.db.id]
  skip_final_snapshot    = var.destroy_snapshots_with_cluster
  storage_encrypted      = true
}

# ── control plane ─────────────────────────────────────────────────────────

module "pki" {
  source        = "../../modules/flint-pki"
  host_endpoint = local.host_endpoint
}

module "server" {
  source        = "../../modules/flint-cloudinit"
  role          = "server"
  flint_ref     = var.flint_ref
  store_url     = local.store_url
  store_region  = var.region
  database_url  = "postgres://flint:${random_password.db.result}@${aws_db_instance.catalog.endpoint}/flint?sslmode=require"
  host_endpoint = local.host_endpoint
  admin_token   = module.pki.admin_token
  join_secret   = module.pki.join_secret
  pki = {
    cp_ca_cert_pem    = module.pki.cp_ca_cert_pem
    cp_ca_key_pem     = module.pki.cp_ca_key_pem
    hosts_ca_cert_pem = module.pki.hosts_ca_cert_pem
    hosts_ca_key_pem  = module.pki.hosts_ca_key_pem
  }
}

resource "aws_launch_template" "server" {
  name_prefix            = "${var.name}-server-"
  image_id               = var.ubuntu_ami_id
  instance_type          = var.server_instance_type
  user_data              = base64encode(module.server.user_data)
  vpc_security_group_ids = [aws_security_group.server.id]
  iam_instance_profile {
    arn = aws_iam_instance_profile.node.arn
  }
  metadata_options {
    http_tokens = "required"
  }
}

resource "aws_autoscaling_group" "server" {
  name_prefix         = "${var.name}-server-"
  min_size            = var.servers
  max_size            = var.servers
  vpc_zone_identifier = local.subnets
  target_group_arns   = [for tg in aws_lb_target_group.control_plane : tg.arn]
  launch_template {
    id      = aws_launch_template.server.id
    version = aws_launch_template.server.latest_version
  }
  instance_refresh {
    strategy = "Rolling"
  }
}

# ── hosts: one group per zone ─────────────────────────────────────────────

module "host" {
  source             = "../../modules/flint-cloudinit"
  for_each           = toset(var.zones)
  role               = "host"
  flint_ref          = contains(var.canary_zones, each.value) && var.canary_host != null ? var.canary_host.flint_ref : var.flint_ref
  backend            = var.backend
  store_url          = local.store_url
  store_region       = var.region
  join_token         = module.pki.join_token
  zone               = each.value
  host_image         = contains(var.canary_zones, each.value) && var.canary_host != null ? var.canary_host.ami_id : var.ubuntu_ami_id
  host_kernel        = contains(var.canary_zones, each.value) && var.canary_host != null ? var.canary_host.kernel : var.host_kernel
  hypervisor_version = contains(var.canary_zones, each.value) && var.canary_host != null ? var.canary_host.hypervisor_version : var.hypervisor_version
  rollout_group      = contains(var.canary_zones, each.value) ? "canary" : "stable"
}

resource "aws_launch_template" "host" {
  for_each               = toset(var.zones)
  name_prefix            = "${var.name}-host-"
  image_id               = contains(var.canary_zones, each.value) && var.canary_host != null ? var.canary_host.ami_id : var.ubuntu_ami_id
  instance_type          = var.host_instance_type
  user_data              = base64encode(module.host[each.value].user_data)
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile {
    arn = aws_iam_instance_profile.node.arn
  }
  metadata_options {
    # Sandboxes can't reach the metadata service anyway (Flint blocks
    # link-local); a hop limit of 1 keeps it host-only regardless.
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size = var.host_disk_gb
      volume_type = "gp3"
    }
  }
}

resource "aws_autoscaling_group" "host" {
  for_each            = toset(var.zones)
  name_prefix         = "${var.name}-host-${each.value}-"
  min_size            = var.hosts_per_zone
  max_size            = var.max_hosts_per_zone
  desired_capacity    = var.hosts_per_zone
  vpc_zone_identifier = [data.aws_subnets.zones[each.value].ids[0]]
  launch_template {
    id      = aws_launch_template.host[each.value].id
    version = aws_launch_template.host[each.value].latest_version
  }
  tag {
    key                 = "flint-zone"
    value               = each.value
    propagate_at_launch = true
  }
}
