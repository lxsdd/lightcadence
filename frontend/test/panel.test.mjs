import {test} from "node:test";
import assert from "node:assert/strict";
import {chromium} from "playwright";
import {createServer} from "node:http";
import {readFile} from "node:fs/promises";
const bundle=await readFile(new URL("../../custom_components/lightcadence/frontend/panel.js",import.meta.url));
const server=createServer((req,res)=>{
  if(req.url==="/panel.js"){res.setHeader("Content-Type","text/javascript");res.end(bundle);return;}
  res.setHeader("Content-Type","text/html");res.end('<!doctype html><meta name="viewport" content="width=device-width"><script type="module" src="/panel.js"></script><lightcadence-panel></lightcadence-panel>');
});
await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
const origin="http://127.0.0.1:"+server.address().port;
const browser=await chromium.launch({headless:true});
try {
  for(const viewport of [{width:1280,height:900},{width:390,height:844}]) {
    await test("production Lit preview path "+viewport.width,async()=>{
      const page=await browser.newPage({viewport});
      const errors=[];page.on("pageerror",e=>errors.push(e.message));
      await page.goto(origin);
      await page.evaluate(async()=>{
        await customElements.whenDefined("lightcadence-panel");
        const el=document.querySelector("lightcadence-panel");
        window.fixture={calls:[],sessions:[],targets:{},fail:false};
        const f=window.fixture;
        const snapshot=()=>({schema_version:1,instance_id:"synthetic",output_mode:"preview",physical_output_enabled:false,sessions:structuredClone(f.sessions),targets:structuredClone(f.targets)});
        window.syntheticHA={callWS:async msg=>{
          f.calls.push(structuredClone(msg));
          if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:false,lights:["a","b"].map(id=>({id,name:"Lamp "+id,entity_id:"light."+id,available:true,area_id:null,observation:{source:"ha_cache",status:id==="a"?"cached_rgb":"incomplete_rgb",power:id==="a",color_mode:"rgb",rgb:id==="a"?[64,128,255]:null,brightness:id==="a"?120:null}}))};
          if(msg.type==="lightcadence/state")return snapshot();
          const p=msg.payload;
          if(f.fail){f.fail=false;throw {code:"device_conflict"};}
          if(p.op==="start"){
            f.sessions.push({id:p.request_id,zone_id:p.zone_id,phase:"running",revision:1,devices:p.devices,effect:p.effect});
            for(const id of p.devices)f.targets[id]={target:{red:1,green:0,blue:0,brightness:1}};
          }else{
            const s=f.sessions.find(s=>s.id===p.session_id);
            if(s.revision!==p.revision)throw {code:"stale_revision"};
            if(p.op==="detach"){s.devices=s.devices.filter(id=>id!==p.device_id);delete f.targets[p.device_id];s.revision++;}
            else{s.devices.forEach(id=>delete f.targets[id]);s.phase="stopped";s.devices=[];s.revision++;}
          }
          return snapshot();
        }};
        el.hass=window.syntheticHA;
      });
      await page.getByText("Connected",{exact:true}).waitFor();
      await page.getByText("On · RGB cached (HA cache, not device feedback)",{exact:true}).waitFor();
      await page.getByText("Off · Incomplete RGB (HA cache, not device feedback)",{exact:true}).waitFor();
      await page.getByRole("textbox",{name:"Zone name"}).fill("Zone A");
      await page.getByRole("checkbox").nth(0).check();
      await page.getByRole("checkbox").nth(1).check();
      await page.getByRole("button",{name:"Start preview"}).click();
      await page.getByRole("heading",{name:"Zone A"}).waitFor();
      assert.equal(await page.locator(".swatch").count(),2);
      await page.getByRole("button",{name:"Detach"}).first().click();
      await page.waitForFunction(()=>document.querySelector("lightcadence-panel").shadowRoot.querySelectorAll(".swatch").length===1);
      assert.equal(await page.evaluate(()=>window.fixture.calls.filter(x=>x.payload?.op==="detach").at(-1).payload.revision),1);
      // Use the released lamp in a different, independent zone.
      await page.getByRole("textbox",{name:"Zone name"}).fill("Zone B");
      await page.getByRole("checkbox").first().check();
      await page.getByRole("button",{name:"Start preview"}).click();
      await page.getByRole("heading",{name:"Zone B"}).waitFor();
      assert.equal(await page.getByRole("button",{name:"Stop preview"}).count(),2);
      // Replace the view with another client; it must not issue a second start.
      await page.evaluate(()=>{const el=document.createElement("lightcadence-panel");el.hass=window.syntheticHA;document.querySelector("lightcadence-panel").replaceWith(el);});
      await page.getByRole("heading",{name:"Zone A"}).waitFor();
      assert.equal(await page.evaluate(()=>window.fixture.calls.filter(x=>x.payload?.op==="start").length),2);
      await page.locator("article").filter({has:page.getByRole("heading",{name:"Zone A"})}).getByRole("button",{name:"Stop preview"}).click();
      await page.getByRole("heading",{name:"Zone B"}).waitFor();
      await page.getByRole("button",{name:"Stop preview"}).click();
      await page.getByText("No active previews.",{exact:false}).waitFor();
      await page.getByRole("textbox",{name:"Zone name"}).fill("Zone C");
      await page.getByRole("checkbox").first().check();
      await page.evaluate(()=>window.fixture.fail=true);
      await page.getByRole("button",{name:"Start preview"}).click();
      await page.getByRole("status").filter({hasText:"already belongs"}).waitFor();
      assert.equal(await page.getByRole("heading",{name:"Zone C"}).count(),0);
      assert.equal(await page.evaluate(()=>document.querySelector("lightcadence-panel").shadowRoot.querySelector("main").scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]);
      await page.close();
    });
  }
  await test("physical candidate uses separate command with current instance",async()=>{
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.goto(origin);
    await page.evaluate(async()=>{
      await customElements.whenDefined("lightcadence-panel");
      const el=document.querySelector("lightcadence-panel");
      window.physicalCalls=[];
      const s={schema_version:1,instance_id:"physical-instance",output_mode:"physical_candidate",physical_output_enabled:true,hardware_qualified:false,enrolled_devices:["a"],sessions:[],targets:{}};
      el.hass={callWS:async msg=>{
        window.physicalCalls.push(structuredClone(msg));
        if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:true,lights:[{id:"a",name:"Lamp A",entity_id:"light.a",available:true,area_id:null}]};
        if(msg.type==="lightcadence/physical" && msg.payload.op==="start")s.sessions=[{id:"show",zone_id:msg.payload.zone_id,phase:"running",revision:1,devices:["a"],effect:msg.payload.effect}];
        if(msg.type==="lightcadence/physical" && msg.payload.op==="stop")s.sessions=[];
        if(msg.type==="lightcadence/physical" && ["manual_set","manual_off"].includes(msg.payload.op)){
          s.sessions=[];return {...structuredClone(s),manual_result:msg.payload.op==="manual_set"?"confirmed":"unconfirmed"};
        }
        return structuredClone(s);
      }};
    });
    await page.getByText("Hardware test workspace",{exact:true}).waitFor();
    await page.getByRole("textbox",{name:"Zone name"}).fill("Test zone");
    await page.getByRole("checkbox").check();
    await page.getByLabel("Stop behaviour").selectOption("hold");
    await page.getByRole("button",{name:"Start hardware test"}).click();
    await page.getByRole("heading",{name:"Test zone"}).waitFor();
    await page.getByRole("button",{name:"Stop show"}).click();
    await page.getByText("No active shows.",{exact:false}).waitFor();
    await page.getByLabel("Manual lamp",{exact:true}).selectOption("a");
    await page.getByLabel("Manual colour").fill("#4080ff");
    await page.getByLabel("Manual brightness (%)").fill("71");
    await page.getByRole("button",{name:"Apply colour and turn on"}).click();
    await page.getByRole("status").filter({hasText:"Manual state readback confirmed"}).waitFor();
    await page.getByRole("button",{name:"Turn off lamp"}).click();
    await page.getByRole("status").filter({hasText:"Manual outcome unconfirmed"}).waitFor();
    const calls=await page.evaluate(()=>window.physicalCalls.filter(x=>x.payload));
    assert.deepEqual(calls.map(x=>[x.type,x.payload.op,x.payload.instance_id]),["start","stop","manual_set","manual_off"].map(op=>["lightcadence/physical",op,"physical-instance"]));
    assert.equal(calls[0].payload.stop_policy,"hold");
    assert.deepEqual(calls[2].payload.state,{power:true,rgb:[64,128,255],brightness_pct:71});
    assert.equal(calls[3].payload.device_id,"a");
    await page.close();
  });
  await test("recovery blocks new commands and requires explicit decision",async()=>{
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.goto(origin);
    await page.evaluate(async()=>{
      await customElements.whenDefined("lightcadence-panel");
      const el=document.querySelector("lightcadence-panel");
      window.recoveryCalls=[];
      const s={schema_version:1,instance_id:"restarted",output_mode:"physical_candidate",physical_output_enabled:true,hardware_qualified:false,enrolled_devices:["a"],sessions:[],targets:{},recovery_required:[{device_id:"a",record_id:"record",original:{power:true,target:{red:1,green:0,blue:0,brightness:.5}},policy:"restore",binding_matches:true}]};
      el.hass={callWS:async msg=>{
        if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:true,lights:[{id:"a",name:"Lamp A",entity_id:"light.a",available:true,area_id:null}]};
        if(msg.payload){window.recoveryCalls.push(structuredClone(msg));s.recovery_required=[];return {...structuredClone(s),manual_result:"confirmed"};}
        return structuredClone(s);
      }};
    });
    await page.getByRole("heading",{name:"Recovery required",exact:true}).waitFor();
    assert.equal(await page.getByRole("checkbox").isDisabled(),true);
    await page.getByLabel("Manual lamp",{exact:true}).selectOption("a");
    assert.equal(await page.getByRole("button",{name:"Turn off lamp"}).isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.recoveryCalls.length),0);
    await page.getByRole("button",{name:"Keep current state"}).click();
    await page.getByRole("status").filter({hasText:"Recovery resolved"}).waitFor();
    assert.equal(await page.getByRole("checkbox").isDisabled(),false);
    const calls=await page.evaluate(()=>window.recoveryCalls);
    assert.deepEqual(calls.map(x=>x.payload),[{op:"recover",instance_id:"restarted",device_id:"a",record_id:"record",decision:"keep"}]);
    await page.close();
  });
  await test("responsive HA content surface resizes without viewport changes",async()=>{
    const page=await browser.newPage({viewport:{width:1920,height:1400}});
    await page.goto(origin);
    await page.evaluate(async()=>{
      await customElements.whenDefined("lightcadence-panel");
      document.body.style.margin="0";
      const el=document.querySelector("lightcadence-panel");
      const parent=document.createElement("div");
      parent.id="ha-content";
      document.body.append(parent);parent.append(el);
      const lights=Array.from({length:3},(_,i)=>({id:String(i),name:"Long lamp "+("identifier".repeat(8))+i,entity_id:"light.synthetic_"+i,available:true,area_id:null}));
      const snapshot={schema_version:1,instance_id:"layout-test",output_mode:"preview",physical_output_enabled:false,sessions:[],targets:{}};
      el.hass={callWS:async msg=>msg.type==="lightcadence/catalog"?{schema_version:1,entries:["entry"],physical_output_enabled:false,lights}:snapshot};
    });
    await page.getByText("Connected",{exact:true}).waitFor();
    for(const [width,height] of [[1600,1200],[1100,950],[800,850],[680,900],[390,844],[320,640],[900,420],[620,420],[1600,1200]]){
      const rect=await page.evaluate(async ([w,h])=>{
        const parent=document.querySelector("#ha-content");
        parent.style.width=w+"px";parent.style.height=h+"px";
        await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        const el=parent.firstElementChild,root=el.shadowRoot;
        const box=q=>{const x=root.querySelector(q).getBoundingClientRect();return {x:x.x,y:x.y,w:x.width,h:x.height,b:x.bottom,r:x.right};};
        return {host:el.getBoundingClientRect().toJSON(),main:box("main"),left:box("section[aria-labelledby=setup]"),right:box(".right-stack"),workspace:box(".workspace"),scrollWidth:root.querySelector("main").scrollWidth,clientWidth:root.querySelector("main").clientWidth,innerScroll:[...root.querySelectorAll("main,.workspace,section,.right-stack")].some(x=>["auto","scroll","hidden","clip"].includes(getComputedStyle(x).overflowY))};
      },[width,height]);
      assert.ok(Math.abs(rect.host.width-width)<1,JSON.stringify(rect));
      assert.ok(Math.abs(rect.main.w-width)<1,JSON.stringify(rect));
      assert.ok(rect.main.h>=height-1,JSON.stringify(rect));
      assert.ok(rect.scrollWidth<=rect.clientWidth+1,JSON.stringify(rect));
      assert.equal(rect.innerScroll,false);
      if(width>760){
        assert.ok(Math.abs(rect.left.y-rect.right.y)<1,JSON.stringify(rect));
        assert.ok(Math.abs(rect.left.b-rect.right.b)<1,JSON.stringify(rect));
        assert.ok(rect.left.r<rect.right.x,JSON.stringify(rect));
      }else{
        assert.ok(rect.left.b<rect.right.y,JSON.stringify(rect));
        assert.ok(Math.abs(rect.left.x-rect.right.x)<1,JSON.stringify(rect));
        assert.ok(Math.abs(rect.left.w-rect.right.w)<1,JSON.stringify(rect));
      }
    }
    await page.close();
  });
  await test("audio source selection and loss in original panel",async()=>{
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.goto(origin);
    await page.evaluate(async()=>{
      await customElements.whenDefined("lightcadence-panel");
      const el=document.querySelector("lightcadence-panel");
      window.audioCalls=[];
      const s={schema_version:1,instance_id:"audio",output_mode:"preview",physical_output_enabled:false,sessions:[],targets:{},audio_sources:[{id:"pcm",name:"Local PCM",status:"live",bands:[.4,.2,.1],bpm:120,beat_count:4}]};
      window.audioFixture=s;
      el.hass={callWS:async msg=>{
        if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:false,lights:[{id:"a",name:"Lamp A",entity_id:"light.a",available:true,area_id:null}]};
        if(msg.payload){window.audioCalls.push(structuredClone(msg.payload));s.sessions=[{id:"show",zone_id:msg.payload.zone_id,phase:"running",revision:1,devices:["a"],effect:msg.payload.effect}];}
        return structuredClone(s);
      }};
    });
    await page.getByText("Connected",{exact:true}).waitFor();
    await page.getByRole("textbox",{name:"Zone name"}).fill("Music");
    await page.getByRole("checkbox").check();
    await page.getByLabel("Effect",{exact:true}).selectOption("audio_spectrum");
    assert.equal(await page.getByRole("button",{name:"Start preview"}).isDisabled(),true);
    await page.getByLabel("Audio source",{exact:true}).selectOption("pcm");
    await page.getByRole("button",{name:"Start preview"}).click();
    await page.getByRole("heading",{name:"Music",exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.audioCalls[0].effect.audio_source),"pcm");
    await page.evaluate(()=>{window.audioFixture.audio_sources[0].status="lost";window.audioFixture.audio_sources[0].bands=null;window.audioFixture.audio_sources[0].bpm=null;});
    await page.getByText("Source lost — fading to ambient",{exact:false}).waitFor();
    assert.equal(await page.evaluate(()=>window.audioCalls.length),1);
    await page.close();
  });
  await test("library explicit save and load in original mobile panel",async()=>{
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.goto(origin);
    await page.evaluate(async()=>{
      await customElements.whenDefined("lightcadence-panel");
      const el=document.querySelector("lightcadence-panel");
      window.libraryCalls=[];
      const s={schema_version:1,instance_id:"library",output_mode:"preview",physical_output_enabled:false,sessions:[],targets:{},library:{revision:0,zones:[],scenes:[],persistent:true,fault:false}};
      el.hass={callWS:async msg=>{
        if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:false,lights:[{id:"a",name:"Lamp A",entity_id:"light.a",available:true,area_id:null}]};
        if(msg.payload){const p=msg.payload;window.libraryCalls.push(structuredClone(p));
          if(p.op==="library_save"){s.library[p.kind].push({...p.value,id:p.kind==="zones"?"zone1":"scene1",revision:1});s.library.revision++;}
          if(p.op==="start")s.sessions=[{id:"show",zone_id:p.zone_id,phase:"running",revision:1,devices:p.devices,effect:p.effect}];
        }
        return structuredClone(s);
      }};
    });
    await page.getByText("Connected",{exact:true}).waitFor();
    await page.getByRole("textbox",{name:"Zone name"}).fill("Test zone");
    await page.getByRole("checkbox").check();
    await page.getByRole("button",{name:"Save new zone"}).click();
    await page.getByLabel("Saved zone",{exact:true}).selectOption("zone1");
    await page.getByLabel("Scene name",{exact:true}).fill("Test scene");
    await page.getByLabel("Effect",{exact:true}).selectOption("scanner");
    await page.getByRole("button",{name:"Save new scene"}).click();
    await page.getByLabel("Saved scene",{exact:true}).selectOption("scene1");
    assert.equal(await page.getByLabel("Effect",{exact:true}).inputValue(),"scanner");
    assert.equal(await page.evaluate(()=>window.libraryCalls.length),2);
    await page.getByRole("button",{name:"Start preview"}).click();
    await page.getByRole("heading",{name:"Test zone",exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(()=>window.libraryCalls.map(x=>x.op)),["library_save","library_save","start"]);
    await page.close();
  });
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
