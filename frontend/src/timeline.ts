import {LitElement, html, css} from "lit";

type Leaf={kind:string;period_seconds:number;brightness:number;color:number[];width:number};
type Key={time:number;value:number;curve:"linear"|"hold"|"smooth"};
type Clip={start:number;duration:number;fade_in:number;fade_out:number;layer:{effect:Leaf;opacity:number;blend:"over"|"add"|"max"};keyframes:Key[]};
export type Track={name:string;clips:Clip[];devices?:string[]};

class TimelineEditor extends LitElement {
  static properties={tracks:{attribute:false},loop:{type:Boolean},locked:{type:Boolean},lamps:{attribute:false},selected:{state:true}};
  declare tracks:Track[];
  declare loop:boolean;
  declare locked:boolean;
  declare selected:string;
  declare lamps:{id:string;name:string}[];
  private drag?:{pointer:number;bar:HTMLElement;tracks:Track[];i:number;j:number;x:number;start:number;duration:number;total:number;width:number;next:number};
  constructor(){super();this.tracks=[];this.loop=true;this.locked=false;this.lamps=[];this.selected="";}
  private change(tracks:Track[],loop=this.loop){
    if(this.locked)return;
    this.dispatchEvent(new CustomEvent("timeline-change",{detail:{tracks,loop},bubbles:true,composed:true}));
  }
  private edit(fn:(tracks:Track[])=>void){const next=structuredClone(this.tracks);fn(next);this.change(next);}
  private trackMove(i:number,delta:number){this.edit(t=>{const dest=i+delta;if(dest>=0&&dest<t.length){[t[i],t[dest]]=[t[dest],t[i]];}});this.selected="";}
  private clipMove(i:number,j:number,delta:number){this.edit(t=>{const clips=t[i].clips,k=j+delta;if(k>=0&&k<clips.length){[clips[j],clips[k]]=[clips[k],clips[j]];}});this.selected="";}
  private addClip(i:number){this.edit(t=>{const clips=t[i].clips;const start=clips.reduce((n,c)=>Math.max(n,Number.isFinite(c.start+c.duration)?c.start+c.duration:0),0);clips.push({start,duration:4,fade_in:0,fade_out:0,layer:{effect:{kind:"static",period_seconds:4,brightness:.7,color:[1,0,0],width:.25},opacity:1,blend:"over"},keyframes:[]});this.selected=i+"/"+(clips.length-1);});}
  private setClip(i:number,j:number,fn:(clip:Clip)=>void){this.edit(t=>fn(t[i].clips[j]));}
  private dragStart(e:PointerEvent,i:number,j:number,total:number){
    if(this.locked||!e.isPrimary||e.button!==0||this.drag)return;
    const bar=e.currentTarget as HTMLElement, lane=bar.parentElement, clip=this.tracks[i]?.clips[j];
    if(!lane||!clip||!Number.isFinite(clip.start+clip.duration)||clip.duration<=0)return;
    const width=lane.getBoundingClientRect().width;
    if(width<=0)return;
    this.selected=i+"/"+j;
    this.drag={pointer:e.pointerId,bar,tracks:this.tracks,i,j,x:e.clientX,start:clip.start,duration:clip.duration,total,width,next:clip.start};
    bar.setPointerCapture(e.pointerId);
  }
  private dragMove(e:PointerEvent){
    const d=this.drag;
    if(!d||e.pointerId!==d.pointer)return;
    if(this.locked||this.tracks!==d.tracks){this.dragEnd(e,false);return;}
    // The ruler is fixed for the gesture. Numeric editing can extend the end.
    d.next=Math.max(0,Math.min(Math.max(0,d.total-d.duration),Math.round((d.start+(e.clientX-d.x)*d.total/d.width)*10)/10));
    d.bar.style.transform="translateX("+((d.next-d.start)/d.total*d.width)+"px)";
  }
  private dragEnd(e:PointerEvent,commit:boolean){
    const d=this.drag;
    if(!d||e.pointerId!==d.pointer)return;
    this.drag=undefined;d.bar.style.transform="";
    if(d.bar.hasPointerCapture(e.pointerId))d.bar.releasePointerCapture(e.pointerId);
    if(commit&&!this.locked&&this.tracks===d.tracks&&Math.abs(e.clientX-d.x)>3&&d.next!==d.start)this.setClip(d.i,d.j,c=>c.start=d.next);
  }
  disconnectedCallback(){
    if(this.drag){this.drag.bar.style.transform="";this.drag=undefined;}
    super.disconnectedCallback();
  }
  private hex(rgb:number[]){return "#"+rgb.map(v=>Math.round(255*(v<=.0031308?12.92*v:1.055*Math.pow(v,1/2.4)-.055)).toString(16).padStart(2,"0")).join("");}
  private rgb(hex:string){return [1,3,5].map(i=>{const v=parseInt(hex.slice(i,i+2),16)/255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);});}
  render(){
    const duration=this.tracks.reduce((d,t)=>t.clips.reduce((n,c)=>Math.max(n,Number.isFinite(c.start+c.duration)?c.start+c.duration:0),d),0);
    const total=duration||1;
    const [i,j]=this.selected.split("/").map(Number),clip=this.selected?this.tracks[i]?.clips[j]:undefined;
    return html`<section aria-label="Timeline editor"><h3>Timeline Studio</h3><p>Seconds · tracks and clips mix bottom to top in list order. Overlapping clips use fades and blend rules. No output until explicit start. Drag a clip within the ruler (0.1 s snap); use the numeric inspector to extend the timeline.</p>
    <label class="inline"><input type="checkbox" .checked=${this.loop} ?disabled=${this.locked} @change=${(e:Event)=>this.change(this.tracks,(e.target as HTMLInputElement).checked)}> Loop timeline</label>
    <p>Duration: ${duration.toFixed(2)} s. Set an explicit end action below the timeline.</p>
    <div class="ruler">${[0,.25,.5,.75,1].map(f=>html`<span>${(total*f).toFixed(1)} s</span>`)}</div>
    ${this.tracks.map((t,ti)=>html`<article><label>Track name <input aria-label=${"Track "+(ti+1)+" name"} .value=${t.name} ?disabled=${this.locked} @change=${(e:Event)=>this.edit(ts=>ts[ti].name=(e.target as HTMLInputElement).value)}></label>
      <p>Targets: ${t.devices===undefined?"All selected lamps":t.devices.length+" specific lamps"}</p>
      <button ?disabled=${this.locked||(!this.lamps.length&&t.devices===undefined)} @click=${()=>this.edit(ts=>{if(ts[ti].devices===undefined)ts[ti].devices=this.lamps.map(l=>l.id);else delete ts[ti].devices;})}>${t.devices===undefined?"Choose track lamps":"Use all track lamps"}</button>
      ${t.devices!==undefined?html`<div class="actions" aria-label=${"Track "+(ti+1)+" lamp targets"}>${[...this.lamps,...t.devices.filter(id=>!this.lamps.some(l=>l.id===id)).map(id=>({id,name:"Outside selection: "+id}))].map(l=>html`<button aria-label=${"Track "+(ti+1)+" target "+l.name} aria-pressed=${t.devices?.includes(l.id)===true} ?disabled=${this.locked} @click=${()=>this.edit(ts=>{const ds=ts[ti].devices??[];ts[ti].devices=ds.includes(l.id)?ds.filter(d=>d!==l.id):[...ds,l.id];})}>${l.name}</button>`)}</div>`:""}
      <div class="lane" aria-label=${"Track "+(ti+1)+" time ruler"}>${t.clips.map((c,ci)=>html`<div class="cliprow"><button type="button" class="clipbar" aria-label=${"Move clip "+(ti+1)+"/"+(ci+1)} title="Drag to move in time; numeric editing can extend the ruler" ?disabled=${this.locked} style=${"margin-inline-start:"+Math.min(100,Math.max(0,c.start/total*100||0))+"%;width:"+Math.min(100,Math.max(0,c.duration/total*100||0))+"%"} @pointerdown=${(e:PointerEvent)=>this.dragStart(e,ti,ci,total)} @pointermove=${(e:PointerEvent)=>this.dragMove(e)} @pointerup=${(e:PointerEvent)=>{this.dragMove(e);this.dragEnd(e,true);}} @pointercancel=${(e:PointerEvent)=>this.dragEnd(e,false)} @lostpointercapture=${(e:PointerEvent)=>this.dragEnd(e,false)} @click=${()=>this.selected=ti+"/"+ci}></button></div>`)}</div>
      <div class="actions">${t.clips.map((c,ci)=>html`<button aria-pressed=${this.selected===ti+"/"+ci} ?disabled=${this.locked} @click=${()=>this.selected=ti+"/"+ci}>Clip ${ci+1}: ${c.layer.effect.kind} · ${c.start} s</button>`)}
        <button ?disabled=${this.locked} @click=${()=>this.addClip(ti)}>Add clip</button>
        <button aria-label=${"Move track "+(ti+1)+" earlier"} ?disabled=${this.locked||ti===0} @click=${()=>this.trackMove(ti,-1)}>Earlier</button>
        <button aria-label=${"Move track "+(ti+1)+" later"} ?disabled=${this.locked||ti===this.tracks.length-1} @click=${()=>this.trackMove(ti,1)}>Later</button>
        <button aria-label=${"Remove track "+(ti+1)} ?disabled=${this.locked} @click=${()=>{this.edit(ts=>ts.splice(ti,1));this.selected="";}}>Remove track</button></div></article>`)}
    <button ?disabled=${this.locked} @click=${()=>this.edit(t=>t.push({name:"Track "+(t.length+1),clips:[]}))}>Add track</button>
    ${clip?html`<article><h3>Clip inspector · ${i+1}/${j+1}</h3>
      <label>Clip effect <select aria-label="Clip effect" .value=${clip.layer.effect.kind} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.layer.effect.kind=(e.target as HTMLSelectElement).value)}>${["static","rainbow","scanner","pulse","audio_spectrum"].map(k=>html`<option value=${k}>${k}</option>`)}</select></label>
      ${(["start","duration","fade_in","fade_out"] as const).map(k=>html`<label>${k} (s) <input aria-label=${"Clip "+k} type="number" step="any" min="0" .value=${String(clip[k])} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c[k]=(e.target as HTMLInputElement).valueAsNumber)}></label>`)}
      ${(["period_seconds","brightness","width"] as const).map(k=>html`<label>${k} <input aria-label=${"Clip "+k} type="number" step="any" .value=${String(clip.layer.effect[k])} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.layer.effect[k]=(e.target as HTMLInputElement).valueAsNumber)}></label>`)}
      <label>Colour <input aria-label="Clip colour" type="color" .value=${this.hex(clip.layer.effect.color)} ?disabled=${this.locked} @input=${(e:Event)=>this.setClip(i,j,c=>c.layer.effect.color=this.rgb((e.target as HTMLInputElement).value))}></label>
      <label>Opacity <input aria-label="Clip opacity" type="number" min="0" max="1" step="any" .value=${String(clip.layer.opacity)} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.layer.opacity=(e.target as HTMLInputElement).valueAsNumber)}></label>
      <label>Blend <select aria-label="Clip blend" .value=${clip.layer.blend} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.layer.blend=(e.target as HTMLSelectElement).value as Clip["layer"]["blend"])}>${["over","add","max"].map(k=>html`<option value=${k}>${k}</option>`)}</select></label>
      <h3>Opacity keyframes</h3><p>Clip-local seconds; increasing times, outgoing curve.</p>
      ${clip.keyframes.map((k,ki)=>html`<div class="key"><label>Time <input aria-label=${"Key "+(ki+1)+" time"} type="number" min="0" step="any" .value=${String(k.time)} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.keyframes[ki].time=(e.target as HTMLInputElement).valueAsNumber)}></label>
        <label>Value <input aria-label=${"Key "+(ki+1)+" value"} type="number" min="0" max="1" step="any" .value=${String(k.value)} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.keyframes[ki].value=(e.target as HTMLInputElement).valueAsNumber)}></label>
        <label>Curve <select aria-label=${"Key "+(ki+1)+" curve"} .value=${k.curve} ?disabled=${this.locked} @change=${(e:Event)=>this.setClip(i,j,c=>c.keyframes[ki].curve=(e.target as HTMLSelectElement).value as Key["curve"])}>${["linear","hold","smooth"].map(v=>html`<option value=${v}>${v}</option>`)}</select></label>
        <button aria-label=${"Remove key "+(ki+1)} ?disabled=${this.locked} @click=${()=>this.setClip(i,j,c=>c.keyframes.splice(ki,1))}>Remove</button></div>`)}
      <div class="actions"><button ?disabled=${this.locked||(clip.keyframes.at(-1)?.time??-1)>=clip.duration} @click=${()=>this.setClip(i,j,c=>{const last=c.keyframes.at(-1)?.time;c.keyframes.push({time:last===undefined?0:Math.min(c.duration,last+1),value:1,curve:"linear"});})}>Add keyframe</button>
      <button ?disabled=${this.locked||j===0} @click=${()=>this.clipMove(i,j,-1)}>Earlier priority</button><button ?disabled=${this.locked||j===this.tracks[i].clips.length-1} @click=${()=>this.clipMove(i,j,1)}>Later priority</button>
      <button ?disabled=${this.locked} @click=${()=>{this.edit(t=>t[i].clips.splice(j,1));this.selected="";}}>Remove clip</button></div>
    </article>`:html`<p>Select a clip to edit timing and keys.</p>`}
    </section>`;
  }
  static styles=css`
    :host{display:block;min-inline-size:0;color:var(--primary-text-color);font:inherit}
    *,*::before,*::after{box-sizing:border-box}
    section,article{min-inline-size:0;border:1px solid var(--divider-color,#777);border-radius:8px;padding:12px;margin-block:8px}
    label{display:block;min-inline-size:0;margin-block:6px;overflow-wrap:anywhere}
    input:not([type=checkbox]),select{display:block;width:100%;min-width:0;min-height:44px;background:var(--card-background-color,#292929);color:var(--primary-text-color);border:1px solid var(--divider-color,#777);border-radius:5px;padding:8px;font:inherit}
    button{min-height:44px;min-width:44px;max-width:100%;white-space:normal;overflow-wrap:anywhere;color:var(--primary-text-color);background:var(--card-background-color,#292929);border:1px solid var(--divider-color,#777);border-radius:6px;padding:8px;font:inherit}
    button[aria-pressed=true]{border:2px solid var(--primary-color,#448aff)}
    .inline{display:flex;align-items:center;gap:8px;min-height:44px}.actions{display:flex;flex-wrap:wrap;gap:6px}
    .ruler{display:flex;justify-content:space-between;font-size:.75rem;gap:3px;flex-wrap:wrap}
    .lane{min-height:22px;border-inline:1px solid var(--divider-color,#777);background:var(--secondary-background-color,#383838);margin-block:8px;min-width:0}
    .cliprow{min-width:0;padding-block:2px}.clipbar{display:block;height:44px;min-width:0;min-height:44px;margin-block:0;padding:0;background:var(--primary-color,#448aff);border-radius:4px;max-width:100%;touch-action:none;cursor:grab}.clipbar:active{cursor:grabbing}
    .key{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,100px),1fr));align-items:end;gap:6px}
    :focus-visible{outline:2px solid var(--primary-color,#448aff);outline-offset:2px}
    button:disabled{opacity:.5}
  `;
}
customElements.define("lightcadence-timeline",TimelineEditor);
