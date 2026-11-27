import {LitElement, html, css} from "lit";
type Binding={model:string;firmware:string;transport:string;device_id?:string};
type Point={goal:number[];command:number[]};
type Level={goal:number;command:number};
export type CalibrationRow={id:string;name:string;selector:Binding;points:Point[];brightness:Level[];minimum:number;revision:number};
type Raw={rgb:number[];brightness:number};
type Target={red:number;green:number;blue:number;brightness:number};
type Comparison={reference:string;targets:Record<string,Target>};
type Session={id:string;phase:string;revision:number;devices:string[];comparison?:Comparison};
type Snap={instance_id:string;calibration_bindings?:Record<string,Binding>;sessions:Session[];library?:{revision:number;calibrations?:CalibrationRow[];fault:boolean;persistent:boolean}};
type Lamp={id:string;name:string;transport?:{white_kelvin?:number}|null};
export type CalibrationCommand=
  {op:"calibration_start";request_id:string;instance_id:string;devices:string[];reference:string;targets:Record<string,Raw>}|
  {op:"calibration_adjust";instance_id:string;session_id:string;revision:number;targets:Record<string,Raw>}|
  {op:"library_save"|"library_delete";kind:"calibrations";instance_id:string;revision:number;id:string|null;value:Record<string,unknown>|null};
const hex=(rgb:number[])=>"#"+rgb.map(v=>v.toString(16).padStart(2,"0")).join("");
const rgb=(value:string)=>[1,3,5].map(i=>parseInt(value.slice(i,i+2),16));
const toHex=(t:Target)=>hex([t.red,t.green,t.blue].map(v=>Math.round(255*(v<=.0031308?12.92*v:1.055*v**(1/2.4)-.055))));
class CalibrationAssistant extends LitElement {
  static properties={lights:{attribute:false},snapshot:{attribute:false},disabled:{type:Boolean},reference:{state:true},device:{state:true},goal:{state:true},command:{state:true},level:{state:true},outLevel:{state:true},points:{state:true},curve:{state:true},minimum:{state:true},title:{state:true},model:{state:true},selected:{state:true},draftRevision:{state:true}};
  declare lights:Lamp[];declare snapshot?:Snap;declare disabled:boolean;
  declare reference:string;declare device:string;declare goal:string;declare command:string;declare level:number;declare outLevel:number;declare points:Point[];declare curve:Level[];declare minimum:number;declare title:string;declare model:boolean;declare selected:string;declare draftRevision:number;
  constructor(){super();this.lights=[];this.disabled=false;this.reference="";this.device="";this.goal="#ffffff";this.command="#ffffff";this.level=50;this.outLevel=50;this.points=[];this.curve=[];this.minimum=1;this.title="";this.model=false;this.selected="";this.draftRevision=-1;}
  private emit(command:CalibrationCommand){this.dispatchEvent(new CustomEvent("calibration-command",{detail:command,bubbles:true,composed:true}));}
  private get active(){return this.snapshot?.sessions.find(s=>s.phase==="running"&&s.comparison&&s.devices.length===2&&s.comparison.reference===this.reference&&s.devices.includes(this.device));}
  private get valid(){return !!this.reference&&!!this.device&&this.reference!==this.device&&[this.level,this.outLevel].every(v=>Number.isFinite(v)&&v>=1&&v<=100);}
  private get targets(){return {[this.reference]:{rgb:rgb(this.goal),brightness:this.level/100},[this.device]:{rgb:rgb(this.command),brightness:this.outLevel/100}};}
  private compare(){const s=this.snapshot;if(!s||!this.valid||this.disabled)return;const a=this.active;
    if(a)this.emit({op:"calibration_adjust",instance_id:s.instance_id,session_id:a.id,revision:a.revision,targets:this.targets});
    else this.emit({op:"calibration_start",instance_id:s.instance_id,request_id:crypto.randomUUID(),devices:[this.reference,this.device],reference:this.reference,targets:this.targets});}
  private choose(device:string){this.device=device;this.selected="";this.points=[];this.curve=[];this.minimum=1;this.title="";this.model=false;this.draftRevision=this.snapshot?.library?.revision??-1;}
  private load(id:string){const row=this.snapshot?.library?.calibrations?.find(p=>p.id===id);if(!row)return;this.selected=id;this.title=row.name;this.model=!row.selector.device_id;this.points=structuredClone(row.points);this.curve=structuredClone(row.brightness);this.minimum=row.minimum*100;this.draftRevision=this.snapshot?.library?.revision??-1;}
  private save(remove=false){const s=this.snapshot,b=s?.calibration_bindings?.[this.device],lib=s?.library;if(!s||!b||!lib||this.disabled)return;
    const value={name:this.title,selector:{...b,...(this.model?{}:{device_id:this.device})},points:this.points,brightness:this.curve,minimum:this.minimum/100};
    this.emit({op:remove?"library_delete":"library_save",kind:"calibrations",instance_id:s.instance_id,revision:this.draftRevision,id:this.selected||null,value:remove?null:value});}
  render(){const s=this.snapshot,lib=s?.library,b=s?.calibration_bindings?.[this.device],active=this.active;
    const lamps=this.lights.filter(l=>s?.calibration_bindings?.[l.id]&&l.transport?.white_kelvin===undefined);
    const profiles=(lib?.calibrations??[]).filter(p=>b&&p.selector.model===b.model&&p.selector.firmware===b.firmware&&p.selector.transport===b.transport&&(!p.selector.device_id||p.selector.device_id===this.device));
    const confirmed=active?.comparison&&active.comparison.targets[this.reference]&&active.comparison.targets[this.device]&&toHex(active.comparison.targets[this.reference])===this.goal&&toHex(active.comparison.targets[this.device])===this.command&&Math.abs(active.comparison.targets[this.reference].brightness-this.level/100)<1e-8&&Math.abs(active.comparison.targets[this.device].brightness-this.outLevel/100)<1e-8;
    const stale=this.draftRevision!==lib?.revision;
    return html`<p>Visual RGB comparison — choose any two enrolled RGB lamps. Compare sends real light commands only after pressing Start/Apply. Raw test targets bypass calibration. Stop the comparison in Running shows to restore originals. Other HA/LAN clients are not intercepted.</p>
    <div class="cols"><label>Reference lamp<select aria-label="Calibration reference" .value=${this.reference} ?disabled=${this.disabled||!!active} @change=${(e:Event)=>this.reference=(e.target as HTMLSelectElement).value}><option value="">Choose reference</option>${lamps.map(l=>html`<option value=${l.id}>${l.name}</option>`)}</select></label>
    <label>Lamp to calibrate<select aria-label="Calibration lamp" .value=${this.device} ?disabled=${this.disabled||!!active} @change=${(e:Event)=>this.choose((e.target as HTMLSelectElement).value)}><option value="">Choose lamp</option>${lamps.map(l=>html`<option value=${l.id}>${l.name}</option>`)}</select></label>
    <label>Reference colour<input aria-label="Calibration goal colour" type="color" .value=${this.goal} @input=${(e:Event)=>this.goal=(e.target as HTMLInputElement).value}></label>
    <label>Adjusted lamp colour<input aria-label="Calibration command colour" type="color" .value=${this.command} @input=${(e:Event)=>this.command=(e.target as HTMLInputElement).value}></label>
    <label>Reference brightness (%)<input type="number" min="1" max="100" .value=${String(this.level)} @input=${(e:Event)=>this.level=(e.target as HTMLInputElement).valueAsNumber}></label>
    <label>Adjusted brightness (%)<input type="number" min="1" max="100" .value=${String(this.outLevel)} @input=${(e:Event)=>this.outLevel=(e.target as HTMLInputElement).valueAsNumber}></label></div>
    <button ?disabled=${this.disabled||!this.valid} @click=${()=>this.compare()}>${active?"Apply comparison targets":"Start comparison"}</button>
    <p>${confirmed?"Engine targets accepted — visually check both lamps before recording.":"Apply the current targets before recording."}</p>
    <button ?disabled=${this.disabled||!confirmed} @click=${()=>{const a={goal:rgb(this.goal),command:rgb(this.command)};this.points=[...this.points.filter(p=>hex(p.goal)!==this.goal),a];}}>Record colour point</button>
    <button ?disabled=${this.disabled||!confirmed} @click=${()=>{this.curve=[...this.curve.filter(p=>p.goal!==this.level/100),{goal:this.level/100,command:this.outLevel/100}].sort((a,b)=>a.goal-b.goal);}}>Record brightness point</button>
    <p>Colour anchors: ${this.points.length}; brightness anchors: ${this.curve.length}</p>
    ${this.points.map((p,i)=>html`<div>${hex(p.goal)} → ${hex(p.command)} <button @click=${()=>this.points=this.points.filter((_,j)=>i!==j)}>Remove colour point</button></div>`)}
    ${this.curve.map((p,i)=>html`<div>${p.goal*100}% → ${p.command*100}% <button @click=${()=>this.curve=this.curve.filter((_,j)=>i!==j)}>Remove brightness point</button></div>`)}
    <label>Load existing profile<select .value=${this.selected} @change=${(e:Event)=>{const id=(e.target as HTMLSelectElement).value;if(id)this.load(id);else this.choose(this.device);}}><option value="">New profile</option>${profiles.map(p=>html`<option value=${p.id}>${p.name}</option>`)}</select></label>
    <label>Profile name<input .value=${this.title} @input=${(e:Event)=>this.title=(e.target as HTMLInputElement).value}></label>
    <label><input type="checkbox" .checked=${this.model} ?disabled=${!!this.selected} @change=${(e:Event)=>this.model=(e.target as HTMLInputElement).checked}> Reusable model / firmware / transport profile (otherwise this lamp only)</label>
    <label>Minimum stable brightness (%)<input type="number" min="0" max="100" .value=${String(this.minimum)} @input=${(e:Event)=>this.minimum=(e.target as HTMLInputElement).valueAsNumber}></label>
    <p>${lib?.persistent?"Owner-local persistent library":"Volatile library — profiles lost on restart"}. ${stale?"Library changed: reload the profile or start a new draft.":""} Stop ordinary shows before saving.</p>
    <button ?disabled=${this.disabled||!b||!this.title.trim()||stale||!!lib?.fault||!Number.isFinite(this.minimum)||this.minimum<0||this.minimum>100} @click=${()=>this.save()}>Save calibration profile</button>
    <button ?disabled=${this.disabled||!this.selected||stale} @click=${()=>this.save(true)}>Delete calibration profile</button>`;
  }
  static styles=css`:host{display:block;min-width:0}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,13rem),1fr));gap:12px}label{display:block;margin:8px 0}select,input:not([type=checkbox]){display:block;width:100%;box-sizing:border-box}button,input,select{font:inherit;min-height:44px;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:6px;padding:6px}button{margin:4px}button:disabled{opacity:.5}input[type=color]{padding:4px}p{color:var(--secondary-text-color)}`;
}
customElements.define("lightcadence-calibration",CalibrationAssistant);
