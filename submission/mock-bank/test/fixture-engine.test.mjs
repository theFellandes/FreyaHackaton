import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createFixtureEngine, lookupFixture } from "../src/fixture-engine.mjs";

const fixtures = JSON.parse(readFileSync(new URL("../../case/test-environment/test-customers.json", import.meta.url), "utf8"));
const engine = createFixtureEngine(fixtures);

function leaves() {
  const rows = [];
  for (const customer of fixtures.customers) {
    for (const [service, root] of Object.entries(customer.services)) {
      function walk(value, path = []) {
        if (Object.hasOwn(value, "http")) {
          rows.push({ args: { customerNo: customer.customerNo, service, cardKey: path[0], weekKey: path[1] }, leaf: value });
          return;
        }
        for (const [key, child] of Object.entries(value)) walk(child, [...path, key]);
      }
      walk(root);
    }
  }
  return rows;
}

test("all 617 released fixture leaves preserve exact status, body, fields, and delay", () => {
  const original = JSON.stringify(fixtures);
  const rows = leaves();
  assert.equal(fixtures.customers.length, 34);
  assert.equal(rows.length, 617);
  for (const { args, leaf } of rows) {
    assert.deepEqual(engine.lookup(args), { found: true, ...leaf }, JSON.stringify(args));
  }
  assert.equal(rows.filter(({ leaf }) => leaf.http !== 200).length, 8);
  assert.equal(rows.filter(({ leaf }) => Object.hasOwn(leaf, "delaySeconds")).length, 1);
  assert.equal(JSON.stringify(fixtures), original);
});

test("every returned response is deeply independent from fixtures and other lookups", () => {
  const args = { customerNo: "90000001", service: "cards.list" };
  const first = engine.lookup(args);
  first.response.data.cards[0].cardNumber = "changed";
  first.response.result.details.guid = "changed";
  const second = engine.lookup(args);
  assert.equal(second.response.data.cards[0].cardNumber, "540000******1234");
  assert.notEqual(second.response.result.details.guid, "changed");
  assert.deepEqual(lookupFixture(fixtures, args), second);
});

test("explicit unique returned masks resolve to their original listed card keys", () => {
  let checked = 0;
  for (const customer of fixtures.customers) {
    const listed = customer.services["cards.list"].response?.data?.cards ?? [];
    const all = Object.entries(customer.services["cards.balance"] ?? {});
    for (const [cardKey, leaf] of all) {
      const alias = leaf.response?.data?.cardNumber;
      if (!alias || alias === cardKey || !listed.some((card) => card.cardNumber === cardKey)) continue;
      if (all.filter(([, entry]) => entry.response?.data?.cardNumber === alias).length !== 1) continue;
      for (const service of ["cards.balance", "statement.week.get"]) {
        assert.deepEqual(engine.lookup({ customerNo: customer.customerNo, service, cardKey: alias }),
          engine.lookup({ customerNo: customer.customerNo, service, cardKey }));
      }
      checked++;
    }
  }
  assert.ok(checked > 0);
});

test("duplicate last-four digits never choose a card", () => {
  for (const customerNo of ["92030487", "93931069", "94030575"]) {
    const customer = fixtures.customers.find((c) => c.customerNo === customerNo);
    const cards = customer.services["cards.list"].response.data.cards;
    const repeated = cards.find((a) => cards.some((b) => a.cardNumber !== b.cardNumber && a.cardNumberLast4 === b.cardNumberLast4));
    assert.deepEqual(engine.lookup({ customerNo, service: "cards.balance", cardKey: repeated.cardNumberLast4 }),
      { found: false, reason: "card_not_found" });
    for (const card of cards) assert.equal(engine.lookup({ customerNo, service: "cards.balance", cardKey: card.cardNumber }).found, true);
  }
});

test("a returned mask shared by two canonical cards is rejected", () => {
  const leaf = (cardNumber) => ({ http: 200, response: { data: { cardNumber } } });
  const ambiguous = createFixtureEngine({ customers: [{ customerNo: "00000001", services: {
    "cards.list": { http: 200, response: { data: { cards: [{ cardNumber: "first" }, { cardNumber: "second" }] } } },
    "cards.balance": { first: leaf("shared"), second: leaf("shared") },
  } }] });
  assert.deepEqual(ambiguous.lookup({ customerNo: "00000001", service: "cards.balance", cardKey: "shared" }),
    { found: false, reason: "ambiguous_card_key" });
  assert.equal(ambiguous.lookup({ customerNo: "00000001", service: "cards.balance", cardKey: "first" }).found, true);
});

test("all sparse current-week holes stay missing, with no replacement success", () => {
  let checked = 0;
  for (const customer of fixtures.customers) {
    for (const [cardKey, entry] of Object.entries(customer.services["statement.week.get"] ?? {})) {
      if (entry.http !== 200) continue;
      const current = entry.response.data.statementWeek;
      for (const service of ["statement.week.decide", "statement.week.change"]) {
        assert.deepEqual(engine.lookup({ customerNo: customer.customerNo, service, cardKey, weekKey: current }),
          { found: false, reason: "week_not_found" });
        for (const weekKey of Object.keys(customer.services[service][cardKey])) {
          assert.deepEqual(engine.lookup({ customerNo: customer.customerNo, service, cardKey, weekKey: Number(weekKey) }),
            engine.lookup({ customerNo: customer.customerNo, service, cardKey, weekKey }));
        }
        checked++;
      }
    }
  }
  assert.equal(checked, 128);
});

test("unknown and incomplete lookup keys return descriptors without bank results", () => {
  const base = { customerNo: "90000001", service: "cards.balance", cardKey: "540000******1234" };
  for (const [args, reason] of [
    [{ ...base, customerNo: "99999999" }, "customer_not_found"],
    [{ ...base, customerNo: 90000001 }, "customer_not_found"],
    [{ ...base, service: "invented.operation" }, "service_not_found"],
    [{ ...base, cardKey: undefined }, "card_key_required"],
    [{ ...base, cardKey: " 540000******1234 " }, "card_not_found"],
    [{ ...base, cardKey: "__proto__" }, "card_not_found"],
    [{ ...base, service: "statement.week.change" }, "week_key_required"],
    [{ ...base, service: "statement.week.change", weekKey: "2.0" }, "week_not_found"],
    [{ ...base, service: "statement.week.change", weekKey: " 2 " }, "week_not_found"],
    [{ ...base, service: "statement.week.change", weekKey: 5 }, "week_not_found"],
    [{ customerNo: "96125950", service: "statement.pdf.send" }, "service_not_found"],
  ]) assert.deepEqual(engine.lookup(args), { found: false, reason });
});

test("fixture failures, blank email, related cards, and latency remain deliberate", () => {
  const failedList = engine.lookup({ customerNo: "93821776", service: "cards.list" });
  assert.equal(failedList.http, 404);
  assert.equal(failedList.response.result.returnCode, "FB_IVR_CHANNEL_102");
  assert.equal(Object.hasOwn(failedList.response, "data"), false);
  const pdf = engine.lookup({ customerNo: "93635512", service: "statement.pdf.send" });
  assert.equal(pdf.http, 404);
  assert.equal(pdf.response.result.returnCode, "FB_IVR_CHANNEL_107");
  const delayed = engine.lookup({ customerNo: "94907600", service: "cards.balance", cardKey: "540000******7211" });
  assert.equal(delayed.delaySeconds, 12);
  const email = engine.lookup({ customerNo: "94907600", service: "contact.email.get" });
  assert.equal(email.response.data.ibanEmail, "");
  assert.equal(email.response.data.communicationType, "Phone");
  const relation = engine.lookup({ customerNo: "92220795", service: "statement.week.get", cardKey: "540000******3201" });
  assert.equal(relation.response.data.relationType, "M");
  assert.equal(relation.response.data.relatedCardNumber, "540000******3202");
});

test("malformed or duplicate customer indexes fail instead of shadowing fixtures", () => {
  assert.throws(() => createFixtureEngine({}), /customers must be an array/);
  assert.throws(() => createFixtureEngine({ customers: [{ customerNo: "same" }, { customerNo: "same" }] }), /unique string/);
});
