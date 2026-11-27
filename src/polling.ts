export type PollSettings = {
  intervalMs: number;
  error: unknown | null;
};

const DEFAULT_POLL_INTERVAL_MS = 1000;

export async function readPollSettings(
  request: () => Promise<unknown>,
): Promise<PollSettings> {
  try {
    const data = await request();
    const settings = data as { pollIntervalMs?: number };
    return {
      intervalMs: settings.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
      error: null,
    };
  } catch (error) {
    return { intervalMs: DEFAULT_POLL_INTERVAL_MS, error };
  }
}
