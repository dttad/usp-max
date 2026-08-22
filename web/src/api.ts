import type {
  CreateCrawlPayload,
  Job,
  StreamEvent,
} from './types';

const BASE = '/api';

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: string;
    try {
      detail = (await res.json()).detail ?? res.statusText;
    } catch {
      detail = res.statusText;
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  async health(): Promise<{ ok: boolean; version: string }> {
    const r = await fetch(`${BASE}/health`);
    return jsonOrThrow(r);
  },

  async startCrawl(payload: CreateCrawlPayload): Promise<Job> {
    const r = await fetch(`${BASE}/crawl`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return jsonOrThrow(r);
  },

  async listJobs(search?: string, limit?: number): Promise<Job[]> {
    const qs = new URLSearchParams();
    if (search) qs.set('search', search);
    if (limit) qs.set('limit', String(limit));
    const q = qs.toString();
    const r = await fetch(`${BASE}/jobs${q ? `?${q}` : ''}`);
    const data = await jsonOrThrow<{ jobs: Job[] }>(r);
    return data.jobs;
  },

  async getJob(id: string): Promise<Job> {
    const r = await fetch(`${BASE}/jobs/${id}`);
    return jsonOrThrow(r);
  },

  async cancelJob(id: string): Promise<{ id: string; cancelled: boolean }> {
    const r = await fetch(`${BASE}/jobs/${id}`, { method: 'DELETE' });
    return jsonOrThrow(r);
  },

  urlsUrl(id: string): string {
    return `${BASE}/jobs/${id}/urls`;
  },

  manifestUrl(id: string): string {
    return `${BASE}/jobs/${id}/manifest`;
  },

  /**
   * Open an SSE stream and invoke ``onEvent`` for each parsed event.
   * Returns a cleanup function that closes the underlying EventSource.
   */
  openStream(id: string, onEvent: (evt: StreamEvent) => void): () => void {
    const src = new EventSource(`${BASE}/jobs/${id}/stream`);
    src.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data) as StreamEvent;
        onEvent(evt);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('bad SSE payload', e.data, err);
      }
    };
    src.onerror = () => {
      // Browser will auto-reconnect on transient errors; we surface a
      // synthetic 'connection-error' event so the UI can show feedback.
      onEvent({ type: 'connection-error' });
    };
    return () => src.close();
  },
};