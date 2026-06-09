mock_provider "cloudflare" {}

variables {
  cloudflare_account_id     = "00000000000000000000000000000000"
  cloudflare_zone_id        = "11111111111111111111111111111111"
  bucket_name               = "build-index-test"
  public_hostname           = "packages.example.com"
  enable_smart_tiered_cache = true
}

run "manages_public_r2_index" {
  command = plan

  assert {
    condition     = !cloudflare_r2_managed_domain.index.enabled
    error_message = "Production must disable the public r2.dev endpoint."
  }

  assert {
    condition     = cloudflare_r2_custom_domain.index.domain == "packages.example.com"
    error_message = "The R2 bucket must be attached to the configured custom domain."
  }

  assert {
    condition     = cloudflare_ruleset.simple_cache.phase == "http_request_cache_settings"
    error_message = "The stack must own the zone cache ruleset."
  }

  assert {
    condition     = cloudflare_ruleset.simple_rewrites.phase == "http_request_transform"
    error_message = "The stack must own the zone transform ruleset."
  }

  assert {
    condition     = length(cloudflare_tiered_cache.index) == 1
    error_message = "The stack must enable Smart Tiered Cache when requested."
  }

  assert {
    condition = (
      length(cloudflare_ruleset.simple_rewrites.rules) == 3
      && cloudflare_ruleset.simple_rewrites.rules[0].action_parameters.uri.path.expression
      == "wildcard_replace(http.request.uri.path, \"/simple/*\", \"/simple/v1+json/$${1}index.json\")"
      && cloudflare_ruleset.simple_rewrites.rules[1].action_parameters.uri.path.expression
      == "concat(http.request.uri.path, \"index.json\")"
      && cloudflare_ruleset.simple_rewrites.rules[2].action_parameters.uri.path.expression
      == "concat(http.request.uri.path, \"index.html\")"
    )
    error_message = "The transform rules must map default, JSON, and HTML trailing-slash URLs to generated index files."
  }

  assert {
    condition = (
      output.github_actions_variables.R2_BUCKET == "build-index-test"
      && output.github_actions_variables.R2_ENDPOINT
      == "https://00000000000000000000000000000000.r2.cloudflarestorage.com"
    )
    error_message = "The stack must expose the non-secret publisher settings."
  }
}
