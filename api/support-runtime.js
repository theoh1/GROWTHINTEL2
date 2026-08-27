const crypto = require("crypto");

const SESSION_COOKIE = "gi_session";
const SUPPORT_STATUSES = new Set([
  "OPEN",
  "AI_HANDLING",
  "HUMAN_REQUESTED",
  "HUMAN_REVIEWING",
  "WAITING_FOR_CUSTOMER",
  "RESOLVED",
  "CLOSED",
]);
const attempts = new Map();

function now() {
  return Math.floor(Date.now() / 1000);
}

function json(res, status, payload) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json; charset=utf-8");
  res.setHeader("cache-control", "no-store, max-age=0, must-revalidate");
  res.end(JSON.stringify(payload));
}

function parseCookies(header = "") {
  return Object.fromEntries(
    String(header || "")
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const index = part.indexOf("=");
        if (index < 0) return [part, ""];
        return [part.slice(0, index), decodeURIComponent(part.slice(index + 1))];
      }),
  );
}

function hash(value) {
  return crypto.createHash("sha256").update(String(value || "")).digest("hex");
}

function supportConfig() {
  const url = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || "";
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SERVICE_KEY || "";
  return {
    url: url.replace(/\/$/, ""),
    key,
    adminEmails: new Set(String(process.env.MEMBERSHIP_ADMIN_EMAILS || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)),
  };
}

async function readBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  const chunks = [];
  for await (const chunk of req) chunks.push(Buffer.from(chunk));
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    const params = new URLSearchParams(raw);
    return Object.fromEntries(params.entries());
  }
}

async function supabase(table, options = {}) {
  const config = supportConfig();
  if (!config.url || !config.key) {
    const error = new Error("Support database is not configured");
    error.status = 503;
    throw error;
  }
  const headers = {
    apikey: config.key,
    authorization: `Bearer ${config.key}`,
    "content-type": "application/json",
    accept: "application/json",
    ...(options.headers || {}),
  };
  const response = await fetch(`${config.url}/rest/v1/${table}${options.query || ""}`, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const error = new Error(payload?.message || payload?.hint || `Support database request failed (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function redact(value, limit = 6000) {
  let text = String(value || "").slice(0, limit);
  text = text.replace(/(password|passcode|api[_ -]?key|secret|token)\s*[:=]\s*\S+/gi, "$1: [redacted]");
  text = text.replace(/\b(?:\d[ -]*?){13,19}\b/g, "[redacted card number]");
  return text.trim();
}

function rateLimit(req, bucket, max, windowMs) {
  const forwarded = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim();
  const key = `${bucket}:${forwarded || req.socket?.remoteAddress || "unknown"}`;
  const cutoff = Date.now() - windowMs;
  const recent = (attempts.get(key) || []).filter((stamp) => stamp > cutoff);
  recent.push(Date.now());
  attempts.set(key, recent);
  if (recent.length > max) {
    const error = new Error("Too many support requests. Please wait a few minutes and try again.");
    error.status = 429;
    throw error;
  }
}

function categoryFor(message) {
  const text = message.toLowerCase();
  const categories = [
    ["account", ["sign in", "login", "password", "account", "create account", "email"]],
    ["premium_access", ["premium", "membership", "subscription", "access", "approved", "expired"]],
    ["payment", ["payment", "bank", "transfer", "reference", "gi-", "paid", "refund"]],
    ["stale_data", ["stale", "old data", "missing data", "not loading", "refresh", "backend", "live data"]],
    ["canslim", ["canslim", "screener", "score"]],
    ["engine", ["engine", "ai engine", "setup"]],
    ["market", ["market", "index", "vix", "breadth"]],
    ["creator_intel", ["creator", "youtube", "video", "channel"]],
    ["alerts", ["alert", "notification"]],
    ["portfolio", ["portfolio", "holding"]],
  ];
  return categories.find(([, words]) => words.some((word) => text.includes(word)))?.[0] || "general";
}

function priorityFor(category, message) {
  const text = message.toLowerCase();
  if (text.includes("paid") || text.includes("cannot access premium") || text.includes("payment")) return "High";
  if (["premium_access", "account", "payment"].includes(category)) return "High";
  if (["stale_data", "alerts"].includes(category)) return "Medium";
  return "Low";
}

function aiAnswer(message) {
  const category = categoryFor(message);
  const steps = {
    account: [
      "Check that you are using the same email address you used to create the GrowthIntel account.",
      "Try signing in again after refreshing the page once.",
      "If password reset is needed, use the forgot-password option when it is available, or request human support so the account can be checked safely.",
    ],
    premium_access: [
      "Premium access is controlled by the server-side membership record, not by the browser.",
      "If your payment was approved recently, refresh GrowthIntel and sign in with the same account email.",
      "If the Premium area is still locked, request human support so the membership record and expiry date can be checked.",
    ],
    payment: [
      "Use your personal GI reference exactly as shown on the membership screen when paying.",
      "GrowthIntel support should never ask for your bank password, PIN, card number, CVV, OTP, or login details.",
      "If you have already paid and access is not active, request human support so the payment request can be matched.",
    ],
    stale_data: [
      "Use the page refresh/rescan control if the section has one.",
      "Check the data timestamp on the page. GrowthIntel should label delayed, stale, or unavailable data clearly.",
      "If a section is still not refreshing, request human support and include the section name and the timestamp you can see.",
    ],
    canslim: [
      "CANSLIM is available to Free users and Premium users.",
      "Check the scan timestamp and use refresh if the result looks stale.",
      "If scores look wrong, send the ticker and what appears incorrect.",
    ],
    engine: [
      "Use Apply & Scan after changing Engine preferences.",
      "Refresh Engine should rescan using the selected sector, setup type, risk and market regime controls.",
      "If results repeat or do not change, request human support with your selected controls.",
    ],
    creator_intel: [
      "Creator Intel depends on YouTube channel refreshes and API quota.",
      "If new videos are missing, include the channel and video link so support can check the scan pipeline.",
    ],
    alerts: [
      "Alerts are informational notices, not buy or sell instructions.",
      "Check whether the alert type is muted and whether the related feature data is current.",
    ],
    portfolio: [
      "Portfolio calculations depend on saved holdings and current price data.",
      "If a holding looks wrong, include the ticker, saved quantity, and the displayed price/time.",
    ],
  };
  const selected = steps[category] || [
    "Tell me which GrowthIntel page you were on and what you expected to happen.",
    "Refresh the page once and check whether the same problem remains.",
    "If the problem is account-specific, request human support so it can be checked safely.",
  ];
  return {
    category,
    answer: [
      "I can help with that.",
      "",
      ...selected.map((step, index) => `${index + 1}. ${step}`),
      "",
      "Did this solve the issue? If not, you can request human support and I will attach this conversation to a ticket.",
    ].join("\n"),
    solvedPrompt: true,
  };
}

function makeSummary({ problem, category, priority, transcript }) {
  const safeProblem = redact(problem, 1200);
  return [
    `Problem: ${safeProblem}`,
    "What user expected: GrowthIntel should complete the requested account, data, feature, or navigation action.",
    "What actually happened: The customer reported that GrowthIntel did not behave as expected.",
    `Steps AI attempted: Provided controlled troubleshooting for ${category.replace(/_/g, " ")} and asked whether the issue was solved.`,
    `Results: Customer requested human support.`,
    "Likely cause: Needs human review of account state, data freshness, or the specific page behaviour.",
    "Recommended human action: Review the ticket context, check the customer's account safely if logged in, then reply inside GrowthIntel Support.",
    `Priority: ${priority}`,
    `Transcript messages: ${Array.isArray(transcript) ? transcript.length : 0}`,
  ].join("\n");
}

async function currentUser(req) {
  const token = parseCookies(req.headers.cookie || "")[SESSION_COOKIE];
  if (!token) return null;
  const tokenHash = hash(token);
  const rows = await supabase("membership_sessions", {
    query: `?select=user_id&token_hash=eq.${encodeURIComponent(tokenHash)}&expires_at=gt.${now()}&limit=1`,
  });
  const session = Array.isArray(rows) ? rows[0] : null;
  if (!session) return null;
  const users = await supabase("membership_users", {
    query: `?select=id,email,name,membership_state,membership_expires_at,plan_version&id=eq.${encodeURIComponent(session.user_id)}&limit=1`,
  });
  return Array.isArray(users) ? users[0] : null;
}

function isAdmin(user) {
  return Boolean(user && supportConfig().adminEmails.has(String(user.email || "").toLowerCase()));
}

function cleanTicket(row, includeMessages, messages = []) {
  const convert = (value) => (value ? new Date(value * 1000).toISOString() : null);
  return {
    ...row,
    created_at: convert(row.created_at),
    updated_at: convert(row.updated_at),
    customer_last_reply_at: convert(row.customer_last_reply_at),
    admin_last_reply_at: convert(row.admin_last_reply_at),
    sms_sent_at: convert(row.sms_sent_at),
    messages: includeMessages ? messages.map((message) => ({ ...message, created_at: convert(message.created_at) })) : undefined,
  };
}

async function messagesFor(ticketId) {
  return supabase("support_messages", {
    query: `?select=id,sender,body,created_at&ticket_id=eq.${encodeURIComponent(ticketId)}&order=created_at.asc`,
  });
}

async function ticketByRef(ticketRef) {
  const rows = await supabase("support_tickets", {
    query: `?select=*&ticket_ref=eq.${encodeURIComponent(ticketRef)}&limit=1`,
  });
  return Array.isArray(rows) ? rows[0] : null;
}

async function insert(table, body) {
  return supabase(table, {
    method: "POST",
    headers: { prefer: "return=representation" },
    body,
  });
}

async function patch(table, query, body) {
  return supabase(table, {
    method: "PATCH",
    query,
    headers: { prefer: "return=representation" },
    body,
  });
}

function ticketRef() {
  return `GI-${crypto.randomInt(100000, 999999)}`;
}

function smsText(ref, category, priority, summary) {
  const first = String(summary || "").split("\n")[0].replace(/^Problem:\s*/i, "").slice(0, 95);
  const adminUrl = (process.env.SUPPORT_ADMIN_URL || "https://www.growthintel.app/admin/support").replace(/\/$/, "");
  return [`GrowthIntel Support #${ref}`, first || category, `Priority: ${priority}.`, `Open ticket: ${adminUrl}?ticket=${encodeURIComponent(ref)}`].join("\n");
}

async function sendSms(text) {
  const provider = String(process.env.SMS_PROVIDER || "").trim().toLowerCase();
  const to = String(process.env.SUPPORT_SMS_TO || "").trim();
  if (!provider || !to) return { status: "NOT_CONFIGURED", provider: provider || "none", error: "SMS provider or SUPPORT_SMS_TO is not configured" };
  if (provider === "webhook") {
    const url = process.env.SMS_WEBHOOK_URL;
    if (!url) return { status: "FAILED", provider, error: "SMS_WEBHOOK_URL is missing" };
    const response = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ to, from: process.env.SMS_FROM || "", body: text }) });
    if (!response.ok) return { status: "FAILED", provider, error: `Webhook returned ${response.status}` };
    return { status: "SENT", provider };
  }
  if (provider === "twilio") {
    const sid = process.env.SMS_API_KEY;
    const secret = process.env.SMS_API_SECRET;
    const from = process.env.SMS_FROM;
    if (!sid || !secret || !from) return { status: "FAILED", provider, error: "Twilio credentials are incomplete" };
    const form = new URLSearchParams({ To: to, From: from, Body: text });
    const response = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${encodeURIComponent(sid)}/Messages.json`, {
      method: "POST",
      headers: { authorization: `Basic ${Buffer.from(`${sid}:${secret}`).toString("base64")}`, "content-type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    if (!response.ok) return { status: "FAILED", provider, error: `Twilio returned ${response.status}` };
    return { status: "SENT", provider };
  }
  return { status: "FAILED", provider, error: `Unsupported SMS_PROVIDER ${provider}` };
}

async function sendFallbackEmail(subject, text) {
  const to = process.env.SUPPORT_FALLBACK_EMAIL;
  const apiKey = process.env.MAILJET_API_KEY;
  const secret = process.env.MAILJET_SECRET_KEY;
  const sender = process.env.SUPPORT_EMAIL_FROM_ADDRESS || process.env.EMAIL_FROM_ADDRESS || "";
  const from = String(sender).match(/<([^>]+)>/)?.[1] || sender;
  if (!to || !apiKey || !secret || !from) return { status: "NOT_CONFIGURED", provider: "mailjet" };
  const response = await fetch("https://api.mailjet.com/v3.1/send", {
    method: "POST",
    headers: { authorization: `Basic ${Buffer.from(`${apiKey}:${secret}`).toString("base64")}`, "content-type": "application/json" },
    body: JSON.stringify({ Messages: [{ From: { Email: from, Name: "GrowthIntel Support" }, To: [{ Email: to }], Subject: subject, TextPart: text }] }),
  });
  return response.ok ? { status: "SENT", provider: "mailjet" } : { status: "FAILED", provider: "mailjet", error: `Mailjet returned ${response.status}` };
}

async function notify(ticket, category, priority, summary) {
  const text = smsText(ticket.ticket_ref, category, priority, summary);
  let result;
  try {
    result = await sendSms(text);
  } catch (error) {
    result = { status: "FAILED", provider: process.env.SMS_PROVIDER || "unknown", error: error.message };
  }
  await insert("support_notification_events", { ticket_id: ticket.id, channel: "sms", status: result.status, provider: result.provider, error: result.error || null, created_at: now() }).catch(() => null);
  if (result.status !== "SENT") {
    const fallback = await sendFallbackEmail(`GrowthIntel Support ${ticket.ticket_ref}`, text).catch((error) => ({ status: "FAILED", provider: "mailjet", error: error.message }));
    await insert("support_notification_events", { ticket_id: ticket.id, channel: "email", status: fallback.status, provider: fallback.provider, error: fallback.error || null, created_at: now() }).catch(() => null);
  }
  return result;
}

function supportTableMissing(error) {
  return String(error?.message || "").includes("support_tickets") || String(error?.payload?.message || "").includes("support_tickets");
}

async function auditEvent(eventType, ticketRef, payload, userId = null) {
  const stamp = now();
  await insert("membership_audit_logs", {
    actor_user_id: userId,
    action: "SUPPORT_TICKET_EVENT",
    target_user_id: userId,
    payment_id: null,
    detail: JSON.stringify({ event_type: eventType, ticket_ref: ticketRef, ...payload }),
    created_at: stamp,
    ip_address: null,
  });
  return stamp;
}

async function readSupportAuditEvents() {
  const rows = await supabase("membership_audit_logs", {
    query: "?select=id,actor_user_id,target_user_id,detail,created_at&action=eq.SUPPORT_TICKET_EVENT&order=created_at.asc&limit=1000",
  });
  return (rows || []).map((row) => {
    try {
      return { ...JSON.parse(row.detail || "{}"), audit_id: row.id, audit_created_at: row.created_at };
    } catch {
      return null;
    }
  }).filter((event) => event && event.ticket_ref);
}

function ticketFromAudit(ticketRef, events, includeMessages = false) {
  const ticketEvents = events.filter((event) => event.ticket_ref === ticketRef);
  const created = ticketEvents.find((event) => event.event_type === "created");
  if (!created) return null;
  const ticket = { ...created.ticket };
  const messages = [...(ticket.messages || [])];
  for (const event of ticketEvents) {
    if (event.event_type === "customer_reply") {
      messages.push({ id: event.audit_id, sender: "customer", body: event.body, created_at: event.created_at || event.audit_created_at });
      ticket.status = "HUMAN_REQUESTED";
      ticket.customer_last_reply_at = event.created_at || event.audit_created_at;
    }
    if (event.event_type === "admin_reply") {
      messages.push({ id: event.audit_id, sender: "admin", body: event.body, created_at: event.created_at || event.audit_created_at });
      ticket.status = "WAITING_FOR_CUSTOMER";
      ticket.assigned_status = "admin_replied";
      ticket.admin_last_reply_at = event.created_at || event.audit_created_at;
    }
    if (event.event_type === "status") {
      ticket.status = event.status || ticket.status;
      ticket.updated_at = event.created_at || event.audit_created_at;
    }
    ticket.updated_at = Math.max(ticket.updated_at || 0, event.created_at || event.audit_created_at || 0);
  }
  return cleanTicket(ticket, includeMessages, messages);
}

async function createAuditTicket({ body, user, problem, category, priority, summary }) {
  const stamp = now();
  const accessToken = user ? null : crypto.randomBytes(24).toString("hex");
  const ref = ticketRef();
  const ticket = {
    id: ref,
    ticket_ref: ref,
    user_id: user?.id || null,
    customer_email: String(user?.email || body.email || "").trim().toLowerCase(),
    customer_name: String(user?.name || body.name || "").trim().slice(0, 100) || null,
    account_plan: user?.plan_version || (user?.membership_state === "ACTIVE" ? "premium_v1" : "free_or_logged_out"),
    membership_state: user?.membership_state || "LOGGED_OUT",
    current_page: String(body.current_page || "").slice(0, 500),
    category,
    priority,
    original_problem: problem,
    ai_troubleshooting: redact(body.ai_troubleshooting || "GrowthIntel AI support attempted first-line troubleshooting.", 6000),
    ai_summary: summary,
    status: "HUMAN_REQUESTED",
    assigned_status: "unassigned",
    notification_status: "PENDING",
    notification_attempts: 0,
    notification_error: null,
    sms_sent_at: null,
    customer_access_hash: accessToken ? hash(accessToken) : null,
    created_at: stamp,
    updated_at: stamp,
    customer_last_reply_at: stamp,
    admin_last_reply_at: null,
    messages: [
      { id: `${ref}-customer`, sender: "customer", body: problem, created_at: stamp },
      { id: `${ref}-ai`, sender: "ai", body: summary, created_at: stamp + 1 },
    ],
  };
  await auditEvent("created", ref, { ticket }, user?.id || null);
  const sms = await notify({ id: null, ticket_ref: ref }, category, priority, summary).catch((error) => ({ status: "FAILED", provider: "unknown", error: error.message }));
  ticket.notification_status = sms.status === "SENT" ? "SENT" : sms.status === "NOT_CONFIGURED" ? "NOT_CONFIGURED" : "FAILED";
  ticket.notification_attempts = 1;
  ticket.notification_error = sms.error || null;
  ticket.sms_sent_at = sms.status === "SENT" ? now() : null;
  await auditEvent("notification", ref, { notification_status: ticket.notification_status, provider: sms.provider || null, error: sms.error || null }, user?.id || null);
  return { ...cleanTicket(ticket, true, ticket.messages), access_token: accessToken || undefined };
}

async function listAuditTickets(user = null, admin = false) {
  const events = await readSupportAuditEvents();
  const refs = [...new Set(events.map((event) => event.ticket_ref))];
  return refs
    .map((ref) => ticketFromAudit(ref, events, false))
    .filter(Boolean)
    .filter((ticket) => admin || (user && ticket.user_id === user.id))
    .sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
}

async function getAuditTicket(ticketRefValue, user = null, token = "", admin = false) {
  const events = await readSupportAuditEvents();
  const ticket = ticketFromAudit(ticketRefValue, events, true);
  if (!ticket) return null;
  const tokenOk = token && ticket.customer_access_hash === hash(token);
  if (!admin && !tokenOk && !(user && user.id === ticket.user_id)) {
    const error = new Error("You can only view your own support tickets");
    error.status = 403;
    throw error;
  }
  return ticket;
}

async function handleSupport(req, res, route) {
  try {
    if (req.method === "OPTIONS") return json(res, 204, {});
    if (req.method === "POST" && route === "support/ai") {
      rateLimit(req, "support-ai", 20, 10 * 60 * 1000);
      const body = await readBody(req);
      const message = redact(body.message || body.problem || "", 4000);
      if (message.length < 2) return json(res, 422, { detail: "Please enter a support question." });
      const answer = aiAnswer(message);
      return json(res, 200, { ...answer, status: "AI_HANDLING", can_escalate: true, source: "growthintel-support-knowledge-base" });
    }

    if (req.method === "POST" && route === "support/tickets") {
      rateLimit(req, "support-escalation", 3, 60 * 60 * 1000);
      const body = await readBody(req);
      const user = await currentUser(req).catch(() => null);
      const problem = redact(body.problem || body.message || "", 6000);
      if (problem.length < 4) return json(res, 422, { detail: "Please describe the issue." });
      const email = String(user?.email || body.email || "").trim().toLowerCase();
      const name = String(user?.name || body.name || "").trim().slice(0, 100);
      if (!user && (!email.includes("@") || name.length < 2)) return json(res, 422, { detail: "Please add your name and email before requesting human support." });
      const category = categoryFor(problem);
      const priority = priorityFor(category, problem);
      const summary = makeSummary({ problem, category, priority, transcript: body.transcript || [] });
      const created = now();
      let ticket = null;
      for (let attempt = 0; attempt < 5 && !ticket; attempt += 1) {
        const accessToken = crypto.randomBytes(24).toString("hex");
        try {
          const rows = await insert("support_tickets", {
            ticket_ref: ticketRef(),
            user_id: user?.id || null,
            customer_email: email,
            customer_name: name || null,
            account_plan: user?.plan_version || (user?.membership_state === "ACTIVE" ? "premium_v1" : "free_or_logged_out"),
            membership_state: user?.membership_state || "LOGGED_OUT",
            current_page: String(body.current_page || req.headers.referer || "").slice(0, 500),
            category,
            priority,
            original_problem: problem,
            ai_troubleshooting: redact(body.ai_troubleshooting || "GrowthIntel AI support attempted first-line troubleshooting.", 6000),
            ai_summary: summary,
            status: "HUMAN_REQUESTED",
            assigned_status: "unassigned",
            notification_status: "PENDING",
            notification_attempts: 0,
            customer_access_hash: user ? null : hash(accessToken),
            created_at: created,
            updated_at: created,
            customer_last_reply_at: created,
          });
          ticket = rows?.[0];
          if (ticket && !user) ticket.access_token = accessToken;
        } catch (error) {
          if (supportTableMissing(error)) {
            return json(res, 201, await createAuditTicket({ body, user, problem, category, priority, summary }));
          }
          if (!String(error.message || "").toLowerCase().includes("duplicate")) throw error;
        }
      }
      if (!ticket) return json(res, 500, { detail: "Could not create a unique support ticket. Try again." });
      await insert("support_messages", { ticket_id: ticket.id, sender: "customer", body: problem, created_at: created });
      await insert("support_messages", { ticket_id: ticket.id, sender: "ai", body: summary, created_at: created + 1 });
      const sms = await notify(ticket, category, priority, summary);
      const notificationStatus = sms.status === "SENT" ? "SENT" : sms.status === "NOT_CONFIGURED" ? "NOT_CONFIGURED" : "FAILED";
      const patched = await patch("support_tickets", `?id=eq.${ticket.id}`, { notification_status: notificationStatus, notification_attempts: 1, notification_error: sms.error || null, sms_sent_at: sms.status === "SENT" ? now() : null, updated_at: now() });
      const finalTicket = patched?.[0] || ticket;
      const messages = await messagesFor(ticket.id);
      return json(res, 201, { ...cleanTicket(finalTicket, true, messages), access_token: ticket.access_token || undefined });
    }

    if (req.method === "GET" && route === "support/tickets") {
      const user = await currentUser(req);
      if (!user) return json(res, 401, { detail: "Authentication required" });
      try {
        const rows = await supabase("support_tickets", { query: `?select=*&user_id=eq.${encodeURIComponent(user.id)}&order=updated_at.desc&limit=100` });
        return json(res, 200, rows.map((row) => cleanTicket(row, false)));
      } catch (error) {
        if (supportTableMissing(error)) return json(res, 200, await listAuditTickets(user, false));
        throw error;
      }
    }

    const ticketDetail = route.match(/^support\/tickets\/([^/]+)$/);
    if (req.method === "GET" && ticketDetail) {
      let ticket = null;
      try {
        ticket = await ticketByRef(ticketDetail[1]);
      } catch (error) {
        if (supportTableMissing(error)) {
          const user = await currentUser(req).catch(() => null);
          const url = new URL(req.url, "https://www.growthintel.app");
          return json(res, 200, await getAuditTicket(ticketDetail[1], user, url.searchParams.get("token") || "", isAdmin(user)));
        }
        throw error;
      }
      if (!ticket) return json(res, 404, { detail: "Support ticket not found" });
      const user = await currentUser(req).catch(() => null);
      const url = new URL(req.url, "https://www.growthintel.app");
      const tokenOk = url.searchParams.get("token") && ticket.customer_access_hash === hash(url.searchParams.get("token"));
      if (!tokenOk && !(user && (user.id === ticket.user_id || isAdmin(user)))) return json(res, 403, { detail: "You can only view your own support tickets" });
      return json(res, 200, cleanTicket(ticket, true, await messagesFor(ticket.id)));
    }

    const customerReply = route.match(/^support\/tickets\/([^/]+)\/messages$/);
    if (req.method === "POST" && customerReply) {
      rateLimit(req, "support-reply", 12, 5 * 60 * 1000);
      let ticket = null;
      try {
        ticket = await ticketByRef(customerReply[1]);
      } catch (error) {
        if (supportTableMissing(error)) {
          const body = await readBody(req);
          const message = redact(body.message || "", 4000);
          if (message.length < 2) return json(res, 422, { detail: "Message is too short" });
          const user = await currentUser(req).catch(() => null);
          const url = new URL(req.url, "https://www.growthintel.app");
          await getAuditTicket(customerReply[1], user, url.searchParams.get("token") || "", isAdmin(user));
          await auditEvent("customer_reply", customerReply[1], { body: message, created_at: now() }, user?.id || null);
          return json(res, 200, await getAuditTicket(customerReply[1], user, url.searchParams.get("token") || "", isAdmin(user)));
        }
        throw error;
      }
      if (!ticket) return json(res, 404, { detail: "Support ticket not found" });
      const user = await currentUser(req).catch(() => null);
      const url = new URL(req.url, "https://www.growthintel.app");
      const tokenOk = url.searchParams.get("token") && ticket.customer_access_hash === hash(url.searchParams.get("token"));
      if (!tokenOk && !(user && (user.id === ticket.user_id || isAdmin(user)))) return json(res, 403, { detail: "You can only reply to your own support tickets" });
      const body = await readBody(req);
      const message = redact(body.message || "", 4000);
      if (message.length < 2) return json(res, 422, { detail: "Message is too short" });
      const stamp = now();
      await insert("support_messages", { ticket_id: ticket.id, sender: "customer", body: message, created_at: stamp });
      const rows = await patch("support_tickets", `?id=eq.${ticket.id}`, { status: "HUMAN_REQUESTED", updated_at: stamp, customer_last_reply_at: stamp });
      return json(res, 200, cleanTicket(rows?.[0] || ticket, true, await messagesFor(ticket.id)));
    }

    if (route === "support/admin/tickets" && req.method === "GET") {
      const user = await currentUser(req);
      if (!isAdmin(user)) return json(res, 403, { detail: "Administrator access required" });
      const url = new URL(req.url, "https://www.growthintel.app");
      const status = url.searchParams.get("status");
      try {
        const filter = status ? `&status=eq.${encodeURIComponent(status)}` : "";
        const rows = await supabase("support_tickets", { query: `?select=*&order=updated_at.desc&limit=200${filter}` });
        return json(res, 200, rows.map((row) => cleanTicket(row, false)));
      } catch (error) {
        if (supportTableMissing(error)) {
          const rows = await listAuditTickets(null, true);
          return json(res, 200, status ? rows.filter((row) => row.status === status) : rows);
        }
        throw error;
      }
    }

    const adminDetail = route.match(/^support\/admin\/tickets\/([^/]+)$/);
    if (adminDetail && req.method === "GET") {
      const user = await currentUser(req);
      if (!isAdmin(user)) return json(res, 403, { detail: "Administrator access required" });
      let ticket = null;
      try {
        ticket = await ticketByRef(adminDetail[1]);
      } catch (error) {
        if (supportTableMissing(error)) return json(res, 200, await getAuditTicket(adminDetail[1], user, "", true));
        throw error;
      }
      if (!ticket) return json(res, 404, { detail: "Support ticket not found" });
      return json(res, 200, cleanTicket(ticket, true, await messagesFor(ticket.id)));
    }

    const adminReply = route.match(/^support\/admin\/tickets\/([^/]+)\/reply$/);
    if (adminReply && req.method === "POST") {
      const user = await currentUser(req);
      if (!isAdmin(user)) return json(res, 403, { detail: "Administrator access required" });
      let ticket = null;
      try {
        ticket = await ticketByRef(adminReply[1]);
      } catch (error) {
        if (supportTableMissing(error)) {
          const body = await readBody(req);
          const message = redact(body.message || "", 4000);
          if (message.length < 2) return json(res, 422, { detail: "Message is too short" });
          await getAuditTicket(adminReply[1], user, "", true);
          await auditEvent("admin_reply", adminReply[1], { body: message, created_at: now() }, user.id);
          return json(res, 200, await getAuditTicket(adminReply[1], user, "", true));
        }
        throw error;
      }
      if (!ticket) return json(res, 404, { detail: "Support ticket not found" });
      const body = await readBody(req);
      const message = redact(body.message || "", 4000);
      if (message.length < 2) return json(res, 422, { detail: "Message is too short" });
      const stamp = now();
      await insert("support_messages", { ticket_id: ticket.id, sender: "admin", body: message, created_at: stamp });
      const rows = await patch("support_tickets", `?id=eq.${ticket.id}`, { status: "WAITING_FOR_CUSTOMER", assigned_status: "admin_replied", updated_at: stamp, admin_last_reply_at: stamp });
      return json(res, 200, cleanTicket(rows?.[0] || ticket, true, await messagesFor(ticket.id)));
    }

    const adminStatus = route.match(/^support\/admin\/tickets\/([^/]+)\/status$/);
    if (adminStatus && req.method === "POST") {
      const user = await currentUser(req);
      if (!isAdmin(user)) return json(res, 403, { detail: "Administrator access required" });
      let ticket = null;
      try {
        ticket = await ticketByRef(adminStatus[1]);
      } catch (error) {
        if (supportTableMissing(error)) {
          const body = await readBody(req);
          const status = String(body.status || "").toUpperCase();
          if (!SUPPORT_STATUSES.has(status)) return json(res, 422, { detail: "Unsupported ticket status" });
          await getAuditTicket(adminStatus[1], user, "", true);
          await auditEvent("status", adminStatus[1], { status, created_at: now() }, user.id);
          return json(res, 200, await getAuditTicket(adminStatus[1], user, "", true));
        }
        throw error;
      }
      if (!ticket) return json(res, 404, { detail: "Support ticket not found" });
      const body = await readBody(req);
      const status = String(body.status || "").toUpperCase();
      if (!SUPPORT_STATUSES.has(status)) return json(res, 422, { detail: "Unsupported ticket status" });
      const rows = await patch("support_tickets", `?id=eq.${ticket.id}`, { status, updated_at: now() });
      return json(res, 200, cleanTicket(rows?.[0] || ticket, true, await messagesFor(ticket.id)));
    }

    return null;
  } catch (error) {
    return json(res, error.status || 500, { detail: error.message || "Support service failed safely.", support_status: "DEGRADED" });
  }
}

module.exports = { handleSupport };
