/**
 * The single React Query client.
 *
 * It lives in its own module so non-React code (the auth store's logout) can
 * clear it. A per-account cache that survives a logout is a data leak: the next
 * operator would see the previous operator's devices and audit entries until
 * the queries happened to refetch.
 */

import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The socket pushes changes; this interval is only the repair path for a
      // dropped event (kept short by each query, not globally).
      staleTime: 3_000,
      refetchOnWindowFocus: true,
      // Retry is decided by the server: `retryable` is part of the error
      // contract (spec §5.5). Network/timeout failures are retryable, 4xx are not.
      retry(failureCount, error) {
        if (error instanceof ApiError) return error.retryable && failureCount < 2;
        return false;
      },
    },
    mutations: {
      retry: false,
    },
  },
});