output "bucket_name" {
  description = "R2 bucket name for the index publisher."
  value       = cloudflare_r2_bucket.index.name
}

output "r2_endpoint" {
  description = "R2 S3 endpoint for the index publisher."
  value       = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
}

output "public_base_url" {
  description = "Public base URL for Simple API requests."
  value       = "https://${cloudflare_r2_custom_domain.index.domain}"
}

output "public_custom_domain_status" {
  description = "Cloudflare ownership and SSL status for the R2 custom domain."
  value       = cloudflare_r2_custom_domain.index.status
}

output "github_actions_variables" {
  description = "Non-secret GitHub Actions variables consumed by the publication workflow."
  value = {
    R2_BUCKET   = cloudflare_r2_bucket.index.name
    R2_ENDPOINT = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
  }
}

output "cache_ruleset_id" {
  description = "ID of the zone cache ruleset."
  value       = cloudflare_ruleset.simple_cache.id
}

output "transform_ruleset_id" {
  description = "ID of the zone URL rewrite ruleset."
  value       = cloudflare_ruleset.simple_rewrites.id
}
