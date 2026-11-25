import {LitElement, css, html, nothing} from "lit";

type HA = { callWS<T>(message: Record<string, unknown>): Promise<T> };
type Light = {id: string; entity_id: string; name: string; area_id: string | null; available: boolean};
type Catalog = {schema_version: 1; entries: string[]; lights: Light[]; physical_output_enabled: false};
type Session = {id: string; zone_id: string; phase: string; revision: number; devices: string[]; effect: {kind: string}};
type RGB = {red: number; green: number; blue: number; brightness: number};
type Snapshot = {schema_version: 1; instance_id: string; output_mode: "preview"; physical_output_enabled: false; sessions: Session[]; targets: Record<string, {target: RGB}>};
type Start = {op: "start"; request_id: string; zone_id: string; devices: string[]; effect: {kind: string; period_seconds: number; brightness: number}};
type Command = Start | {op: "stop" | "detach"; session_id: string; revision: number; device_id?: string};

const descriptions: Record<string, string> = {rainbow:"Rainbow", scanner:"Knight Rider / Scanner", pulse:"Pulse"};
function explain(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
  return ({device_conflict:"A selected lamp already belongs to another preview. Choose other lamps or stop that session.",
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
    busy:{state:true}, status:{state:true}, online:{state:true},
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
  private entry?: string;
  private refreshing = false;
  constructor() {
    super(); this.chosen=[]; this.zone=""; this.effect="rainbow"; this.period=4;
    this.brightness=1; this.busy=false; this.status="Connecting to local engine…"; this.online=false;
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
    if(s.schema_version!==1 || s.output_mode!=="preview" || s.physical_output_enabled!==false) throw new Error("incompatible preview API");
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
        if(cat.schema_version!==1 || cat.physical_output_enabled!==false || cat.entries.length!==1) throw new Error("engine entry unavailable");
        this.catalog=cat; this.entry=cat.entries[0];
        this.chosen=this.chosen.filter(id=>cat.lights.some(l=>l.id===id));
      }
      const state=await this.hass.callWS<Snapshot>({type:"lightcadence/state",entry_id:this.entry});
      if(epoch!==this.epoch) return;
      this.accept(state);
      if(!this.online) this.status="Connected · preview only";
      this.online=true;
    } catch(error) {
      if(epoch===this.epoch) {this.online=false; this.entry=undefined; this.status=explain(error);}
    } finally {if(epoch===this.epoch) this.refreshing=false;}
  }
  private async send(payload: Command) {
    if(!this.hass || !this.entry || !this.online || this.busy) return;
    const epoch=++this.epoch; this.refreshing=false; this.busy=true; this.status="Applying preview command…";
    try {
      const state=await this.hass.callWS<Snapshot>({type:"lightcadence/preview",entry_id:this.entry,payload});
      if(epoch!==this.epoch) return;
      this.accept(state); this.pendingStart=undefined;
      if(payload.op==="start") {this.chosen=[]; this.zone="";}
      this.status="Preview command confirmed";
    } catch(error) {
      if(epoch===this.epoch) {
        this.status=explain(error);
        // Preserve a potentially delivered start for an explicit identical retry.
        // Known rejections did not create a session.
        const code=typeof error==="object" && error!==null && "code" in error ? String(error.code) : "";
        if(["device_conflict","stale_revision","invalid_selection","invalid_request","not_found"].includes(code)) this.pendingStart=undefined;
      }
    } finally {if(epoch===this.epoch) {this.busy=false; void this.refresh();}}
  }
  private start() {
    if(this.pendingStart) {void this.send(this.pendingStart); return;}
    if(!this.zone.trim() || !this.chosen.length || !Number.isFinite(this.period) || this.period<=0 || !Number.isFinite(this.brightness) || this.brightness<0 || this.brightness>1) return;
    const payload:Start={op:"start",request_id:Array.from(crypto.getRandomValues(new Uint8Array(16)), b=>b.toString(16).padStart(2,"0")).join(""),zone_id:this.zone.trim(),devices:[...this.chosen],
      effect:{kind:this.effect,period_seconds:this.period,brightness:this.brightness}};
    this.pendingStart=payload;
    void this.send(payload);
  }
  private toggle(id:string, checked:boolean) {
    this.chosen=checked?[...this.chosen,id]:this.chosen.filter(d=>d!==id);
  }
  private name(id:string) {return this.catalog?.lights.find(l=>l.id===id)?.name ?? id;}
  render() {
    const active=this.snapshot?.sessions.filter(s=>s.phase!=="stopped")??[];
    const locked=new Set(active.flatMap(s=>s.devices));
    const formLocked=this.busy||!!this.pendingStart;
    return html`
      <main>
        <header><div><h1>LightCadence</h1><p>Live preview workspace</p></div><span class="badge">${this.online?"Connected":"Disconnected"}</span></header>
        <p class="notice">Preview only — no physical lamps are controlled. Colours below are engine targets, not device feedback.</p>
        <div class="toolbar"><p role="status" aria-live="polite">${this.status}</p><button @click=${()=>this.refresh(true)} ?disabled=${this.busy}>Refresh</button></div>
        <div class="workspace">
          <section aria-labelledby="setup"><h2 id="setup">New zone preview</h2>
            <label>Zone name <input aria-label="Zone name" title="Label for this independent preview session" .value=${this.zone} ?disabled=${formLocked} @input=${(e:Event)=>this.zone=(e.target as HTMLInputElement).value}></label>
            <label>Effect <select .value=${this.effect} ?disabled=${formLocked} @change=${(e:Event)=>this.effect=(e.target as HTMLSelectElement).value}>${Object.entries(descriptions).map(([key,title])=>html`<option value=${key}>${title}</option>`)}</select></label>
            <label>Period (seconds) <input type="number" step="any" .value=${String(this.period)} ?disabled=${formLocked} @input=${(e:Event)=>this.period=(e.target as HTMLInputElement).valueAsNumber}></label>
            <label>Brightness <input type="range" min="0" max="1" step="0.01" .value=${String(this.brightness)} ?disabled=${formLocked} @input=${(e:Event)=>this.brightness=(e.target as HTMLInputElement).valueAsNumber}> <output>${Math.round(this.brightness*100)}%</output></label>
            <h3>Lamps · ${this.chosen.length} selected</h3>
            ${this.catalog?.lights.length?this.catalog.lights.map(l=>html`<label class="lamp"><input type="checkbox" .checked=${this.chosen.includes(l.id)} ?disabled=${formLocked||(locked.has(l.id)&&!this.chosen.includes(l.id))} @change=${(e:Event)=>this.toggle(l.id,(e.target as HTMLInputElement).checked)}><span>${l.name}<small>${l.entity_id} · ${locked.has(l.id)?"In preview":l.available?"Available":"Unavailable in HA"}</small></span></label>`):html`<p>No registered lights available.</p>`}
            <button class="primary" ?disabled=${!this.online||this.busy||(!this.pendingStart&&(!this.zone.trim()||!this.chosen.length||this.chosen.some(id=>locked.has(id))||!(this.period>0)))} @click=${()=>this.start()}>${this.pendingStart?"Retry same start":"Start preview"}</button>
            ${this.pendingStart&&!this.busy?html`<button @click=${()=>{this.pendingStart=undefined;this.status="Start retry cleared; refresh server state before a new start.";void this.refresh(true);this.requestUpdate();}}>Clear retry</button>`:nothing}
          </section>
          <section aria-labelledby="running"><h2 id="running">Running previews · ${active.length}</h2>
          ${active.length?active.map(s=>html`<article><h3>${s.zone_id}</h3><p>${descriptions[s.effect.kind]??s.effect.kind} · ${s.phase} · ${s.devices.length} lamps</p>
            <div class="targets">${s.devices.map(id=>html`<div class="target"><span class="swatch" style=${"background:"+color(this.snapshot?.targets[id]?.target)}></span><span>${this.name(id)}</span><button title="Remove this lamp from this preview only; other sessions are unaffected" ?disabled=${!this.online||this.busy} @click=${()=>this.send({op:"detach",session_id:s.id,revision:s.revision,device_id:id})}>Detach</button></div>`)}</div>
            <button class="primary" ?disabled=${!this.online||this.busy} @click=${()=>this.send({op:"stop",session_id:s.id,revision:s.revision})} title="Stop only this preview session">Stop preview</button>
          </article>`):html`<p>No active previews. Choose lamps and an effect to begin.</p>`}
          </section>
        </div>
      </main>`;
  }
  static styles=css`
    :host{display:block;color:var(--primary-text-color,#eef1f5);background:var(--primary-background-color,#141923);min-height:100%;font-family:inherit}
    *{box-sizing:border-box} main{max-width:1300px;margin:auto;padding:clamp(12px,3vw,32px);overflow-wrap:anywhere}
    h1{font-size:clamp(24px,4vw,34px);margin:0} h2{font-size:20px} h3{font-size:16px;margin:16px 0 8px} p{margin:8px 0} header,.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
    .badge,.notice{border:1px solid var(--divider-color,#687080);border-radius:8px;padding:10px}.notice{margin:16px 0}
    .workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:16px;margin-top:16px}
    section,article{background:var(--card-background-color,#202735);border:1px solid var(--divider-color,#596273);border-radius:12px;padding:16px;min-width:0} article+article{margin-top:12px}
    label{display:block;margin:10px 0} input:not([type=checkbox]),select{display:block;width:100%;min-height:44px;color:inherit;background:var(--secondary-background-color,#303948);border:1px solid var(--divider-color,#657080);border-radius:6px;padding:8px}
    input[type=checkbox]{width:20px;height:20px;flex:none} .lamp,.target{display:flex;align-items:center;gap:12px;min-height:44px;padding:6px 0} small{display:block;color:var(--secondary-text-color,#bec5cf)}
    button{min-height:44px;min-width:44px;padding:8px 14px;border:1px solid var(--divider-color,#657080);background:var(--secondary-background-color,#303948);color:inherit;border-radius:8px;cursor:pointer;font:inherit}
    button.primary{margin-top:12px;background:var(--primary-color,#237db8);color:var(--text-primary-color,white)} button:disabled{opacity:.5;cursor:not-allowed} button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#48a9e8);outline-offset:2px}
    .target span:nth-child(2){flex:1;min-width:0}.swatch{width:32px;height:32px;flex:none;border:1px solid var(--divider-color,#657080);border-radius:50%}
    @media(max-width:700px){.workspace{grid-template-columns:minmax(0,1fr)} section{padding:12px}}
  `;
}
customElements.define("lightcadence-panel",LightCadencePanel);
