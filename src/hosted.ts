// The hosted page runs on GitHub Pages and calls a server on this computer.
// The local page sends every request to its own origin.

export const HOSTED_ORIGIN = "https://whitelicorice.github.io";
export const DEFAULT_SERVER_ADDRESS = "http://127.0.0.1:55440";

export const CONNECT_PROMPT =
  "Start the harness page server on your computer, then click Connect.";
// Every panel except Status has no Connect button, so its prompt names the
// panel that has one.
export const CONNECT_IN_STATUS_PROMPT =
  "Start the harness page server on your computer, then open the Status panel and click Connect.";
export const TOO_OLD_MESSAGE =
  "This harness server is too old for the hosted page. Use the local page that the server prints.";

// A browser withholds the status body of a server that sends no CORS header,
// which is every release up to v1.32. The page then cannot tell that server
// from a stopped one or from one the reader denied. The connection message
// therefore asserts no cause. It names the minimum release and the local
// route, and it asks about permission only as a condition. A readable response
// from an unsupported server still gets the exact version message.
export const MINIMUM_RELEASE_MESSAGE =
  "The hosted page requires harness server v1.33 or later.";
export const LOCAL_PAGE_ADVICE =
  "For an earlier release, open the local address that the server prints.";
export const PERMISSION_ADVICE =
  "If the browser asks for permission to connect to your computer, allow it.";

export function unreachableMessage(address: string): string {
  return `The page can't reach the harness server at ${address}. ${MINIMUM_RELEASE_MESSAGE} ${LOCAL_PAGE_ADVICE} ${PERMISSION_ADVICE}`;
}

// Release 1 is the first API release that reports the version field. A server
// without the field predates it and counts as older than the minimum.
export const MINIMUM_API_VERSION = 1;

declare global {
  interface RequestInit {
    // Chrome asks the user for loopback access, then needs this value before
    // an HTTPS page may reach http://127.0.0.1.
    targetAddressSpace?: "loopback" | "local" | "public" | "unknown";
  }
}

export function isHostedPage(
  loc: Location | undefined = typeof window === "undefined" ? undefined : window.location,
): boolean {
  return loc?.origin === HOSTED_ORIGIN;
}

// The address of the server this page talks to. The local page has none: it
// sends relative requests to the server that served it.
export function serverAddress(
  loc: Location | undefined = typeof window === "undefined" ? undefined : window.location,
): string | null {
  if (!isHostedPage(loc) || loc === undefined) {
    return null;
  }
  return addressFromSearch(loc.search);
}

// A reader may run the server on a port the server picked. `?port=NNNNN` names
// it. Anything else keeps the default port.
export function addressFromSearch(search: string): string {
  const port = new URLSearchParams(search).get("port");
  if (port === null || !/^\d{1,5}$/.test(port)) {
    return DEFAULT_SERVER_ADDRESS;
  }
  const number = Number(port);
  if (number < 1 || number > 65535) {
    return DEFAULT_SERVER_ADDRESS;
  }
  return `http://127.0.0.1:${number}`;
}

export function apiUrl(path: string, address: string | null): string {
  return address === null ? path : `${address}${path}`;
}

export function loopbackInit(address: string | null): RequestInit {
  const init: RequestInit = { headers: { Accept: "application/json" } };
  if (address !== null) {
    init.targetAddressSpace = "loopback";
  }
  return init;
}

export function apiVersionOf(payload: unknown): number {
  if (typeof payload !== "object" || payload === null) {
    return 0;
  }
  const data = payload as Record<string, unknown>;
  if (!("api_version" in data) || data["api_version"] === null) {
    return 0;
  }
  const version = data["api_version"];
  return typeof version === "number" && Number.isInteger(version) && version >= 1 ? version : 0;
}

export function versionSupported(payload: unknown): boolean {
  return apiVersionOf(payload) >= MINIMUM_API_VERSION;
}
