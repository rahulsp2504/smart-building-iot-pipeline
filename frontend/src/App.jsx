import React, { useCallback, useEffect, useReducer, useState } from "react";
import { Building2, Wifi, WifiOff, Activity, Zap, BookOpen, Brain } from "lucide-react";

import { useWebSocket, mergeHistory, latestPerZone } from "./hooks/useWebSocket.js";
import { ZoneCard, SensorChart, DRPanel, AuditLog, PredictionPanel } from "./components/index.jsx";

const API = import.meta.env.VITE_API_URL || "/api";

const FALLBACK_ZONES = [
  { zone_id:"zone_1", zone_name:"Conference Room A", floor:1, capacity:20 },
  { zone_id:"zone_2", zone_name:"Open Office B",     floor:2, capacity:50 },
  { zone_id:"zone_3", zone_name:"Lab C",             floor:2, capacity:15 },
  { zone_id:"zone_4", zone_name:"Lobby",             floor:1, capacity:30 },
];

const CHARTS = [
  { key:"temperature", label:"Temperature",  unit:"°C"  },
  { key:"co2",         label:"CO₂",          unit:"ppm" },
  { key:"energy_kw",   label:"Energy Draw",  unit:"kW"  },
  { key:"humidity",    label:"Humidity",     unit:"%"   },
];

function Clock() {
  const [t, setT] = useState(new Date());
  useEffect(() => { const id = setInterval(() => setT(new Date()), 1000); return () => clearInterval(id); }, []);
  return (
    <span style={{ fontFamily:"var(--font-mono)", fontSize:11, color:"var(--text-sec)" }}>
      {t.toLocaleString("en-US",{ year:"numeric",month:"2-digit",day:"2-digit",
        hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false })}
    </span>
  );
}

export default function App() {
  const [zones, setZones]         = useState(FALLBACK_ZONES);
  const [history, dispatch]       = useReducer((s, m) => mergeHistory(s, m), {});
  const [audit, setAudit]         = useState([]);
  const [predictions, setPreds]   = useState({});
  const [setpoints, setSetpoints] = useState({});
  const [msgCount, setMsgCount]   = useState(0);
  const [totalKwh, setTotalKwh]   = useState(0);
  const [drEvents, setDrEvents]   = useState([]);

  // Fetch static data
  useEffect(() => {
    fetch(`${API}/zones/`).then(r=>r.ok?r.json():null).then(d=>{ if(d) setZones(d); }).catch(()=>{});
    fetchAudit();
    fetchPredictions();
    fetchSetpoints();
    fetchDrEvents();
    const id = setInterval(() => { fetchAudit(); fetchPredictions(); fetchSetpoints(); fetchDrEvents(); }, 10000);
    return () => clearInterval(id);
  }, []);

  function fetchAudit() {
    fetch(`${API}/audit/?limit=30`).then(r=>r.ok?r.json():null).then(d=>{ if(d) setAudit(d); }).catch(()=>{});
  }
  function fetchPredictions() {
    fetch(`${API}/dr/predict`).then(r=>r.ok?r.json():null).then(d=>{ if(d) setPreds(d); }).catch(()=>{});
  }
  function fetchSetpoints() {
    fetch(`${API}/zones/setpoints`).then(r=>r.ok?r.json():null).then(d=>{ if(d) setSetpoints(d); }).catch(()=>{});
  }
  function fetchDrEvents() {
    fetch(`${API}/dr/events?limit=5`).then(r=>r.ok?r.json():null).then(d=>{ if(d) setDrEvents(d); }).catch(()=>{});
  }

  const onMessage = useCallback((msg) => {
    dispatch(msg);
    setMsgCount(c => c + 1);
    if (msg.sensor_type === "energy_kw") {
      setTotalKwh(prev => prev + msg.value * (5/3600)); // 5s interval
    }
  }, []);

  const { ready, count } = useWebSocket(onMessage);
  const snapshot = latestPerZone(history);

  // Total building energy
  const totalKw = Object.values(snapshot).reduce((s, r) => s + (r.energy_kw||0), 0);

  // Completed DR events
  const completedDR = drEvents.filter(e => e.status === "completed");
  const totalKwhAvoided = completedDR.reduce((s, e) => s + (e.kwh_avoided||0), 0);

  // ─── Layout ──────────────────────────────────────────────────────────────
  const C = {
    app:     { minHeight:"100vh", display:"flex", flexDirection:"column" },
    topBar:  { display:"flex", alignItems:"center", justifyContent:"space-between",
               padding:"10px 24px", borderBottom:"1px solid var(--border)",
               background:"var(--bg-panel)", position:"sticky", top:0, zIndex:100 },
    logo:    { display:"flex", alignItems:"center", gap:10 },
    logoTxt: { fontFamily:"var(--font-ui)", fontWeight:700, fontSize:14, letterSpacing:"0.18em", textTransform:"uppercase" },
    logoSub: { fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-muted)", letterSpacing:"0.1em" },
    badge:   (ok) => ({ display:"flex", alignItems:"center", gap:5, padding:"3px 9px",
               border:`1px solid ${ok?"var(--green)":"var(--border)"}`,
               borderRadius:"var(--r)", fontFamily:"var(--font-mono)", fontSize:9,
               color: ok?"var(--green)":"var(--text-muted)",
               background: ok?"var(--green-dim)":"transparent" }),
    main:    { flex:1, padding:"20px 24px", display:"flex", flexDirection:"column", gap:20 },
    section: { display:"flex", alignItems:"center", gap:8, marginBottom:10 },
    secLabel:{ fontFamily:"var(--font-mono)", fontSize:8, letterSpacing:"0.18em",
               textTransform:"uppercase", color:"var(--text-muted)" },
    secLine: { flex:1, height:1, background:"var(--border)" },
    zonesG:  { display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(255px,1fr))", gap:12 },
    chartsG: { display:"grid", gridTemplateColumns:"1fr 1fr", gap:12 },
    chartP:  { background:"var(--bg-panel)", border:"1px solid var(--border)", borderRadius:"var(--r)", padding:"14px 16px" },
    chartTit:{ fontFamily:"var(--font-ui)", fontWeight:600, fontSize:10, letterSpacing:"0.14em",
               textTransform:"uppercase", color:"var(--text-sec)", marginBottom:10 },
    bottomG: { display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap:12 },
    stat:    { background:"var(--bg-panel)", border:"1px solid var(--border)", borderRadius:"var(--r)",
               padding:"12px 16px" },
    statLbl: { fontFamily:"var(--font-mono)", fontSize:8, color:"var(--text-muted)", letterSpacing:"0.1em", textTransform:"uppercase" },
    statVal: { fontFamily:"var(--font-mono)", fontSize:22, color:"var(--amber)", marginTop:4 },
  };

  function Sec({ icon: Icon, label }) {
    return (
      <div style={C.section}>
        {Icon && <Icon size={9} color="var(--text-muted)" />}
        <span style={C.secLabel}>{label}</span>
        <div style={C.secLine} />
      </div>
    );
  }

  return (
    <div style={C.app}>
      {/* ── Top Bar ─────────────────────────────────────────────────── */}
      <header style={C.topBar}>
        <div style={C.logo}>
          <Building2 size={17} color="var(--amber)" />
          <div>
            <div style={C.logoTxt}>DR Middleware</div>
            <div style={C.logoSub}>COMFORT-CONSTRAINED DEMAND RESPONSE · BACNET/MQTT</div>
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:14 }}>
          <Clock />
          <div style={C.badge(ready)}>
            {ready ? <Wifi size={9}/> : <WifiOff size={9}/>}
            {ready ? "LIVE" : "CONNECTING"}
          </div>
          <span style={{ fontFamily:"var(--font-mono)", fontSize:9, color:"var(--text-muted)" }}>
            <span style={{ color:"var(--amber)" }}>{msgCount.toLocaleString()}</span> msgs
          </span>
        </div>
      </header>

      {/* ── Main ────────────────────────────────────────────────────── */}
      <main style={C.main}>

        {/* Zone Cards */}
        <section>
          <Sec icon={Activity} label="Zone Status — Live" />
          <div style={C.zonesG}>
            {zones.map(z => (
              <ZoneCard key={z.zone_id} zone={z} readings={snapshot[z.zone_id]||{}}
                setpoint={setpoints[z.zone_id]?.cooling_setpoint?.present_value} />
            ))}
          </div>
        </section>

        {/* Charts */}
        <section>
          <Sec label="Sensor Time-Series" />
          <div style={C.chartsG}>
            {CHARTS.map(({ key, label, unit }) => (
              <div key={key} style={C.chartP}>
                <div style={C.chartTit}>{label}</div>
                <SensorChart sensorType={key} history={history} unit={unit} />
                <div style={{ display:"flex", gap:12, marginTop:6, flexWrap:"wrap" }}>
                  {["zone_1","zone_2","zone_3","zone_4"].map((zid, i) => (
                    <span key={zid} style={{ fontFamily:"var(--font-mono)", fontSize:8,
                      color:["#f0a500","#00e676","#40c4ff","#b388ff"][i] }}>
                      ── {zid}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* DR + Predictions + Audit */}
        <section>
          <Sec icon={Zap} label="Demand Response Control" />
          <div style={C.bottomG}>
            <DRPanel onEventCreated={() => { fetchDrEvents(); fetchAudit(); setTimeout(fetchSetpoints, 2000); }} />
            <div style={{ display:"flex", flexDirection:"column", gap:12 }}>
              <div style={C.chartP}>
                <div style={{ ...C.chartTit, display:"flex", alignItems:"center", gap:6 }}>
                  <Brain size={10} /> Occupancy Forecast (+30 min)
                </div>
                <PredictionPanel predictions={predictions} />
              </div>
            </div>
            <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
              {[
                { label:"Total Building Power", value:`${totalKw.toFixed(2)}`, unit:"kW" },
                { label:"kWh Avoided (DR)", value:totalKwhAvoided.toFixed(3), unit:"kWh" },
                { label:"DR Events Run", value:drEvents.length, unit:"total" },
                { label:"MQTT Messages", value:msgCount.toLocaleString(), unit:"total" },
              ].map(({ label, value, unit }) => (
                <div key={label} style={C.stat}>
                  <div style={C.statLbl}>{label}</div>
                  <div style={C.statVal}>
                    {value}
                    <span style={{ fontSize:9, color:"var(--text-muted)", marginLeft:5 }}>{unit}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Audit Log */}
        <section>
          <Sec icon={BookOpen} label="Audit Trail" />
          <div style={C.chartP}>
            <div style={C.chartTit}>
              <span className="blink" style={{ color:"var(--green)", fontSize:8 }}>◉</span>
              &nbsp; System Events
            </div>
            <AuditLog entries={audit} />
          </div>
        </section>

      </main>
    </div>
  );
}
