import {LitElement,html,svg,css,nothing} from "lit";
export type Point=[number,number];
export type Feature={id:string;kind:string;name:string;points:Point[];device_id:string|null;zone_id:string|null};
export type Plan={id:string;name:string;revision:number;width:number;height:number;features:Feature[]};
export type SpatialPath={floorplan_id:string;path_id:string;name:string;zone_id:string;positions:Record<string,number>|null;missing:string[]};
type Zone={id:string;name:string;devices:string[]};
type Library={revision:number;floorplans?:Plan[];zones:Zone[];fault:boolean};
type Light={id:string;name:string};
type Draft={name:string;width:number;height:number;features:Feature[]};
const polygon=(kind:string)=>["room","zone","furniture"].includes(kind);
const lamp=(kind:string)=>["lamp","strip"].includes(kind);
const zoned=(kind:string)=>["zone","effect_path"].includes(kind);
const key=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,"0")).join("");
const blank=():Draft=>({name:"",width:10,height:8,features:[]});
const clone=<T,>(v:T):T=>structuredClone(v);
const kinds:Record<string,string>={room:"Room outline",zone:"Functional zone",furniture:"Furniture",wall:"Wall",opening:"Opening",lamp:"Point lamp",strip:"Light strip path",effect_path:"Effect path"};

class FloorplanEditor extends LitElement {
  static properties={library:{attribute:false},lights:{attribute:false},disabled:{type:Boolean},targetColors:{attribute:false},instanceId:{attribute:false},
    draft:{state:true},selected:{state:true},planId:{state:true},kind:{state:true},featureName:{state:true},device:{state:true},zone:{state:true},points:{state:true},snap:{state:true},message:{state:true},dirty:{state:true}};
  declare library?:Library;
  declare lights:Light[];
  declare disabled:boolean;
  declare targetColors:Record<string,string>;
  declare instanceId:string;
  declare draft:Draft;
  declare planId:string;
  declare selected:string;
  declare kind:string;
  declare featureName:string;
  declare device:string;
  declare zone:string;
  declare points:Point[];
  declare snap:number;
  declare message:string;
  declare dirty:boolean;
  private baseRevision=-1;
  private baseInstance="";
  private undoStack:Draft[]=[];
  private redoStack:Draft[]=[];
  private drag?:{id:string;index:number;pointer:number;before:Draft};
  constructor(){
    super();this.lights=[];this.disabled=false;this.targetColors={};this.instanceId="";
    this.draft=blank();this.planId="";this.selected="";this.kind="room";this.featureName="";this.device="";this.zone="";this.points=[];this.snap=.1;this.message="";this.dirty=false;
  }
  private load(id:string){
    const p=this.library?.floorplans?.find(p=>p.id===id);
    if(id&&!p)return;
    if(this.dirty||this.points.length){this.message="Discard draft first before loading another floor.";return;}
    this.draft=p?clone({name:p.name,width:p.width,height:p.height,features:p.features}):blank();
    this.planId=id;this.baseRevision=this.library?.revision??-1;this.baseInstance=this.instanceId;this.selected="";this.points=[];this.undoStack=[];this.redoStack=[];this.dirty=false;this.message="";
  }
  private change(next:Draft){
    if(this.disabled)return;
    if(this.baseRevision<0){this.baseRevision=this.library?.revision??-1;this.baseInstance=this.instanceId;}
    this.undoStack.push(clone(this.draft));this.redoStack=[];this.draft=next;this.dirty=true;
  }
  private history(redo:boolean){
    const from=redo?this.redoStack:this.undoStack,to=redo?this.undoStack:this.redoStack;
    const value=from.pop();if(!value)return;
    to.push(clone(this.draft));this.draft=value;this.dirty=true;this.selected="";
  }
  private point(e:PointerEvent):Point|undefined{
    const canvas=this.renderRoot.querySelector("svg");
    const m=canvas?.getScreenCTM();if(!m)return;
    const v=new DOMPoint(e.clientX,e.clientY).matrixTransform(m.inverse());
    const limit=(n:number,max:number)=>Math.max(0,Math.min(max,this.snap>0?Math.round(n/this.snap)*this.snap:n));
    return [limit(v.x,this.draft.width),limit(v.y,this.draft.height)];
  }
  private draw(e:PointerEvent){
    if(this.disabled||e.button!==0||this.drag)return;
    const p=this.point(e);if(!p)return;
    this.points=this.kind==="lamp"?[p]:[...this.points,p];
  }
  private add(){
    const min=this.kind==="lamp"?1:polygon(this.kind)?3:2;
    if(this.points.length<min||!this.featureName.trim()||(lamp(this.kind)&&!this.device)||(zoned(this.kind)&&!this.zone))return;
    if(lamp(this.kind)&&this.draft.features.some(f=>f.device_id===this.device)){this.message="Device already placed on this floor.";return;}
    const f:Feature={id:key(),kind:this.kind,name:this.featureName.trim(),points:clone(this.points),device_id:lamp(this.kind)?this.device:null,zone_id:zoned(this.kind)?this.zone:null};
    this.change({...this.draft,features:[...this.draft.features,f]});this.selected=f.id;this.points=[];this.featureName="";this.message="";
  }
  private dragStart(e:PointerEvent,id:string,index:number){
    e.stopPropagation();if(this.disabled||e.button!==0)return;
    this.drag={id,index,pointer:e.pointerId,before:clone(this.draft)};
    (e.currentTarget as SVGElement).setPointerCapture(e.pointerId);
  }
  private move(e:PointerEvent){
    const d=this.drag;if(!d||d.pointer!==e.pointerId)return;
    const p=this.point(e);if(!p)return;
    this.draft={...this.draft,features:this.draft.features.map(f=>f.id!==d.id?f:{...f,points:f.points.map((v,i)=>i===d.index?p:v)})};
  }
  private end(e:PointerEvent,cancel=false){
    const d=this.drag;if(!d||d.pointer!==e.pointerId)return;
    this.drag=undefined;
    const next=this.draft;this.draft=d.before;if(!cancel)this.change(next);
  }
  private vertex(id:string,index:number,axis:number,value:number){
    if(!Number.isFinite(value))return;
    this.change({...this.draft,features:this.draft.features.map(f=>f.id!==id?f:{...f,points:f.points.map((p,i)=>i!==index?p:axis===0?[value,p[1]]:[p[0],value])})});
  }
  private save(remove=false){
    if(this.disabled||!this.library||this.library.fault||this.baseRevision!==this.library.revision||this.baseInstance!==this.instanceId){this.message="Library changed — discard and reload before saving.";return;}
    if(!remove&&(!this.draft.name.trim()||!Number.isFinite(this.draft.width)||!Number.isFinite(this.draft.height)||this.draft.width<=0||this.draft.height<=0||this.points.length)){this.message="Finish drawing and enter valid floor dimensions.";return;}
    this.dispatchEvent(new CustomEvent("floorplan-command",{detail:{op:remove?"library_delete":"library_save",kind:"floorplans",id:this.planId||null,revision:this.baseRevision,instance_id:this.instanceId,value:remove?null:clone(this.draft)},bubbles:true,composed:true}));
  }
  // Only a matching successful response clears a draft; server refresh alone does not.
  confirmed(){this.dirty=false;this.points=[];this.undoStack=[];this.redoStack=[];this.load("");this.baseRevision=-1;this.baseInstance="";this.message="Floor library updated.";}
  render(){
    const f=this.draft.features.find(x=>x.id===this.selected),w=this.draft.width,h=this.draft.height;
    const valid=Number.isFinite(w)&&Number.isFinite(h)&&w>0&&h>0;
    const pts=(p:Point[])=>p.map(v=>v.join(",")).join(" ");
    const radius=valid?Math.min(w,h)/65:.1;
    return html`
      <div class="editor-controls">
        <label>Saved floor <select aria-label="Saved floor" .value=${this.planId} ?disabled=${this.disabled||this.dirty||!!this.points.length} @change=${(e:Event)=>this.load((e.target as HTMLSelectElement).value)}><option value="">New floor</option>${this.library?.floorplans?.map(p=>html`<option value=${p.id}>${p.name}</option>`)}</select></label>
        <button ?disabled=${this.disabled} @click=${()=>{this.dirty=false;this.points=[];this.load(this.library?.floorplans?.some(p=>p.id===this.planId)?this.planId:"");}}>Discard draft / reload</button>
        <label>Floor name <input aria-label="Floor name" .value=${this.draft.name} ?disabled=${this.disabled} @input=${(e:Event)=>this.change({...this.draft,name:(e.target as HTMLInputElement).value})}></label>
        <div class="pair">${(["width","height"] as const).map(axis=>html`<label>Floor ${axis} (m)<input aria-label=${"Floor "+axis+" (m)"} type="number" min=".01" step=".1" .value=${String(this.draft[axis])} ?disabled=${this.disabled} @change=${(e:Event)=>this.change({...this.draft,[axis]:(e.target as HTMLInputElement).valueAsNumber})}></label>`)}</div>
        <label>Geometry type <select aria-label="Geometry type" .value=${this.kind} ?disabled=${this.disabled} @change=${(e:Event)=>{this.kind=(e.target as HTMLSelectElement).value;this.points=[];this.selected="";}}>${Object.entries(kinds).map(([k,v])=>html`<option value=${k}>${v}</option>`)}</select></label>
        <label>Geometry name <input aria-label="Geometry name" .value=${this.featureName} ?disabled=${this.disabled} @input=${(e:Event)=>this.featureName=(e.target as HTMLInputElement).value}></label>
        ${lamp(this.kind)?html`<label>Registry lamp <select aria-label="Registry lamp" .value=${this.device} ?disabled=${this.disabled} @change=${(e:Event)=>this.device=(e.target as HTMLSelectElement).value}><option value="">Select lamp</option>${this.lights.map(l=>html`<option value=${l.id}>${l.name}</option>`)}</select></label>`:nothing}
        ${zoned(this.kind)?html`<label>Linked zone <select aria-label="Linked zone" .value=${this.zone} ?disabled=${this.disabled} @change=${(e:Event)=>this.zone=(e.target as HTMLSelectElement).value}><option value="">Select zone</option>${this.library?.zones.map(z=>html`<option value=${z.id}>${z.name}</option>`)}</select></label>`:nothing}
        <label>Snap (m) <select .value=${String(this.snap)} @change=${(e:Event)=>this.snap=Number((e.target as HTMLSelectElement).value)}><option value="0">Free</option><option value="0.1">0.1</option><option value="0.5">0.5</option><option value="1">1</option></select></label>
        <p>Tap or click vertices on the floor. Finish adds the shape to your draft. Select a saved shape below to move vertices.</p>
        <div class="buttons"><button ?disabled=${this.disabled||!this.points.length} @click=${()=>this.add()}>Finish geometry</button><button ?disabled=${this.disabled||!this.points.length} @click=${()=>this.points=this.points.slice(0,-1)}>Remove last point</button></div>
      </div>
      ${valid?html`<svg aria-label="Floor drawing surface" viewBox=${"0 0 "+w+" "+h} @pointerdown=${this.draw} @pointermove=${this.move} @pointerup=${(e:PointerEvent)=>this.end(e)} @pointercancel=${(e:PointerEvent)=>this.end(e,true)}>
        <rect width=${w} height=${h} fill="var(--primary-background-color,#202735)" stroke="var(--divider-color,#8290a2)" stroke-width=".025"/>
        ${this.draft.features.map(g=>svg`<g class=${"geometry "+g.kind} pointer-events="none">
          ${g.kind==="lamp"?svg`<circle cx=${g.points[0][0]} cy=${g.points[0][1]} r=${radius*2} fill=${this.targetColors[g.device_id??""]??"var(--primary-color,#448bd0)"}/>`:polygon(g.kind)?svg`<polygon points=${pts(g.points)} vector-effect="non-scaling-stroke"/>`:svg`<polyline points=${pts(g.points)} vector-effect="non-scaling-stroke" style=${g.kind==="strip"?"stroke:"+(this.targetColors[g.device_id??""]??"var(--primary-color,#448bd0)"):""}/>`}
          ${g.points.length?svg`<text x=${g.points[0][0]} y=${g.points[0][1]+radius*3} font-size=${radius*2.5}>${g.name}</text>`:nothing}</g>`)}
        ${this.points.length?svg`<polyline class="draft-line" points=${pts(this.points)} vector-effect="non-scaling-stroke"/>`:nothing}
        ${this.points.map(p=>svg`<circle cx=${p[0]} cy=${p[1]} r=${radius} fill="var(--primary-color,#448bd0)" pointer-events="none"/>`)}
        ${f?.points.map((p,i)=>svg`<circle class="vertex" cx=${p[0]} cy=${p[1]} r=${radius*2} @pointerdown=${(e:PointerEvent)=>this.dragStart(e,f.id,i)}/>`)}
      </svg>`:html`<p role="alert">Enter positive floor dimensions.</p>`}
      <label>Selected geometry <select aria-label="Selected geometry" .value=${this.selected} @change=${(e:Event)=>this.selected=(e.target as HTMLSelectElement).value}><option value="">Select shape</option>${this.draft.features.map(g=>html`<option value=${g.id}>${g.name}</option>`)}</select></label>
      ${f?html`<div class="vertices">${f.points.map((p,i)=>html`<div class="pair">${p.map((n,a)=>html`<label>Vertex ${i+1} ${a?"Y":"X"}<input aria-label=${"Vertex "+(i+1)+(a?" Y":" X")} type="number" step=".1" .value=${String(n)} ?disabled=${this.disabled} @change=${(e:Event)=>this.vertex(f.id,i,a,(e.target as HTMLInputElement).valueAsNumber)}></label>`)}</div>`)}</div><button ?disabled=${this.disabled} @click=${()=>{this.change({...this.draft,features:this.draft.features.filter(g=>g.id!==f.id)});this.selected="";}}>Delete geometry</button>`:nothing}
      <div class="buttons"><button ?disabled=${this.disabled||!this.undoStack.length} @click=${()=>this.history(false)}>Undo</button><button ?disabled=${this.disabled||!this.redoStack.length} @click=${()=>this.history(true)}>Redo</button><button ?disabled=${this.disabled||!this.dirty} @click=${()=>this.save()}>Save floor</button><button ?disabled=${this.disabled||!this.planId||this.dirty} @click=${()=>this.save(true)}>Delete floor</button></div>
      <p role="status">${this.message|| (this.dirty?"Unsaved geometry":"No unsaved geometry")}</p>`;
  }
  static styles=css`
    :host{display:block;min-inline-size:0;color:var(--primary-text-color,#eef1f5)}
    *{box-sizing:border-box} .pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.buttons{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
    label{display:block;margin:8px 0;min-inline-size:0} input,select,button{font:inherit;color:inherit;min-block-size:44px;min-inline-size:0;background:var(--secondary-background-color,#303948);border:1px solid var(--divider-color,#657080);border-radius:6px;padding:8px}input,select{inline-size:100%;display:block}button:disabled{opacity:.5}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#448bd0)}
    svg{display:block;inline-size:100%;min-block-size:180px;aspect-ratio:auto;touch-action:none;border:1px solid var(--divider-color,#657080);border-radius:8px} .geometry polygon{fill:var(--primary-color,#448bd0);fill-opacity:.13;stroke:var(--primary-color,#448bd0);stroke-width:1.5}.geometry.furniture polygon{fill:var(--secondary-text-color,#aab3c0);stroke:var(--secondary-text-color,#aab3c0)}.geometry.zone polygon{stroke-dasharray:5 4}.geometry polyline,.draft-line{fill:none;stroke:var(--primary-text-color,#eef1f5);stroke-width:2}.geometry.opening polyline,.geometry.effect_path polyline{stroke-dasharray:5 3}.geometry.strip polyline{stroke-width:5}.geometry text{fill:var(--primary-text-color,#eef1f5);pointer-events:none}.vertex{fill:var(--card-background-color,#202735);stroke:var(--primary-color,#448bd0);stroke-width:.03;cursor:grab}.draft-line{stroke:var(--primary-color,#448bd0);pointer-events:none}
  `;
}
customElements.define("lightcadence-floorplan",FloorplanEditor);
