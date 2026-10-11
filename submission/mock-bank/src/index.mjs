import fixtures from "../../case/test-environment/test-customers.json" with { type: "json" };
import { createBankService } from "./bank-core.mjs";

export { BankSession } from "./bank-session.mjs";
export default createBankService(fixtures);
