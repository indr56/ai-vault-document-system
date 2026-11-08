import { useEffect, useRef, useState } from "react";
import { eventsUrl } from "./api";
import type { VaultEvent } from "./types";

/** Subscribes to the backend's WebSocket (live processing / comparison updates). Reconnects with backoff. */
export function useVaultEvents(onEvent: (event: VaultEvent) => void): boolean {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let closed = false;

    const scheduleReconnect = () => {
      if (closed) return;
      timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
    };

    const connect = async () => {
      let url: string;
      try {
        url = await eventsUrl(); // fresh single-use ticket for every (re)connect
      } catch {
        return scheduleReconnect(); // API down or key invalid: try again later
      }
      if (closed) return;
      socket = new WebSocket(url);
      socket.onopen = () => {
        retry = 0;
        setConnected(true);
      };
      socket.onmessage = (msg) => {
        try {
          handler.current(JSON.parse(msg.data));
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        scheduleReconnect();
      };
    };
    void connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return connected;
}
