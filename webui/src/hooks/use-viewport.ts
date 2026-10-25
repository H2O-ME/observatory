import { useEffect, useState } from "react";

export type Viewport = "watch" | "mobile" | "desktop";

function measure(): Viewport {
  if (typeof window === "undefined") return "desktop";
  const width = window.innerWidth;
  const height = window.innerHeight;

  // Square-ish tiny screens are watches (Wear OS: 320×320, 384×384, 454×454).
  // A 390×844 phone must never fall into this branch.
  const squareWatch = width <= 480 && height <= 560;
  if (squareWatch || width <= 340) return "watch";
  if (width <= 900) return "mobile";
  return "desktop";
}

/**
 * Coarse viewport class.
 *
 * Layout decisions that differ structurally (navigation variant, column count,
 * density) key off this; ordinary responsive layout still uses CSS media
 * queries. The watch branch is deliberately square-screen-aware so a portrait
 * phone is never mistaken for a watch.
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
