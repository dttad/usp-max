export interface JobStatus {
  status:
    | 'queued'
    | 'running'
    | 'done'
    | 'error'
    | 'cancelled';
  urls_total: number;
  sitemaps_fetched: number;
  sitemaps_failed: number;
  elapsed_s: number;
}

export type CrawlStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'error'
  | 'cancelled';

export interface CrawlSettings {
  url: string;
  concurrency: number;
  fanout_cap: number;
  max_depth: number;
  parser: 'auto' | 'expat';
  proxy: string | null;
  user_agent: string;
}

export interface JobStats {
  elapsed_s: number;
  urls_total: number;
  sitemaps_fetched: number;
  sitemaps_failed: number;
  bytes_written: number;
  urls_per_s: number;
}

export interface Job {
  id: string;
  status: CrawlStatus;
  url: string;
  created_at: number;
  started_at?: number;
  ended_at?: number;
  urls_total: number;
  sitemaps_fetched: number;
  sitemaps_failed: number;
  bytes_written: number;
  elapsed_s: number;
  error: string | null;
  settings?: CrawlSettings;
}

export interface StreamEvent {
  type: string;
  ts?: number;
  [key: string]: unknown;
}

export interface CreateCrawlPayload {
  url: string;
  concurrency?: number;
  fanout_cap?: number;
  max_depth?: number;
  parser?: 'auto' | 'expat';
  proxy?: string;
}