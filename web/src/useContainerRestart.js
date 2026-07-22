import { useState } from "react";
import { api, ApiError } from "./api.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Shared "restart the NAS containers" flow (the picker's Rescan button and the
// Machines page both use it). The server dies with the restart, so after
// firing the request we wait out the outage and poll /health until the
// coordinator is back, then invoke onBack so the caller can refresh whatever
// it was showing. A structured API error (no watcher, copy running, bad
// token) means nothing restarted — surface it and stop; a dropped connection
// means the restart beat our response out the door — keep waiting.
export function useContainerRestart(onBack) {
  const [restarting, setRestarting] = useState(false);
  const [message, setMessage] = useState(null); // {ok, text}

  const restart = async () => {
    if (
      !window.confirm(
        "Restart the NAS containers to re-scan cards?\n" +
          "The server goes away for ~10–20 seconds; running NAS copies are blocked."
      )
    )
      return;
    setRestarting(true);
    setMessage(null);
    try {
      await api.restartContainers();
    } catch (err) {
      if (err instanceof ApiError) {
        setMessage({ ok: false, text: err.message });
        setRestarting(false);
        return;
      }
    }
    await sleep(5000);
    const deadline = Date.now() + 90000;
    let back = false;
    while (Date.now() < deadline) {
      try {
        await api.health();
        back = true;
        break;
      } catch {
        await sleep(2000);
      }
    }
    setMessage(
      back
        ? { ok: true, text: "Containers restarted — back online." }
        : {
            ok: false,
            text: "The server hasn't come back yet — give it a moment and refresh.",
          }
    );
    setRestarting(false);
    if (back) onBack?.();
  };

  return { restarting, message, restart };
}
