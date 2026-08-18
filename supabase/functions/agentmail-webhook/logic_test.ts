import {
  deliveryStateForEvent,
  extractMessageId,
  parseAgentMailEvent,
  verifySvixPayload,
} from "./logic.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

Deno.test("extracts message ids from each AgentMail delivery shape", () => {
  assertEquals(extractMessageId({ send: { message_id: "msg_sent" } }), "msg_sent");
  assertEquals(extractMessageId({ delivery: { message_id: "msg_delivered" } }), "msg_delivered");
  assertEquals(extractMessageId({ bounce: { message_id: "msg_bounced" } }), "msg_bounced");
  assertEquals(extractMessageId({ reject: { message_id: "msg_rejected" } }), "msg_rejected");
  assertEquals(extractMessageId({ complaint: { message_id: "msg_complained" } }), "msg_complained");
});

Deno.test("maps lifecycle events without treating inbound mail as delivery", () => {
  assertEquals(deliveryStateForEvent("message.delivered"), "delivered");
  assertEquals(deliveryStateForEvent("message.received"), null);
});

Deno.test("uses payload event_id and falls back to svix id", () => {
  assertEquals(
    parseAgentMailEvent({ event_type: "message.sent", event_id: "evt_1" }, "svix_1").eventId,
    "evt_1",
  );
  assertEquals(
    parseAgentMailEvent({ event_type: "domain.verified" }, "svix_2").eventId,
    "svix_2",
  );
});

function base64(bytes: Uint8Array): string {
  let value = "";
  for (const byte of bytes) value += String.fromCharCode(byte);
  return btoa(value);
}

async function signedHeaders(body: string, timestamp: number) {
  const secretBytes = crypto.getRandomValues(new Uint8Array(32));
  const secret = `whsec_${base64(secretBytes)}`;
  const id = "msg_test_123";
  const key = await crypto.subtle.importKey(
    "raw",
    secretBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signed = new TextEncoder().encode(`${id}.${timestamp}.${body}`);
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, signed));
  return {
    secret,
    headers: { id, timestamp: String(timestamp), signature: `v1,${base64(signature)}` },
  };
}

Deno.test("verifies a real Svix signature and rejects body tampering", async () => {
  const body = JSON.stringify({ event_type: "message.delivered", event_id: "evt_1" });
  const signed = await signedHeaders(body, Math.floor(Date.now() / 1000));
  const verified = verifySvixPayload(body, signed.headers, signed.secret) as Record<string, unknown>;
  assertEquals(verified.event_id, "evt_1");
  let rejected = false;
  try {
    verifySvixPayload(`${body} `, signed.headers, signed.secret);
  } catch {
    rejected = true;
  }
  assertEquals(rejected, true);
});

Deno.test("rejects a correctly signed but expired Svix timestamp", async () => {
  const body = JSON.stringify({ event_type: "message.sent", event_id: "evt_old" });
  const signed = await signedHeaders(body, Math.floor(Date.now() / 1000) - 10 * 60);
  let rejected = false;
  try {
    verifySvixPayload(body, signed.headers, signed.secret);
  } catch {
    rejected = true;
  }
  assertEquals(rejected, true);
});
