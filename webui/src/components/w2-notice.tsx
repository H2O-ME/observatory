/**
 * Placeholder for the views that move to the W2 front-end wave.
 *
 * W0 rewrites the client layer (api/clock/presence) and three representative
 * pages (wall, overview, live). The other views keep their route, their
 * capability gating and their place in the navigation, but their grid/list UI is
 * rebuilt in W2. Saying so is better than shipping a half-migrated view whose
 * numbers nobody can trust.
 */

import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  title: string;
  description: string;
  items?: string[];
  children?: ReactNode;
  testId?: string;
}

export function W2Notice({ title, description, items = [], children, testId }: Props) {
  return (
    <div className="space-y-3" data-testid={testId}>
      <Card className="rounded-panel border-line bg-panel">
        <CardHeader>
          <CardTitle className="text-sm text-ink">{title}</CardTitle>
          <CardDescription className="text-xs leading-relaxed text-soft">{description}</CardDescription>
        </CardHeader>
        {items.length > 0 ? (
          <CardContent>
            <ul className="space-y-1 text-[0.72rem] leading-relaxed text-soft">
              {items.map((item) => (
                <li key={item} className="flex gap-1.5">
                  <span className="text-faint">·</span>
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        ) : null}
      </Card>
      {children}
    </div>
  );
}