import { createClient } from "@supabase/supabase-js";
import { parseAgentMailEvent, verifySvixPayload } from "./logic.ts";

const MAX_BODY_BYTES = 1_048_576;

function jsonResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function requiredEnv(name: string): string {
  const value = Deno.env.get(name);
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return jsonResponse({ error: "payload_too_large" }, 413);
  }

  const svixId = request.headers.get("svix-id");
  const svixTimestamp = request.headers.get("svix-timestamp");
  const svixSignature = request.headers.get("svix-signature");
  if (!svixId || !svixTimestamp || !svixSignature) {
    return jsonResponse({ error: "missing_signature_headers" }, 400);
  }

  const rawBody = await request.text();
  if (new TextEncoder().encode(rawBody).byteLength > MAX_BODY_BYTES) {
    return jsonResponse({ error: "payload_too_large" }, 413);
  }

  let webhookSecret: string;
  try {
    webhookSecret = requiredEnv("AGENTMAIL_WEBHOOK_SECRET");
  } catch {
    console.error("AgentMail webhook secret is not configured");
    return jsonResponse({ error: "temporary_configuration_failure" }, 503);
  }

  let verifiedPayload: unknown;
  try {
    verifiedPayload = verifySvixPayload(
      rawBody,
      { id: svixId, timestamp: svixTimestamp, signature: svixSignature },
      webhookSecret,
    );
  } catch (error) {
    console.warn("AgentMail webhook signature verification failed", {
      svixId,
      error: error instanceof Error ? error.message : "unknown_error",
    });
    return jsonResponse({ error: "invalid_signature" }, 400);
  }

  let event;
  try {
    event = parseAgentMailEvent(verifiedPayload, svixId);
  } catch (error) {
    return jsonResponse({
      error: "invalid_payload",
      detail: error instanceof Error ? error.message : "unknown_error",
    }, 400);
  }

  try {
    const supabaseUrl = requiredEnv("SUPABASE_URL");
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ??
      Deno.env.get("SUPABASE_SECRET_KEY");
    if (!serviceKey) {
      throw new Error("Missing Supabase service-role/secret key");
    }

    const supabase = createClient(supabaseUrl, serviceKey, {
      auth: { persistSession: false, autoRefreshToken: false },
    });
    const { data, error } = await supabase.rpc("apply_agentmail_webhook", {
      p_provider_event_id: event.eventId,
      p_event_type: event.eventType,
      p_message_id: event.messageId,
      p_payload: event.payload,
      p_signature_verified: true,
    });

    if (error) throw error;
    const result = Array.isArray(data) ? data[0] : data;

    return jsonResponse({
      received: true,
      duplicate: result?.was_applied === false,
      matched: Boolean(result?.matched_delivery_key),
    });
  } catch (error) {
    // A 5xx response tells AgentMail to retry. Never acknowledge an event that
    // could not be committed to the idempotent webhook ledger.
    console.error("AgentMail webhook persistence failed", {
      eventId: event.eventId,
      error: error instanceof Error ? error.message : "unknown_error",
    });
    return jsonResponse({ error: "temporary_persistence_failure" }, 503);
  }
});
