locals {
  simple_path_prefix      = "/simple/"
  simple_json_path_prefix = "${local.simple_path_prefix}v1+json/"
  simple_html_path_prefix = "${local.simple_path_prefix}v1+html/"

  simple_default_json_rewrite_rule = {
    action      = "rewrite"
    description = "Serve conventional Simple API URLs from JSON index objects"
    enabled     = true
    expression = format(
      "(http.host eq %s and starts_with(http.request.uri.path, %s) and not starts_with(http.request.uri.path, %s) and http.request.uri.path ne %s and ends_with(http.request.uri.path, \"/\"))",
      jsonencode(var.public_hostname),
      jsonencode(local.simple_path_prefix),
      jsonencode("${local.simple_path_prefix}v1+"),
      jsonencode(local.simple_path_prefix),
    )
    ref = "build_index_simple_default_json"

    action_parameters = {
      uri = {
        path = {
          expression = format(
            "wildcard_replace(http.request.uri.path, %s, %s)",
            jsonencode("${local.simple_path_prefix}*"),
            jsonencode("${local.simple_json_path_prefix}$${1}index.json"),
          )
        }
      }
    }
  }

  simple_json_index_rewrite_rule = {
    action      = "rewrite"
    description = "Resolve JSON Simple API trailing-slash URLs"
    enabled     = true
    expression = format(
      "(http.host eq %s and starts_with(http.request.uri.path, %s) and ends_with(http.request.uri.path, \"/\"))",
      jsonencode(var.public_hostname),
      jsonencode(local.simple_json_path_prefix),
    )
    ref = "build_index_simple_json_index"

    action_parameters = {
      uri = {
        path = {
          expression = "concat(http.request.uri.path, \"index.json\")"
        }
      }
    }
  }

  simple_html_index_rewrite_rule = {
    action      = "rewrite"
    description = "Resolve HTML Simple API trailing-slash URLs"
    enabled     = true
    expression = format(
      "(http.host eq %s and starts_with(http.request.uri.path, %s) and ends_with(http.request.uri.path, \"/\"))",
      jsonencode(var.public_hostname),
      jsonencode(local.simple_html_path_prefix),
    )
    ref = "build_index_simple_html_index"

    action_parameters = {
      uri = {
        path = {
          expression = "concat(http.request.uri.path, \"index.html\")"
        }
      }
    }
  }

  transform_rules = [
    local.simple_default_json_rewrite_rule,
    local.simple_json_index_rewrite_rule,
    local.simple_html_index_rewrite_rule,
  ]

  simple_cache_rule = {
    action      = "set_cache_settings"
    description = "Cache static build index Simple API responses"
    enabled     = true
    expression = format(
      "(http.host eq %s and starts_with(http.request.uri.path, %s))",
      jsonencode(var.public_hostname),
      jsonencode(local.simple_path_prefix),
    )
    ref = "build_index_simple_cache"

    action_parameters = {
      cache = true

      browser_ttl = {
        mode = "respect_origin"
      }

      cache_key = {
        cache_deception_armor = true
        custom_key = {
          query_string = {
            exclude = {
              all = true
            }
          }
        }
      }

      edge_ttl = {
        mode    = "override_origin"
        default = var.simple_edge_ttl_seconds
        status_code_ttl = [
          {
            status_code_range = {
              from = 200
              to   = 299
            }
            value = var.simple_edge_ttl_seconds
          },
          {
            status_code_range = {
              from = 300
              to   = 399
            }
            value = 0
          },
          {
            status_code_range = {
              from = 400
              to   = 499
            }
            value = 0
          },
          {
            status_code_range = {
              from = 500
              to   = 599
            }
            value = -1
          },
        ]
      }

      respect_strong_etags = true
    }
  }

  cache_rules = [
    local.simple_cache_rule,
  ]
}

resource "cloudflare_r2_bucket" "index" {
  account_id    = var.cloudflare_account_id
  name          = var.bucket_name
  jurisdiction  = var.jurisdiction
  location      = var.location
  storage_class = var.storage_class

  lifecycle {
    precondition {
      condition     = var.manage_zone_cache_ruleset != null
      error_message = "manage_zone_cache_ruleset must be set explicitly."
    }

    precondition {
      condition     = var.manage_zone_transform_ruleset != null
      error_message = "manage_zone_transform_ruleset must be set explicitly."
    }
  }
}

resource "cloudflare_r2_managed_domain" "index" {
  account_id   = var.cloudflare_account_id
  bucket_name  = cloudflare_r2_bucket.index.name
  enabled      = false
  jurisdiction = var.jurisdiction
}

resource "cloudflare_r2_custom_domain" "index" {
  account_id   = var.cloudflare_account_id
  bucket_name  = cloudflare_r2_bucket.index.name
  domain       = var.public_hostname
  enabled      = true
  zone_id      = var.cloudflare_zone_id
  jurisdiction = var.jurisdiction
  min_tls      = var.minimum_tls_version
}

resource "cloudflare_ruleset" "simple_cache" {
  count = var.manage_zone_cache_ruleset == true ? 1 : 0

  zone_id     = var.cloudflare_zone_id
  name        = "Build index public cache"
  description = "Cache rules for the static build index"
  kind        = "zone"
  phase       = "http_request_cache_settings"
  rules       = local.cache_rules
}

resource "cloudflare_ruleset" "simple_rewrites" {
  count = var.manage_zone_transform_ruleset == true ? 1 : 0

  zone_id     = var.cloudflare_zone_id
  name        = "Build index public URL rewrites"
  description = "URL rewrites for the static build index"
  kind        = "zone"
  phase       = "http_request_transform"
  rules       = local.transform_rules
}

resource "cloudflare_tiered_cache" "index" {
  count = var.enable_smart_tiered_cache ? 1 : 0

  zone_id = var.cloudflare_zone_id
  value   = "on"
}
