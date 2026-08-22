import { useEffect, useRef } from 'react';
import type { JobStats } from '../types';

interface Props {
  lines: LogLine[];
  status: string;
  stats: JobStats;
  jobId: string | null;
  onClear: () => void;
}

export interface LogLine {
  id: number;
  level: string;
  message: string;
  ts: number;
}

const MAX_RENDERED = 1000;

export function LogPanel({ lines, status, stats, jobId, onClear }: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  // Auto-scroll only when the user has not scrolled up.
  useEffect(() => {
    if (pinnedRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [lines]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - (el.scrollTop + el.clientHeight);
    pinnedRef.current = dist < 32;
  }

  const visible = lines.length > MAX_RENDERED ? lines.slice(-MAX_RENDERED) : lines;
  const trimmed = lines.length - visible.length;

  return (
    <section className="rounded-xl border border-ink-200 dark:border-ink-700 bg-ink-900 text-ink-100 shadow-sm overflow-hidden">
      <header className="flex items-center justify-between px-3 py-1.5 border-b border-ink-700 bg-ink-800/60">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-ink-400">
          <StatusDot status={status} />
          <span>{status}</span>
          {jobId && (
            <span className="font-mono normal-case text-ink-500">
              · {jobId}
            </span>
          )}
          <span className="font-mono normal-case text-ink-500 ml-2">
            {stats.urls_total.toLocaleString()} urls · {stats.urls_per_s.toFixed(1)} u/s
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onClear}
            className="btn-ghost text-ink-300 hover:text-white hover:bg-ink-700 text-xs"
            disabled={lines.length === 0}
          >
            Clear
          </button>
        </div>
      </header>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="font-mono text-[12px] leading-relaxed h-72 overflow-auto px-3 py-2"
      >
        {trimmed > 0 && (
          <div className="text-ink-500 italic">
              … {trimmed} earlier line(s) hidden …
            </div>
        )}
        {visible.length === 0 ? (
          <div className="text-ink-500 italic">
            Press "Start crawl" to begin. Logs will stream here in real time.
          </div>
        ) : (
          visible.map((l) => (
            <div key={l.id} className={`log-line ${levelClass(l.level)}`}>
              <span className="text-ink-500 select-none mr-2">
                {formatTime(l.ts)}
              </span>
              <span className="select-none mr-2 uppercase tracking-wide opacity-80">
                {pad(l.level, 5)}
              </span>
              {l.message}
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function StatusDot({ status }: { status: string }) {
  const color: Record<string, string> = {
    idle: 'bg-ink-500',
    queued: 'bg-sky-400 animate-pulse-soft',
    running: 'bg-emerald-400 animate-pulse-soft',
    done: 'bg-emerald-500',
    error: 'bg-rose-500',
    cancelled: 'bg-amber-500',
  };
  return <span className={`inline-block size-1.5 rounded-full ${color[status] ?? color.idle}`} />;
}

function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL':
      return 'text-rose-300';
    case 'WARNING':
      return 'text-amber-300';
    case 'INFO':
      return 'text-ink-100';
    case 'DEBUG':
      return 'text-ink-400';
    default:
      return 'text-ink-200';
  }
}

function pad(s: string, n: number): string {
  return s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length);
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
}