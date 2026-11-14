import { describe, expect, it } from "vitest";
import { renderSchema } from "../../src/panels/schema.js";

describe("schema empty state", () => {
  it("keeps the missing README explanation outside Mermaid input", () => {
    const html = renderSchema({ schema: "public", erdText: "", tables: [], relationships: [], differences: [] });
    expect(html).not.toContain('<pre class="mermaid">No Mermaid');
    expect(html).not.toContain('<pre class="mermaid">erDiagram</pre>');
    expect(html).toContain("No Mermaid erDiagram block appears in README.md.");
    expect(html).toContain("<h2>ERD from README.md</h2>");
  });
});
