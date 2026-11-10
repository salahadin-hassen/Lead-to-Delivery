export function Pagination({ total, limit, offset, onChange }: { total: number; limit: number; offset: number;
  onChange: (offset: number) => void }) {
  if (total <= limit) return null;
  const from = Math.min(offset + 1, total);
  const to = Math.min(offset + limit, total);
  return (
    <nav className="pagination" aria-label="Pagination">
      <span>{from}–{to} of {total}</span>
      <button type="button" className="button small" disabled={offset === 0}
              onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
      <button type="button" className="button small" disabled={offset + limit >= total}
              onClick={() => onChange(offset + limit)}>Next</button>
    </nav>
  );
}
