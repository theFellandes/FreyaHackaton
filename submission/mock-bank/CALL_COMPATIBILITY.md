# Released bank transport

The final contract declares seven POST services with nested JSON `data` and `call` objects. Canonical paths are `/voice/v1/<service>`; the Worker also serves `/<service>`. There are no path placeholders in this contract.

The generic service compiler projects nested request fields into typed scalar Freya arguments and reconstructs the documented JSON envelope in a pre-hook. Customer identifiers bind to the supplied `customerNumber`; dates use the supplied `now`. The selected output still needs live inspection.

The original 34-customer JSON is served with exact status codes and response bodies. Token, headers, call metadata and `aiFlow` do not affect fixture selection, as the test overview specifies.

SQLite Durable Objects record operations and successful write acknowledgments. Optional `call.sessionId`, `call.callId`, `call.conversationId` or `x-session-id` separate conversations. Without one, records share a stream per customer; the supplied platform variables alone cannot distinguish simultaneous calls for the same customer. Responses remain the original fixture answers.

Local tests compare every fixture through both prefixes. Actual Workers runtime restart persistence and live deployment evidence are required before accepting the final build. Use Cloudflare Free; no paid upgrade is authorized.
