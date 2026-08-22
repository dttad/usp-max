import { useEffect, useState } from 'react';
import { api } from '../api';
import type { CrawlStatus, Job } from '../types';

interface Props {
  activeId: string | null;
  onSelect: (job: Job) => void;
  refreshKey: number;
}

const STATUS_FILTERS: Array<{ key: CrawlStatus | 'all'; label: string }> = [
  { key: 'all', label: 'all' },
  { key: 'done', label: 'ok' },
  { key: 'error', label: 'err' },
  { key: 'cancelled', label: 'cancel' },
  { key: 'running', label: 'live' },
];

export function HistoryPanel({ activeId, onSelect, refreshKey }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<CrawlStatus | 'all'>('all');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .listJobs(search || undefined, 200)
      .then((rows) => {
        if (!alive) return;
        const filtered = statusFilter === 'all'
          ? rows
          : rows.filter((r) => r.status === statusFilter);
        setJobs(filtered);
      })
      .catch(() => alive && setJobs([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [search, statusFilter, refreshKey]);

  return (
    <section className="rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-800 overflow-hidden">
      <header className="flex items-center gap-2 px-3 py-2 border-b border-ink-200 dark:border-ink-700 bg-ink-50/60 dark:bg-ink-900/40">
        <span className="field-label">History</span>
        <span className="text-xs text-ink-500">{jobs.length}</span>
        <div className="ml-2 flex-1">
          <input
            type="search"
            placeholder="filter by URL…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="field-input h-7 text-xs py-0.5"
          />
        </div>
        <div className="flex items-center gap-1 text-xs">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setStatusFilter(f.key)}
              className={`rounded-md px-1.5 py-0.5 transition ${
                statusFilter === f.key
                  ? 'bg-emerald-600 text-white'
                  : 'text-ink-500 hover:bg-ink-100 dark:hover:bg-ink-700'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </header>
      <div className="max-h-72 overflow-auto">
        {loading && jobs.length === 0 ? (
          <div className="px-3 py-2 text-xs text-ink-500">loading…</div>
        ) : jobs.length === 0 ? (
          <div className="px-3 py-2 text-xs text-ink-500 italic">
            {search || statusFilter !== 'all'
              ? 'no matching crawls'
              : 'no crawls yet'}
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-ink-500 sticky top-0 bg-white dark:bg-ink-800">
              <tr className="border-b border-ink-100 dark:border-ink-700">
                <th className="text-left font-medium px-3 py-1 w-20">when</th>
                <th className="text-left font-medium px-1 py-1">URL</th>
                <th className="text-right font-medium px-2 py-1 w-20">URLs</th>
                <th className="text-right font-medium px-2 py-1 w-14">dur</th>
                <th className="text-right font-medium px-2 py-1 w-12">ok</th>
                <th className="text-right font-medium px-2 py-1 w-12">fail</th>
                <th className="text-left font-medium px-3 py-1 w-14">stat</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr
                  key={j.id}
                  onClick={() => onSelect(j)}
                  className={`cursor-pointer border-b border-ink-100 dark:border-ink-700/50 hover:bg-ink-50 dark:hover:bg-ink-700/40 ${
                    activeId === j.id ? 'bg-emerald-50 dark:bg-emerald-900/20' : ''
                  }`}
                >
                  <td className="px-3 py-1 text-ink-500 font-mono tabular-nums whitespace-nowrap">
                    {formatTime(j.created_at)}
                  </td>
                  <td className="px-1 py-1 truncate max-w-0">
                    <span className="font-mono">{truncateUrl(j.url)}</span>
                  </td>
                  <td className="px-2 py-1 text-right tabular-nums">
                    {j.urls_total.toLocaleString()}
                  </td>
                  <td className="px-2 py-1 text-right tabular-nums text-ink-500">
                    {j.elapsed_s ? `${j.elapsed_s.toFixed(1)}s` : '—'}
                  </td>
                  <td className="px-2 py-1 text-right tabular-nums text-emerald-600 dark:text-emerald-400">
                    {j.sitemaps_fetched || ''}
                  </td>
                  <td className="px-2 py-1 text-right tabular-nums text-rose-600 dark:text-rose-400">
                    {j.sitemaps_failed ? j.sitemaps_failed : ''}
                  </td>
                  <td className="px-3 py-1">
                    <StatusPill status={j.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function StatusPill({ status }: { status: CrawlStatus }) {
  const palette: Record<CrawlStatus, string> = {
    queued: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-200',
    running:
      'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-200',
    done: 'bg-emerald-200 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-100',
    error: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-200',
    cancelled:
      'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-200',
  };
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${palette[status]}`}
    >
      {status === 'done' ? 'ok' : status === 'cancelled' ? 'cancel' : status}
    </span>
  );
}

function truncateUrl(u: string): string {
  // Strip protocol for compactness; cap at 64 chars.
  const stripped = u.replace(/^https?:\/\//, '');
  return stripped.length <= 64 ? stripped : `${stripped.slice(0, 60)}…`;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return `${h}:${m}`;
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${mo}-${day} ${h}:${m}`;
}