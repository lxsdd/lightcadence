import {css} from "lit";

// Shared layout foundation for live, floorplan, scene and timeline views.
// The containing HA panel, not the browser viewport, determines available size.
export const workspaceStyles=css`
  :host {
    display:grid; grid-template-rows: minmax(min-content,1fr);
    inline-size:100%; min-inline-size:0; min-block-size:100%;
    container:lightcadence / inline-size;
    color:var(--primary-text-color,#eef1f5);
    background:var(--primary-background-color,#141923);
    font-family:inherit;
  }
  *, *::before, *::after {box-sizing:border-box}
  [hidden]{display:none!important}
  main {
    display:flex; flex-direction:column; inline-size:100%; min-inline-size:0;
    padding:clamp(10px,2cqi,32px); gap:clamp(10px,1.3cqi,20px);
    overflow-wrap:anywhere;
  }
  h1{font-size:clamp(24px,2.6cqi,36px);margin:0}
  h2{font-size:20px;margin:0 0 12px}
  h3{font-size:16px;margin:16px 0 8px}
  p{margin:8px 0}
  header,.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
  header>div,.toolbar>p{min-inline-size:0;flex:1 1 12rem}
  .badge,.notice{border:1px solid var(--divider-color,#687080);border-radius:8px;padding:10px}
  .notice{margin:0}
  .badge[data-connected="yes"]{border-color:var(--success-color,#42966b)}
  .badge[data-connected="no"]{border-color:var(--warning-color,#d89c43)}
  .workspace {
    flex:1 0 auto; display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);
    align-items:stretch; gap:clamp(10px,1.3cqi,20px); min-inline-size:0;
  }
  .right-stack{display:flex;flex-direction:column;min-inline-size:0;gap:clamp(10px,1.3cqi,20px)}
  .right-stack>section:first-child{flex:1 0 auto}
  section,article {
    background:var(--card-background-color,#202735);
    border:1px solid var(--divider-color,#596273);border-radius:12px;
    padding:clamp(12px,1.5cqi,24px);min-inline-size:0;
  }
  article+article{margin-top:12px}
  article[data-phase="running"]{border-inline-start:3px solid var(--primary-color,#237db8)}
  article[data-phase="stopping"]{border-inline-start:3px solid var(--warning-color,#d89c43)}
  label{display:block;margin:10px 0}
  input:not([type=checkbox]),select{
    display:block;inline-size:100%;min-inline-size:0;min-block-size:44px;
    color:inherit;background:var(--secondary-background-color,#303948);
    border:1px solid var(--divider-color,#657080);border-radius:6px;padding:8px;
    font:inherit;
  }
  input[type=checkbox]{width:20px;height:20px;flex:none}
  .lamp,.target{display:flex;align-items:center;gap:12px;min-block-size:44px;padding:6px 0}
  .lamp span,.target span:nth-child(2){flex:1;min-inline-size:0}
  small{display:block;color:var(--secondary-text-color,#bec5cf)}
  button{
    min-block-size:44px;min-inline-size:44px;max-inline-size:100%;padding:8px 14px;
    border:1px solid var(--divider-color,#657080);background:var(--secondary-background-color,#303948);
    color:inherit;border-radius:8px;cursor:pointer;font:inherit;white-space:normal;
  }
  button.primary{margin-top:12px;background:var(--primary-color,#237db8);color:var(--text-primary-color,white)}
  button:disabled{opacity:.5;cursor:not-allowed}
  button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#48a9e8);outline-offset:2px}
  .timeline-controls{display:flex;flex-wrap:wrap;align-items:end;gap:8px;margin-block:8px}
  .timeline-controls form{display:flex;flex-wrap:wrap;align-items:end;gap:8px;flex:1 1 15rem;min-width:0}
  .timeline-controls form label{flex:1 1 8rem;min-width:0;margin:0}
  .target button{flex:0 0 auto}
  .swatch{width:32px;height:32px;flex:none;border:1px solid var(--divider-color,#657080);border-radius:50%}
  /* Do not lock or clip content vertically: the HA page is the scroll owner.
     Small/short surfaces grow naturally to keep every control reachable. */
  @container lightcadence (max-width:760px){
    .workspace{grid-template-columns:minmax(0,1fr)}
    .right-stack>section:first-child{flex:0 0 auto}
    section,article{padding:12px}
  }
  @container lightcadence (max-width:360px){
    .target{flex-wrap:wrap;gap:8px}
    .target button{margin-inline-start:auto}
  }
`;
