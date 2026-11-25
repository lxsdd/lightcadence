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
          if(msg.type==="lightcadence/catalog")return {schema_version:1,entries:["entry"],physical_output_enabled:false,lights:["a","b"].map(id=>({id,name:"Lamp "+id,entity_id:"light."+id,available:true,area_id:null}))};
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
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
