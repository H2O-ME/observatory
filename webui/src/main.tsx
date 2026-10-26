// Must be imported first: it patches APIs the watch's Chromium 81 is missing
// before any other module body runs.
import "./compat";

import React from "react";
import ReactDOM from "react-dom/client";
import { Theme } from "@radix-ui/themes";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ErrorBoundary } from "@/components/error-boundary";
import { queryClient } from "@/lib/query-client";
import "@radix-ui/themes/styles.css";
import "./index.css";
import "./mobile.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          {/* Radix Themes 提供组件基元与主题令牌。appearance="inherit" 让
              既有主题切换器（html.dark）继续生效；accent/gray/radius 与
              index.css 的 --c-* 令牌对齐（品牌蓝、石板灰、小圆角、高密度）。 */}
          <Theme accentColor="indigo" grayColor="slate" radius="small" scaling="90%" appearance="inherit">
            <App />
          </Theme>
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);