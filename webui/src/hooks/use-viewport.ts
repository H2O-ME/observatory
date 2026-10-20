import { useEffect, useState } from "react";

export type Viewport = "watch" | "mobile" | "desktop";

function measure(): Viewport {
  const width = typeof window === "undefined" ? 1280 : window.innerWidth;
  if (width <= 380) return "watch";
  if (width <= 860) return "mobile";
  return "desktop";
}

/**
 * Coarse viewport class.
 *
 * The watch breakpoint exists because those screens are ~320-360 CSS pixels wide
 * and every extra column makes text unreadable. Layout decisions key off this
 * instead of duplicating media queries in twenty components.
 */
export function useViewport(): Viewport {
  const [viewport, setViewport] = useState<Viewport>(measure);

  useEffect(() => {
    let frame = 0;
    const onResize = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        setViewport(measure());
      });
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  return viewport;
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    const list = window.matchMedia(query);
    const handler = () => setMatches(list.matches);
    handler();
    list.addEventListener("change", handler);
    return () => list.removeEventListener("change", handler);
  }, [query]);

  return matches;
}