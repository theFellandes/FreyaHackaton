import { DurableObject } from "cloudflare:workers";
import { createBankStore } from "./bank-store.mjs";

export class BankSession extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.bankStore = createBankStore(ctx.storage);
    ctx.blockConcurrencyWhile(async () => { this.bankStore.initialize(); });
  }
  async recordOperation(record) { return this.bankStore.recordOperation(record); }
  async snapshot(customerNo, sessionId) { return this.bankStore.snapshot(customerNo, sessionId); }
  async probe() { return this.bankStore.probe(); }
}
