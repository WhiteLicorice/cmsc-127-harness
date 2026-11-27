const DEFAULT_POLL_INTERVAL_MS = 1000;
export async function readPollSettings(request) {
    try {
        const data = await request();
        const settings = data;
        return {
            intervalMs: settings.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
            error: null,
        };
    }
    catch (error) {
        return { intervalMs: DEFAULT_POLL_INTERVAL_MS, error };
    }
}
