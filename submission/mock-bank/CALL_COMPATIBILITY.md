# Compiled-agent transport compatibility

The user reports these behaviors from the end-to-end invented-case test. Confirm them again against the settled builder before selecting the final run.

- The agent sends POST requests with JSON bodies for every service, including operations described as GET in the contract. Keep the contract's canonical routes and methods; add POST JSON compatibility routes using the same field names and response/status behavior.
- Path placeholders such as `{id}` are not interpolated. Accept the literal placeholder route, including URL-encoded braces, and read the identifier from the contract's JSON field. If a concrete path identifier and body identifier are both supplied, require them to agree.
- A bare mock origin retains a contract server prefix. Serve both the exact contract-prefixed routes and their root aliases. Do not guess the prefix or bank endpoint names before the integration document arrives.
- Persist every stateful operation required by the actual contract using SQLite-backed Durable Objects on the Workers Free plan. Do not copy identity attempts, promises or collections state from the invented case into the final inbound statement enquiry without a contract requirement.
- Return the released test customers' exact values, response shapes and HTTP statuses. Preserve the original fixture bytes and hash them in deployment evidence.

The released brief supplies `customerNumber` and `now`, with the caller already authenticated by IVR. The full contract must establish any conditional verification and call/session identifier. A customer number alone cannot separate simultaneous conversations for that customer.

Implementation remains blocked by the missing integration contract and customer fixtures. The available files are in `submission/case`; their acquisition receipt explicitly marks the pack incomplete. No functioning mock or deployment is claimed.
