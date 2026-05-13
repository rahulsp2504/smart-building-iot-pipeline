// useWebSocket.js — auto-reconnecting WS with per-zone rolling history
import { useCallback, useEffect, useRef, useState } from "react";

const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.host}/ws`;
const MAX_HISTORY = 80;

export function useWebSocket(onMessage) {
  const wsRef  = useRef(null);
  const timer  = useRef(null);
  const alive  = useRef(true);
  const [ready, setReady]   = useState(false);
  const [count, setCount]   = useState(0);

  const connect = useCallback(() => {
    if (!alive.current) return;
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen    = () => { if (alive.current) { setReady(true); setCount(c => c + 1); } };
    ws.onmessage = (e) => { if (alive.current) try { onMessage(JSON.parse(e.data)); } catch(_){} };
    ws.onclose   = () => { if (alive.current) { setReady(false); timer.current = setTimeout(connect, 3000); } };
    ws.onerror   = () => ws.close();
  }, [onMessage]);

  useEffect(() => {
    alive.current = true;
    connect();
    return () => { alive.current = false; clearTimeout(timer.current); wsRef.current?.close(); };
  }, [connect]);

  return { ready, count };
}

export function mergeHistory(prev, msg) {
  const key = `${msg.zone_id}__${msg.sensor_type}`;
  const arr  = prev[key] || [];
  return { ...prev, [key]: [...arr, { t: new Date(msg.timestamp).getTime(), v: msg.value }].slice(-MAX_HISTORY) };
}

export function latestPerZone(history) {
  const zones = {};
  for (const key of Object.keys(history)) {
    const [zone_id, sensor_type] = key.split("__");
    if (!zones[zone_id]) zones[zone_id] = {};
    const arr = history[key];
    if (arr.length) zones[zone_id][sensor_type] = arr[arr.length - 1].v;
  }
  return zones;
}
