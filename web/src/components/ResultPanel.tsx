import { useEffect, useState } from 'react';
import { api } from '../api';
import type { CrawlStatus, Job } from '../types';

interface Props {
  activeJob: Job | null;
  onCancel: () => void;
  canCancel: boolean;
  onClear: () => void;
}

export function ResultPanel({ activeJob, onCancel, canCancel, onClear }: Props) {
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const jobId = activeJob?.id ?? null;
  const status: CrawlStatus = activeJob?.status ?? 'done';

  useEffect(() => {
    if (!jobId) {
      setCount(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setError(null);

    async function refresh() {
      try {
        const r = await fetch(api.urlsUrl(jobId!), { cache: 'no-store' });
        if (!r.ok) {
          setCount(null);
          return;
        }
        const text = await r.text();
        if (cancelled) return;
        const n = text ? text.split('\n').filter(Boolean).length : 0;
        setCount(n);
      } catch (ex) {
        if (!cancelled) setError((ex as Error).message);
      }
    }

    void refresh();
    const id =
      status === 'running' || status === 'queued'
        ? window.setInterval(refresh, 1500)
        : null;
    return () => {
      cancelled = true;
      if (id) window.clearInterval(id);
    };
  }, [jobId, status]);

  if (!jobId) {
    return (
      <div className="text-xs text-ink-500 italic">
        Submit a URL to start. Results will appear here.
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-500">
      <span>
        <strong className="text-ink-700 dark:text-ink-200 tabular-nums">
          {count === null ? '…' : count.toLocaleString()}
        </strong>{' '}
        URLs
      </span>
      {activeJob?.error && (
        <span className="text-rose-600 dark:text-rose-300">
          {activeJob.error}
        </span>
      )}
      <div className="ml-auto flex items-center gap-3">
        <button
          type="button"
          onClick={onClear}
          className="hover:text-ink-700 dark:hover:text-ink-200"
        >
          clear log
        </button>
        {canCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="hover:text-ink-700 dark:hover:text-ink-200"
          >
            cancel
          </button>
        )}
        <a
          href={api.urlsUrl(jobId)}
          download={`urls-${jobId}.txt`}
          className="hover:text-ink-700 dark:hover:text-ink-200"
        >
          urls.txt
        </a>
        <a
          href={api.manifestUrl(jobId)}
          target="_blank"
          rel="noreferrer"
          className="hover:text-ink-700 dark:hover:text-ink-200"
        >
          manifest
        </a>
      </div>
      {error && (
        <span className="text-rose-600 dark:text-rose-300 w-full">{error}</span>
      )}
    </div>
  );
}