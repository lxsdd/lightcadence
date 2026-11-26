import {LitElement, html, nothing} from "lit";
import {workspaceStyles} from "./workspace-styles";
import "./floorplan";
import "./timeline";
import type {Track} from "./timeline";
import type {Plan,SpatialPath} from "./floorplan";

type HA = { callWS<T>(message: Record<string, unknown>): Promise<T> };
type Light = {id: string; entity_id: string; name: string; area_id: string | null; available: boolean; observation?: {source: "ha_cache"; status: string; power: boolean | null; color_mode: string | null; rgb: number[] | null; brightness: number | null}};
type Catalog = {schema_version: 1; entries: string[]; lights: Light[]; physical_output_enabled: boolean};
type Session = {timeline?:{duration:number;playhead:number;ended:boolean;loop:boolean;paused?:boolean;auto_end?:boolean;ending?:boolean};id: string; zone_id: string; phase: string; revision: number; devices: string[]; effect: {kind: string; audio_source?:string}};
type RGB = {red: number; green: number; blue: number; brightness: number};
type Recovery = {device_id:string; record_id:string; original:{power:boolean;target:RGB}; policy:string; binding_matches:boolean};
type AudioSource = {id:string;name:string;status:"waiting"|"live"|"lost";bands:number[]|null;bpm:number|null;beat_count:number};
type LeafEffect={kind:string;period_seconds:number;brightness:number;color:number[];width:number};
type Layer={effect:LeafEffect;opacity:number;blend:"over"|"add"|"max";devices?:string[]};
type PresetEffect=LeafEffect & {layers?:Layer[];tracks?:Track[];loop?:boolean;auto_end?:boolean};
type Zone={id:string;name:string;revision:number;devices:string[]};
type Scene={id:string;name:string;revision:number;zone_id:string;effect:PresetEffect;stop_policy:StopPolicy};
type Library={floorplans?:Plan[];spatial_paths?:SpatialPath[];revision:number;zones:Zone[];scenes:Scene[];persistent:boolean;fault:boolean};
type LibraryCommand={op:"library_save"|"library_delete";instance_id:string;revision:number;kind:"zones"|"scenes"|"floorplans";id:string|null;value:Record<string,unknown>|null};
type Snapshot = {library?:Library;audio_sources?:AudioSource[];recovery_required?:Recovery[]; recovery_journal_fault?:boolean; schema_version: 1; instance_id: string; output_mode: "preview" | "physical_candidate"; physical_output_enabled: boolean; hardware_qualified?: boolean; enrolled_devices?: string[]; manual_result?: "confirmed" | "unconfirmed"; sessions: Session[]; targets: Record<string, {target: RGB}>};
type StopPolicy = "restore" | "hold" | "off";
type Start = {stop_policy?: StopPolicy; instance_id?: string; op: "start"; request_id: string; zone_id: string; devices: string[]; positions?:Record<string,number>; effect: {kind: string; period_seconds: number; brightness: number; color?:number[];width?:number;audio_source?:string;layers?:Layer[];tracks?:Track[];loop?:boolean;auto_end?:boolean}};
type Manual = {instance_id: string; op: "manual_set"; request_id: string; device_id: string; state: {power: true; rgb: number[]; brightness_pct: number}} | {instance_id: string; op: "manual_off"; request_id: string; device_id: string};
type TimelineControl={op:"timeline_control";instance_id:string;session_id:string;revision:number;action:"pause"|"resume"|"seek";position?:number};
type Command = TimelineControl | LibraryCommand | Start | Manual | {op:"recover";instance_id:string;device_id:string;record_id:string;decision:"keep"|"restore"} | {instance_id?: string; op: "stop" | "detach"; session_id: string; revision: number; device_id?: string};
function requestId() {return Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,"0")).join("");}

const descriptions: Record<string, string> = {rainbow:"Rainbow", scanner:"Knight Rider / Scanner", pulse:"Pulse",audio_spectrum:"Audio spectrum",static:"Static colour",layered:"Layered scene",timeline:"Timeline Studio"};
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
    zone:{state:true}, effect:{state:true},layers:{state:true}, tracks:{state:true},timelineLoop:{state:true},timelineAutoEnd:{state:true}, audioSource:{state:true}, selectedZone:{state:true},spatialPath:{state:true},floorplanOpen:{state:true},selectedScene:{state:true},sceneName:{state:true}, period:{state:true}, brightness:{state:true},
    busy:{state:true}, status:{state:true}, online:{state:true}, stopPolicy:{state:true},
    manualDevice:{state:true}, manualColor:{state:true}, manualBrightness:{state:true}, pendingManual:{state:true},
  };
  declare hass?: HA;
  declare catalog?: Catalog;
  declare snapshot?: Snapshot;
  declare chosen: string[];
  declare zone: string;
  declare effect: string;
  declare layers:Layer[];
  declare tracks:Track[];
  declare timelineLoop:boolean;
  declare timelineAutoEnd:boolean;
  declare audioSource:string;
  declare selectedZone:string;
  declare spatialPath:string;
  declare floorplanOpen:boolean;
  declare selectedScene:string;
  declare sceneName:string;
  private effectColor=[1,0,0];
  private effectWidth=.25;
  private loadedLibraryRevision=-1;
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
    super(); this.chosen=[]; this.zone=""; this.effect="rainbow"; this.layers=[];this.tracks=[];this.timelineLoop=true;this.timelineAutoEnd=false; this.audioSource=""; this.selectedZone="";this.spatialPath="";this.floorplanOpen=false;this.selectedScene="";this.sceneName=""; this.period=4;
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
    if(s.library){if(!s.library.zones.some(z=>z.id===this.selectedZone))this.selectedZone="";if(!s.library.scenes.some(x=>x.id===this.selectedScene))this.selectedScene="";}
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
  private controlTimeline(s:Session,action:"pause"|"resume"|"seek",position?:number) {
    if(!this.snapshot||!s.timeline||s.phase!=="running")return;
    if(action==="seek"&&(position===undefined||!Number.isFinite(position)||position<0||position>s.timeline.duration))return;
    void this.send({op:"timeline_control",instance_id:this.snapshot.instance_id,session_id:s.id,revision:s.revision,action,...(action==="seek"?{position}:{})});
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
      if((payload.op==="library_save"||payload.op==="library_delete")&&payload.kind==="floorplans"){(this.renderRoot.querySelector("lightcadence-floorplan") as (HTMLElement & {confirmed:()=>void})|null)?.confirmed();}
      if(payload.op==="library_save"||payload.op==="library_delete"){this.spatialPath="";this.selectedZone="";this.selectedScene="";this.loadedLibraryRevision=-1;}
      if(payload.op==="start") this.pendingStart=undefined;
      if(manual) this.pendingManual=undefined;
      if(payload.op==="start") {this.chosen=[]; this.zone="";}
      this.status=payload.op==="library_save"||payload.op==="library_delete"?"Library updated — no lamp commands sent":payload.op==="recover"?(state.manual_result==="confirmed"?"Recovery resolved after state readback":"Recovery unconfirmed — inspect the lamp before making a new decision"):manual?(state.manual_result==="confirmed"?"Manual state readback confirmed (not optical confirmation)":"Manual outcome unconfirmed — inspect device before another command"):state.physical_output_enabled?"Engine command accepted · not optical confirmation":"Preview command confirmed";
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
      effect:{kind:this.effect,period_seconds:this.period,brightness:this.brightness,color:[...this.effectColor],width:this.effectWidth}};
    if(this.effect==="layered") {if(!this.validLayers())return;payload.effect.layers=structuredClone(this.layers);}
    if(this.effect==="timeline"){if(!this.validTimeline())return;payload.effect.tracks=structuredClone(this.tracks);payload.effect.loop=this.timelineLoop;if(!this.timelineLoop&&this.timelineAutoEnd)payload.effect.auto_end=true;}
    if(this.needsAudio()) {
      if(!this.snapshot?.audio_sources?.some(s=>s.id===this.audioSource)) return;
      payload.effect.audio_source=this.audioSource;
    }
    if(this.spatialPath){
      if(this.loadedLibraryRevision!==this.snapshot?.library?.revision){this.status="Spatial library changed — reload the zone and path before starting";return;}
      const path=this.snapshot?.library?.spatial_paths?.find(p=>p.floorplan_id+"/"+p.path_id===this.spatialPath);
      if(!path?.positions||Object.keys(path.positions).length!==this.chosen.length||!this.chosen.every(d=>Object.prototype.hasOwnProperty.call(path.positions,d))){this.status="Spatial path does not match the selected lamps";return;}
      payload.positions={...path.positions};
    }
    if(this.catalog?.physical_output_enabled) {payload.instance_id=this.snapshot?.instance_id;payload.stop_policy=this.stopPolicy;}
    this.pendingStart=payload;
    void this.send(payload);
  }
  private loadZone(id:string) {
    const z=this.snapshot?.library?.zones.find(x=>x.id===id);
    if(!z) return;
    if(!z.devices.every(d=>this.catalog?.lights.some(l=>l.id===d))) {this.status="Zone contains missing registry devices";return;}
    this.loadedLibraryRevision=this.snapshot?.library?.revision??-1;this.spatialPath="";this.selectedZone=z.id;this.zone=z.name;this.chosen=[...z.devices];this.selectedScene="";
  }
  private loadScene(id:string) {
    const s=this.snapshot?.library?.scenes.find(x=>x.id===id);
    const z=this.snapshot?.library?.zones.find(x=>x.id===s?.zone_id);
    if(!s||!z||!z.devices.every(d=>this.catalog?.lights.some(l=>l.id===d))) {this.status="Scene contains missing registry devices";return;}
    this.loadZone(z.id);this.selectedScene=s.id;this.sceneName=s.name;this.effect=s.effect.kind;this.layers=structuredClone(s.effect.layers??[]);this.tracks=structuredClone(s.effect.tracks??[]);this.timelineLoop=s.effect.loop??true;this.timelineAutoEnd=s.effect.auto_end??false;
    this.effectColor=[...s.effect.color];this.effectWidth=s.effect.width;this.period=s.effect.period_seconds;this.brightness=s.effect.brightness;this.stopPolicy=s.stop_policy;this.audioSource="";
  }
  private libraryWrite(kind:"zones"|"scenes",remove=false,create=false) {
    const state=this.snapshot, lib=state?.library;
    if(!state||!lib||lib.fault||this.pendingStart||this.pendingManual) return;
    const id=create?null:(kind==="zones"?this.selectedZone:this.selectedScene)||null;
    if(remove&&!id)return;
    if(kind==="scenes"&&!remove&&this.loadedLibraryRevision!==lib.revision){this.status="Library changed — reload the selected zone before saving";return;}
    if(id&&this.loadedLibraryRevision!==lib.revision){this.status="Library changed — reload the selected item before editing";return;}
    const value=remove?null:kind==="zones"?{name:this.zone,devices:[...this.chosen]}:
      {name:this.sceneName,zone_id:this.selectedZone,effect:{kind:this.effect,period_seconds:this.period,brightness:this.brightness,color:[...this.effectColor],width:this.effectWidth,...(this.effect==="layered"?{layers:structuredClone(this.layers)}:this.effect==="timeline"?{tracks:structuredClone(this.tracks),loop:this.timelineLoop,...(!this.timelineLoop&&this.timelineAutoEnd?{auto_end:true}:{})}:{})},stop_policy:this.stopPolicy};
    void this.send({op:remove?"library_delete":"library_save",instance_id:state.instance_id,revision:id?this.loadedLibraryRevision:lib.revision,kind,id,value});
  }
  private needsAudio(){return this.effect==="audio_spectrum"||(this.effect==="layered"&&this.layers.some(l=>l.effect.kind==="audio_spectrum"))||(this.effect==="timeline"&&this.tracks.some(t=>t.clips.some(c=>c.layer.effect.kind==="audio_spectrum")));}
  private validTimeline(){return this.validTargetMasks(this.chosen)&&this.tracks.some(t=>t.clips.length>0)&&this.tracks.every(t=>t.name.trim()&&t.clips.every(c=>[c.start,c.duration,c.fade_in,c.fade_out,c.layer.opacity,c.layer.effect.period_seconds,c.layer.effect.brightness,c.layer.effect.width].every(Number.isFinite)&&c.start>=0&&c.duration>0&&Number.isFinite(c.start+c.duration)&&c.start+c.duration>c.start&&c.fade_in>=0&&c.fade_out>=0&&c.fade_in+c.fade_out<=c.duration&&c.layer.opacity>=0&&c.layer.opacity<=1&&c.layer.effect.period_seconds>0&&c.layer.effect.brightness>=0&&c.layer.effect.brightness<=1&&c.layer.effect.width>0&&c.layer.effect.width<=1&&c.keyframes.every((k,i)=>Number.isFinite(k.time)&&Number.isFinite(k.value)&&k.time>=0&&k.time<=c.duration&&k.value>=0&&k.value<=1&&(i===0||k.time>c.keyframes[i-1].time))));}
  private validLayers(){return this.validTargetMasks(this.chosen)&&this.layers.length>0&&this.layers.every(l=>Number.isFinite(l.opacity)&&l.opacity>=0&&l.opacity<=1&&Number.isFinite(l.effect.period_seconds)&&l.effect.period_seconds>0&&Number.isFinite(l.effect.brightness)&&l.effect.brightness>=0&&l.effect.brightness<=1&&Number.isFinite(l.effect.width)&&l.effect.width>0&&l.effect.width<=1);}
  private validTargetMasks(devices:string[]){
    const groups=this.effect==="layered"?this.layers:this.effect==="timeline"?this.tracks:[];
    if(!groups.length)return true;
    const covered=new Set<string>();
    for(const g of groups){const mask=g.devices;
      if(mask!==undefined&&(!mask.length||new Set(mask).size!==mask.length||mask.some(d=>!devices.includes(d))))return false;
      if(!("clips" in g)||g.clips.length)for(const id of mask??devices)covered.add(id);
    }
    return devices.every(d=>covered.has(d));
  }
  private addLayer(){this.layers=[...this.layers,{effect:{kind:"static",period_seconds:4,brightness:.5,color:[1,1,1],width:.25},opacity:1,blend:"over"}];}
  private setLayer(index:number,patch:Partial<Layer>){this.layers=this.layers.map((l,i)=>i===index?{...l,...patch}:l);}
  private setLeaf(index:number,patch:Partial<LeafEffect>){this.setLayer(index,{effect:{...this.layers[index].effect,...patch}});}
  private moveLayer(index:number,delta:number){const next=[...this.layers];const dest=index+delta;if(dest<0||dest>=next.length)return;[next[index],next[dest]]=[next[dest],next[index]];this.layers=next;}
  private layerHex(rgb:number[]){return "#"+rgb.map(v=>Math.round(255*(v<=.0031308?12.92*v:1.055*Math.pow(v,1/2.4)-.055)).toString(16).padStart(2,"0")).join("");}
  private fromHex(hex:string){return [1,3,5].map(i=>{const v=parseInt(hex.slice(i,i+2),16)/255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);});}
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
    const sources=this.snapshot?.audio_sources??[];
    const lib=this.snapshot?.library;
    const libraryLocked=formLocked||!this.online||!!this.pendingManual||!!lib?.fault;
    const paths=lib?.spatial_paths?.filter(p=>p.zone_id===this.selectedZone)??[];
    const validZone=!!this.zone.trim()&&this.chosen.length>0;
    const validScene=this.validTargetMasks(lib?.zones.find(z=>z.id===this.selectedZone)?.devices??[])&&(this.effect!=="timeline"||this.validTimeline())&&(this.effect!=="layered"||this.validLayers())&&!!this.sceneName.trim()&&!!lib?.zones.some(z=>z.id===this.selectedZone)&&Number.isFinite(this.period)&&this.period>0&&Number.isFinite(this.brightness)&&this.brightness>=0&&this.brightness<=1;
    return html`
      <main>
        <header><div><h1>LightCadence</h1><p>${physical?"Hardware test workspace":"Live preview workspace"}</p></div><span class="badge" data-connected=${this.online?"yes":"no"}>${this.online?"Connected":"Disconnected"}</span></header>
        <p class="notice">${physical?"Hardware candidate — real lamp commands are enabled for enrolled devices only. Not hardware-qualified. Use explicit LightCadence HA actions for cooperating automations. Ordinary light services and Google Home are not intercepted. Colours are targets, not optical feedback.":"Preview only — no physical lamps are controlled. Colours below are engine targets, not device feedback."}</p>
        <div class="toolbar"><p role="status" aria-live="polite">${this.status}</p><button @click=${()=>this.refresh(true)} ?disabled=${this.busy}>Refresh</button></div>
        ${lib?.floorplans?html`<button aria-expanded=${String(this.floorplanOpen)} @click=${()=>this.floorplanOpen=!this.floorplanOpen}>${this.floorplanOpen?"Hide floorplan editor":"Open floorplan editor"}</button>`:nothing}
        ${lib?.floorplans?html`<section ?hidden=${!this.floorplanOpen} aria-labelledby="floorplan"><h2 id="floorplan">Floorplan editor</h2><lightcadence-floorplan .library=${lib} .lights=${this.catalog?.lights??[]} .instanceId=${this.snapshot?.instance_id??""} .disabled=${libraryLocked} .targetColors=${Object.fromEntries(Object.entries(this.snapshot?.targets??{}).map(([id,v])=>[id,color(v.target)]))} @floorplan-command=${(e:CustomEvent<LibraryCommand>)=>this.send(e.detail)}></lightcadence-floorplan></section>`:nothing}
        <div class="workspace">
          <section aria-labelledby="setup"><h2 id="setup">${physical?"New zone show":"New zone preview"}</h2>
            <label>Zone name <input aria-label="Zone name" title="Label for this independent preview session" .value=${this.zone} ?disabled=${formLocked} @input=${(e:Event)=>this.zone=(e.target as HTMLInputElement).value}></label>
            <label>Effect <select aria-label="Effect" .value=${this.effect} ?disabled=${formLocked} @change=${(e:Event)=>this.effect=(e.target as HTMLSelectElement).value}>${Object.entries(descriptions).map(([key,title])=>html`<option value=${key}>${title}</option>`)}</select></label>
            ${this.needsAudio()?html`<label>Audio source <select aria-label="Audio source" .value=${this.audioSource} ?disabled=${formLocked} @change=${(e:Event)=>this.audioSource=(e.target as HTMLSelectElement).value}><option value="">Select local PCM source</option>${sources.map(s=>html`<option value=${s.id}>${s.name} · ${s.status}</option>`)}</select></label><p>No audio is captured by this panel. Start an authorized local PCM sender first.</p>`:html`<label>Period (seconds) <input type="number" step="any" .value=${String(this.period)} ?disabled=${formLocked} @input=${(e:Event)=>this.period=(e.target as HTMLInputElement).valueAsNumber}></label>`}
            ${this.effect==="static"?html`<label>Static colour <input type="color" .value=${this.layerHex(this.effectColor)} ?disabled=${formLocked} @input=${(e:Event)=>{this.effectColor=this.fromHex((e.target as HTMLInputElement).value);this.requestUpdate();}}></label>`:nothing}
            ${this.effect==="layered"?html`<section aria-label="Layer editor"><h3>Scene layers</h3><p>Bottom to top. One mixed light target per lamp. No changes until an explicit start or save.</p>
              ${this.layers.map((l,i)=>html`<article><h3>Layer ${i+1}</h3>
                <label>Layer ${i+1} effect <select aria-label=${"Layer "+(i+1)+" effect"} .value=${l.effect.kind} ?disabled=${formLocked} @change=${(e:Event)=>this.setLeaf(i,{kind:(e.target as HTMLSelectElement).value})}>${Object.entries(descriptions).filter(([k])=>k!=="layered"&&k!=="timeline").map(([k,v])=>html`<option value=${k}>${v}</option>`)}</select></label>
                <label>Colour <input aria-label=${"Layer "+(i+1)+" colour"} type="color" .value=${this.layerHex(l.effect.color)} ?disabled=${formLocked} @input=${(e:Event)=>this.setLeaf(i,{color:this.fromHex((e.target as HTMLInputElement).value)})}></label>
                <label>Period (s) <input aria-label=${"Layer "+(i+1)+" period"} type="number" step="any" .value=${String(l.effect.period_seconds)} ?disabled=${formLocked} @input=${(e:Event)=>this.setLeaf(i,{period_seconds:(e.target as HTMLInputElement).valueAsNumber})}></label>
            <label>Brightness <input type="range" min="0" max="1" step=".01" .value=${String(l.effect.brightness)} ?disabled=${formLocked} @input=${(e:Event)=>this.setLeaf(i,{brightness:(e.target as HTMLInputElement).valueAsNumber})}> ${Math.round(l.effect.brightness*100)}%</label>
                <label>Scanner width <input type="number" min=".01" max="1" step=".01" .value=${String(l.effect.width)} ?disabled=${formLocked} @input=${(e:Event)=>this.setLeaf(i,{width:(e.target as HTMLInputElement).valueAsNumber})}></label>
                <label>Opacity <input type="range" min="0" max="1" step=".01" .value=${String(l.opacity)} ?disabled=${formLocked} @input=${(e:Event)=>this.setLayer(i,{opacity:(e.target as HTMLInputElement).valueAsNumber})}> ${Math.round(l.opacity*100)}%</label>
                <label>Blend <select aria-label=${"Layer "+(i+1)+" blend"} .value=${l.blend} ?disabled=${formLocked} @change=${(e:Event)=>this.setLayer(i,{blend:(e.target as HTMLSelectElement).value as Layer["blend"]})}><option value="over">Over</option><option value="add">Add</option><option value="max">Maximum</option></select></label>
                <p>Targets: ${l.devices===undefined?"All selected lamps":l.devices.length+" specific lamps"}</p>
                <button ?disabled=${formLocked||(!this.chosen.length&&l.devices===undefined)} @click=${()=>{const next={...l};if(next.devices===undefined)next.devices=[...this.chosen];else delete next.devices;this.layers=this.layers.map((x,j)=>i===j?next:x);}}>${l.devices===undefined?"Choose layer lamps":"Use all layer lamps"}</button>
                ${l.devices!==undefined?html`<div class="timeline-controls">${[...new Set([...this.chosen,...l.devices])].map(id=>html`<button aria-label=${"Layer "+(i+1)+" target "+this.name(id)} aria-pressed=${l.devices?.includes(id)===true} ?disabled=${formLocked} @click=${()=>this.setLayer(i,{devices:l.devices?.includes(id)?l.devices.filter(d=>d!==id):[...(l.devices??[]),id]})}>${this.name(id)}</button>`)}</div>`:nothing}
                <button aria-label=${"Move layer "+(i+1)+" down"}  ?disabled=${formLocked||i===0} @click=${()=>this.moveLayer(i,-1)}>Move down</button> <button aria-label=${"Move layer "+(i+1)+" up"} ?disabled=${formLocked||i===this.layers.length-1} @click=${()=>this.moveLayer(i,1)}>Move up</button> <button aria-label=${"Remove layer "+(i+1)} ?disabled=${formLocked} @click=${()=>this.layers=this.layers.filter((_,j)=>i!==j)}>Remove</button>
              </article>`)}
              <button ?disabled=${formLocked} @click=${()=>this.addLayer()}>Add layer</button></section>`:nothing}
            ${this.effect==="timeline"?html`<lightcadence-timeline .tracks=${this.tracks} .loop=${this.timelineLoop} .locked=${formLocked} .lamps=${this.chosen.map(id=>({id,name:this.name(id)}))} @timeline-change=${(e:CustomEvent<{tracks:Track[];loop:boolean}>)=>{this.tracks=e.detail.tracks;this.timelineLoop=e.detail.loop;if(this.timelineLoop)this.timelineAutoEnd=false;}}></lightcadence-timeline>${!this.timelineLoop?html`<label>End action <select aria-label="Timeline end action" .value=${this.timelineAutoEnd?"stop":"wait"} ?disabled=${formLocked} @change=${(e:Event)=>this.timelineAutoEnd=(e.target as HTMLSelectElement).value==="stop"}><option value="wait">End dark, wait for explicit Stop</option><option value="stop">Automatically apply stop behaviour</option></select></label><p>${this.timelineAutoEnd?"At natural end, release using the selected stop behaviour. Unconfirmed device restoration requires explicit retry.":"Lamps remain reserved until Stop."}</p>`:nothing}`:nothing}
            <label>Brightness <input type="range" min="0" max="1" step="0.01" .value=${String(this.brightness)} ?disabled=${formLocked} @input=${(e:Event)=>this.brightness=(e.target as HTMLInputElement).valueAsNumber}> <output>${Math.round(this.brightness*100)}%</output></label>
            ${physical?html`<label>Stop behaviour <select .value=${this.stopPolicy} ?disabled=${formLocked} @change=${(e:Event)=>this.stopPolicy=(e.target as HTMLSelectElement).value as StopPolicy}><option value="restore">Restore original state</option><option value="hold">Hold last state</option><option value="off">Turn off</option></select></label>`:nothing}
            ${paths.length?html`<label>Spatial effect path <select aria-label="Spatial effect path" .value=${this.spatialPath} ?disabled=${formLocked} @change=${(e:Event)=>this.spatialPath=(e.target as HTMLSelectElement).value}><option value="">Ordered lamps</option>${paths.map(p=>html`<option value=${p.floorplan_id+"/"+p.path_id} ?disabled=${!p.positions}>${p.name}${p.positions?"":" · missing placements"}</option>`)}</select></label>`:nothing}
            ${(this.effect==="timeline"||this.effect==="layered")&&!this.validTargetMasks(this.chosen)?html`<p role="alert">Every selected lamp must be targeted by a layer or nonempty track. Remove targets outside this selection.</p>`:nothing}
            <h3>Lamps · ${this.chosen.length} selected</h3>
            ${this.catalog?.lights.length?this.catalog.lights.map(l=>html`<label class="lamp"><input type="checkbox" .checked=${this.chosen.includes(l.id)} ?disabled=${formLocked||blocked.has(l.id)||(locked.has(l.id)&&!this.chosen.includes(l.id))} @change=${(e:Event)=>this.toggle(l.id,(e.target as HTMLInputElement).checked)}><span>${l.name}<small>${l.entity_id} · ${blocked.has(l.id)?"Recovery required":locked.has(l.id)?(physical?"In show":"In preview"):l.available?"Available":"Unavailable in HA"}</small><small>${observationLabel(l)}</small></span></label>`):html`<p>No registered lights available.</p>`}
            <button class="primary" ?disabled=${!this.online||this.busy||fault||this.chosen.some(id=>blocked.has(id))||(!this.pendingStart&&(!this.zone.trim()||!this.chosen.length||this.chosen.some(id=>locked.has(id))||!(this.period>0)||(this.needsAudio()&&!sources.some(s=>s.id===this.audioSource))||(this.effect==="layered"&&!this.validLayers())||(this.effect==="timeline"&&!this.validTimeline())))} @click=${()=>this.start()}>${this.pendingStart?"Retry same start":physical?"Start hardware test":"Start preview"}</button>
            ${this.pendingStart&&!this.busy?html`<button @click=${()=>{this.pendingStart=undefined;this.status="Start retry cleared; refresh server state before a new start.";void this.refresh(true);this.requestUpdate();}}>Clear retry</button>`:nothing}
          </section>
          <div class="right-stack">
          ${lib?html`<section aria-labelledby="library"><h2 id="library">Zones and scenes</h2>
            <p>${lib.fault?"Storage fault — editing disabled":lib.persistent?"Stored locally on engine":"Temporary library — configure local storage for persistence"}. Loading only fills the show controls.</p>
            <label>Saved zone <select aria-label="Saved zone" .value=${this.selectedZone} ?disabled=${libraryLocked} @change=${(e:Event)=>{const id=(e.target as HTMLSelectElement).value;if(id)this.loadZone(id);else this.selectedZone="";}}><option value="">Select zone</option>${lib.zones.map(z=>html`<option value=${z.id}>${z.name} · ${z.devices.length} lamps</option>`)}</select></label>
            <button ?disabled=${libraryLocked||!validZone} @click=${()=>this.libraryWrite("zones",false,true)}>Save new zone</button>
            <button ?disabled=${libraryLocked||!validZone||!this.selectedZone} @click=${()=>this.libraryWrite("zones")}>Update zone</button>
            <button ?disabled=${libraryLocked||!this.selectedZone||lib.scenes.some(s=>s.zone_id===this.selectedZone)} @click=${()=>this.libraryWrite("zones",true)}>Delete zone</button>
            <label>Saved scene <select aria-label="Saved scene" .value=${this.selectedScene} ?disabled=${libraryLocked} @change=${(e:Event)=>{const id=(e.target as HTMLSelectElement).value;if(id)this.loadScene(id);else this.selectedScene="";}}><option value="">Select scene</option>${lib.scenes.map(s=>html`<option value=${s.id}>${s.name}</option>`)}</select></label>
            <label>Scene name <input aria-label="Scene name" .value=${this.sceneName} ?disabled=${libraryLocked} @input=${(e:Event)=>this.sceneName=(e.target as HTMLInputElement).value}></label>
            <button ?disabled=${libraryLocked||!validScene} @click=${()=>this.libraryWrite("scenes",false,true)}>Save new scene</button>
            <button ?disabled=${libraryLocked||!validScene||!this.selectedScene} @click=${()=>this.libraryWrite("scenes")}>Update scene</button>
            <button ?disabled=${libraryLocked||!this.selectedScene} @click=${()=>this.libraryWrite("scenes",true)}>Delete scene</button>
          </section>`:nothing}
          <section aria-labelledby="running"><h2 id="running">${physical?"Running shows":"Running previews"} · ${active.length}</h2>
          ${active.length?active.map(s=>html`<article data-phase=${s.phase}><h3>${s.zone_id}</h3><p>${descriptions[s.effect.kind]??s.effect.kind} · ${s.phase} · ${s.devices.length} lamps</p>${s.effect.audio_source?html`<p>Audio: ${sources.find(a=>a.id===s.effect.audio_source)?.name??"Source disconnected"} · ${sources.find(a=>a.id===s.effect.audio_source)?.status??"ambient"}</p>`:nothing}
            ${s.timeline?html`<p>Timeline · ${s.timeline.playhead.toFixed(2)} / ${s.timeline.duration.toFixed(2)} s · ${s.timeline.ending?"Automatic stop in progress":s.phase==="stopping"?"Stop pending — inspect or retry explicitly":s.timeline.paused?"Paused":s.timeline.ended?"Ended — stop to release lamps":s.timeline.loop?"Looping":"Playing"}</p><progress aria-label="Timeline playhead" max=${s.timeline.duration} .value=${s.timeline.playhead}></progress>
            <div class="timeline-controls"><button ?disabled=${!this.online||this.busy||s.phase!=="running"} @click=${()=>this.controlTimeline(s,s.timeline?.paused?"resume":"pause")}>${s.timeline.paused?"Resume timeline":"Pause timeline"}</button>
            <button ?disabled=${!this.online||this.busy||s.phase!=="running"} @click=${()=>this.controlTimeline(s,"seek",0)}>Seek to start</button>
            <form @submit=${(e:SubmitEvent)=>{e.preventDefault();const input=(e.currentTarget as HTMLFormElement).querySelector("input");if(input)this.controlTimeline(s,"seek",input.valueAsNumber);}}>
            <label>Seek to seconds <input aria-label="Seek to seconds" type="number" required min="0" max=${s.timeline.duration} step="any" ?disabled=${!this.online||this.busy||s.phase!=="running"}></label>
            <button type="submit" ?disabled=${!this.online||this.busy||s.phase!=="running"}>Seek timeline</button></form></div>`:nothing}
            <div class="targets">${s.devices.map(id=>html`<div class="target"><span class="swatch" style=${"background:"+color(this.snapshot?.targets[id]?.target)}></span><span>${this.name(id)}</span><button title="Remove this lamp from this preview only; other sessions are unaffected" ?disabled=${!this.online||this.busy} @click=${()=>this.send({op:"detach",session_id:s.id,revision:s.revision,device_id:id})}>Detach</button></div>`)}</div>
            <button class="primary" ?disabled=${!this.online||this.busy||s.timeline?.ending===true} @click=${()=>this.send({op:"stop",session_id:s.id,revision:s.revision})} title="Stop only this preview session">${physical?"Stop show":"Stop preview"}</button>
          </article>`):html`<p>${physical?"No active shows. Choose enrolled lamps and an effect to begin.":"No active previews. Choose lamps and an effect to begin."}</p>`}
          </section>
        ${sources.length||this.needsAudio()?html`<section aria-labelledby="audio"><h2 id="audio">Audio analysis</h2><p>Local PCM · arrival clock only, playback synchronization unverified</p>${sources.length?sources.map(s=>html`<article><h3>${s.name}</h3><p>${s.status==="live"?"Live":s.status==="lost"?"Source lost — fading to ambient":"Waiting for PCM"} · BPM ${s.bpm??"—"} · ${s.beat_count} onsets</p>${s.bands?.map((v,i)=>html`<label>${["Bass","Mid","Treble"][i]} <meter min="0" max="1" .value=${v}>${Math.round(v*100)}%</meter></label>`)}</article>`):html`<p>No local audio source connected.</p>`}</section>`:nothing}
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
