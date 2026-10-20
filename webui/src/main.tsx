// Must be imported first: it patches APIs the watch's Chromium 81 is missing
// before any other module body runs.
import "./compat";

import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./lib/api";
import "./index.css";
import "./mobile.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Device state is pushed over the WebSocket; this interval is only the
      // fallback that repairs the page after a dropped event.
      staleTime: 3_000,
      refetchOnWindowFocus: true,
      retry(failureCount, error) {
        // A 4xx will not become a 2xx by asking again.
        if (error instanceof ApiError && error.status < 500) return false;
        return failureCount < 2;
      },
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);