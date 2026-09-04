"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * Track a media query in JS.
 *
 * Layout itself stays in CSS; this exists for the parts that cannot be
 * expressed there — chiefly whether the incident drawer is currently a modal
 * overlay (narrow screens, so focus should be trapped) or a panel beside the
 * content (wide screens, where trapping focus would strand the keyboard).
 *
 * `useSyncExternalStore` is the right shape for this: `matchMedia` is an
 * external store, and the server snapshot is `false` so the first client
 * render matches the server's before the real value arrives.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onStoreChange);
      return () => list.removeEventListener("change", onStoreChange);
    },
    [query],
  );

  const getSnapshot = useCallback(() => window.matchMedia(query).matches, [query]);

  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
