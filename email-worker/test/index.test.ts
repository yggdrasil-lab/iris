import { afterEach, describe, expect, it, vi } from "vitest";
import worker, { type Env } from "../src/index";

function createRawStream(rawEmail: string): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(rawEmail);
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
}

function createMockMessage(options: {
  from: string;
  to?: string;
  rawEmail: string;
  authResults?: string;
  rawSize?: number;
}): {
  message: ForwardableEmailMessage;
  rejectedReason: () => string | null;
  forwardedTo: () => string | null;
} {
  let rejected: string | null = null;
  let forwarded: string | null = null;
  const headers = new Headers();
  if (options.authResults !== undefined) {
    headers.set("authentication-results", options.authResults);
  }
  const encoded = new TextEncoder().encode(options.rawEmail);

  const message = {
    from: options.from,
    to: options.to || "iris@example.com",
    headers,
    raw: createRawStream(options.rawEmail),
    rawSize: options.rawSize ?? encoded.byteLength,
    setReject(reason: string) {
      rejected = reason;
    },
    async forward(rcptTo: string) {
      forwarded = rcptTo;
    },
    async reply() {
      // Unused in gateway tests
    },
  } as unknown as ForwardableEmailMessage;

  return {
    message,
    rejectedReason: () => rejected,
    forwardedTo: () => forwarded,
  };
}

const baseEnv: Env = {
  IRIS_WEBHOOK_URL: "https://iris.example.com/webhook/email",
  IRIS_WEBHOOK_SECRET: "test-webhook-secret",
  ALLOWED_FORWARDERS: "alice@example.com,owner@gmail.com",
  FORWARD_VERIFICATION_TO: "owner@gmail.com",
};

const ctx = {
  waitUntil: vi.fn(),
  passThroughOnException: vi.fn(),
  props: {},
} as unknown as ExecutionContext;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("iris-email-worker", () => {
  it("forwards Google forwarding verification emails to FORWARD_VERIFICATION_TO", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const mock = createMockMessage({
      from: "forwarding-noreply@google.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Gmail Forwarding Confirmation\r\n\r\nConfirmation code: 123456",
    });

    await worker.email(mock.message, baseEnv, ctx);

    expect(mock.rejectedReason()).toBeNull();
    expect(mock.forwardedTo()).toBe("owner@gmail.com");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects unauthorized senders not in ALLOWED_FORWARDERS", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const mock = createMockMessage({
      from: "intruder@untrusted.example.net",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Spam\r\n\r\nHello",
    });

    await worker.email(mock.message, baseEnv, ctx);

    expect(mock.rejectedReason()).toContain("not authorized");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects emails failing SPF/DKIM Authentication-Results check", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const mock = createMockMessage({
      from: "alice@example.com",
      authResults: "mx.cloudflare.net; spf=fail; dkim=fail",
      rawEmail: "Subject: Spoofed\r\n\r\nSpoof attempt",
    });

    await worker.email(mock.message, baseEnv, ctx);

    expect(mock.rejectedReason()).toContain("SPF/DKIM");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("parses MIME, strips small inline images, preserves PDFs, and handles Gmail +caf_= forwarder", async () => {
    const smallPngBase64 = btoa("tiny-logo-bytes");
    const pdfBase64 = btoa("%PDF-1.4\nSample Receipt PDF");

    const boundary = "----=_Part_12345";
    const rawMime = [
      "From: DBS Alerts <alerts@bank.example.com>",
      "To: iris@example.com",
      "Subject: Card Transaction Alert",
      "Message-ID: <dbs-alert-001@bank.example.com>",
      "Date: Fri, 20 Nov 2026 08:15:00 +0800",
      "MIME-Version: 1.0",
      `Content-Type: multipart/mixed; boundary="${boundary}"`,
      "",
      `--${boundary}`,
      "Content-Type: text/plain; charset=utf-8",
      "",
      "SGD 42.50 charged at Grab Singapore.",
      `--${boundary}`,
      "Content-Type: image/png; name=logo.png",
      "Content-Disposition: inline; filename=logo.png",
      "Content-ID: <logo@bank.example.com>",
      "Content-Transfer-Encoding: base64",
      "",
      smallPngBase64,
      `--${boundary}`,
      "Content-Type: application/pdf; name=receipt.pdf",
      "Content-Disposition: attachment; filename=receipt.pdf",
      "Content-Transfer-Encoding: base64",
      "",
      pdfBase64,
      `--${boundary}--`,
      "",
    ].join("\r\n");

    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "needs_review" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const mock = createMockMessage({
      from: "owner+caf_=iris=example.com@gmail.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: rawMime,
    });

    await worker.email(mock.message, baseEnv, ctx);

    expect(mock.rejectedReason()).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://iris.example.com/webhook/email");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer test-webhook-secret");

    const posted = JSON.parse(init.body as string);
    expect(posted.message_id).toBe("<dbs-alert-001@bank.example.com>");
    expect(posted.subject).toBe("Card Transaction Alert");
    expect(posted.text_body).toContain("SGD 42.50");
    expect(posted.attachments).toHaveLength(1);
    expect(posted.attachments[0].filename).toBe("receipt.pdf");
    expect(posted.attachments[0].content_type).toBe("application/pdf");
    expect(posted.attachments[0].content_base64).toBe(pdfBase64);
  });

  it("handles 422 cleanly without throwing, and throws on 503 or network error", async () => {
    const mock422 = createMockMessage({
      from: "alice@example.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Bad Payload\r\n\r\nTest 422",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unprocessable", { status: 422 })));

    await expect(worker.email(mock422.message, baseEnv, ctx)).resolves.toBeUndefined();

    const mock503 = createMockMessage({
      from: "alice@example.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Retry Me\r\n\r\nTest 503",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })));

    await expect(worker.email(mock503.message, baseEnv, ctx)).rejects.toThrow("HTTP 503");

    const mockNet = createMockMessage({
      from: "alice@example.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Network Error\r\n\r\nTest network",
    });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connect ECONNREFUSED")));

    await expect(worker.email(mockNet.message, baseEnv, ctx)).rejects.toThrow("ECONNREFUSED");
  });

  it("rejects messages exceeding 10 MB payload limit", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const mock = createMockMessage({
      from: "alice@example.com",
      authResults: "mx.cloudflare.net; spf=pass; dkim=pass",
      rawEmail: "Subject: Huge\r\n\r\nBody",
      rawSize: 11 * 1024 * 1024,
    });

    await worker.email(mock.message, baseEnv, ctx);

    expect(mock.rejectedReason()).toContain("10 MB");
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
