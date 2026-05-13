// ─── ZoneCard ────────────────────────────────────────────────────────────────
import React from "react";
import { AlertTriangle, Thermometer, Wind, Droplets, Users, Zap } from "lucide-react";

const SENSORS = [
  { key:"temperature", label:"TEMP",  unit:"°C",  Icon:Thermometer, warn:v=>v>26||v<18 },
  { key:"co2",         label:"CO₂",   unit:"ppm", Icon:Wind,        warn:v=>v>1000 },
  { key:"humidity",    label:"RH",    unit:"%",   Icon:Droplets,    warn:v=>v>65||v<30 },
  { key:"occupancy",   label:"OCC",   unit:"ppl", Icon:Users,       warn:()=>false },
  { key:"energy_kw",   label:"POWER", unit:"kW",  Icon:Zap,         warn:v=>v>18 },
];

export function ZoneCard({ zone, readings, setpoint }) {
  const anyWarn = SENSORS.some(s => {
    const v = readings?.[s.key];
    return v !== undefined && s.warn(v);
  });

  return (
    <div style={{
      background:"var(--bg-card)", borderRadius:"var(--r)",
      border:`1px solid ${anyWarn?"var(--red)":"var(--border)"}`,
      padding:"14px 16px", position:"relative", overflow:"hidden",
      transition:"border-color var(--t)",
    }}>
      <div style={{ position:"absolute", top:0, left:0, width:3, height:"100%",
        background: anyWarn?"var(--red)":"var(--amber)", transition:"background var(--t)" }} />

      {/* Header */}
      <div style={{ paddingLeft:10, marginBottom:12, display:"flex", justifyContent:"space-between" }}>
        <div>
          <div style={{ fontWeight:700, fontSize:12, letterSpacing:"0.12em", textTransform:"uppercase" }}>
            {zone.zone_name}
          </div>
          <div style={{ fontFamily:"var(--font-mono)", fontSize:9, color:"var(--text-muted)", marginTop:2 }}>
            FLOOR {zone.floor} · CAP {zone.capacity} · SP {setpoint?.toFixed(1)||"24.0"}°C
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:6 }}>
          {anyWarn && <AlertTriangle size={12} color="var(--red)" className="blink" />}
          <div style={{ width:7, height:7, borderRadius:"50%",
            background: readings && Object.keys(readings).length > 0 ? "var(--green)" : "var(--text-muted)",
            animation: "glow-green 2.5s ease infinite",
          }} />
        </div>
      </div>

      {/* Grid */}
      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"10px 14px", paddingLeft:10 }}>
        {SENSORS.map(({ key, label, unit, Icon, warn }) => {
          const val = readings?.[key];
          const w   = val !== undefined && warn(val);
          return (
            <div key={key}>
              <div style={{ display:"flex", alignItems:"center", gap:4, marginBottom:2 }}>
                <Icon size={9} color={w?"var(--red)":"var(--text-muted)"} />
                <span style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-muted)",
                  letterSpacing:"0.1em", textTransform:"uppercase" }}>{label}</span>
              </div>
              <div style={{ fontFamily:"var(--font-mono)", fontSize:20,
                color: w?"var(--red)":"var(--amber)", lineHeight:1, transition:"color var(--t)" }}>
                {val !== undefined
                  ? (Number.isInteger(val) ? val : val.toFixed(1))
                  : <span style={{ color:"var(--text-muted)", fontSize:13 }}>—</span>}
                <span style={{ fontSize:8, color:"var(--text-muted)", marginLeft:3 }}>{unit}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Occupancy bar */}
      {readings?.occupancy !== undefined && (
        <div style={{ paddingLeft:10, marginTop:12 }}>
          <div style={{ display:"flex", justifyContent:"space-between", marginBottom:3 }}>
            <span style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-muted)" }}>OCCUPANCY LOAD</span>
            <span style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--amber)" }}>
              {Math.round(readings.occupancy/zone.capacity*100)}%
            </span>
          </div>
          <div style={{ height:3, background:"var(--border)", borderRadius:2, overflow:"hidden" }}>
            <div style={{ height:"100%", borderRadius:2, transition:"width .5s ease",
              width:`${Math.min(100, readings.occupancy/zone.capacity*100)}%`,
              background: readings.occupancy/zone.capacity > 0.85 ? "var(--red)" : "var(--amber)",
            }} />
          </div>
        </div>
      )}
    </div>
  );
}


// ─── SensorChart ─────────────────────────────────────────────────────────────
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ResponsiveContainer } from "recharts";

const ZONE_COLORS = { zone_1:"#f0a500", zone_2:"#00e676", zone_3:"#40c4ff", zone_4:"#b388ff" };
const REFS = { temperature:{ y:26, label:"26°C" }, co2:{ y:1000, label:"1000 ppm" } };

function fmtTime(ts) {
  return new Date(ts).toLocaleTimeString("en-US", { hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit" });
}

export function SensorChart({ sensorType, history, unit }) {
  const zoneIds = [];
  const map = {};
  for (const key of Object.keys(history)) {
    const [zid, st] = key.split("__");
    if (st !== sensorType) continue;
    if (!zoneIds.includes(zid)) zoneIds.push(zid);
    for (const { t, v } of history[key]) {
      if (!map[t]) map[t] = { t };
      map[t][zid] = v;
    }
  }
  const data = Object.values(map).sort((a, b) => a.t - b.t);
  const ref  = REFS[sensorType];

  const TT = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null;
    return (
      <div style={{ background:"var(--bg-panel)", border:"1px solid var(--border-hi)",
        borderRadius:"var(--r)", padding:"8px 12px", fontFamily:"var(--font-mono)", fontSize:10 }}>
        <div style={{ color:"var(--text-muted)", marginBottom:4 }}>{fmtTime(label)}</div>
        {payload.map(p => (
          <div key={p.dataKey} style={{ color:p.stroke, marginBottom:2 }}>
            {p.dataKey}: {p.value?.toFixed(1)} {unit}
          </div>
        ))}
      </div>
    );
  };

  return (
    <ResponsiveContainer width="100%" height={170}>
      <LineChart data={data} margin={{ top:4, right:10, left:-10, bottom:0 }}>
        <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="t" tickFormatter={fmtTime}
          tick={{ fontFamily:"var(--font-mono)", fontSize:8, fill:"var(--text-muted)" }}
          tickLine={false} axisLine={{ stroke:"var(--border)" }} minTickGap={60} />
        <YAxis tick={{ fontFamily:"var(--font-mono)", fontSize:8, fill:"var(--text-muted)" }}
          tickLine={false} axisLine={false} width={32} tickFormatter={v=>v.toFixed(0)} />
        <Tooltip content={<TT />} />
        {ref && <ReferenceLine y={ref.y} stroke="var(--red)" strokeDasharray="4 3" strokeOpacity={0.6}
          label={{ value:ref.label, position:"insideTopRight", fontSize:8, fill:"var(--red)", fontFamily:"var(--font-mono)" }} />}
        {zoneIds.map(zid => (
          <Line key={zid} type="monotone" dataKey={zid} stroke={ZONE_COLORS[zid]||"#fff"}
            strokeWidth={1.5} dot={false} activeDot={{ r:3, strokeWidth:0 }} connectNulls />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}


// ─── DRPanel ─────────────────────────────────────────────────────────────────
import { useState } from "react";
import { Zap as ZapIcon, CheckCircle, XCircle, Loader } from "lucide-react";

const API = import.meta.env.VITE_API_URL || "/api";

export function DRPanel({ onEventCreated }) {
  const [targetKw, setTargetKw]   = useState(8);
  const [duration, setDuration]   = useState(15);
  const [status, setStatus]       = useState("idle"); // idle | loading | success | error
  const [result, setResult]       = useState(null);
  const [error, setError]         = useState("");

  async function trigger() {
    setStatus("loading"); setError(""); setResult(null);
    try {
      const resp = await fetch(`${API}/dr/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_kw_reduction: targetKw, duration_minutes: duration, triggered_by: "dashboard" }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Request failed");
      setResult(data); setStatus("success");
      onEventCreated?.(data);
    } catch (e) {
      setError(e.message); setStatus("error");
    }
  }

  const inp = (label, value, setValue, min, max, step=1) => (
    <div style={{ flex:1 }}>
      <div style={{ fontFamily:"var(--font-mono)", fontSize:9, color:"var(--text-muted)",
        letterSpacing:"0.1em", marginBottom:5 }}>{label}</div>
      <div style={{ display:"flex", alignItems:"center", gap:6 }}>
        <input type="number" value={value} min={min} max={max} step={step}
          onChange={e => setValue(Number(e.target.value))}
          style={{ width:"100%", background:"var(--bg)", border:"1px solid var(--border-hi)",
            borderRadius:"var(--r)", padding:"6px 10px", color:"var(--amber)",
            fontFamily:"var(--font-mono)", fontSize:16, outline:"none" }} />
      </div>
    </div>
  );

  return (
    <div style={{ background:"var(--bg-card)", border:"1px solid var(--border)", borderRadius:"var(--r)", padding:"16px" }}>
      <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:14 }}>
        <ZapIcon size={13} color="var(--amber)" />
        <span style={{ fontFamily:"var(--font-mono)", fontSize:10, letterSpacing:"0.14em",
          textTransform:"uppercase", color:"var(--text-sec)" }}>Demand Response Trigger</span>
      </div>

      <div style={{ display:"flex", gap:12, marginBottom:14 }}>
        {inp("TARGET REDUCTION (kW)", targetKw, setTargetKw, 1, 50, 0.5)}
        {inp("DURATION (min)", duration, setDuration, 5, 120)}
      </div>

      <button onClick={trigger} disabled={status==="loading"}
        style={{ width:"100%", padding:"10px", background: status==="loading"?"var(--bg)":"var(--amber)",
          color:"var(--bg)", border:"none", borderRadius:"var(--r)", cursor: status==="loading"?"not-allowed":"pointer",
          fontFamily:"var(--font-mono)", fontWeight:600, fontSize:11, letterSpacing:"0.12em",
          textTransform:"uppercase", transition:"background var(--t)", opacity: status==="loading"?0.5:1 }}>
        {status==="loading" ? "Sending DR Signal…" : "Dispatch DR Event"}
      </button>

      {status==="success" && result && (
        <div className="fade-up" style={{ marginTop:12, background:"var(--green-dim)",
          border:"1px solid var(--green)", borderRadius:"var(--r)", padding:"10px 12px" }}>
          <div style={{ display:"flex", alignItems:"center", gap:6, marginBottom:6 }}>
            <CheckCircle size={11} color="var(--green)" />
            <span style={{ fontFamily:"var(--font-mono)", fontSize:9, color:"var(--green)", letterSpacing:"0.1em" }}>
              DR EVENT ACTIVE · {result.event_id?.slice(0,8)}…
            </span>
          </div>
          <div style={{ fontFamily:"var(--font-mono)", fontSize:10, color:"var(--text-sec)", lineHeight:1.8 }}>
            Target: {result.target_kw} kW &nbsp;·&nbsp;
            Projected shed: <span style={{ color:"var(--green)" }}>{result.projected_kw_shed?.toFixed(2)} kW</span>
            &nbsp;·&nbsp; Zones: {result.zones_affected?.length}
            &nbsp;·&nbsp; Duration: {result.duration_minutes} min
          </div>
          {result.zone_actions?.filter(a=>a.setpoint_delta_c>0).map(a => (
            <div key={a.zone_id} style={{ fontFamily:"var(--font-mono)", fontSize:9, color:"var(--text-muted)", marginTop:4 }}>
              › {a.zone_name}: +{a.setpoint_delta_c}°C setpoint · {a.kw_shed?.toFixed(2)} kW shed
            </div>
          ))}
        </div>
      )}

      {status==="error" && (
        <div className="fade-up" style={{ marginTop:12, background:"var(--red-dim)",
          border:"1px solid var(--red)", borderRadius:"var(--r)", padding:"10px 12px",
          display:"flex", alignItems:"center", gap:8 }}>
          <XCircle size={11} color="var(--red)" />
          <span style={{ fontFamily:"var(--font-mono)", fontSize:10, color:"var(--red)" }}>{error}</span>
        </div>
      )}
    </div>
  );
}


// ─── AuditLog ────────────────────────────────────────────────────────────────
const SEV_COLORS = { info:"var(--text-sec)", warn:"var(--amber)", error:"var(--red)" };
const EVT_COLORS = { dr_triggered:"var(--green)", dr_completed:"var(--cyan)", sensor_anomaly:"var(--red)",
  comfort_violation:"var(--red)", setpoint_write:"var(--amber)", system_start:"var(--text-muted)" };

export function AuditLog({ entries }) {
  return (
    <div style={{ height:200, overflowY:"auto", fontFamily:"var(--font-mono)", fontSize:10, lineHeight:1.9 }}>
      {entries.length === 0 && (
        <span style={{ color:"var(--text-muted)" }}>Waiting for events<span className="blink">_</span></span>
      )}
      {entries.map((e, i) => (
        <div key={i} className="fade-up" style={{ display:"flex", gap:10, borderBottom:"1px solid var(--border)", paddingBottom:2 }}>
          <span style={{ color:"var(--text-muted)", minWidth:80, flexShrink:0 }}>
            {new Date(e.timestamp).toLocaleTimeString("en-US",{hour12:false})}
          </span>
          <span style={{ color:EVT_COLORS[e.event_type]||"var(--text-sec)", minWidth:110, flexShrink:0 }}>
            {e.event_type}
          </span>
          <span style={{ color:SEV_COLORS[e.severity]||"var(--text)", fontSize:9 }}>{e.message}</span>
        </div>
      ))}
    </div>
  );
}


// ─── PredictionPanel ─────────────────────────────────────────────────────────
export function PredictionPanel({ predictions }) {
  if (!predictions || !Object.keys(predictions).length) return null;
  return (
    <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:8 }}>
      {Object.entries(predictions).map(([zid, p]) => (
        <div key={zid} style={{ background:"var(--bg-card)", border:"1px solid var(--border)",
          borderRadius:"var(--r)", padding:"10px 12px" }}>
          <div style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-muted)",
            letterSpacing:"0.1em", marginBottom:4 }}>{zid.toUpperCase()}</div>
          <div style={{ fontFamily:"var(--font-mono)", fontSize:18,
            color: p.is_low_occupancy ? "var(--green)" : "var(--amber)" }}>
            {p.predicted_occupancy?.toFixed(0)}
            <span style={{ fontSize:8, color:"var(--text-muted)", marginLeft:3 }}>ppl in 30min</span>
          </div>
          <div style={{ marginTop:4, height:3, background:"var(--border)", borderRadius:2 }}>
            <div style={{ height:"100%", borderRadius:2,
              width:`${Math.min(100, p.occupancy_ratio*100)}%`,
              background: p.is_low_occupancy ? "var(--green)" : "var(--amber)" }} />
          </div>
          {p.is_low_occupancy && (
            <div style={{ fontFamily:"var(--font-mono)", fontSize:8, color:"var(--green)", marginTop:3 }}>
              ✓ DR candidate
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
