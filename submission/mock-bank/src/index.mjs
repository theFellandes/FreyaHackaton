import { readinessMetadata } from "./readiness-metadata.mjs";

/**
 * Preparation bootstrap only. Replace after reading the hidden service contract.
 * No bank operation, fixture, authentication or persistent storage exists.
 */
const notReady = {
  ready: false,
  bank_ready: false,
  status: "preparation_only",
  reason: "The hidden service contract and customer fixtures have not been implemented."
};

export default {
  async fetch(request, env = {}) {
    const url = new URL(request.url);
    const body = url.pathname === "/__submission_health"
      ? { ...notReady, ...readinessMetadata(env) }
      : { ...notReady, error: "bank_mock_not_implemented" };
    return Response.json(body, {
      status: 503,
      headers: { "Cache-Control": "no-store" }
    });
  }
};
