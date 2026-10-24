import { useState } from "react";
export function useLocalStorage<T extends object>(
  key: string,
  initial: T,
): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored ? { ...initial, ...JSON.parse(stored) } : initial;
    } catch {
      return initial;
    }
  });
  const update = (next: T) => {
    setValue(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      /* Browser storage may be unavailable. */
    }
  };
  return [value, update];
}
