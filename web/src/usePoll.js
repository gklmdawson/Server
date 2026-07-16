import { useEffect, useRef, useState } from "react";

// Poll an async fetcher on an interval (LAN + 3 nodes: polling beats
// websockets on simplicity, per DESIGN.md). Keeps the last good payload on
// transient errors and surfaces the error alongside it.
export function usePoll(fetcher, intervalMs = 5000, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const timer = useRef(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let inFlight = false;

    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await fetcher();
        if (alive.current) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (alive.current) setError(err);
      } finally {
        inFlight = false;
      }
    };

    tick();
    timer.current = setInterval(tick, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const refresh = async () => {
    try {
      setData(await fetcher());
      setError(null);
    } catch (err) {
      setError(err);
    }
  };

  return { data, error, refresh };
}
