variable "cloudflare_account_id" {
  description = "Cloudflare account ID that owns the R2 bucket and DNS zone."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a 32-character lowercase hexadecimal Cloudflare account ID."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID containing public_hostname."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character lowercase hexadecimal Cloudflare zone ID."
  }
}

variable "bucket_name" {
  description = "Globally unique name for the R2 bucket."
  type        = string

  validation {
    condition     = length(trimspace(var.bucket_name)) > 0 && trimspace(var.bucket_name) == var.bucket_name
    error_message = "bucket_name must be non-empty and must not contain leading or trailing whitespace."
  }
}

variable "public_hostname" {
  description = "Lowercase custom hostname attached directly to the R2 bucket."
  type        = string

  validation {
    condition = (
      length(trimspace(var.public_hostname)) > 0
      && trimspace(var.public_hostname) == var.public_hostname
      && lower(var.public_hostname) == var.public_hostname
      && !strcontains(var.public_hostname, "://")
      && !strcontains(var.public_hostname, "/")
    )
    error_message = "public_hostname must be a lowercase bare hostname without whitespace, a URL scheme, or a path."
  }
}

variable "jurisdiction" {
  description = "R2 data jurisdiction applied to the bucket and public-domain settings."
  type        = string
  default     = "default"

  validation {
    condition     = contains(["default", "eu", "fedramp"], var.jurisdiction)
    error_message = "jurisdiction must be one of: default, eu, fedramp."
  }
}

variable "location" {
  description = "Optional best-effort R2 bucket location hint, honored only when the bucket is first created."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.location == null
      || contains(["apac", "eeur", "enam", "weur", "wnam", "oc"], var.location)
    )
    error_message = "location must be null or one of: apac, eeur, enam, weur, wnam, oc."
  }
}

variable "storage_class" {
  description = "Default storage class for newly uploaded R2 objects."
  type        = string
  default     = "Standard"

  validation {
    condition     = contains(["Standard", "InfrequentAccess"], var.storage_class)
    error_message = "storage_class must be Standard or InfrequentAccess."
  }
}

variable "minimum_tls_version" {
  description = "Minimum TLS version accepted by the R2 custom hostname."
  type        = string
  default     = "1.2"

  validation {
    condition     = contains(["1.0", "1.1", "1.2", "1.3"], var.minimum_tls_version)
    error_message = "minimum_tls_version must be one of: 1.0, 1.1, 1.2, 1.3."
  }
}

variable "simple_edge_ttl_seconds" {
  description = "Cloudflare edge-cache TTL for successful Simple API responses."
  type        = number
  default     = 60

  validation {
    condition     = var.simple_edge_ttl_seconds > 0
    error_message = "simple_edge_ttl_seconds must be greater than zero."
  }
}

variable "enable_smart_tiered_cache" {
  description = "Whether this stack enables the zone-wide Smart Tiered Cache setting."
  type        = bool
  default     = false
}
