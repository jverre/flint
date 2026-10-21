// Keyboard model: one global listener, pages register page keys with usePageKeys().
// Rules (DESIGN-SYSTEM.md §5): navigation keys only — never bind kill/pause/checkpoint/resize.
import { useEffect, useRef } from "react";

export type KeyMap = Record<string, (e: KeyboardEvent) => void>;
export interface KeyHint { k: string; label: string }

const stack: { map: React.MutableRefObject<KeyMap> }[] = [];

export function keyName(e: KeyboardEvent): string {
  const base = e.key === " " ? "space" : e.key;
  return (e.ctrlKey || e.metaKey ? "mod+" : "") + base;
}

export const isTyping = (t: EventTarget | null) =>
  t instanceof Element && !!t.closest("input,textarea,select,[contenteditable],.xterm,[role=dialog]");

/** Register keys for the current page; the latest registration wins on conflicts. */
export function usePageKeys(map: KeyMap) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    const entry = { map: ref };
    stack.push(entry);
    return () => { const i = stack.indexOf(entry); if (i >= 0) stack.splice(i, 1); };
  }, []);
}

/** Called by the shell's global listener after global keys were handled. */
export function dispatchPageKey(e: KeyboardEvent): boolean {
  const k = keyName(e);
  for (let i = stack.length - 1; i >= 0; i--) {
    const fn = stack[i].map.current[k];
    if (fn) { e.preventDefault(); fn(e); return true; }
  }
  return false;
}
