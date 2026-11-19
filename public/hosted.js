// The hosted page runs on GitHub Pages and calls a server on this computer.
// The local page sends every request to its own origin.
export const HOSTED_ORIGIN = "https://whitelicorice.github.io";
export const DEFAULT_SERVER_ADDRESS = "http://127.0.0.1:55440";
export const CONNECT_PROMPT = "Start the harness page server on your computer, then click Connect.";
export const TOO_OLD_MESSAGE = "This harness server is too old for the hosted page. Use the local page that the server prints.";
export function unreachableMessage(address) {
    return `The page can't reach the harness server at ${address}. Start it, allow the browser's local network prompt, then click Connect.`;
}
// Server release 1 is the oldest release that answers every endpoint. A server
// without the version field predates it and counts as older than the minimum.
export const MINIMUM_API_VERSION = 1;
export function isHostedPage(loc = typeof window === "undefined" ? undefined : window.location) {
    return loc?.origin === HOSTED_ORIGIN;
}
// The address of the server this page talks to. The local page has none: it
// sends relative requests to the server that served it.
export function serverAddress(loc = typeof window === "undefined" ? undefined : window.location) {
    if (!isHostedPage(loc) || loc === undefined) {
        return null;
    }
    return addressFromSearch(loc.search);
}
// A reader may run the server on a port the server picked. `?port=NNNNN` names
// it. Anything else keeps the default port.
export function addressFromSearch(search) {
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
export function apiUrl(path, address) {
    return address === null ? path : `${address}${path}`;
}
export function loopbackInit(address) {
    const init = { headers: { Accept: "application/json" } };
    if (address !== null) {
        init.targetAddressSpace = "loopback";
    }
    return init;
}
export function apiVersionOf(payload) {
    if (typeof payload !== "object" || payload === null) {
        return 0;
    }
    const data = payload;
    if (!("api_version" in data) || data["api_version"] === null) {
        return 0;
    }
    const version = data["api_version"];
    return typeof version === "number" && Number.isInteger(version) && version >= 1 ? version : 0;
}
export function versionSupported(payload) {
    return apiVersionOf(payload) >= MINIMUM_API_VERSION;
}
