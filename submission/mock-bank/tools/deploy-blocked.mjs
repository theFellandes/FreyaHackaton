console.error(
  "Deployment is blocked: this is a preparation bootstrap, not the bank service. " +
  "Read the hidden contract, implement and test its fixture adapter, authentication " +
  "and persistent storage, then replace this command with the verified Wrangler invocation."
);
process.exitCode = 2;
