import {LitElement, html, nothing} from "lit";
import {workspaceStyles} from "./workspace-styles";

type HA = { callWS<T>(message: Record<string, unknown>): Promise<T> };
type Light = {id: string; entity_id: string; name: string; area_id: string | null; available: boolean; observation?: {source: "ha_cache"; status: string; power: boolean | null; color_mode: string | null; rgb: number[] | null; brightness: number | null}};
type Catalog = {schema_version: 1; entries: string[]; lights: Light[]; physical_output_enabled: boolean};
type Session = {id: string; zone_id: string; phase: string; revision: number; devices: string[]; effect: {kind: string}};
type RGB = {red: number; green: number; blue: number; brightness: number};
type Recovery = {device_id:string; record_id:string; original:{power:boolean;target:RGB}; policy:string; binding_matches:boolean};
type Snapshot = {recovery_required?:Recovery[]; recovery_journal_fault?:boolean; schema_version: 1; instance_id: string; output_mode: "preview" | "physical_candidate"; physical_output_enabled: boolean; hardware_qualified?: boolean; enrolled_devices?: string[]; manual_result?: "confirmed" | "unconfirmed"; sessions: Session[]; targets: Record<string, {target: RGB}>};
type StopPolicy = "restore" | "hold" | "off";
type Start = {stop_policy?: StopPolicy; instance_id?: string; op: "start"; request_id: string; zone_id: string; devices: string[]; effect: {kind: string; period_seconds: number; brightness: number}};
type Manual = {instance_id: string; op: "manual_set"; request_id: string; device_id: string; state: {power: true; rgb: number[]; brightness_pct: number}} | {instance_id: string; op: "manual_off"; request_id: string; device_id: string};
type Command = Start | Manual | {op:"recover";instance_id:string;device_id:string;record_id:string;decision:"keep"|"restore"} | {instance_id?: string; op: "stop" | "detach"; session_id: string; revision: number; device_id?: string};
function requestId() {return Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,"0")).join("");}

const descriptions: Record<string, string> = {rainbow:"Rainbow", scanner:"Knight Rider / Scanner", pulse:"Pulse"};
function observationLabel(light: Light): string {
  const o=light.observation;
  if(!o || o.source!=="ha_cache") return "No cached state";
  const power=o.power===null?"Unknown":o.power?"On":"Off";
  const label=({cached_rgb:"RGB cached",effect_active:"Effect active",incomplete_rgb:"Incomplete RGB",unsupported_or_unknown_mode:"Mode unsupported or unknown",unavailable:"Unavailable"} as Record<string,string>)[o.status]??"State unknown";
  return power+" · "+label+" (HA cache, not device feedback)";
}
function explain(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
  return ({device_conflict:"A selected lamp already belongs to another preview or show, or requires recovery. Choose other lamps or resolve the affected session.",
    stale_instance:"The engine restarted. Refresh and explicitly start a new session." ,
    stale_revision:"The session changed on another client. Refresh before trying again.",
    invalid_selection:"The lamp registry changed. Refresh the selection.",
    unauthorized:"Access denied.", not_found:"No loaded LightCadence engine."} as Record<string,string>)[code]
    ?? "Engine unavailable or request failed. Refresh to reconcile the server state.";
}
function color(t: RGB | undefined): string {
  if (!t) return "transparent";
  // Display conversion only; no independent effect computation.
  const channels = [t.red,t.green,t.blue].map(v => Math.round(255*Math.pow(Math.max(0,Math.min(1,v*t.brightness)),1/2.2)));
  return "rgb("+channels.join(",")+")";
}
class LightCadencePanel extends LitElement {
  static properties = {
    hass:{attribute:false}, catalog:{state:true}, snapshot:{state:true}, chosen:{state:true},
    zone:{state:true}, effect:{state:true}, period:{state:true}, brightness:{state:true},
    busy:{state:true}, status:{state:true}, online:{state:true}, stopPolicy:{state:true},
    manualDevice:{state:true}, manualColor:{state:true}, manualBrightness:{state:true}, pendingManual:{state:true},
  };
  declare hass?: HA;
  declare catalog?: Catalog;
  declare snapshot?: Snapshot;
  declare chosen: string[];
  declare zone: string;
  declare effect: string;
  declare period: number;
  declare brightness: number;
  declare busy: boolean;
  declare status: string;
  declare online: boolean;
  private timer?: number;
  private epoch = 0;
  private pendingStart?: Start;
  declare stopPolicy: StopPolicy;
  declare manualDevice: string;
  declare manualColor: string;
  declare manualBrightness: number;
  declare pendingManual?: Manual;
  private entry?: string;
  private refreshing = false;
  constructor() {
    super(); this.chosen=[]; this.zone=""; this.effect="rainbow"; this.period=4;
    this.brightness=1; this.stopPolicy="restore"; this.manualDevice=""; this.manualColor="#ffffff"; this.manualBrightness=50; this.busy=false; this.status="Connecting to local engine…"; this.online=false;
  }
  connectedCallback() {
    super.connectedCallback(); this.epoch++;
    this.timer=window.setInterval(()=>{void this.refresh();},500);
    void this.refresh(true);
  }
  disconnectedCallback() {
    this.epoch++; window.clearInterval(this.timer); this.refreshing=false; this.busy=false;
    super.disconnectedCallback();
  }
  updated(changed: Map<PropertyKey,unknown>) {
    if(changed.has("hass") && !changed.get("hass") && this.hass) void this.refresh(true);
  }
  private accept(s: Snapshot) {
    const physical=this.catalog?.physical_output_enabled===true;
    if(s.schema_version!==1 || s.output_mode!==(physical?"physical_candidate":"preview") || s.physical_output_enabled!==physical || (physical && (s.hardware_qualified!==false || !Array.isArray(s.enrolled_devices)))) throw new Error("incompatible engine API");
    if(this.snapshot && this.snapshot.instance_id!==s.instance_id) {this.pendingStart=undefined;this.pendingManual=undefined;}
    this.snapshot=s;
  }
  private async refresh(full=false) {
    if(!this.hass || !this.isConnected || this.busy || this.refreshing) return;
    const epoch=this.epoch;
    this.refreshing=true;
    try {
      if(full || !this.entry || !this.catalog) {
        const cat=await this.hass.callWS<Catalog>({type:"lightcadence/catalog"});
        if(epoch!==this.epoch) return;
        if(cat.schema_version!==1 || typeof cat.physical_output_enabled!=="boolean" || cat.entries.length!==1) throw new Error("engine entry unavailable");
        this.catalog=cat; this.entry=cat.entries[0];
        this.chosen=this.chosen.filter(id=>cat.lights.some(l=>l.id===id));
      }
      const state=await this.hass.callWS<Snapshot>({type:"lightcadence/state",entry_id:this.entry});
      if(epoch!==this.epoch) return;
      this.accept(state);
      if(!this.online) this.status=state.physical_output_enabled?"Connected · hardware candidate (unqualified)":"Connected · preview only";
      this.online=true;
    } catch(error) {
      if(epoch===this.epoch) {this.online=false; this.entry=undefined; this.status=explain(error);}
    } finally {if(epoch===this.epoch) this.refreshing=false;}
  }
  private async send(payload: Command) {
    if(!this.hass || !this.entry || !this.online || this.busy) return;
    const manual=payload.op==="manual_set" || payload.op==="manual_off";
    if(manual && !this.catalog?.physical_output_enabled) return;
    const epoch=++this.epoch; this.refreshing=false; this.busy=true; this.status="Applying engine command…";
    try {
      const physical=this.catalog?.physical_output_enabled===true;
      const command=physical?{...payload,instance_id:payload.instance_id??this.snapshot?.instance_id}:payload;
      const state=await this.hass.callWS<Snapshot>({type:physical?"lightcadence/physical":"lightcadence/preview",entry_id:this.entry,payload:command});
      if(epoch!==this.epoch) return;
      this.accept(state);
      if(payload.op==="start") this.pendingStart=undefined;
      if(manual) this.pendingManual=undefined;
      if(payload.op==="start") {this.chosen=[]; this.zone="";}
      this.status=payload.op==="recover"?(state.manual_result==="confirmed"?"Recovery resolved after state readback":"Recovery unconfirmed — inspect the lamp before making a new decision"):manual?(state.manual_result==="confirmed"?"Manual state readback confirmed (not optical confirmation)":"Manual outcome unconfirmed — inspect device before another command"):state.physical_output_enabled?"Engine command accepted · not optical confirmation":"Preview command confirmed";
    } catch(error) {
      if(epoch===this.epoch) {
        this.status=explain(error);
        // Preserve a potentially delivered start for an explicit identical retry.
        // Known rejections did not create a session.
        const code=typeof error==="object" && error!==null && "code" in error ? String(error.code) : "";
        if(["device_conflict","stale_revision","stale_instance","invalid_selection","invalid_request","not_found"].includes(code)) {
          if(payload.op==="start") this.pendingStart=undefined;
          if(manual) this.pendingManual=undefined;
        }
      }
    } finally {if(epoch===this.epoch) {this.busy=false; void this.refresh();}}
  }
  private start() {
    if(this.pendingStart) {void this.send(this.pendingStart); return;}
    if(!this.zone.trim() || !this.chosen.length || !Number.isFinite(this.period) || this.period<=0 || !Number.isFinite(this.brightness) || this.brightness<0 || this.brightness>1) return;
    const payload:Start={op:"start",request_id:requestId(),zone_id:this.zone.trim(),devices:[...this.chosen],
      effect:{kind:this.effect,period_seconds:this.period,brightness:this.brightness}};
    if(this.catalog?.physical_output_enabled) {payload.instance_id=this.snapshot?.instance_id;payload.stop_policy=this.stopPolicy;}
    this.pendingStart=payload;
    void this.send(payload);
  }
  private manual(off: boolean) {
    if(this.pendingManual) {void this.send(this.pendingManual);return;}
    if(!this.catalog?.physical_output_enabled || !this.snapshot || !this.catalog.lights.some(l=>l.id===this.manualDevice)) return;
    const common={instance_id:this.snapshot.instance_id,request_id:requestId(),device_id:this.manualDevice};
    if(off) this.pendingManual={...common,op:"manual_off"};
    else {
      if(!/^#[0-9a-f]{6}$/i.test(this.manualColor) || !Number.isInteger(this.manualBrightness) || this.manualBrightness<1 || this.manualBrightness>100) return;
      const rgb=[1,3,5].map(i=>parseInt(this.manualColor.slice(i,i+2),16));
      this.pendingManual={...common,op:"manual_set",state:{power:true,rgb,brightness_pct:this.manualBrightness}};
    }
    void this.send(this.pendingManual);
  }
  private toggle(id:string, checked:boolean) {
    this.chosen=checked?[...this.chosen,id]:this.chosen.filter(d=>d!==id);
  }
  private name(id:string) {return this.catalog?.lights.find(l=>l.id===id)?.name ?? id;}
  render() {
    const physical=this.catalog?.physical_output_enabled===true;
    const active=this.snapshot?.sessions.filter(s=>s.phase!=="stopped")??[];
    const recovery=this.snapshot?.recovery_required??[];
    const blocked=new Set(recovery.map(r=>r.device_id));
    const fault=this.snapshot?.recovery_journal_fault===true;
    const locked=new Set(active.flatMap(s=>s.devices));
    const formLocked=this.busy||!!this.pendingStart||fault;
    return html`
      <main>
        <header><div><h1>LightCadence</h1><p>${physical?"Hardware test workspace":"Live preview workspace"}</p></div><span class="badge" data-connected=${this.online?"yes":"no"}>${this.online?"Connected":"Disconnected"}</span></header>
        <p class="notice">${physical?"Hardware candidate — real lamp commands are enabled for enrolled devices only. Not hardware-qualified. Use explicit LightCadence HA actions for cooperating automations. Ordinary light services and Google Home are not intercepted. Colours are targets, not optical feedback.":"Preview only — no physical lamps are controlled. Colours below are engine targets, not device feedback."}</p>
        <div class="toolbar"><p role="status" aria-live="polite">${this.status}</p><button @click=${()=>this.refresh(true)} ?disabled=${this.busy}>Refresh</button></div>
        <div class="workspace">
          <section aria-labelledby="setup"><h2 id="setup">${physical?"New zone show":"New zone preview"}</h2>
            <label>Zone name <input aria-label="Zone name" title="Label for this independent preview session" .value=${this.zone} ?disabled=${formLocked} @input=${(e:Event)=>this.zone=(e.target as HTMLInputElement).value}></label>
            <label>Effect <select .value=${this.effect} ?disabled=${formLocked} @change=${(e:Event)=>this.effect=(e.target as HTMLSelectElement).value}>${Object.entries(descriptions).map(([key,title])=>html`<option value=${key}>${title}</option>`)}</select></label>
            <label>Period (seconds) <input type="number" step="any" .value=${String(this.period)} ?disabled=${formLocked} @input=${(e:Event)=>this.period=(e.target as HTMLInputElement).valueAsNumber}></label>
            <label>Brightness <input type="range" min="0" max="1" step="0.01" .value=${String(this.brightness)} ?disabled=${formLocked} @input=${(e:Event)=>this.brightness=(e.target as HTMLInputElement).valueAsNumber}> <output>${Math.round(this.brightness*100)}%</output></label>
            ${physical?html`<label>Stop behaviour <select .value=${this.stopPolicy} ?disabled=${formLocked} @change=${(e:Event)=>this.stopPolicy=(e.target as HTMLSelectElement).value as StopPolicy}><option value="restore">Restore original state</option><option value="hold">Hold last state</option><option value="off">Turn off</option></select></label>`:nothing}
            <h3>Lamps · ${this.chosen.length} selected</h3>
            ${this.catalog?.lights.length?this.catalog.lights.map(l=>html`<label class="lamp"><input type="checkbox" .checked=${this.chosen.includes(l.id)} ?disabled=${formLocked||blocked.has(l.id)||(locked.has(l.id)&&!this.chosen.includes(l.id))} @change=${(e:Event)=>this.toggle(l.id,(e.target as HTMLInputElement).checked)}><span>${l.name}<small>${l.entity_id} · ${blocked.has(l.id)?"Recovery required":locked.has(l.id)?(physical?"In show":"In preview"):l.available?"Available":"Unavailable in HA"}</small><small>${observationLabel(l)}</small></span></label>`):html`<p>No registered lights available.</p>`}
            <button class="primary" ?disabled=${!this.online||this.busy||fault||this.chosen.some(id=>blocked.has(id))||(!this.pendingStart&&(!this.zone.trim()||!this.chosen.length||this.chosen.some(id=>locked.has(id))||!(this.period>0)))} @click=${()=>this.start()}>${this.pendingStart?"Retry same start":physical?"Start hardware test":"Start preview"}</button>
            ${this.pendingStart&&!this.busy?html`<button @click=${()=>{this.pendingStart=undefined;this.status="Start retry cleared; refresh server state before a new start.";void this.refresh(true);this.requestUpdate();}}>Clear retry</button>`:nothing}
          </section>
          <div class="right-stack"><section aria-labelledby="running"><h2 id="running">${physical?"Running shows":"Running previews"} · ${active.length}</h2>
          ${active.length?active.map(s=>html`<article data-phase=${s.phase}><h3>${s.zone_id}</h3><p>${descriptions[s.effect.kind]??s.effect.kind} · ${s.phase} · ${s.devices.length} lamps</p>
            <div class="targets">${s.devices.map(id=>html`<div class="target"><span class="swatch" style=${"background:"+color(this.snapshot?.targets[id]?.target)}></span><span>${this.name(id)}</span><button title="Remove this lamp from this preview only; other sessions are unaffected" ?disabled=${!this.online||this.busy} @click=${()=>this.send({op:"detach",session_id:s.id,revision:s.revision,device_id:id})}>Detach</button></div>`)}</div>
            <button class="primary" ?disabled=${!this.online||this.busy} @click=${()=>this.send({op:"stop",session_id:s.id,revision:s.revision})} title="Stop only this preview session">${physical?"Stop show":"Stop preview"}</button>
          </article>`):html`<p>${physical?"No active shows. Choose enrolled lamps and an effect to begin.":"No active previews. Choose lamps and an effect to begin."}</p>`}
          </section>
        ${physical&&(fault||recovery.length)?html`<section aria-labelledby="recovery"><h2 id="recovery">Recovery required</h2><p>Interrupted shows are never restarted automatically. Check the actual lamp before choosing to keep its current state or explicitly restore its recorded original state. Restoring may overwrite changes made outside LightCadence.</p>${fault?html`<p role="alert">Journal unavailable. New starts and manual actions are blocked. Inspect local storage and restart safely.</p>`:nothing}${recovery.map(r=>html`<article><h3>${this.name(r.device_id)}</h3><p>Recorded: ${r.original.power?"On":"Off"} · ${Math.round(r.original.target.brightness*100)}% · ${r.policy}</p><p>${r.binding_matches?"Identity will be rechecked":"Binding changed — recovery blocked"}</p>${(["keep","restore"] as const).map(decision=>html`<button ?disabled=${!this.online||this.busy||fault||!r.binding_matches||!this.catalog?.lights.some(l=>l.id===r.device_id)} @click=${()=>this.snapshot&&this.send({op:"recover",instance_id:this.snapshot.instance_id,device_id:r.device_id,record_id:r.record_id,decision})}>${decision==="keep"?"Keep current state":"Restore recorded state"}</button> `)}</article>`)}</section>`:nothing}
        ${physical?html`<section class="manual" aria-labelledby="manual"><h2 id="manual">Manual lamp control</h2><p>Take the selected lamp out of any running show before changing it. Other lamps continue. No automatic retry after an uncertain result.</p>
          <label>Manual lamp <select aria-label="Manual lamp" .value=${this.manualDevice} ?disabled=${this.busy||!!this.pendingManual} @change=${(e:Event)=>this.manualDevice=(e.target as HTMLSelectElement).value}><option value="">Select a lamp</option>${this.catalog?.lights.map(l=>html`<option value=${l.id}>${l.name}</option>`)}</select></label>
          <label>Manual colour <input aria-label="Manual colour" type="color" .value=${this.manualColor} ?disabled=${this.busy||!!this.pendingManual} @input=${(e:Event)=>this.manualColor=(e.target as HTMLInputElement).value}></label>
          <label>Manual brightness (%) <input aria-label="Manual brightness (%)" type="number" min="1" max="100" step="1" .value=${String(this.manualBrightness)} ?disabled=${this.busy||!!this.pendingManual} @input=${(e:Event)=>this.manualBrightness=(e.target as HTMLInputElement).valueAsNumber}></label>
          ${this.pendingManual?html`<button ?disabled=${!this.online||this.busy} @click=${()=>this.manual(false)}>Check same manual request</button><button ?disabled=${this.busy} @click=${()=>{this.pendingManual=undefined;this.status="Manual outcome may be uncertain. Inspect the device before a new action.";}}>Clear pending request</button>`:html`<button ?disabled=${!this.online||this.busy||fault||blocked.has(this.manualDevice)||!this.manualDevice||!!this.pendingStart} @click=${()=>this.manual(false)}>Apply colour and turn on</button> <button ?disabled=${!this.online||this.busy||fault||blocked.has(this.manualDevice)||!this.manualDevice||!!this.pendingStart} @click=${()=>this.manual(true)}>Turn off lamp</button>`}
        </section>`:nothing}
        </div>
        </div>
      </main>`;
  }
  static styles=workspaceStyles;
}
customElements.define("lightcadence-panel",LightCadencePanel);
