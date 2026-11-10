import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from './api/client';

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/** Load data from the API; reloads when `key` changes. Ignores stale responses. */
export function useResource<T>(loader: () => Promise<T>, key: string): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    loaderRef.current()
      .then((value) => { if (current) setData(value); })
      .catch((err: unknown) => {
        if (current) setError(err instanceof ApiError ? err : new ApiError(0, 'Unexpected error.'));
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [key, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
