# R2 Index Infrastructure

This Terraform stack manages the Cloudflare resources that store and serve the
generated package indexes:

- One R2 bucket.
- One public R2 custom domain with TLS 1.2 or newer.
- Disabled public `r2.dev` access.
- URL rewrites from Simple API trailing-slash URLs to the generated
  `index.json` and `index.html` objects.
- Short-lived cache settings for `/simple/`.
- Optional Smart Tiered Cache.

It does not create the R2 S3 access key used by GitHub Actions. Creating that
secret through Terraform would place it in Terraform state. Create a
bucket-scoped Object Read & Write token separately and store its access key ID
and secret as GitHub Actions secrets.

## URL Layout

The publication workflow uploads objects such as:

```text
simple/v1+json/cu128/index.json
simple/v1+json/cu128/vllm/index.json
simple/v1+html/cu128/index.html
simple/v1+html/cu128/vllm/index.html
```

The transform rules expose them as:

```text
/simple/cu128/                 -> /simple/v1+json/cu128/index.json
/simple/cu128/vllm/            -> /simple/v1+json/cu128/vllm/index.json
/simple/v1+json/cu128/vllm/    -> /simple/v1+json/cu128/vllm/index.json
/simple/v1+html/cu128/vllm/    -> /simple/v1+html/cu128/vllm/index.html
```

These are internal rewrites. Client-visible URLs remain unchanged.

## Zone Ruleset Ownership

Cloudflare permits one zone entry-point ruleset per phase. Before applying,
determine which Terraform stack owns:

- `http_request_cache_settings`
- `http_request_transform`

If this stack is the sole owner, set the corresponding
`manage_zone_*_ruleset` variable to `true`.

If another stack owns a phase, set the variable to `false` and merge this
stack's `cache_rules` or `transform_rules` output into the owner. Do not create
two independent `cloudflare_ruleset` resources for the same zone and phase.

Smart Tiered Cache is also zone-wide. Enable it here only when this stack owns
that setting.

## Deploy

The Cloudflare API token used by Terraform needs:

- Account: Workers R2 Storage Write
- Account: Workers R2 Storage Read
- Zone access for the configured zone
- Zone: Cache Settings Write when managing the cache ruleset
- Zone: Transform Rules Write and Account Rulesets Read when managing rewrites
- Zone: Zone Settings Write when enabling Smart Tiered Cache

Configure a Terraform backend appropriate for the deployment environment before
the first production apply. Then:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars

export CLOUDFLARE_API_TOKEN="..."
terraform init
terraform plan
terraform apply
```

After apply, configure `_build-index` with:

```text
GitHub variable R2_BUCKET   = terraform output -raw bucket_name
GitHub variable R2_ENDPOINT = terraform output -raw r2_endpoint
GitHub secret   R2_ACCESS_KEY_ID
GitHub secret   R2_SECRET_ACCESS_KEY
```

The custom domain can take several minutes to become active. Check:

```bash
terraform output public_custom_domain_status
terraform output public_base_url
```

R2 buckets must be empty before Terraform can destroy them.
