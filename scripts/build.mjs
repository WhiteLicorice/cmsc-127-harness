import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const bundle = resolve(root, "dist", "bundle");
const publicRoot = resolve(bundle, "public");

await mkdir(resolve(publicRoot, "vendor"), { recursive: true });
await copyFile(resolve(root, "public", "index.html"), resolve(publicRoot, "index.html"));
await copyFile(resolve(root, "public", "style.css"), resolve(publicRoot, "style.css"));
await copyFile(resolve(root, "public", "vendor", "mermaid.min.js"), resolve(publicRoot, "vendor", "mermaid.min.js"));
await copyFile(resolve(root, "public", "vendor", "MERMAID-LICENSE.txt"), resolve(publicRoot, "vendor", "MERMAID-LICENSE.txt"));
await copyFile(resolve(root, "server.py"), resolve(bundle, "server.py"));
await copyFile(resolve(root, "comparator.py"), resolve(bundle, "comparator.py"));
await copyFile(resolve(root, "erd_parser.py"), resolve(bundle, "erd_parser.py"));
await copyFile(resolve(root, "start.sh"), resolve(bundle, "start.sh"));
console.log(`Built the bundle at ${bundle}`);
