import { Webhook } from "svix";

export const DELIVERY_EVENT_STATES = {
  "message.sent": "sent",
  "message.delivered": "delivered",
  "message.bounced": "bounced",
  "message.rejected": "rejected",
  "message.complained": "complained",
} as const;

export type DeliveryEventType = keyof typeof DELIVERY_EVENT_STATES;

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return undefined;
  }
  return value as JsonObject;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function extractMessageId(payload: JsonObject): string | null {
  for (const key of ["send", "delivery", "bounce", "complaint", "reject", "message"]) {
    const candidate = asObject(payload[key]);
    const messageId = nonEmptyString(candidate?.message_id);
    if (messageId) return messageId;
  }

  return nonEmptyString(payload.message_id) ?? null;
}

export function deliveryStateForEvent(eventType: string): string | null {
  return DELIVERY_EVENT_STATES[eventType as DeliveryEventType] ?? null;
}

export function parseAgentMailEvent(payload: unknown, fallbackEventId: string) {
  const event = asObject(payload);
  if (!event) throw new Error("Webhook payload must be a JSON object");

  const eventType = nonEmptyString(event.event_type);
  if (!eventType) throw new Error("Webhook payload is missing event_type");

  return {
    eventId: nonEmptyString(event.event_id) ?? fallbackEventId,
    eventType,
    messageId: extractMessageId(event),
    payload: event,
  };
}

export function verifySvixPayload(
  rawBody: string,
  headers: { id: string; timestamp: string; signature: string },
  secret: string,
): unknown {
  const webhook = new Webhook(secret);
  return webhook.verify(rawBody, {
    "svix-id": headers.id,
    "svix-timestamp": headers.timestamp,
    "svix-signature": headers.signature,
  });
}
