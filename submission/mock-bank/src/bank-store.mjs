/** SQLite audit state. Returned bank answers continue to come from immutable fixtures. */
export function createBankStore(storage) {
  const sql = storage.sql;
  function initialize() {
    sql.exec(`CREATE TABLE IF NOT EXISTS operations (
      record_id TEXT PRIMARY KEY, customer_no TEXT NOT NULL, session_id TEXT NOT NULL,
      service TEXT NOT NULL, request_json TEXT NOT NULL, response_http INTEGER NOT NULL,
      response_json TEXT NOT NULL, created_at_utc TEXT NOT NULL
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS sessions (
      customer_no TEXT NOT NULL, session_id TEXT NOT NULL, operation_count INTEGER NOT NULL,
      last_record_id TEXT NOT NULL, last_service TEXT NOT NULL, last_http INTEGER NOT NULL,
      last_seen_utc TEXT NOT NULL, PRIMARY KEY (customer_no, session_id)
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS acknowledgments (
      id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
      customer_no TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
      card_number TEXT NOT NULL, data_json TEXT NOT NULL
    )`);
  }
  function recordOperation(record) {
    return storage.transactionSync(() => {
      sql.exec(`INSERT INTO operations
        (record_id, customer_no, session_id, service, request_json, response_http, response_json, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, record.recordId, record.customerNo, record.sessionId,
      record.service, JSON.stringify(record.requestData), record.http, JSON.stringify(record.response), record.createdAtUtc);
      sql.exec(`INSERT INTO sessions
        (customer_no, session_id, operation_count, last_record_id, last_service, last_http, last_seen_utc)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT (customer_no, session_id) DO UPDATE SET
        operation_count = sessions.operation_count + 1, last_record_id = excluded.last_record_id,
        last_service = excluded.last_service, last_http = excluded.last_http, last_seen_utc = excluded.last_seen_utc`,
      record.customerNo, record.sessionId, record.recordId, record.service, record.http, record.createdAtUtc);
      for (const acknowledgment of record.acknowledgments ?? []) sql.exec(`INSERT INTO acknowledgments
        (record_id, customer_no, session_id, kind, card_number, data_json) VALUES (?, ?, ?, ?, ?, ?)`,
      record.recordId, record.customerNo, record.sessionId, acknowledgment.kind, acknowledgment.cardNumber, JSON.stringify(acknowledgment.data));
      return { recorded: true, recordId: record.recordId };
    });
  }
  function snapshot(customerNo, sessionId) {
    const session = sql.exec(`SELECT operation_count, last_record_id, last_service, last_http, last_seen_utc
      FROM sessions WHERE customer_no = ? AND session_id = ?`, customerNo, sessionId).toArray()[0];
    const operations = sql.exec(`SELECT record_id, service, response_http, created_at_utc FROM operations
      WHERE customer_no = ? AND session_id = ? ORDER BY rowid`, customerNo, sessionId).toArray();
    const acknowledgments = sql.exec(`SELECT record_id, kind, card_number FROM acknowledgments
      WHERE customer_no = ? AND session_id = ? ORDER BY id`, customerNo, sessionId).toArray();
    return { customerNo, sessionId, operationCount: session?.operation_count ?? 0,
      lastService: session?.last_service ?? null, lastHttp: session?.last_http ?? null,
      lastSeenUtc: session?.last_seen_utc ?? null, operations, acknowledgments };
  }
  function probe() {
    sql.exec("SELECT COUNT(*) AS count FROM operations").toArray();
    return { ready: true, backend: "sqlite-durable-object" };
  }
  return Object.freeze({ initialize, recordOperation, snapshot, probe });
}
