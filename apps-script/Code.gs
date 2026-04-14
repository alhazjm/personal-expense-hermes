/**
 * Gmail Apps Script for DBS/UOB transaction email parsing.
 * Extracts transaction details and sends webhooks to Hermes agent.
 *
 * DBS sends two types:
 *   - PayLah! (debit): "Amount: SGD25.00" / "To: MERCHANT"
 *   - Card (credit):   "Amount: SGD3.08" / "From: DBS/POSB card ending XXXX" / "To: MERCHANT"
 * UOB sends:
 *   - Credit card: "A transaction of SGD 55.00 was made with your UOB Card ending XXXX on DD/MM/YY at MERCHANT"
 */

const WEBHOOK_URL = "http://localhost:8644/webhook/expense-ingest";
const WEBHOOK_SECRET = PropertiesService.getScriptProperties().getProperty("WEBHOOK_HMAC_SECRET");

const GMAIL_QUERY = 'from:(alerts@dbs.com OR unialerts@uobgroup.com) subject:(transaction OR alert OR PayLah) is:unread';


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
        sendWebhook(parsed);
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
 *   From:        PayLah! Wallet (Mobile ending 5167)
 *   To:          LEMBAGA PENTADBIR MASJID AR RAUDHAH
 *
 * Card format:
 *   Date & Time: 14 APR 06:44 (SGT)
 *   Amount: SGD3.08
 *   From: DBS/POSB card ending 5305
 *   To: BUS/MRT
 */
function parseDBS(body) {
  var amountMatch = body.match(/Amount:\s*SGD\s?([\d,]+\.?\d*)/i);
  var merchantMatch = body.match(/To:\s*(.+)/im);
  var dateMatch = body.match(/Date\s*&\s*Time:\s*(\d{1,2}\s+\w+)\s+/i);
  var yearMatch = body.match(/dated\s+(\d{1,2}\s+\w+)/i);

  if (!amountMatch || !merchantMatch) return null;

  var fromMatch = body.match(/From:\s*(.+)/im);
  var fromText = fromMatch ? fromMatch[1].trim() : "";
  var isPayLah = fromText.toLowerCase().indexOf("paylah") !== -1;

  var cardMatch = fromText.match(/ending\s+(\d{4})/i);

  var dateStr = "";
  if (dateMatch) {
    var currentYear = new Date().getFullYear();
    dateStr = formatDate(dateMatch[1].trim() + " " + currentYear);
  }

  return {
    bank: "DBS",
    type: isPayLah ? "paylah" : "card",
    amount: parseFloat(amountMatch[1].replace(/,/g, "")),
    currency: "SGD",
    merchant: merchantMatch[1].trim(),
    date: dateStr || new Date().toISOString().slice(0, 10),
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: fromText,
  };
}


/**
 * Parse UOB transaction alert email.
 * Format: "A transaction of SGD 55.00 was made with your UOB Card ending 0886 on 12/04/26 at UrbanCompany."
 * Sender: unialerts@uobgroup.com
 */
function parseUOB(body) {
  var amountMatch = body.match(/SGD\s+([\d,]+\.?\d*)\s+was\s+made/i);
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
    amount: parseFloat(amountMatch[1].replace(/,/g, "")),
    currency: "SGD",
    merchant: merchant,
    date: dateMatch ? formatDateSlash(dateMatch[1]) : new Date().toISOString().slice(0, 10),
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: "UOB Card" + (cardMatch ? " ending " + cardMatch[1] : ""),
  };
}


/** Convert "14 Apr 2026" → "2026-04-14" */
function formatDate(dateStr) {
  var d = new Date(dateStr);
  if (isNaN(d.getTime())) return new Date().toISOString().slice(0, 10);
  return d.toISOString().slice(0, 10);
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


/** Send parsed transaction to Hermes webhook with HMAC signature. */
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
    Logger.log("Webhook response: " + response.getResponseCode());
  } catch (e) {
    Logger.log("Webhook error: " + e.toString());
  }
}
