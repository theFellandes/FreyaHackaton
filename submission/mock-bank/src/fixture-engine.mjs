/** Exact fixture lookup only: no HTTP routing, bank error invention, or state mutation. */
const SERVICE_DEPTH = Object.freeze({
  "cards.list": 0,
  "cards.balance": 1,
  "statement.week.get": 1,
  "statement.week.decide": 2,
  "statement.week.change": 2,
  "contact.email.get": 0,
  "statement.pdf.send": 0,
});

const own = (value, key) => value !== null && typeof value === "object" && Object.hasOwn(value, key);
const missing = (reason) => ({ found: false, reason });

function cardAliases(customer) {
  // A returned mask is an alias only when fixture structure explicitly ties it
  // to a full cards.list key. Last-four matching cannot distinguish all cards.
  const listed = new Set((customer.services?.["cards.list"]?.response?.data?.cards ?? [])
    .map((card) => card.cardNumber));
  const aliases = new Map();
  function add(alias, canonical) {
    if (typeof alias !== "string" || !listed.has(canonical)) return;
    if (!aliases.has(alias)) aliases.set(alias, new Set());
    aliases.get(alias).add(canonical);
  }
  for (const canonical of listed) add(canonical, canonical);
  for (const [service, depth] of Object.entries(SERVICE_DEPTH)) {
    if (depth !== 1) continue;
    for (const [canonical, leaf] of Object.entries(customer.services?.[service] ?? {})) {
      add(leaf.response?.data?.cardNumber, canonical);
    }
  }
  return aliases;
}

export function createFixtureEngine(fixtureData) {
  if (!Array.isArray(fixtureData?.customers)) throw new TypeError("Fixture customers must be an array.");
  const customers = new Map();
  const aliases = new Map();
  for (const customer of fixtureData.customers) {
    if (typeof customer.customerNo !== "string" || customers.has(customer.customerNo)) {
      throw new TypeError("Fixture customerNo must be a unique string.");
    }
    customers.set(customer.customerNo, customer);
    aliases.set(customer.customerNo, cardAliases(customer));
  }

  function lookup({ customerNo, service, cardKey, weekKey } = {}) {
    if (!customers.has(customerNo)) return missing("customer_not_found");
    const customer = customers.get(customerNo);
    if (!own(SERVICE_DEPTH, service) || !own(customer.services, service)) return missing("service_not_found");
    const depth = SERVICE_DEPTH[service];
    let leaf = customer.services[service];
    if (depth > 0) {
      if (typeof cardKey !== "string" || cardKey.length === 0) return missing("card_key_required");
      let canonical = cardKey;
      if (!own(leaf, canonical)) {
        const candidates = aliases.get(customerNo).get(cardKey);
        if (!candidates) return missing("card_not_found");
        if (candidates.size !== 1) return missing("ambiguous_card_key");
        canonical = candidates.values().next().value;
        if (!own(leaf, canonical)) return missing("card_not_found");
      }
      leaf = leaf[canonical];
    }
    if (depth > 1) {
      // JSON object keys are strings, while the contract may carry integer weeks.
      // Do not trim, round, or select another week when an exact key is absent.
      const key = typeof weekKey === "number" && Number.isInteger(weekKey)
        ? String(weekKey) : weekKey;
      if (typeof key !== "string" || key.length === 0) return missing("week_key_required");
      if (!own(leaf, key)) return missing("week_not_found");
      leaf = leaf[key];
    }
    if (!own(leaf, "http") || !own(leaf, "response")) return missing("invalid_fixture_leaf");
    return { found: true, ...structuredClone(leaf) };
  }

  return Object.freeze({ lookup });
}

export function lookupFixture(fixtureData, args) {
  return createFixtureEngine(fixtureData).lookup(args);
}
