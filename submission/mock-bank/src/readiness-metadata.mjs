/**
 * Runtime bindings avoid embedding a bundle's own hash or its not-yet-created version ID.
 * These public hashes must be set from the exact bundle and original inputs before deployment.
 * This helper does not declare the bank ready.
 */
export function readinessMetadata(env = {}) {
  return {
    cloudflare_version_id: env.CF_VERSION_METADATA?.id ?? null,
    bundle_sha256: env.BANK_BUNDLE_SHA256 ?? null,
    contract_sha256: env.BANK_CONTRACT_SHA256 ?? null,
    customer_fixture_sha256: env.BANK_CUSTOMER_FIXTURE_SHA256 ?? null
  };
}
