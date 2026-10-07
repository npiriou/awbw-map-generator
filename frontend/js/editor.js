import { EditorDocument } from './editor-model.js';

const DRAFT_KEY = 'awbw-map-editor-draft-v1';
const $ = id => document.getElementById(id);
const buildingKinds = new Set(['city','base','airport','port','hq','com_tower','lab']);
const symmetries = [['none','None'],['rotate-2','Rotate 2Q'],['rotate-4','Rotate 4Q'],['flip-x','Flip X'],['flip-y','Flip Y'],['flip-4','Flip 4Q'],['diagonal-x','Diagonal X'],['diagonal-y','Diagonal Y']];

export class MapEditor {
  constructor(renderer, { onChange, onError, getSettings, isBusy } = {}) {
    Object.assign(this, { renderer, onChange, onError, getSettings, isBusy });
    this.layer = 'terrain'; this.tile = 1; this.terrainTile = 1; this.buildingTile = 34; this.unit = 1; this.space = false; this.tab = 'terrain';
    this.dirty = false; this.painting = false; this.previous = null; this.ready = false;
    this.mount();
    renderer.shouldEdit = event => this.ready && !this.isBusy?.() && !this.space && this.layer !== 'pan' && !event.ctrlKey;
    renderer.onEdit = (phase, info, event) => this.pointer(phase, info, event);
  }

  mount() {
    const viewport = $('map-viewport');
    const bar = document.createElement('div'); bar.id = 'editor-controls';
    bar.innerHTML = `
      <div class="editor-row">
        <button id="editor-new" type="button">New map</button><button id="editor-open" type="button">Open</button>
        <input id="editor-file" type="file" accept=".json,application/json" hidden>
        <button id="editor-save" type="button" title="Save draft in this browser · Shift+S or Ctrl+S">Save draft</button><button id="editor-project" type="button" title="Export the map, locks and editor settings">Project JSON</button>
        <label>Auto-save <select id="editor-autosave"><option value="0">Off</option>${[1,2,3,4,5].map(n=>`<option value="${n}">${n} min</option>`).join('')}</select></label>
        <span id="editor-save-status" role="status">No draft</span>
        <button id="editor-undo" type="button" title="Ctrl+Z" disabled>Undo</button><button id="editor-redo" type="button" title="Ctrl+Y" disabled>Redo</button>
      </div>
      <div class="editor-row">
        <label>Symmetry <select id="editor-symmetry">${symmetries.map(([id,label])=>`<option value="${id}">${label}</option>`).join('')}</select></label>
        <button id="editor-asymmetry" type="button" aria-pressed="false" title="Red: different terrain. Orange: orientation only. Building owners are ignored. Shortcut: A.">Asymmetry: 0</button>
        <button id="editor-fix-symmetry" type="button" disabled title="Fix terrain symmetry. Locked cells are preferred, then the topmost / leftmost cell. Existing building owners are preserved; new buildings are neutral when possible. Undo with Ctrl+Z."><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m4 20 13-13 3 3L7 23zM14 10l3 3M5 2v6M2 5h6M19 1v4M17 3h4M21 15v6M18 18h6"/></svg> Fix symmetry</button>
        <button id="editor-fill" type="button">Fill map</button><button id="editor-resize-toggle" type="button" aria-expanded="false" title="R">Resize</button>
        <button id="editor-hide-units" type="button" aria-pressed="false" title="H">Hide units</button><button id="editor-income-toggle" type="button" aria-expanded="false" title="I">Income & counts</button>
        <button id="editor-zoom-out" type="button" aria-label="Zoom out">−</button><button id="editor-zoom-in" type="button" aria-label="Zoom in">+</button><span id="editor-zoom">100%</span>
        <label><input id="editor-cursor" type="checkbox" checked> Cursor</label>
        <label>Theme <select id="editor-theme"><option value="classic">Classic</option><option value="default">Default</option><option value="desert">Desert</option><option value="days-of-ruin">Days of Ruin</option></select></label>
      </div>
      <div id="editor-resize" class="editor-row" hidden>
        <label>Width <input id="editor-width" type="number" value="20" min="1" max="100"></label><label>Height <input id="editor-height" type="number" value="20" min="1" max="100"></label>
        <label>Anchor <select id="editor-anchor"><option value="top-left">Top left</option><option value="center">Center</option><option value="bottom-right">Bottom right</option></select></label><button id="editor-resize-apply" type="button">Apply size</button>
        ${['top','bottom','left','right'].map(edge=>`<span class="edge-controls">${edge}<button type="button" data-edge="${edge}" data-delta="-1" aria-label="Remove ${edge} edge">−</button><button type="button" data-edge="${edge}" data-delta="1" aria-label="Add ${edge} edge">+</button></span>`).join('')}
        <button type="button" data-transform="flip-x">Flip X</button><button type="button" data-transform="flip-y">Flip Y</button><button type="button" data-transform="rotate-ccw">Rotate left</button><button type="button" data-transform="rotate-cw">Rotate right</button>
      </div>
      <div class="editor-row editor-lock-row">
        <label><input id="editor-preserve" type="checkbox" checked> Preserve preplaced cells when generating</label>
        <label><input id="editor-lock-paint" type="checkbox" checked> Lock painted cells</label>
        <label><input id="editor-show-locks" type="checkbox" checked> Show locks</label>
        <label><input id="editor-connect" type="checkbox"> Connect roads / rivers / pipes</label>
        <button id="editor-lock-all" type="button">Lock all</button><button id="editor-unlock-all" type="button">Unlock all</button><span id="editor-lock-count">0 locks</span>
      </div>`;
    viewport.before(bar);
    const shell = document.createElement('div'); shell.className = 'editor-shell'; viewport.before(shell);
    const palette = document.createElement('aside'); palette.id = 'editor-palette'; palette.setAttribute('aria-label','Map editor palette');
    palette.innerHTML = `<div class="editor-tools" role="group" aria-label="Drawing tools">${[['terrain','Paint'],['unit','Units'],['erase','Delete unit'],['picker','Pick'],['lock','Lock'],['unlock','Unlock'],['pan','Pan']].map(([id,label])=>`<button type="button" data-tool="${id}" aria-pressed="${id==='terrain'}">${label}</button>`).join('')}</div>
      <label class="editor-country-label">Country <select id="editor-country"></select></label><label class="editor-hp-label">Unit HP <input id="editor-hp" type="number" min="1" max="10" value="10"></label>
      <div class="palette-tabs" role="tablist" aria-label="Palette">${['terrain','buildings','units'].map(id=>`<button type="button" role="tab" data-palette="${id}" aria-selected="${id==='terrain'}">${id}</button>`).join('')}</div>
      <input id="editor-search" type="search" placeholder="Search palette" aria-label="Search palette">
      <div id="editor-selected" role="status"></div><div id="editor-swatches"></div>
      <details class="editor-help"><summary>Controls</summary><p>Drag to paint. Space + drag or middle mouse to pan. Scroll to zoom. Alt + click picks a tile or unit.</p><p>T terrain · B buildings · U units · D delete unit · C country · A asymmetry · H hide units · I income · R resize · + / − zoom</p><p>1–9, 0: armies 1–10. Shift + digits: armies 11–20. Ctrl+Z undo · Ctrl+Y redo · Shift+S save.</p><p>Gold marks show cells preserved during generation. New map backgrounds are unlocked. Painting after generation locks only your changes.</p></details>`;
    shell.append(palette, viewport);
    const stats = document.createElement('div'); stats.id='editor-income'; stats.hidden=true; shell.after(stats);
    const status = document.createElement('p'); status.id='editor-message'; status.setAttribute('role','status'); status.className='hint'; stats.after(status);
    const bind = (id, fn) => $(id).addEventListener('click', () => this.run(fn));
    bind('editor-new', () => this.newMap()); bind('editor-open', () => $('editor-file').click());
    $('editor-file').addEventListener('change', event => this.run(async()=> {
      const file=event.target.files[0]; if(!file)return;
      if(file.size>8*1024*1024)throw new Error('Map files must be smaller than 8 MB.');
      const data=JSON.parse(await file.text());
      if(data.document) { this.document.restore(data.document); await this.restorePreferences(data.preferences); }
      else this.document.load(data.map || data.result?.map || data);
      await this.changed(true); event.target.value='';
    }));
    bind('editor-save',()=>this.save()); bind('editor-project',()=>this.downloadProject());
    bind('editor-undo',async()=>{if(this.document.undo())await this.changed();});
    bind('editor-redo',async()=>{if(this.document.redo())await this.changed();});
    bind('editor-fill',()=>this.fill());
    bind('editor-resize-toggle',()=>{ $('editor-resize').hidden=!$('editor-resize').hidden; $('editor-resize-toggle').setAttribute('aria-expanded',String(!$('editor-resize').hidden)); });
    bind('editor-resize-apply',async()=>{this.document.resize(Number($('editor-width').value),Number($('editor-height').value),$('editor-anchor').value,this.fillTile());await this.changed(true);});
    bar.querySelectorAll('[data-edge]').forEach(button=>button.addEventListener('click',()=>this.run(async()=>{this.document.resizeEdge(button.dataset.edge,Number(button.dataset.delta),this.fillTile());await this.changed(true);} )));
    bar.querySelectorAll('[data-transform]').forEach(button=>button.addEventListener('click',()=>this.run(async()=>{this.document.transform(button.dataset.transform);await this.changed(true);} )));
    bind('editor-lock-all',async()=>{this.document.lockAll();await this.changed();});bind('editor-unlock-all',async()=>{this.document.unlockAll();await this.changed();});
    bind('editor-asymmetry',()=>{const b=$('editor-asymmetry');b.setAttribute('aria-pressed',String(b.getAttribute('aria-pressed')!=='true'));this.overlays();});
    bind('editor-fix-symmetry',async()=>{this.renderer.autoFit=false;this.document.fixSymmetry($('editor-symmetry').value,{lock:$('editor-lock-paint').checked});await this.changed();});
    bind('editor-hide-units',()=>{this.renderer.showUnits=!this.renderer.showUnits;$('editor-hide-units').textContent=this.renderer.showUnits?'Hide units':'Show units';$('editor-hide-units').setAttribute('aria-pressed',String(!this.renderer.showUnits));this.renderer.draw();});
    bind('editor-income-toggle',()=>{$('editor-income').hidden=!$('editor-income').hidden;$('editor-income-toggle').setAttribute('aria-expanded',String(!$('editor-income').hidden));this.stats();});
    bind('editor-zoom-in',()=>this.renderer.zoom(1.15));bind('editor-zoom-out',()=>this.renderer.zoom(1/1.15));
    rendererZoom(this.renderer);
    ['editor-symmetry','editor-show-locks','editor-cursor'].forEach(id=>$(id).addEventListener('change',()=>this.run(()=>{this.renderer.hoverEnabled=$('editor-cursor').checked;if(!this.renderer.hoverEnabled){this.renderer.selectionCell=null;this.renderer.hoverCell=null;}this.overlays();})));
    $('editor-theme').addEventListener('change',()=>this.run(async()=>{await this.renderer.setTheme($('editor-theme').value);this.palette();this.markDirty();}));
    $('editor-country').addEventListener('change',()=>this.countryChanged());$('editor-search').addEventListener('input',()=>this.palette());
    $('editor-autosave').addEventListener('change',()=>this.scheduleAutosave());
    palette.querySelectorAll('[data-tool]').forEach(button=>button.addEventListener('click',()=>this.setTool(button.dataset.tool)));
    palette.querySelectorAll('[data-palette]').forEach(button=>button.addEventListener('click',()=>this.setTab(button.dataset.palette)));
    document.addEventListener('keydown',event=>this.key(event));
    document.addEventListener('keyup',event=>{if(event.code==='Space')this.space=false;});
    window.addEventListener('blur',()=>{this.space=false;if(this.painting)this.pointer('end',null,{});});
  }

  async initialize(manifest) {
    this.manifest=manifest; this.document=new EditorDocument(manifest); this.ready=true;
    const country=$('editor-country');
    for(const [code,id] of Object.entries(manifest.country_codes)){const option=document.createElement('option');option.value=code;option.textContent=manifest.country_names[id].replace(/([a-z])([A-Z])/g,'$1 $2');country.append(option);}
    this.unit=Number(Object.keys(manifest.units)[0]);
    this.palette();
    try {
      const saved=localStorage.getItem(DRAFT_KEY);
      if(saved){const draft=JSON.parse(saved);this.document.restore(draft.document);await this.restorePreferences(draft.preferences);await this.changed(true,false);$('editor-save-status').textContent='Draft restored';}
    } catch(error){this.onError?.(new Error(`Draft could not be restored: ${error.message}`));}
    this.renderer.preloadSprites?.();
  }
  run(fn) { if(!this.ready && this.document===undefined)return; if(this.isBusy?.())return; Promise.resolve().then(fn).catch(error=>this.onError?.(error)); }
  setTool(tool) {
    this.layer=tool;
    if(tool==='unit'||tool==='erase')this.setTab('units',false);
    if(tool==='terrain'&&this.tab==='units')this.setTab('terrain',false);
    $('editor-palette').querySelectorAll('[data-tool]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.tool===tool)));
    $('map-viewport').classList.toggle('editing',tool!=='pan');this.palette();
  }
  setTab(tab,setTool=true) {this.tab=tab;if(tab==='terrain')this.tile=this.terrainTile;else if(tab==='buildings')this.tile=this.buildingTile;$('editor-search').value='';if(setTool)this.setTool(tab==='units'?'unit':'terrain');else this.palette();}
  countryChanged() {
    const selected=this.manifest.terrain[this.buildingTile], owner=this.manifest.country_codes[$('editor-country').value];
    if(selected?.owner!=null){const counterpart=Object.entries(this.manifest.terrain).find(([,def])=>def.kind===selected.kind&&def.owner===owner);if(counterpart)this.buildingTile=Number(counterpart[0]);}
    if(this.tab==='buildings')this.tile=this.buildingTile;
    this.palette();
  }
  palette() {
    if(!this.manifest)return;
    $('editor-palette').querySelectorAll('[data-palette]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.palette===this.tab)));
    const owner=this.manifest.country_codes[$('editor-country').value], search=$('editor-search').value.trim().toLowerCase();
    let entries=this.tab==='units'?Object.entries(this.manifest.units):Object.entries(this.manifest.terrain).filter(([,def])=>this.tab==='buildings'?buildingKinds.has(def.kind)&&(def.owner==null||def.owner===owner):!buildingKinds.has(def.kind));
    entries=entries.filter(([id,def])=>`${id} ${def.name} ${def.kind||''}`.toLowerCase().includes(search));
    $('editor-swatches').replaceChildren();
    for(const [id,def] of entries){
      const b=document.createElement('button');b.type='button';b.className='editor-swatch';b.dataset.id=id;b.title=`${def.name} · ${id}`;b.setAttribute('aria-label',b.title);
      const selected=Number(id)===(this.tab==='units'?this.unit:this.tile);b.setAttribute('aria-pressed',String(selected));
      const img=document.createElement('img');img.alt='';
      const sprite=this.tab==='units'?this.renderer.unitSprite({'Unit ID':Number(id),'Country Code':$('editor-country').value}).rel:(this.renderer.terrainDef?.(id)||def).sprite;
      img.src=`/assets/${sprite}.png`;b.append(img);const caption=document.createElement('span');caption.textContent=def.name;b.append(caption);
      b.addEventListener('click',()=>{if(this.tab==='units'){this.unit=Number(id);this.setTool('unit');}else{this.tile=Number(id);if(this.tab==='buildings')this.buildingTile=this.tile;else this.terrainTile=this.tile;this.setTool('terrain');}});$('editor-swatches').append(b);
    }
    const selected=this.layer==='unit'?this.manifest.units[this.unit]:this.manifest.terrain[this.tile];
    $('editor-selected').textContent=this.layer==='erase'?'Delete unit':this.layer==='pan'?'Drag to pan':selected?.name||this.layer;
  }
  async newMap(){const settings=this.getSettings?.()||{};this.document.newMap(settings.width||20,settings.height||20,1,settings.players||2);await this.changed(true);this.setTool('terrain');}
  fillTile(){return this.layer==='terrain'?this.tile:1;}
  brush(){return {layer:this.layer,id:this.layer==='unit'?this.unit:this.tile,country:$('editor-country').value,hp:Number($('editor-hp').value),symmetry:$('editor-symmetry').value,autoConnect:$('editor-connect').checked,lock:$('editor-lock-paint').checked};}
  pointer(phase,info,event) {
    if(!this.document?.map||this.isBusy?.())return;
    try {
      if(phase==='start'){
        if(event.altKey||this.layer==='picker'){if(info)this.pick(info);return;}
        this.document.begin();this.painting=true;this.previous=null;
      }
      if(phase==='end'){
        if(this.painting){this.painting=false;this.previous=null;this.document.commit();this.changed().catch(error=>this.onError?.(error));}return;
      }
      if(!this.painting||!info)return;
      const cells=[];const old=this.previous||info;
      let x=old.x,y=old.y;const dx=Math.abs(info.x-x),dy=-Math.abs(info.y-y),sx=x<info.x?1:-1,sy=y<info.y?1:-1;let err=dx+dy;
      for(;;){cells.push({x,y});if(x===info.x&&y===info.y)break;const e2=2*err;if(e2>=dy){err+=dy;x+=sx;}if(e2<=dx){err+=dx;y+=sy;}}
      for(const cell of cells){
        if(this.layer==='lock'||this.layer==='unlock'){
          const key=`${cell.x},${cell.y}`;
          for(const set of [this.document.tileLocks,this.document.unitLocks])this.layer==='lock'?set.add(key):set.delete(key);
        }else this.document.paint(cell.x,cell.y,this.brush());
      }
      this.previous=info;this.overlays();this.renderer.updateMap(this.document.map).catch(error=>this.onError?.(error));
    }catch(error){this.painting=false;this.document.commit();this.onError?.(error);this.changed().catch(()=>{});}
  }
  pick(info){if(info.unit){this.unit=info.unit['Unit ID'];$('editor-country').value=info.unit['Country Code'].toLowerCase();this.countryChanged();$('editor-hp').value=Math.ceil(info.unit['Unit HP']??10);this.setTool('unit');}else{const building=buildingKinds.has(info.terrain?.kind);if(building)this.buildingTile=info.id;else this.terrainTile=info.id;const owner=info.terrain?.owner;if(owner)$('editor-country').value=Object.keys(this.manifest.country_codes).find(code=>this.manifest.country_codes[code]===owner);this.setTab(building?'buildings':'terrain');}}
  async fill(){if(!this.document.map)return;this.document.begin();try{const brush=this.brush();if(!['terrain','unit','erase'].includes(brush.layer))throw new Error('Select terrain, a building, a unit or Delete unit to fill the map.');if(brush.layer==='terrain')this.document.fill(this.tile,brush);else for(let x=0;x<this.document.map['Size X'];x++)for(let y=0;y<this.document.map['Size Y'];y++)this.document.paint(x,y,brush);}finally{this.document.commit();}await this.changed();}
  async acceptMap(map,{preserveLocks=true}={}){
    const previous=this.document.map, tiles=new Set(this.document.tileLocks), units=new Set(this.document.unitLocks);
    this.document.load(map);
    if(preserveLocks&&previous&&map['Size X']===previous['Size X']&&map['Size Y']===previous['Size Y']){this.document.tileLocks=tiles;this.document.unitLocks=units;}
    await this.changed(true,false);this.markDirty();
  }
  generationInput(){if(!this.document?.map||!$('editor-preserve').checked)return undefined;const locks=this.document.generationInput();return locks.tiles.length||locks.units.length||locks.empty_units.length?{...locks,symmetry:$('editor-symmetry').value}:undefined;}
  async changed(fit=false,notify=true){
    if(!this.document.map)return;
    await this.renderer.updateMap(this.document.map,{fit});$('empty-preview').hidden=true;
    $('editor-width').value=this.document.map['Size X'];$('editor-height').value=this.document.map['Size Y'];
    $('editor-undo').disabled=!this.document.undoStack.length;$('editor-redo').disabled=!this.document.redoStack.length;
    for(const option of $('editor-symmetry').options)option.disabled=['rotate-4','diagonal-x','diagonal-y'].includes(option.value)&&this.document.map['Size X']!==this.document.map['Size Y'];
    if($('editor-symmetry').selectedOptions[0].disabled)$('editor-symmetry').value='none';
    this.overlays();this.stats();if(notify){this.markDirty();this.onChange?.(this.document.map);}
  }
  overlays(){
    if(!this.document?.map)return;
    const asym=this.document.asymmetry($('editor-symmetry').value,{classify:true});$('editor-asymmetry').textContent=`Asymmetry: ${asym.length}`;
    $('editor-fix-symmetry').disabled=!asym.length;
    this.renderer.lockCells=$('editor-show-locks').checked?[...new Set([...this.document.tileLocks,...this.document.unitLocks])]:[];
    this.renderer.asymmetricCells=$('editor-asymmetry').getAttribute('aria-pressed')==='true'?asym:[];
    $('editor-lock-count').textContent=`${this.document.tileLocks.size} tile / ${this.document.unitLocks.size} unit locks`;
    this.renderer.draw();
  }
  hover(info){this.renderer.selectionCell=$('editor-cursor').checked?info:null;this.renderer.hoverCell=$('editor-cursor').checked?info:null;this.renderer.draw();}
  stats(){
    if(!this.document?.map||$('editor-income').hidden)return;
    const s=this.document.stats(), host=$('editor-income');host.replaceChildren();
    const summary=document.createElement('p');summary.textContent=`${s.tiles} tiles · ${s.units} units · ${s.buildings} buildings · Total income ${s.totalIncome.toLocaleString()} / turn · Owned income ${s.income.toLocaleString()} · Per player ${Math.floor(s.totalIncome/(this.document.map['Player Count']||1)).toLocaleString()} · Per base ${Math.floor(s.incomePerBase).toLocaleString()}`;host.append(summary);
    const table=document.createElement('table');const head=document.createElement('tr');for(const text of ['Country','Buildings','Units','Income / turn','Building counts']){const th=document.createElement('th');th.textContent=text;head.append(th);}table.append(head);
    for(const owner of s.owners){const row=document.createElement('tr');for(const value of [owner.name,owner.buildings,owner.units,owner.income,Object.entries(owner.byBuilding).map(([k,v])=>`${k}: ${v}`).join(' · ')]){const td=document.createElement('td');td.textContent=String(value);row.append(td);}table.append(row);}host.append(table);
  }
  markDirty(){this.dirty=true;$('editor-save-status').textContent='Unsaved changes';}
  preferences(){return {theme:$('editor-theme').value,symmetry:$('editor-symmetry').value,country:$('editor-country').value,autosave:$('editor-autosave').value};}
  async restorePreferences(prefs={}){for(const [key,id]of Object.entries({theme:'editor-theme',symmetry:'editor-symmetry',country:'editor-country',autosave:'editor-autosave'}))if(prefs[key]!==undefined&&[...$(id).options].some(o=>o.value===String(prefs[key])))$(id).value=String(prefs[key]);await this.renderer.setTheme?.($('editor-theme').value);this.scheduleAutosave();this.countryChanged();}
  project(){return {format:'awbw-map-editor',version:1,saved_at:new Date().toISOString(),document:this.document.serialize(),preferences:this.preferences()};}
  save(){if(!this.document?.map)return;localStorage.setItem(DRAFT_KEY,JSON.stringify(this.project()));this.dirty=false;$('editor-save-status').textContent=`Saved ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;}
  scheduleAutosave(){clearInterval(this.autosaveTimer);const minutes=Number($('editor-autosave').value);this.autosaveAt=Date.now()+minutes*60000;if(minutes)this.autosaveTimer=setInterval(()=>{if(!this.document?.map)return;const seconds=Math.max(0,Math.ceil((this.autosaveAt-Date.now())/1000));if(!seconds){try{this.save();this.autosaveAt=Date.now()+minutes*60000;}catch(error){this.onError?.(error);clearInterval(this.autosaveTimer);}}else if(this.dirty)$('editor-save-status').textContent=`Unsaved · auto-save in ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;},1000);}
  downloadProject(){if(!this.document?.map)return;const url=URL.createObjectURL(new Blob([JSON.stringify(this.project(),null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='awbw-editor-project.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  key(event){
    if(!this.ready||this.isBusy?.())return;
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();$('editor-save').click();return;}
    if(event.target.closest('input,select,textarea,[contenteditable]'))return;
    const key=event.key.toLowerCase();
    if(event.code==='Space'){event.preventDefault();this.space=true;return;}
    if((event.ctrlKey||event.metaKey)&&key==='z'){event.preventDefault();$(event.shiftKey?'editor-redo':'editor-undo').click();return;}
    if((event.ctrlKey||event.metaKey)&&key==='y'){event.preventDefault();$('editor-redo').click();return;}
    if(key==='s'&&(event.shiftKey||event.ctrlKey||event.metaKey)){event.preventDefault();$('editor-save').click();return;}
    if(event.ctrlKey||event.metaKey)return;
    if(/^Digit\d$/.test(event.code)){event.preventDefault();let n=Number(event.code.slice(-1))||10;if(event.shiftKey)n+=10;const code=Object.keys(this.manifest.country_codes).find(code=>this.manifest.country_codes[code]===n);if(code){$('editor-country').value=code;this.countryChanged();}return;}
    if(key==='t')this.setTab('terrain');else if(key==='b')this.setTab('buildings');else if(key==='u')this.setTab('units');else if(key==='d')this.setTool('erase');else if(key==='c')$('editor-country').focus();
    else {const id={a:'editor-asymmetry',h:'editor-hide-units',i:'editor-income-toggle',r:'editor-resize-toggle','+':'editor-zoom-in','=':'editor-zoom-in','-':'editor-zoom-out'}[key];if(id){event.preventDefault();$(id).click();}}
  }
}
function rendererZoom(renderer){const previous=renderer.onZoom;renderer.onZoom=scale=>{previous?.(scale);$('editor-zoom').textContent=`${Math.round(scale*100)}%`;};}
