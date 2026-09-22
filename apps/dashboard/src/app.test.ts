import { describe, expect, it } from "vitest";
import { parsePreviewError } from "./api";
import { buildFillSnippet } from "./fillSnippet";

describe("parsePreviewError", () => {
  it("splits status from server detail", () => {
    expect(parsePreviewError(new Error("422: this link opens a Teams meeting")))
      .toEqual({ status: 422, message: "this link opens a Teams meeting" });
  });

  it("handles bare network failures", () => {
    expect(parsePreviewError(new TypeError("fetch failed")))
      .toEqual({ status: null, message: "fetch failed" });
  });

  it("handles non-errors", () => {
    expect(parsePreviewError("boom").status).toBeNull();
  });
});

describe("buildFillSnippet", () => {
  const snippet = buildFillSnippet({
    full_name: "Nitish Kumar",
    registration_number: "12515641",
    email: "",
    mobile: null,
  });

  it("is a javascript URL with baked values", () => {
    expect(snippet.startsWith("javascript:")).toBe(true);
    const code = decodeURIComponent(snippet.slice("javascript:".length));
    expect(code).toContain('"full_name":"Nitish Kumar"');
    expect(code).toContain('"registration_number":"12515641"');
    expect(code).not.toContain('"email":');
    expect(code).not.toContain('"mobile":');
    expect(code).not.toContain("__V__");
    expect(code).not.toContain("__H__");
  });

  it("embeds the hint catalog for in-page matching", () => {
    const code = decodeURIComponent(snippet.slice("javascript:".length));
    expect(code).toContain("registration_number");
    expect(code).toContain("conducted by");
  });

  it("is syntactically valid JavaScript", () => {
    const code = decodeURIComponent(snippet.slice("javascript:".length));
    expect(() => new Function(code)).not.toThrow();
  });
});
