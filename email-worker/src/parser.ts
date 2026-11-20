import PostalMime from "postal-mime";

export const MAX_PAYLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_ATTACHMENTS = 5;
export const MIN_INLINE_IMAGE_BYTES = 15 * 1024;

export class PayloadTooLargeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PayloadTooLargeError";
  }
}

export interface NormalizedAttachment {
  filename: string;
  content_type: string;
  size_bytes: number;
  content_base64: string;
}

export interface NormalizedInboundEmail {
  message_id?: string;
  from_address: string;
  to_address: string;
  subject: string;
  received_at: string;
  text_body?: string;
  html_body?: string;
  attachments: NormalizedAttachment[];
}

function toUint8Array(content: ArrayBuffer | Uint8Array | string): Uint8Array {
  if (content instanceof Uint8Array) {
    return content;
  }
  if (content instanceof ArrayBuffer) {
    return new Uint8Array(content);
  }
  return new TextEncoder().encode(content);
}

export function uint8ArrayToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000;
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length));
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function resolveReceivedAt(parsedDate: string | undefined, headerDate: string | null): string {
  const candidate = (parsedDate || headerDate || "").trim();
  if (candidate) {
    const parsed = new Date(candidate);
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toISOString();
    }
  }
  return new Date().toISOString();
}

export async function parseEmailMessage(
  raw: ReadableStream<Uint8Array> | ArrayBuffer,
  envelopeFrom: string,
  envelopeTo: string,
  headerDate: string | null,
  headerMessageId: string | null,
): Promise<NormalizedInboundEmail> {
  const rawBuffer =
    raw instanceof ArrayBuffer ? raw : await new Response(raw).arrayBuffer();

  if (rawBuffer.byteLength > MAX_PAYLOAD_BYTES) {
    throw new PayloadTooLargeError(
      `Email payload size ${rawBuffer.byteLength} exceeds ${MAX_PAYLOAD_BYTES} byte limit`,
    );
  }

  const parser = new PostalMime();
  const parsed = await parser.parse(rawBuffer);

  const rawMessageId = (parsed.messageId || headerMessageId || "").trim();
  const fromAddr = parsed.from?.address
    ? parsed.from.name
      ? `${parsed.from.name} <${parsed.from.address}>`
      : parsed.from.address
    : envelopeFrom;
  const toAddr = parsed.to?.[0]?.address || envelopeTo;

  const attachments: NormalizedAttachment[] = [];
  for (const att of parsed.attachments ?? []) {
    if (attachments.length >= MAX_ATTACHMENTS) {
      break;
    }
    const bytes = toUint8Array(att.content);
    const mimeType = (att.mimeType || "application/octet-stream").toLowerCase();
    const isInline = att.disposition === "inline" || Boolean(att.contentId);
    if (isInline && mimeType.startsWith("image/") && bytes.byteLength < MIN_INLINE_IMAGE_BYTES) {
      continue;
    }

    attachments.push({
      filename: att.filename || "attachment.bin",
      content_type: mimeType,
      size_bytes: bytes.byteLength,
      content_base64: uint8ArrayToBase64(bytes),
    });
  }

  return {
    ...(rawMessageId ? { message_id: rawMessageId } : {}),
    from_address: fromAddr,
    to_address: toAddr,
    subject: parsed.subject || "",
    received_at: resolveReceivedAt(parsed.date, headerDate),
    ...(parsed.text ? { text_body: parsed.text } : {}),
    ...(parsed.html ? { html_body: parsed.html } : {}),
    attachments,
  };
}
