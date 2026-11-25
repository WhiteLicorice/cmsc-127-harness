import type { Server } from "node:http";

// Bind the server to a port that the operating system chooses, and return that
// port. The caller keeps the port for the whole suite. No test owns a port
// number, so a range that the operating system reserves cannot take one.
export async function listenOnFreePort(target: Server, name: string): Promise<number> {
  try {
    await new Promise<void>((ready, fail) => {
      const failed = (error: Error) => {
        target.off("listening", listened);
        fail(error);
      };
      const listened = () => {
        target.off("error", failed);
        ready();
      };
      target.once("error", failed);
      target.once("listening", listened);
      target.listen(0, "127.0.0.1");
    });
  } catch (error) {
    const failure = error as NodeJS.ErrnoException;
    throw new Error(
      `The ${name} test server could not bind a free port. ` +
        `Bind error: ${failure.code ?? failure.message}.`,
    );
  }
  const address = target.address();
  if (address === null || typeof address === "string") {
    throw new Error(`The ${name} test server did not report the port it bound.`);
  }
  return address.port;
}
