mock_provider "cloudflare" {}

variables {
  cloudflare_account_id         = "00000000000000000000000000000000"
  cloudflare_zone_id            = "11111111111111111111111111111111"
  bucket_name                   = "build-index-test"
  public_hostname               = "packages.example.com"
  manage_zone_cache_ruleset     = true
  manage_zone_transform_ruleset = true
  enable_smart_tiered_cache     = true
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
    condition     = length(cloudflare_ruleset.simple_cache) == 1
    error_message = "The stack must create the cache ruleset when it owns the phase."
  }

  assert {
    condition     = length(cloudflare_ruleset.simple_rewrites) == 1
    error_message = "The stack must create the transform ruleset when it owns the phase."
  }

  assert {
    condition     = length(cloudflare_tiered_cache.index) == 1
    error_message = "The stack must enable Smart Tiered Cache when requested."
  }

  assert {
    condition = (
      length(output.transform_rules) == 3
      && output.transform_rules[0].action_parameters.uri.path.expression
      == "wildcard_replace(http.request.uri.path, \"/simple/*\", \"/simple/v1+json/$${1}index.json\")"
      && output.transform_rules[1].action_parameters.uri.path.expression
      == "concat(http.request.uri.path, \"index.json\")"
      && output.transform_rules[2].action_parameters.uri.path.expression
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

run "supports_shared_zone_rulesets" {
  command = plan

  variables {
    manage_zone_cache_ruleset     = false
    manage_zone_transform_ruleset = false
    enable_smart_tiered_cache     = false
  }

  assert {
    condition     = length(cloudflare_ruleset.simple_cache) == 0
    error_message = "The stack must not replace a cache ruleset owned elsewhere."
  }

  assert {
    condition     = length(cloudflare_ruleset.simple_rewrites) == 0
    error_message = "The stack must not replace a transform ruleset owned elsewhere."
  }

  assert {
    condition     = length(output.cache_rules) == 1 && length(output.transform_rules) == 3
    error_message = "The stack must expose composable cache and transform rules."
  }
}
