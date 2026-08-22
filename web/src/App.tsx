import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { CrawlForm } from './components/CrawlForm';
import { HistoryPanel } from './components/HistoryPanel';
import { LogPanel, type LogLine } from './components/LogPanel';
import { ResultPanel } from './components/ResultPanel';
import type {
  CreateCrawlPayload,
  CrawlStatus,
  Job,
  JobStats,
  StreamEvent,
} from './types';

const EMPTY_STATS: JobStats = {
  elapsed_s: 0,
  urls_total: 0,
  sitemaps_fetched: 0,
  sitemaps_failed: 0,
  bytes_written: 0,
  urls_per_s: 0,
};

export default function App() {
  const [version, setVersion] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [status, setStatus] = useState<CrawlStatus | 'idle'>('idle');
  const [stats, setStats] = useState<JobStats>(EMPTY_STATS);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const logIdRef = useRef(0);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    api
      .health()
      .then((h) => setVersion(h.version))
      .catch(() => setVersion(null));
  }, []);

  const closeStream = useCallback(() => {
    if (cleanupRef.current) {
      cleanupRef.current();
      cleanupRef.current = null;
    }
  }, []);

  useEffect(() => closeStream, [closeStream]);

  const handleEvent = useCallback(
    (evt: StreamEvent) => {
      switch (evt.type) {
        case 'snapshot': {
          const data = evt as unknown as {
            stats: JobStats;
            settings: { url: string };
            status: CrawlStatus;
          };
          setStats(data.stats);
          setStatus(data.status);
          break;
        }
        case 'started': {
          const data = evt as unknown as { settings: { url: string } };
          setActiveJob((prev) =>
            prev ? { ...prev, url: data.settings.url } : prev
          );
          setStatus('running');
          break;
        }
        case 'log': {
          const data = evt as unknown as {
            level: string;
            message: string;
            ts: number;
          };
          logIdRef.current += 1;
          setLogs((cur) => [
            ...cur,
            {
              id: logIdRef.current,
              level: data.level,
              message: data.message,
              ts: data.ts ?? Date.now() / 1000,
            },
          ]);
          break;
        }
        case 'progress': {
          const data = evt as unknown as Partial<JobStats>;
          setStats((cur) => ({ ...cur, ...data }));
          break;
        }
        case 'done':
        case 'error':
        case 'cancelled': {
          const data = evt as unknown as Partial<JobStats> & {
            error?: string | null;
          };
          setStats((cur) => ({ ...cur, ...data }));
          setStatus(evt.type as CrawlStatus);
          if (data.error) setError(data.error);
          closeStream();
          setRefreshKey((k) => k + 1);
          break;
        }
        case 'heartbeat': {
          const data = evt as unknown as { stats: JobStats };
          if (data.stats) setStats((cur) => ({ ...cur, ...data.stats }));
          break;
        }
        case 'connection-error':
          break;
        default:
          break;
      }
    },
    [closeStream],
  );

  const startCrawl = useCallback(
    async (payload: CreateCrawlPayload) => {
      setError(null);
      setLogs([]);
      setStats(EMPTY_STATS);
      setStatus('queued');
      closeStream();
      try {
        const job = await api.startCrawl(payload);
        setActiveJob(job);
        setStatus('running');
        cleanupRef.current = api.openStream(job.id, handleEvent);
        setRefreshKey((k) => k + 1);
      } catch (ex) {
        setError((ex as Error).message);
        setStatus('idle');
        setActiveJob(null);
      }
    },
    [closeStream, handleEvent],
  );

  const cancel = useCallback(async () => {
    if (!activeJob) return;
    try {
      await api.cancelJob(activeJob.id);
    } catch (ex) {
      setError((ex as Error).message);
    }
  }, [activeJob]);

  const clearLogs = useCallback(() => setLogs([]), []);

  // Click a row in history → load it as the active job (no live logs).
  const selectFromHistory = useCallback(
    (job: Job) => {
      closeStream();
      setActiveJob(job);
      setStatus(job.status);
      setError(job.error);
      setLogs([]);
      setStats({
        elapsed_s: job.elapsed_s ?? 0,
        urls_total: job.urls_total,
        sitemaps_fetched: job.sitemaps_fetched,
        sitemaps_failed: job.sitemaps_failed,
        bytes_written: job.bytes_written,
        urls_per_s:
          job.urls_total / (job.elapsed_s || 1),
      });
    },
    [closeStream],
  );

  const running = status === 'running' || status === 'queued';

  return (
    <div className="min-h-full flex flex-col">
      <Header version={version} />
      <main className="flex-1 max-w-5xl w-full mx-auto px-4 sm:px-6 py-4 space-y-3">
        {error && (
          <div className="rounded-md border border-rose-300 bg-rose-50 dark:bg-rose-900/30 dark:border-rose-700 text-rose-800 dark:text-rose-200 px-3 py-1.5 text-xs">
            <strong>Error:</strong> {error}
          </div>
        )}

        <CrawlForm disabled={running} onSubmit={startCrawl} />

        <ResultPanel
          activeJob={activeJob}
          onCancel={cancel}
          canCancel={running}
          onClear={clearLogs}
        />

        <LogPanel
          lines={logs}
          status={status}
          stats={stats}
          jobId={activeJob?.id ?? null}
          onClear={clearLogs}
        />

        <HistoryPanel
          activeId={activeJob?.id ?? null}
          onSelect={selectFromHistory}
          refreshKey={refreshKey}
        />
      </main>
      <Footer />
    </div>
  );
}

function Header({ version }: { version: string | null }) {
  return (
    <header className="border-b border-ink-200 dark:border-ink-700 bg-white/80 dark:bg-ink-800/80 backdrop-blur sticky top-0 z-10">
      <div className="max-w-5xl w-full mx-auto px-4 sm:px-6 py-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="size-6" />
          <h1 className="text-sm font-semibold tracking-tight">usp-max</h1>
          <span className="text-xs text-ink-500 dark:text-ink-400">
            paste a URL, watch it work
          </span>
        </div>
        {version && (
          <span className="text-xs text-ink-500 dark:text-ink-400 font-mono">
            v{version}
          </span>
        )}
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-ink-200 dark:border-ink-700 text-center text-[11px] text-ink-500 dark:text-ink-400 py-2">
      usp-max — GPL-3.0-or-later · drop-in fork of{' '}
      <a
        className="underline hover:text-ink-700 dark:hover:text-ink-200"
        href="https://github.com/GateNLP/ultimate-sitemap-parser"
        target="_blank"
        rel="noreferrer"
      >
        ultimate-sitemap-parser
      </a>
    </footer>
  );
}