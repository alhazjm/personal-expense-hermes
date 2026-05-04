/**
 * Gmail Apps Script for DBS/UOB transaction email parsing.
 * Extracts transaction details and sends webhooks to Hermes agent.
 *
 * DBS sends two types:
 *   - PayLah! (debit): "Amount: SGD25.00" / "To: MERCHANT"
 *   - Card (credit):   "Amount: SGD3.08" / "From: DBS/POSB card ending XXXX" / "To: MERCHANT"
 *     Foreign-currency: "Amount: IDR2334082.00" / "To: MERCHANT" (overseas spend)
 * UOB sends:
 *   - Credit card: "A transaction of SGD 55.00 was made with your UOB Card ending XXXX on DD/MM/YY at MERCHANT"
 *     Foreign-currency: "A transaction of EUR 18.50 was made with your UOB Card..."
 *
 * Non-SGD transactions are converted to SGD at ingest via frankfurter.app
 * (ECB rates, no API key). The original currency + amount is stamped in the
 * `notes` field so the conversion is traceable. If the FX lookup fails, a
 * Telegram alert is sent and no Transactions row is written — the user logs
 * it manually after checking the bank app.
 */

const WEBHOOK_URL = "https://<YOUR-RENDER-SERVICE>.onrender.com/webhooks/expense-ingest";
const WEBHOOK_SECRET = PropertiesService.getScriptProperties().getProperty("WEBHOOK_HMAC_SECRET");
const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
const TELEGRAM_BOT_TOKEN = PropertiesService.getScriptProperties().getProperty("TELEGRAM_BOT_TOKEN");
const TELEGRAM_CHAT_ID = PropertiesService.getScriptProperties().getProperty("TELEGRAM_CHAT_ID");

const WEBHOOK_LOG_SHEET = "WebhookLog";
const WEBHOOK_LOG_HEADER = [
  "timestamp", "bank", "type", "amount", "currency",
  "merchant", "date", "payment_method", "idempotency_key",
  "webhook_status", "matched",
];

const GMAIL_QUERY = 'from:(alerts@dbs.com OR ibanking.alert@dbs.com OR paylah.alert@dbs.com OR unialerts@uobgroup.com) subject:(transaction OR alert OR PayLah) is:unread newer_than:7d';


function setupTrigger() {
  ScriptApp.newTrigger("checkNewEmails")
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log("Trigger created: checks every 5 minutes");
}


function checkNewEmails() {
  var threads = GmailApp.search(GMAIL_QUERY, 0, 10);

  for (var i = 0; i < threads.length; i++) {
    var messages = threads[i].getMessages();

    for (var j = 0; j < messages.length; j++) {
      var msg = messages[j];
      if (!msg.isUnread()) continue;

      var from = msg.getFrom().toLowerCase();
      var body = msg.getPlainBody();
      var parsed = null;

      if (from.indexOf("dbs.com") !== -1) {
        parsed = parseDBS(body);
      } else if (from.indexOf("uobgroup.com") !== -1) {
        parsed = parseUOB(body);
      }

      if (parsed) {
        parsed.source = "email";
        parsed.raw_from = msg.getFrom();
        parsed.email_date = msg.getDate().toISOString();

        // FX normalisation. Non-SGD txns get converted to SGD before
        // anything else writes — the audit row, idempotency key, and
        // Transactions row all live in SGD so the budget math works. The
        // original foreign currency + amount is stamped into `notes` for
        // traceability. If the FX lookup fails, we skip the webhook and
        // fire a Telegram nudge so the user can log it manually.
        var fxFailed = false;
        if (parsed.currency !== "SGD") {
          var fx = convertToSGD(parsed.amount, parsed.currency);
          if (fx) {
            var origNote = "orig: " + parsed.currency + " " + parsed.amount.toFixed(2)
                         + " @ " + fx.rate.toFixed(6)
                         + (fx.fxDate ? " (" + fx.source + " " + fx.fxDate + ")"
                                      : " (" + fx.source + ")");
            parsed.notes = parsed.notes ? parsed.notes + "; " + origNote : origNote;
            parsed.amount = Math.round(fx.amount * 100) / 100;
            parsed.currency = "SGD";
          } else {
            fxFailed = true;
          }
        }

        // Audit log FIRST so the transaction is durable even if the webhook /
        // LLM call fails. The sweep tool later diffs WebhookLog vs
        // Transactions to surface anything that never made it into the
        // ledger. idempotency_key is computed here and must produce the
        // same 16-char hex as Python's _compute_idempotency_key — see
        // testIdempotencyKeyParity() for the lock-in values.
        var idemKey = computeIdempotencyKey(
          parsed.date, parsed.merchant, parsed.amount, parsed.payment_method
        );
        logToAuditSheet(parsed, idemKey);

        if (fxFailed) {
          updateWebhookStatus(idemKey, "fx_failed");
          sendTelegramAlert(
            "⚠️ Couldn't auto-log: " + parsed.bank + " "
            + parsed.currency + " " + parsed.amount.toFixed(2) + " at "
            + parsed.merchant + " (FX lookup failed). Reply with the SGD "
            + "amount when you know it and I'll log it manually."
          );
        } else {
          var status = sendWebhook(parsed);
          updateWebhookStatus(idemKey, status);
        }
      }

      msg.markRead();
    }
  }
}


/**
 * Parse DBS transaction alert emails.
 *
 * PayLah! format:
 *   Date & Time: 10 Apr 13:10 (SGT)
 *   Amount:      SGD25.00
 *   From:        PayLah! Wallet (Mobile ending XXXX)
 *   To:          EXAMPLE MERCHANT NAME
 *
 * Card format:
 *   Date & Time: 14 APR 06:44 (SGT)
 *   Amount: SGD3.08
 *   From: DBS/POSB card ending XXXX
 *   To: BUS/MRT
 */
function parseDBS(body) {
  // Currency code is now captured dynamically: any 3-letter code matches
  // (SGD, IDR, USD, EUR, ...). Foreign-currency txns get converted to SGD
  // downstream via convertToSGD(); see checkNewEmails().
  var amountMatch = body.match(/Amount:\s*([A-Z]{3})\s?([\d,]+\.?\d*)/i);
  var merchantMatch = body.match(/To:\s*(.+)/im);
  var dateMatch = body.match(/Date\s*&\s*Time:\s*(\d{1,2}\s+\w+)\s+/i);

  if (!amountMatch || !merchantMatch) return null;

  var fromMatch = body.match(/From:\s*(.+)/im);
  var fromText = fromMatch ? fromMatch[1].trim() : "";
  var isPayLah = fromText.toLowerCase().indexOf("paylah") !== -1;

  var cardMatch = fromText.match(/ending\s+(\d{4})/i);

  var dateStr = "";
  if (dateMatch) {
    var currentYear = String(new Date().getFullYear());
    dateStr = formatDBSDate(dateMatch[1].trim(), currentYear);
  }

  return {
    bank: "DBS",
    type: isPayLah ? "paylah" : "card",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1].toUpperCase(),
    merchant: merchantMatch[1].trim(),
    date: dateStr || new Date().toISOString().slice(0, 10),
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: fromText,
  };
}


/**
 * Parse UOB transaction alert email.
 * Format: "A transaction of SGD 55.00 was made with your UOB Card ending XXXX on 12/04/26 at UrbanCompany."
 * Sender: unialerts@uobgroup.com
 */
function parseUOB(body) {
  // Currency code captured dynamically — same approach as parseDBS.
  var amountMatch = body.match(/transaction\s+of\s+([A-Z]{3})\s+([\d,]+\.?\d*)\s+was\s+made/i);
  var cardMatch = body.match(/Card\s+ending\s+(\d{4})/i);
  var dateMatch = body.match(/on\s+(\d{2}\/\d{2}\/\d{2,4})/i);
  var merchantMatch = body.match(/at\s+(.+?)(?:\.\s*If|\s*$)/im);

  if (!amountMatch) return null;

  var merchant = merchantMatch ? merchantMatch[1].trim() : "Unknown";
  // Clean trailing punctuation
  merchant = merchant.replace(/[.\s]+$/, "");

  return {
    bank: "UOB",
    type: "card",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1].toUpperCase(),
    merchant: merchant,
    date: dateMatch ? formatDateSlash(dateMatch[1]) : new Date().toISOString().slice(0, 10),
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: "UOB Card" + (cardMatch ? " ending " + cardMatch[1] : ""),
  };
}


/** Convert "23 APR" + "2026" → "2026-04-23" via string arithmetic.
 *
 * Why not `new Date("23 APR 2026").toISOString().slice(0, 10)`? JavaScript's
 * Date parser interprets a bare-date string in the runtime's LOCAL timezone.
 * Apps Script, for a Singapore account, defaults to Asia/Singapore (GMT+8).
 * So `new Date("23 APR 2026")` = 2026-04-23T00:00:00+08:00 = 2026-04-22T16:00:00Z.
 * Then `toISOString().slice(0, 10)` = "2026-04-22" — off by one day.
 *
 * Observed impact (pre-fix): DBS-email-derived txn_ids consistently landed
 * on the prior calendar day. UOB emails were unaffected because
 * formatDateSlash already did pure string arithmetic on DD/MM/YY. This
 * function brings DBS parsing to the same TZ-safe approach.
 */
function formatDBSDate(dayMonth, year) {
  var parts = String(dayMonth).trim().split(/\s+/);
  if (parts.length !== 2) return new Date().toISOString().slice(0, 10);
  var day = parts[0].length === 1 ? "0" + parts[0] : parts[0];
  var monthMap = {
    JAN: "01", FEB: "02", MAR: "03", APR: "04", MAY: "05", JUN: "06",
    JUL: "07", AUG: "08", SEP: "09", OCT: "10", NOV: "11", DEC: "12",
  };
  var month = monthMap[parts[1].toUpperCase()];
  if (!month || !/^\d{4}$/.test(String(year))) {
    return new Date().toISOString().slice(0, 10);
  }
  return year + "-" + month + "-" + day;
}


/** Convert "12/04/26" or "14/04/2026" → "2026-04-14" */
function formatDateSlash(dateStr) {
  var parts = dateStr.split("/");
  if (parts.length !== 3) return new Date().toISOString().slice(0, 10);

  var year = parts[2];
  if (year.length === 2) {
    year = "20" + year;
  }

  return year + "-" + parts[1] + "-" + parts[0];
}


/**
 * Convert a foreign-currency amount to SGD via frankfurter.app.
 *
 * Frankfurter publishes daily ECB reference rates with no API key. The
 * `?amount=N&from=XXX&to=SGD` endpoint does the multiply server-side, so
 * `data.rates.SGD` is the SGD-equivalent of the input amount.
 *
 * Returns: {amount, rate, source, fxDate} on success, or null on any
 * failure (network, non-200, currency unsupported, JSON shape unexpected).
 * Callers MUST handle null — see the FX-failed fallback in checkNewEmails.
 *
 * SGD passes through with rate=1 and source="passthrough", so callers can
 * treat this as an unconditional normaliser.
 */
function convertToSGD(amount, currency) {
  if (currency === "SGD") {
    return { amount: amount, rate: 1.0, source: "passthrough", fxDate: "" };
  }
  var url = "https://api.frankfurter.app/latest"
          + "?amount=" + encodeURIComponent(amount)
          + "&from=" + encodeURIComponent(currency)
          + "&to=SGD";
  try {
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      Logger.log("convertToSGD: HTTP " + response.getResponseCode() + " for " + currency);
      return null;
    }
    var data = JSON.parse(response.getContentText());
    if (!data || !data.rates || typeof data.rates.SGD !== "number") {
      Logger.log("convertToSGD: unexpected response shape for " + currency);
      return null;
    }
    var sgdAmount = data.rates.SGD;
    return {
      amount: sgdAmount,
      // Per-unit rate, useful for the notes stamp.
      rate: amount > 0 ? sgdAmount / amount : 0,
      source: "frankfurter",
      fxDate: data.date || "",
    };
  } catch (e) {
    Logger.log("convertToSGD error: " + e.toString());
    return null;
  }
}


/**
 * Send a one-line Telegram message via the Bot API. Used for the FX-failed
 * fallback nudge — when the agent can't auto-log a foreign-currency txn,
 * we tell the user via Telegram so they can log manually.
 *
 * Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID Script Properties.
 * Returns true on 2xx, false on missing config / non-2xx / exception.
 */
function sendTelegramAlert(text) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    Logger.log("sendTelegramAlert: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping");
    return false;
  }
  var url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage";
  try {
    var response = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text: text }),
      muteHttpExceptions: true,
    });
    return response.getResponseCode() >= 200 && response.getResponseCode() < 300;
  } catch (e) {
    Logger.log("sendTelegramAlert error: " + e.toString());
    return false;
  }
}


/**
 * Send parsed transaction to Hermes webhook with HMAC signature.
 * Returns a status string suitable for the WebhookLog `webhook_status` column:
 *   "sent"            — HTTP 2xx
 *   "failed:<code>"   — HTTP non-2xx
 *   "error:<message>" — network/runtime exception
 */
function sendWebhook(payload) {
  var jsonPayload = JSON.stringify(payload);

  var signature = "";
  if (WEBHOOK_SECRET) {
    var hmac = Utilities.computeHmacSha256Signature(jsonPayload, WEBHOOK_SECRET);
    signature = hmac.map(function(byte) {
      return ("0" + (byte & 0xFF).toString(16)).slice(-2);
    }).join("");
  }

  var options = {
    method: "post",
    contentType: "application/json",
    payload: jsonPayload,
    headers: {
      "X-Webhook-Signature": signature,
    },
    muteHttpExceptions: true,
  };

  try {
    var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
    var code = response.getResponseCode();
    Logger.log("Webhook response: " + code);
    if (code >= 200 && code < 300) {
      return "sent";
    }
    return "failed:" + code;
  } catch (e) {
    Logger.log("Webhook error: " + e.toString());
    return "error:" + e.toString();
  }
}


/**
 * Compute a 16-char hex idempotency key. Must match Python's
 * `tools.sheets_client._compute_idempotency_key` byte-for-byte so the sweep
 * tool can diff WebhookLog against Transactions.
 *
 * Normalisation rules:
 *   - merchant: trim + uppercase
 *   - amount: fixed to 2 decimal places
 *   - payment_method: trim
 *
 * The JS `Utilities.computeDigest` returns signed bytes (-128..127); the
 * `& 0xFF` masks them back to unsigned before hex encoding so the output
 * matches Python's `hashlib.sha256(...).hexdigest()[:16]`.
 */
function computeIdempotencyKey(date, merchant, amount, paymentMethod) {
  var raw = date + "|"
          + String(merchant || "").trim().toUpperCase() + "|"
          + parseFloat(amount).toFixed(2) + "|"
          + String(paymentMethod || "").trim();
  var hash = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256, raw
  );
  return hash.slice(0, 8).map(function(b) {
    return ("0" + (b & 0xFF).toString(16)).slice(-2);
  }).join("");
}


/**
 * One-off verification function — run manually from the Apps Script editor
 * to confirm the JS idempotency key matches the Python implementation.
 * Expected values are pinned in the Python parity test in
 * tests/test_expense_sheets_tool.py (class TestIdempotencyKeyParity).
 *
 * Drift here = silent duplicates in the sweep tool, so verify before shipping.
 */
function testIdempotencyKeyParity() {
  var cases = [
    ["2026-04-16", "Starbucks",    5.50,  "DBS card ending XXXX",       "4d76c6fcac79c419"],
    ["2026-04-14", "Cold Storage", 45.30, "DBS/POSB card ending XXXX",  "355148d1cc86f01c"],
    ["2026-04-12", "GRABFOOD",     12.50, "UOB Card ending XXXX",       "de521352538b7ae1"],
    ["2026-04-10", "Sheng Siong",  23.80, "PayLah! Wallet",             "25f16c670462d6c8"],
  ];
  var allPassed = true;
  for (var i = 0; i < cases.length; i++) {
    var c = cases[i];
    var got = computeIdempotencyKey(c[0], c[1], c[2], c[3]);
    var expected = c[4];
    var ok = (got === expected);
    Logger.log(
      (ok ? "OK   " : "FAIL ") +
      c[1] + " $" + c[2] + " -> got=" + got + " expected=" + expected
    );
    if (!ok) allPassed = false;
  }
  Logger.log(allPassed ? "All parity cases passed." : "PARITY MISMATCH — do not deploy.");
  return allPassed;
}


/**
 * Append a row to the WebhookLog tab with the parsed transaction plus a
 * `webhook_status` of "pending" — overwritten to "sent"/"failed"/"error"
 * by updateWebhookStatus() after the webhook call returns.
 *
 * Creates the tab with a header row on first use. Tolerates missing
 * SPREADSHEET_ID: logs a warning and returns so email processing still
 * proceeds (we'd rather send the webhook than block on an audit failure).
 */
function logToAuditSheet(payload, idempotencyKey) {
  if (!SPREADSHEET_ID) {
    Logger.log("logToAuditSheet: SPREADSHEET_ID script property not set; skipping audit log");
    return;
  }
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var ws = ss.getSheetByName(WEBHOOK_LOG_SHEET);
    if (!ws) {
      ws = ss.insertSheet(WEBHOOK_LOG_SHEET);
      ws.appendRow(WEBHOOK_LOG_HEADER);
    }
    ws.appendRow([
      new Date().toISOString(),
      payload.bank || "",
      payload.type || "",
      payload.amount,
      payload.currency || "",
      payload.merchant || "",
      payload.date || "",
      payload.payment_method || "",
      idempotencyKey,
      "pending",
      "",
    ]);
  } catch (e) {
    Logger.log("logToAuditSheet error: " + e.toString());
  }
}


/**
 * Patch the `webhook_status` cell of the most recent WebhookLog row whose
 * idempotency_key matches. Silent no-op if the row can't be found — the
 * sweep tool doesn't depend on the status field, it diffs by key.
 */
function updateWebhookStatus(idempotencyKey, status) {
  if (!SPREADSHEET_ID || !idempotencyKey) return;
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var ws = ss.getSheetByName(WEBHOOK_LOG_SHEET);
    if (!ws) return;
    var idemColIdx = WEBHOOK_LOG_HEADER.indexOf("idempotency_key") + 1;
    var statusColIdx = WEBHOOK_LOG_HEADER.indexOf("webhook_status") + 1;
    var lastRow = ws.getLastRow();
    if (lastRow < 2) return;
    var keys = ws.getRange(2, idemColIdx, lastRow - 1, 1).getValues();
    for (var i = keys.length - 1; i >= 0; i--) {
      if (keys[i][0] === idempotencyKey) {
        ws.getRange(i + 2, statusColIdx).setValue(status);
        return;
      }
    }
  } catch (e) {
    Logger.log("updateWebhookStatus error: " + e.toString());
  }
}
