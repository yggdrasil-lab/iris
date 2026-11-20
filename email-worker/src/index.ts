import { MAX_PAYLOAD_BYTES, PayloadTooLargeError, parseEmailMessage } from "./parser";

export interface Env {
  IRIS_WEBHOOK_URL: string;
  IRIS_WEBHOOK_SECRET: string;
  ALLOWED_FORWARDERS: string;
  FORWARD_VERIFICATION_TO?: string;
}

export function hasPassingAuthResults(headers: Headers): boolean {
  const authResults = headers.get("authentication-results") || "";
  if (!authResults.trim()) {
    return false;
  }
  const spfPass = /\bspf=pass\b/i.test(authResults);
  const dkimPass = /\bdkim=pass\b/i.test(authResults);
  const spfFail = /\bspf=(fail|softfail|permerror)\b/i.test(authResults);
  const dkimFail = /\bdkim=(fail|permerror)\b/i.test(authResults);

  return (spfPass || dkimPass) && !dkimFail && (!spfFail || dkimPass);
}

export function canonicalizeForwarder(address: string): string {
  const lowered = address.trim().toLowerCase();
  const atIndex = lowered.lastIndexOf("@");
  if (atIndex <= 0) {
    return lowered;
  }
  const local = lowered.slice(0, atIndex);
  const domain = lowered.slice(atIndex + 1);
  if ((domain === "gmail.com" || domain === "googlemail.com") && local.includes("+caf_=")) {
    const baseLocal = local.split("+caf_=")[0];
    return `${baseLocal}@${domain}`;
  }
  return lowered;
}

export function isSenderAllowed(sender: string, allowlistRaw: string | undefined): boolean {
  if (!allowlistRaw || !allowlistRaw.trim()) {
    return false;
  }
  const allowed = new Set(
    allowlistRaw
      .split(",")
      .map((entry) => entry.trim().toLowerCase())
      .filter(Boolean),
  );
  if (allowed.size === 0) {
    return false;
  }

  const exactSender = sender.trim().toLowerCase();
  const canonicalSender = canonicalizeForwarder(exactSender);
  return allowed.has(exactSender) || allowed.has(canonicalSender);
}

export default {
  async email(message: ForwardableEmailMessage, env: Env, _ctx: ExecutionContext): Promise<void> {
    if (!hasPassingAuthResults(message.headers)) {
      message.setReject("Missing or failed Authentication-Results SPF/DKIM verification");
      return;
    }

    const sender = message.from.trim().toLowerCase();
    if (sender === "forwarding-noreply@google.com") {
      const target = (env.FORWARD_VERIFICATION_TO || "").trim();
      if (!target) {
        message.setReject("FORWARD_VERIFICATION_TO is not configured");
        return;
      }
      await message.forward(target);
      return;
    }

    if (!isSenderAllowed(sender, env.ALLOWED_FORWARDERS)) {
      message.setReject("Sender is not authorized to forward to this gateway");
      return;
    }

    if (message.rawSize > MAX_PAYLOAD_BYTES) {
      message.setReject("Inbound email exceeds 10 MB maximum size limit");
      return;
    }

    const webhookUrl = (env.IRIS_WEBHOOK_URL || "").trim();
    const webhookSecret = (env.IRIS_WEBHOOK_SECRET || "").trim();
    if (!webhookUrl || !webhookSecret) {
      throw new Error("IRIS_WEBHOOK_URL and IRIS_WEBHOOK_SECRET must be configured");
    }

    let payload;
    try {
      payload = await parseEmailMessage(
        message.raw,
        message.from,
        message.to,
        message.headers.get("date"),
        message.headers.get("message-id"),
      );
    } catch (err) {
      if (err instanceof PayloadTooLargeError) {
        message.setReject(err.message);
        return;
      }
      throw err;
    }

    const response = await fetch(webhookUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${webhookSecret}`,
        "User-Agent": "Yggdrasil-Iris-Worker/1.0",
      },
      body: JSON.stringify(payload),
    });

    if (response.status === 200 || response.status === 201) {
      return;
    }

    if (response.status === 422) {
      const errText = await response.text().catch(() => "");
      console.error(`Iris webhook rejected payload with 422: ${errText.slice(0, 300)}`);
      return;
    }

    const bodyText = await response.text().catch(() => "");
    throw new Error(`Iris webhook dispatch failed with HTTP ${response.status}: ${bodyText.slice(0, 300)}`);
  },
};
