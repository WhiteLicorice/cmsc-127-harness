import { describe, expect, it } from "vitest";

describe("roles panel", () => {
  it("shows privileges added by GRANT", async () => {
    const { describeRolePrivileges } = await import("../../src/panels/roles.js");
    expect(describeRolePrivileges({ role: "student", table: "enrollment", privileges: ["SELECT"] })).toContain("SELECT");
  });

  it("shows that REVOKE removed the privilege", async () => {
    const { describeRolePrivileges } = await import("../../src/panels/roles.js");
    expect(describeRolePrivileges({ role: "student", table: "enrollment", privileges: [] })).toContain("no table privileges");
  });
});
