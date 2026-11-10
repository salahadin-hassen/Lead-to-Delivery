const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  // Plain dates (YYYY-MM-DD) are calendar dates: never shift them through a time zone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.split('-').map(Number);
    return dateFmt.format(new Date(y, m - 1, d));
  }
  return dateFmt.format(new Date(value));
}

export function formatDateTime(value: string | null | undefined): string {
  return value ? dateTimeFmt.format(new Date(value)) : '—';
}

export function humanize(value: string | null | undefined): string {
  if (!value) return '—';
  const text = value.replace(/[._]/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const JOB_LABELS: Record<string, string> = {
  'clickup.create_task': 'ClickUp task',
  'sheets.upsert_project': 'Google Sheets row',
  'slack.notify': 'Slack message',
};

/** Plain-language name for an integration job type. */
export function jobLabel(jobType: string): string {
  return JOB_LABELS[jobType] ?? humanize(jobType);
}

/** Job status as an administrator reads it ("dead" is an internal term). */
export function jobStatusLabel(status: string): string | undefined {
  return status === 'dead' ? 'Failed' : undefined;
}

export function money(cents: number | null, currency: string | null): string {
  if (cents === null || !currency) return '—';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(cents / 100);
}
