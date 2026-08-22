import { useState, type FormEvent } from 'react';
import type { CreateCrawlPayload } from '../types';

interface Props {
  disabled: boolean;
  defaultUrl?: string;
  onSubmit: (payload: CreateCrawlPayload) => void | Promise<void>;
}

const PROXY_PRESETS = [
  { label: 'None', value: '' },
  { label: 'HTTP 127.0.0.1:8080', value: 'http://127.0.0.1:8080' },
  { label: 'SOCKS5 127.0.0.1:1080', value: 'socks5://127.0.0.1:1080' },
];

export function CrawlForm({ disabled, defaultUrl = '', onSubmit }: Props) {
  const [url, setUrl] = useState(defaultUrl);
  const [concurrency, setConcurrency] = useState(16);
  const [fanoutCap, setFanoutCap] = useState(200);
  const [parser, setParser] = useState<'auto' | 'expat'>('auto');
  const [proxy, setProxy] = useState('');

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!url.trim()) return;
    onSubmit({
      url: url.trim(),
      concurrency,
      fanout_cap: fanoutCap,
      max_depth: 16,
      parser,
      proxy: proxy.trim() || undefined,
    });
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-800 p-3 shadow-sm space-y-2"
    >
      <div className="flex flex-col sm:flex-row gap-2">
        <input
          type="url"
          required
          inputMode="url"
          autoComplete="off"
          autoFocus
          placeholder="https://example.com/"
          value={url}
          disabled={disabled}
          onChange={(e) => setUrl(e.target.value)}
          className="field-input h-9 font-mono text-sm flex-1"
        />
        <input
          type="text"
          placeholder="proxy — http://… or socks5://…"
          value={proxy}
          disabled={disabled}
          onChange={(e) => setProxy(e.target.value)}
          list="proxy-presets"
          autoComplete="off"
          className="field-input h-9 font-mono text-sm sm:w-64"
        />
        <button
          type="submit"
          className="btn-primary h-9"
          disabled={disabled || !url.trim()}
        >
          {disabled ? <><Spinner /> running</> : 'Start crawl'}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-500">
        <Field
          label="concurrency"
          value={concurrency}
          min={1}
          max={512}
          disabled={disabled}
          onChange={setConcurrency}
        />
        <Field
          label="fanout"
          value={fanoutCap}
          min={1}
          max={10_000}
          disabled={disabled}
          onChange={setFanoutCap}
        />
        <label className="flex items-center gap-1">
          parser
          <select
            value={parser}
            disabled={disabled}
            onChange={(e) => setParser(e.target.value as 'auto' | 'expat')}
            className="field-input h-6 text-xs py-0"
          >
            <option value="auto">auto</option>
            <option value="expat">expat</option>
          </select>
        </label>
        <datalist id="proxy-presets">
          {PROXY_PRESETS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </datalist>
      </div>
    </form>
  );
}

function Field({
  label,
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-1">
      {label}
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value) || min)}
        className="field-input h-6 text-xs py-0 w-16"
      />
    </label>
  );
}

function Spinner() {
  return (
    <svg className="size-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="3"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}