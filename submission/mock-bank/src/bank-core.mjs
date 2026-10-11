import { createFixtureEngine } from "./fixture-engine.mjs";
import { readinessMetadata } from "./readiness-metadata.mjs";

export const BANK_SERVICES = Object.freeze([
  "cards.list", "cards.balance", "statement.week.get", "statement.week.decide",
  "statement.week.change", "contact.email.get", "statement.pdf.send",
]);
const CARD_SERVICES = ["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change"];
const FIELDS = Object.freeze({
  "cards.list": ["customerNo"],
  "cards.balance": ["cardNumber"],
  "statement.week.get": ["cardNumber"],
  "statement.week.decide": ["cardNumber", "newStatementWeek", "statementDay"],
  "statement.week.change": ["cardNumber", "newStatementWeek", "statementDay"],
  "contact.email.get": ["customerNo", "intAddressInfoType"],
  "statement.pdf.send": ["customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"],
});
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Cache-Control": "no-store",
};
const json = (body, status = 200) => Response.json(body, { status, headers: CORS });
const empty = (value) => value === undefined || value === null || typeof value === "string" && value.trim() === "";
const own = (value, key) => value !== null && typeof value === "object" && Object.hasOwn(value, key);

export function bankError(http, code, message, type = "ERROR", traceId = crypto.randomUUID().replaceAll("-", "")) {
  return { http, response: { result: {
    guid: traceId, returnCode: code, messageText: message, code, message, type,
    details: { guid: traceId, rootCause: http === 404 ? "NotFoundException" : http === 500 ? "TechnicalError" : "InvalidArgumentsException",
      errorOrigin: "ivr-gateway-test:ivr-channel-v0" },
  } } };
}
function invalid(message) { return bankError(400, "INVALID_ARGUMENTS_ERROR", message); }
function notFound(service) {
  const errors = {
    "cards.list": ["FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı."],
    "cards.balance": ["FB_IVR_CHANNEL_120", "Kart bakiye servisi hatası: kart bulunamadı"],
    "statement.week.get": ["FB_IVR_CHANNEL_121", "Kart bulunamadı"],
    "statement.pdf.send": ["FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı"],
  };
  return errors[service] ? bankError(404, ...errors[service], "WARNING") : invalid("customer or card not found");
}
export function sessionObjectName(customerNo, sessionId) { return JSON.stringify([customerNo, sessionId]); }
function sessionIdFor(request, body) {
  const candidates = [request.headers.get("x-session-id"), body?.call?.sessionId, body?.call?.callId, body?.call?.conversationId];
  return candidates.find((value) => typeof value === "string" && value.length > 0 && value.length <= 128) ?? "customer-default";
}

export function createBankService(fixtureData, {
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  now = () => new Date().toISOString(), operationId = () => crypto.randomUUID(),
} = {}) {
  const engine = createFixtureEngine(fixtureData);
  const customers = new Map(fixtureData.customers.map((customer) => [customer.customerNo, customer]));
  const owners = new Map();
  const aliases = new Map();
  for (const customer of fixtureData.customers) {
    const listed = new Set((customer.services["cards.list"]?.response?.data?.cards ?? []).map((card) => card.cardNumber));
    for (const service of CARD_SERVICES) for (const [cardNumber, entry] of Object.entries(customer.services[service] ?? {})) {
      if (owners.has(cardNumber) && owners.get(cardNumber).customerNo !== customer.customerNo) {
        throw new TypeError("A canonical fixture card cannot belong to multiple customers.");
      }
      const owner = { customerNo: customer.customerNo, cardKey: cardNumber };
      owners.set(cardNumber, owner);
      const mask = entry.response?.data?.cardNumber;
      if (listed.has(cardNumber) && typeof mask === "string" && mask !== cardNumber) {
        if (!aliases.has(mask)) aliases.set(mask, new Map());
        aliases.get(mask).set(cardNumber, owner);
      }
    }
  }
  function cardOwner(cardNumber) {
    if (owners.has(cardNumber)) return owners.get(cardNumber);
    const candidates = aliases.get(cardNumber);
    return candidates?.size === 1 ? candidates.values().next().value : null;
  }
  function evaluate(service, body) {
    if (!BANK_SERVICES.includes(service)) return invalid("service not found");
    if (!body?.data || typeof body.data !== "object" || Array.isArray(body.data)) return invalid("data cannot be empty");
    const data = body.data;
    let customerNo = typeof data.customerNo === "number" && Number.isInteger(data.customerNo) ? String(data.customerNo) : data.customerNo;
    const result = (answer, cardKey = undefined, acknowledgments = []) => ({
      ...answer, customerNo: customers.has(customerNo) ? customerNo : null, cardKey, acknowledgments,
      requestData: Object.fromEntries([...new Set([...FIELDS[service], "customerNo", "aiFlow"])]
        .filter((field) => own(data, field)).map((field) => [field, structuredClone(data[field])])),
    });
    for (const field of FIELDS[service]) if (empty(data[field])) {
      return result(invalid(`${field === "cardNumber" ? "card number" : field} cannot be empty`));
    }
    if (own(data, "customerNo") && typeof customerNo !== "string") return result(invalid("customerNo must be a string or integer"));
    let owner;
    if (FIELDS[service].includes("cardNumber")) {
      if (typeof data.cardNumber !== "string") return result(invalid("card number must be a string"));
      owner = cardOwner(data.cardNumber);
      if (!owner || customerNo !== undefined && customerNo !== owner.customerNo) return result(notFound(service));
      customerNo = owner.customerNo;
    }
    if (!customers.has(customerNo)) return result(notFound(service));
    if (service === "statement.week.decide" || service === "statement.week.change") {
      if (![1, 2, 3, 4, "1", "2", "3", "4"].includes(data.newStatementWeek)) return result(invalid("newStatementWeek must be between 1 and 4"), owner.cardKey);
      const day = typeof data.statementDay === "string" && /^\d+$/.test(data.statementDay) ? Number(data.statementDay) : data.statementDay;
      if (!Number.isInteger(day) || day < 1 || day > 31) return result(invalid("statementDay must be a day of the month"), owner.cardKey);
    }
    if (service === "statement.pdf.send") {
      if (!["TL", "USD", "EUR", "GBP"].includes(data.currencyType)) return result(invalid("currencyType must be TL, USD, EUR or GBP"), owner.cardKey);
      if (typeof data.statementDate !== "string" || !/^\d{2}\.\d{2}\.\d{4}$/.test(data.statementDate)) return result(invalid("statementDate must use dd.MM.yyyy"), owner.cardKey);
      if (typeof data.emailAddress !== "string") return result(invalid("emailAddress must be a string"), owner.cardKey);
    }
    const found = engine.lookup({ customerNo, service, cardKey: owner?.cardKey, weekKey: data.newStatementWeek });
    if (!found.found) return result(found.reason === "week_not_found" ? invalid("requested statement week is not available") : notFound(service), owner?.cardKey);
    const { found: ignored, ...answer } = found;
    const acknowledgments = [];
    if (answer.http === 200 && answer.response.result?.returnCode === "1") {
      if (service === "statement.week.change") {
        const related = customers.get(customerNo).services["statement.week.get"]?.[owner.cardKey]?.response?.data?.relatedCardNumber;
        const cards = [owner.cardKey];
        if (related && owners.get(related)?.customerNo === customerNo) cards.push(related);
        for (const cardNumber of cards) acknowledgments.push({ kind: "statement_week_change", cardNumber,
          data: { requestedWeek: data.newStatementWeek, ...structuredClone(answer.response.data) } });
      }
      if (service === "statement.pdf.send") acknowledgments.push({ kind: "statement_pdf_send", cardNumber: owner.cardKey,
        data: { currencyType: data.currencyType, statementDate: data.statementDate, emailAddress: data.emailAddress, ...structuredClone(answer.response.data) } });
    }
    return result(answer, owner?.cardKey, acknowledgments);
  }
  async function fetch(request, env = {}) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method === "GET" && ["/__submission_health", "/health"].includes(url.pathname)) {
      let storageReady = false;
      try { storageReady = Boolean((await env.BANK_SESSIONS.getByName("__submission_health").probe()).ready); } catch { /* Metadata alone cannot report ready. */ }
      return json({ ready: storageReady, bank_ready: storageReady, storage_backend: "sqlite-durable-object",
        customer_count: customers.size, canonical_card_count: owners.size, ...readinessMetadata(env) }, storageReady ? 200 : 503);
    }
    if (request.method === "GET" && url.pathname === "/__submission_state") {
      const customerNo = url.searchParams.get("customerNo"), sessionId = url.searchParams.get("sessionId") || "customer-default";
      if (!customers.has(customerNo) || sessionId.length > 128) return json(invalid("known customerNo and valid sessionId required").response, 400);
      try { return json(await env.BANK_SESSIONS.getByName(sessionObjectName(customerNo, sessionId)).snapshot(customerNo, sessionId)); }
      catch { return json(bankError(500, "FBE00099", "Teknik hata: ABORTED: ").response, 500); }
    }
    const path = url.pathname.startsWith("/voice/v1/") ? url.pathname.slice("/voice/v1/".length) : url.pathname.slice(1);
    if (!BANK_SERVICES.includes(path)) return json(invalid("service not found").response, 400);
    if (request.method !== "POST") return json(invalid("service requires POST").response, 405);
    let body;
    try { body = await request.json(); } catch { return json(invalid("request body must be JSON").response, 400); }
    const answer = evaluate(path, body);
    if (answer.customerNo) {
      const sessionId = sessionIdFor(request, body);
      try {
        await env.BANK_SESSIONS.getByName(sessionObjectName(answer.customerNo, sessionId)).recordOperation({
          recordId: operationId(), customerNo: answer.customerNo, sessionId, service: path,
          requestData: answer.requestData, http: answer.http, response: answer.response,
          acknowledgments: answer.acknowledgments, createdAtUtc: now(),
        });
      } catch { return json(bankError(500, "FBE00099", "Teknik hata: ABORTED: ").response, 500); }
    }
    if (answer.delaySeconds) await sleep(answer.delaySeconds * 1000);
    return json(answer.response, answer.http);
  }
  return Object.freeze({ fetch, evaluate, customerCount: customers.size, canonicalCardCount: owners.size });
}
