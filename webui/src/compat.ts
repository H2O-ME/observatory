/**
 * Runtime shims for the Android watch browser the console has to open in.
 *
 * That WebView is Chromium 81 (April 2020), which is missing a handful of APIs
 * that React, Base UI and TanStack Query call on ordinary code paths. Each
 * block is feature-detected, so on a current browser this module executes a few
 * `in` checks and nothing else.
 *
 * `WeakRef` deliberately has no shim: Base UI already feature-detects it and
 * falls back to a strong reference, so nothing is needed here.
 */

// Chromium 93. Called by Base UI's useRender and toast store on every render.
if (typeof Object.hasOwn !== "function") {
  Object.defineProperty(Object, "hasOwn", {
    value: function hasOwn(target: object, key: PropertyKey): boolean {
      if (target === null || target === undefined) {
        throw new TypeError("Cannot convert undefined or null to object");
      }
      return Object.prototype.hasOwnProperty.call(Object(target), key);
    },
    writable: true,
    configurable: true,
  });
}

// Chromium 98. TanStack Query clones Error instances when one is thrown.
if (typeof globalThis.structuredClone !== "function") {
  globalThis.structuredClone = function structuredClone<T>(value: T): T {
    if (value instanceof Error) {
      const copy = new Error(value.message);
      copy.name = value.name;
      copy.stack = value.stack;
      copy.cause = (value as Error & { cause?: unknown }).cause;
      return copy as unknown as T;
    }
    // Only reached for the Error branch above in practice; structuredClone of
    // arbitrary graphs is not used by this app.
    return value;
  };
}

// Chromium 92. Cheap, and a natural thing for a component to reach for.
if (typeof Array.prototype.at !== "function") {
  Object.defineProperty(Array.prototype, "at", {
    value: function at<T>(this: T[], index: number): T | undefined {
      const i = Math.trunc(index) || 0;
      return this[i < 0 ? this.length + i : i];
    },
    writable: true,
    configurable: true,
  });
}

// Chromium 84. MediaQueryList gained addEventListener late; the watch browser
// only has the deprecated addListener.
if (typeof window !== "undefined" && window.matchMedia) {
  type LegacyMQL = MediaQueryList & { addListener?: (cb: () => void) => void };
  const proto = window.matchMedia("(min-width: 0px)") as LegacyMQL;
  if (
    typeof proto.addEventListener !== "function" &&
    typeof proto.addListener === "function"
  ) {
    // defineProperty rather than assignment: lib.dom already declares the
    // modern overload, and the narrower legacy signature will not assign to it.
    Object.defineProperty(MediaQueryList.prototype, "addEventListener", {
      value: function (this: LegacyMQL, type: string, cb: () => void) {
        if (type === "change") this.addListener?.(cb);
      },
      writable: true,
      configurable: true,
    });
  }
}

export {};
