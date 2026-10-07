export const SYMMETRY_MODES = Object.freeze(['none', 'rotate-2', 'rotate-4', 'flip-x', 'flip-y', 'flip-4', 'diagonal-x', 'diagonal-y']);
const clone = value => JSON.parse(JSON.stringify(value));
const key = (x, y) => `${x},${y}`;
const directions = { N: [0, -1], E: [1, 0], S: [0, 1], W: [-1, 0] };
const propertyKinds = new Set(['city', 'base', 'airport', 'port', 'hq', 'com_tower', 'lab', 'silo']);
const incomeKinds = new Set(['city', 'base', 'airport', 'port', 'hq']);
function vector(kind, x, y) {
  if (kind === 'flip-x') return [-x, y];
  if (kind === 'flip-y') return [x, -y];
  if (kind === 'rotate-cw') return [-y, x];
  if (kind === 'rotate-ccw') return [y, -x];
  if (kind === 'rotate-2') return [-x, -y];
  if (kind === 'diagonal-x') return [y, x];
  if (kind === 'diagonal-y') return [-y, -x];
  return [x, y];
}
function position(kind, x, y, width, height) {
  if (kind === 'flip-x') return [width - 1 - x, y];
  if (kind === 'flip-y') return [x, height - 1 - y];
  if (kind === 'rotate-cw') return [height - 1 - y, x];
  if (kind === 'rotate-ccw') return [y, width - 1 - x];
  if (kind === 'rotate-2') return [width - 1 - x, height - 1 - y];
  if (kind === 'diagonal-x') return [y, x];
  if (kind === 'diagonal-y') return [height - 1 - y, width - 1 - x];
  return [x, y];
}
function shape(name) {
  let match = name.match(/^([HV]|[NESW]{1,3}|C)(River|Road|Bridge|Pipe(?: End| Seam| Rubble)?)$/);
  if (match) return { suffix: match[2], prefix: match[1], dirs: match[1] === 'H' ? 'EW' : match[1] === 'V' ? 'NS' : match[1] === 'C' ? 'NESW' : match[1] };
  match = name.match(/^Shoal-([NESW])$/);
  return match ? { suffix: 'Shoal-', prefix: match[1], dirs: match[1] } : null;
}
function signature(dirs) { return [...dirs].sort().join(''); }

export class EditorDocument {
  constructor(manifest) {
    if (!manifest?.terrain || !manifest?.units || !manifest?.country_codes) throw new Error('Editor sprite metadata is unavailable.');
    this.manifest = manifest; this.map = null;
    this.tileLocks = new Set(); this.unitLocks = new Set(); this.undoStack = []; this.redoStack = [];
    this.transaction = null;
    this.shapes = new Map(Object.entries(manifest.terrain).map(([id, def]) => [Number(id), shape(def.name)]));
  }
  get width() { return this.map?.['Size X'] || 0; }
  get height() { return this.map?.['Size Y'] || 0; }
  _terrain(id) {
    if (!Number.isInteger(id) || !this.manifest.terrain[String(id)]) throw new Error(`Unknown terrain ID: ${id}.`);
    return id;
  }
  _dimensions(width, height) {
    if (![width, height].every(n => Number.isInteger(n) && n >= 1 && n <= 100)) throw new Error('Map dimensions must be whole numbers from 1 to 100.');
  }
  _point(x, y) {
    if (![x, y].every(Number.isInteger) || x < 0 || y < 0 || x >= this.width || y >= this.height) throw new Error('Position is outside the map.');
  }
  _country(country) {
    const code = typeof country === 'number' ? Object.keys(this.manifest.country_codes).find(code => this.manifest.country_codes[code] === country) : String(country || '').toLowerCase();
    if (!Object.hasOwn(this.manifest.country_codes, code)) throw new Error(`Unknown army: ${country}.`);
    return code;
  }
  _validate(map) {
    const width = map?.['Size X'], height = map?.['Size Y']; this._dimensions(width, height);
    const columns = map['Terrain Map'];
    if (!Array.isArray(columns) || columns.length !== width || columns.some(col => !Array.isArray(col) || col.length !== height)) throw new Error('Terrain must be a rectangular column-major AWBW grid.');
    for (const col of columns) for (const id of col) this._terrain(id);
    if (map['Player Count'] != null && (!Number.isInteger(map['Player Count']) || map['Player Count'] < 1 || map['Player Count'] > 20)) throw new Error('Player count must be from 1 to 20.');
    if (map['Predeployed Units'] != null && !Array.isArray(map['Predeployed Units'])) throw new Error('Predeployed units must be an array.');
    const occupied = new Set();
    for (const unit of map['Predeployed Units'] || []) {
      if (!Number.isInteger(unit['Unit ID']) || !this.manifest.units[String(unit['Unit ID'])]) throw new Error(`Unknown unit ID: ${unit['Unit ID']}.`);
      const x = unit['Unit X'], y = unit['Unit Y'];
      if (![x, y].every(Number.isInteger) || x < 0 || y < 0 || x >= width || y >= height) throw new Error('Unit position is outside the map.');
      const hp = unit['Unit HP'] === undefined ? 10 : unit['Unit HP'];
      if (!Number.isFinite(hp) || hp <= 0 || hp > 10) throw new Error('Imported unit HP must be a number greater than 0 and at most 10.');
      this._country(unit['Country Code']);
      if (occupied.has(key(x, y))) throw new Error('Two units cannot occupy the same tile.');
      occupied.add(key(x, y));
    }
    return map;
  }
  _players() {
    const owners = new Set(this.map['Terrain Map'].flat().map(id => this.manifest.terrain[id].owner).filter(owner => owner != null));
    for (const unit of this.map['Predeployed Units']) owners.add(this.manifest.country_codes[unit['Country Code']]);
    this.map['Player Count'] = Math.max(this.map['Player Count'] || 2, owners.size);
  }
  load(map, { resetLocks = true } = {}) {
    const next = clone(this._validate(map));
    next['Predeployed Units'] ||= [];
    for (const unit of next['Predeployed Units']) {
      unit['Country Code'] = this._country(unit['Country Code']);
      if (unit['Unit HP'] === undefined) unit['Unit HP'] = 10;
    }
    this.map = next; this._players();
    if (resetLocks) { this.tileLocks.clear(); this.unitLocks.clear(); }
    else { this._cropLocks(); }
    this.undoStack = []; this.redoStack = []; this.transaction = null;
    return this.map;
  }
  newMap(width, height, fill = 1, players = 2) {
    this._dimensions(width, height); this._terrain(fill);
    return this.load({ Name: 'Untitled map', 'Size X': width, 'Size Y': height, 'Player Count': players,
      'Terrain Map': Array.from({ length: width }, () => Array(height).fill(fill)), 'Predeployed Units': [] });
  }
  _snapshot() { return { map: clone(this.map), tileLocks: [...this.tileLocks], unitLocks: [...this.unitLocks] }; }
  _apply(snapshot) {
    this.map = clone(snapshot.map); this.tileLocks = new Set(snapshot.tileLocks); this.unitLocks = new Set(snapshot.unitLocks);
  }
  begin() {
    if (!this.map) throw new Error('Create or load a map first.');
    if (!this.transaction) this.transaction = this._snapshot();
  }
  commit() {
    if (!this.transaction) return false;
    this._players(); const before = this.transaction; this.transaction = null;
    if (JSON.stringify(before) === JSON.stringify(this._snapshot())) return false;
    this.undoStack.push(before); this.redoStack = []; return true;
  }
  _change(action) {
    const automatic = !this.transaction;
    if (automatic) this.begin();
    try { action(); if (automatic) this.commit(); }
    catch (error) { if (automatic) { this._apply(this.transaction); this.transaction = null; } throw error; }
    return this.map;
  }
  undo() {
    this.commit(); if (!this.undoStack.length) return false;
    this.redoStack.push(this._snapshot()); this._apply(this.undoStack.pop()); return true;
  }
  redo() {
    this.commit(); if (!this.redoStack.length) return false;
    this.undoStack.push(this._snapshot()); this._apply(this.redoStack.pop()); return true;
  }
  oriented(id, kind) {
    const source = this.shapes.get(id); if (!source || kind === 'none') return id;
    const dirs = [...source.dirs].map(d => {
      const [x, y] = vector(kind, ...directions[d]);
      return Object.keys(directions).find(d => directions[d][0] === x && directions[d][1] === y);
    }).join('');
    for (const [candidate, target] of this.shapes) if (target?.suffix === source.suffix && signature(target.dirs) === signature(dirs)) return candidate;
    return id;
  }
  _symmetry(mode) {
    if (!SYMMETRY_MODES.includes(mode)) throw new Error(`Unknown symmetry: ${mode}.`);
    if (['rotate-4', 'diagonal-x', 'diagonal-y'].includes(mode) && this.width !== this.height) throw new Error('Four-way rotation and diagonal symmetry require a square map.');
    return mode === 'rotate-4' ? ['none', 'rotate-cw', 'rotate-2', 'rotate-ccw'] : mode === 'flip-4' ? ['none', 'flip-x', 'flip-y', 'rotate-2'] : mode === 'none' ? ['none'] : ['none', mode];
  }
  paint(x, y, { layer = 'terrain', id = 1, country = 'os', hp = 10, symmetry = 'none', autoConnect = false, lock = true } = {}) {
    this._point(x, y);
    if (!['terrain', 'unit', 'erase'].includes(layer)) throw new Error(`Unknown paint layer: ${layer}.`);
    let transforms;
    if (layer === 'terrain') { this._terrain(id); transforms = this._symmetry(symmetry); }
    if (layer === 'unit') {
      if (!Number.isInteger(id) || !this.manifest.units[id]) throw new Error(`Unknown unit ID: ${id}.`);
      if (!Number.isInteger(hp) || hp < 1 || hp > 10) throw new Error('Unit HP must be a whole number from 1 to 10.');
      country = this._country(country);
    }
    return this._change(() => {
      if (layer === 'terrain') {
        const painted = new Set();
        for (const kind of transforms) {
          const [px, py] = position(kind, x, y, this.width, this.height), tile = key(px, py);
          if (painted.has(tile)) continue; painted.add(tile);
          this.map['Terrain Map'][px][py] = this.oriented(id, kind);
          if (lock) this.tileLocks.add(tile);
          if (autoConnect) this._connectAround(px, py, lock);
        }
      } else {
        this.map['Predeployed Units'] = this.map['Predeployed Units'].filter(unit => unit['Unit X'] !== x || unit['Unit Y'] !== y);
        if (layer === 'unit') this.map['Predeployed Units'].push({ 'Unit ID': id, 'Unit X': x, 'Unit Y': y, 'Unit HP': hp, 'Country Code': country });
        if (lock) this.unitLocks.add(key(x, y));
      }
    });
  }
  _connectAround(x, y, lock) {
    const grid = this.map['Terrain Map'];
    const family = id => {
      const suffix = this.shapes.get(id)?.suffix;
      return suffix === 'Bridge' ? 'Road' : suffix?.startsWith('Pipe') ? 'Pipe' : ['Road', 'River'].includes(suffix) ? suffix : null;
    };
    for (const [px, py] of [[x, y], ...Object.values(directions).map(([dx, dy]) => [x + dx, y + dy])]) {
      if (px < 0 || py < 0 || px >= this.width || py >= this.height) continue;
      const old = grid[px][py], group = family(old), currentShape = this.shapes.get(old);
      if (!group || ['Bridge', 'Pipe Seam', 'Pipe Rubble'].includes(currentShape.suffix)) continue;
      const dirs = Object.entries(directions).filter(([, [dx, dy]]) => family(grid[px + dx]?.[py + dy]) === group).map(([d]) => d).join('');
      let desired = dirs;
      if (group !== 'Pipe' && dirs.length < 2) desired = /[NS]/.test(dirs) ? 'NS' : 'EW';
      const targetSuffix = group === 'Pipe' && dirs.length === 1 ? 'Pipe End' : group;

      if (targetSuffix === 'Pipe End') desired = { N: 'S', E: 'W', S: 'N', W: 'E' }[dirs];
      const match = [...this.shapes].find(([, def]) => def?.suffix === targetSuffix && signature(def.dirs) === signature(desired));
      if (match && match[0] !== old) { grid[px][py] = match[0]; if (lock) this.tileLocks.add(key(px, py)); }
    }
  }
  fill(id, { symmetry = 'none', autoConnect = false, lock = true } = {}) {
    this._terrain(id); this._symmetry(symmetry);
    return this._change(() => {
      for (let x = 0; x < this.width; x++) for (let y = 0; y < this.height; y++) {
        this.map['Terrain Map'][x][y] = id; if (lock) this.tileLocks.add(key(x, y));
      }
      if (autoConnect) for (let x = 0; x < this.width; x++) for (let y = 0; y < this.height; y++) this._connectAround(x, y, lock);
    });
  }
  _cropLocks() {
    for (const locks of [this.tileLocks, this.unitLocks]) for (const tile of locks) {
      const [x, y] = tile.split(',').map(Number); if (x < 0 || y < 0 || x >= this.width || y >= this.height) locks.delete(tile);
    }
  }
  _resize(width, height, dx, dy, fill) {
    const old = this.map['Terrain Map'];
    this.map['Terrain Map'] = Array.from({ length: width }, (_, x) => Array.from({ length: height }, (_, y) => old[x - dx]?.[y - dy] ?? fill));
    this.map['Size X'] = width; this.map['Size Y'] = height;
    this.map['Predeployed Units'] = this.map['Predeployed Units'].map(unit => ({ ...unit, 'Unit X': unit['Unit X'] + dx, 'Unit Y': unit['Unit Y'] + dy }))
      .filter(unit => unit['Unit X'] >= 0 && unit['Unit Y'] >= 0 && unit['Unit X'] < width && unit['Unit Y'] < height);
    for (const field of ['tileLocks', 'unitLocks']) this[field] = new Set([...this[field]].map(tile => { const [x, y] = tile.split(',').map(Number); return key(x + dx, y + dy); }));
    this._cropLocks();
  }
  resize(width, height, anchor = 'top-left', fill = 1) {
    this._dimensions(width, height); this._terrain(fill);
    if (!['top-left', 'top-right', 'bottom-left', 'bottom-right', 'center'].includes(anchor)) throw new Error('Unknown resize anchor.');
    const dx = anchor === 'center' ? Math.floor((width - this.width) / 2) : anchor.endsWith('right') ? width - this.width : 0;
    const dy = anchor === 'center' ? Math.floor((height - this.height) / 2) : anchor.startsWith('bottom') ? height - this.height : 0;
    return this._change(() => this._resize(width, height, dx, dy, fill));
  }
  resizeEdge(edge, delta, fill = 1) {
    if (!['top', 'bottom', 'left', 'right'].includes(edge) || !Number.isInteger(delta)) throw new Error('Choose an edge and a whole number of rows or columns.');
    const width = this.width + (['left', 'right'].includes(edge) ? delta : 0), height = this.height + (['top', 'bottom'].includes(edge) ? delta : 0);
    this._dimensions(width, height); this._terrain(fill);
    return this._change(() => this._resize(width, height, edge === 'left' ? delta : 0, edge === 'top' ? delta : 0, fill));
  }
  transform(kind) {
    if (!['flip-x', 'flip-y', 'rotate-cw', 'rotate-ccw'].includes(kind)) throw new Error('Unknown map transform.');
    return this._change(() => {
      const width = this.width, height = this.height, rotate = kind.startsWith('rotate');
      const columns = Array.from({ length: rotate ? height : width }, () => Array(rotate ? width : height));
      for (let x = 0; x < width; x++) for (let y = 0; y < height; y++) {
        const [px, py] = position(kind, x, y, width, height); columns[px][py] = this.oriented(this.map['Terrain Map'][x][y], kind);
      }
      this.map['Terrain Map'] = columns; this.map['Size X'] = columns.length; this.map['Size Y'] = columns[0].length;
      for (const unit of this.map['Predeployed Units']) [unit['Unit X'], unit['Unit Y']] = position(kind, unit['Unit X'], unit['Unit Y'], width, height);
      for (const field of ['tileLocks', 'unitLocks']) this[field] = new Set([...this[field]].map(tile => key(...position(kind, ...tile.split(',').map(Number), width, height))));
    });
  }
  generationInput() {
    if (!this.map) throw new Error('Create or load a map first.');
    const tiles = [...this.tileLocks].map(tile => { const [x, y] = tile.split(',').map(Number); return { x, y, id: this.map['Terrain Map'][x][y] }; });
    const units = [], empty_units = [], occupied = new Map(this.map['Predeployed Units'].map(unit => [key(unit['Unit X'], unit['Unit Y']), unit]));
    for (const tile of this.unitLocks) {
      if (occupied.has(tile)) units.push(clone(occupied.get(tile)));
      else { const [x, y] = tile.split(',').map(Number); empty_units.push({ x, y }); }
    }
    return { width: this.width, height: this.height, tiles, units, empty_units };
  }
  unlockAll() { return this._change(() => { this.tileLocks.clear(); this.unitLocks.clear(); }); }
  lockAll() { return this._change(() => {
    for (let x = 0; x < this.width; x++) for (let y = 0; y < this.height; y++) { this.tileLocks.add(key(x, y)); this.unitLocks.add(key(x, y)); }
  }); }
  asymmetry(symmetry = 'rotate-2', { classify = false } = {}) {
    const transforms = this._symmetry(symmetry), grid = this.map['Terrain Map'], mismatch = [];
    for (let x = 0; x < this.width; x++) for (let y = 0; y < this.height; y++) {
      let type = null;
      for (const kind of transforms) {
        const [px, py] = position(kind, x, y, this.width, this.height), actual = grid[px][py], expected = this.oriented(grid[x][y], kind);
        if (this.sameTerrain(actual, expected)) continue;
        const rotationOnly = ['none', 'rotate-cw', 'rotate-2', 'rotate-ccw'].some(rotation => this.sameTerrain(actual, this.oriented(expected, rotation)));
        if (!rotationOnly) { type = 'terrain'; break; }
        type = 'orientation';
      }
      if (type) mismatch.push({ x, y, ...(classify ? { type } : {}) });
    }
    return mismatch;
  }
  sameTerrain(a, b) {
    if (a === b) return true;
    const source = this.manifest.terrain[a], target = this.manifest.terrain[b];
    return propertyKinds.has(source?.kind) && source.kind === target?.kind;
  }
  fixSymmetry(symmetry = 'rotate-2', { lock = true } = {}) {
    const transforms = this._symmetry(symmetry), grid = this.map['Terrain Map'];
    return this._change(() => {
      const visited = new Set();
      for (let y = 0; y < this.height; y++) for (let x = 0; x < this.width; x++) {
        if (visited.has(key(x, y))) continue;
        const orbit = [...new Map(transforms.map(kind => {
          const point = position(kind, x, y, this.width, this.height); return [key(...point), point];
        })).values()].sort((a, b) => Number(this.tileLocks.has(key(...b))) - Number(this.tileLocks.has(key(...a))) || a[1] - b[1] || a[0] - b[0]);
        for (const point of orbit) visited.add(key(...point));
        const [sx, sy] = orbit[0]; let source = grid[sx][sy];

        const stabilizers = transforms.filter(kind => {
          const [px, py] = position(kind, sx, sy, this.width, this.height); return px === sx && py === sy;
        });
        const invariant = id => stabilizers.every(kind => this.sameTerrain(id, this.oriented(id, kind)));
        if (!invariant(source)) {
          const original = this.shapes.get(source);
          const candidates = [...this.shapes].filter(([id, def]) => def?.suffix === original.suffix && invariant(id));
          candidates.sort((a, b) => {
            const distance = def => [...new Set(original.dirs + def.dirs)].filter(d => original.dirs.includes(d) !== def.dirs.includes(d)).length;
            return distance(a[1]) - distance(b[1]);
          });
          source = candidates[0]?.[0] ?? (original.suffix === 'Shoal-' ? 28 : 1);
        }
        for (const kind of transforms) {
          const [px, py] = position(kind, sx, sy, this.width, this.height), old = grid[px][py];
          let desired = this.oriented(source, kind);
          if (this.sameTerrain(old, desired)) continue;
          const def = this.manifest.terrain[desired];
          if (propertyKinds.has(def.kind)) {
            const owner = this.manifest.terrain[old].owner;
            const variant = Object.entries(this.manifest.terrain).find(([, candidate]) => candidate.kind === def.kind && candidate.owner === owner);
            if (variant) desired = Number(variant[0]);
          }
          grid[px][py] = desired;
          if (lock) this.tileLocks.add(key(px, py));
        }
      }
    });
  }
  stats() {
    if (!this.map) return null;
    const owners = new Map(), byTerrain = {}, byUnit = {}; let buildings = 0, income = 0, totalIncome = 0, bases = 0;
    const army = owner => {
      owner ??= 0;
      if (!owners.has(owner)) owners.set(owner, { owner, country: Object.keys(this.manifest.country_codes).find(code => this.manifest.country_codes[code] === owner) || null,
        name: this.manifest.country_names?.[owner] || 'Neutral', buildings: 0, units: 0, income: 0, byBuilding: {}, byUnit: {} });
      return owners.get(owner);
    };
    for (const id of this.map['Terrain Map'].flat()) {
      byTerrain[id] = (byTerrain[id] || 0) + 1; const def = this.manifest.terrain[id];
      if (propertyKinds.has(def.kind)) {
        buildings++; const row = army(def.owner); row.buildings++; row.byBuilding[def.kind] = (row.byBuilding[def.kind] || 0) + 1;
        if (def.kind === 'base') bases++;
        if (incomeKinds.has(def.kind)) totalIncome += 1000;
        if (def.owner != null && incomeKinds.has(def.kind)) { row.income += 1000; income += 1000; }
      }
    }
    for (const unit of this.map['Predeployed Units']) {
      const kind = this.manifest.units[unit['Unit ID']].type, row = army(this.manifest.country_codes[unit['Country Code']]);
      row.units++; row.byUnit[kind] = (row.byUnit[kind] || 0) + 1; byUnit[kind] = (byUnit[kind] || 0) + 1;
    }
    return { width: this.width, height: this.height, tiles: this.width * this.height, units: this.map['Predeployed Units'].length,
      buildings, income, totalIncome, potentialIncome: totalIncome, incomePerBase: bases ? totalIncome / bases : 0,
      owners: [...owners.values()].sort((a, b) => a.owner - b.owner), byTerrain, byUnit };
  }
  serialize() { return JSON.stringify({ version: 1, ...this._snapshot() }); }
  restore(serialized) {
    const state = typeof serialized === 'string' ? JSON.parse(serialized) : clone(serialized);
    if (state?.version !== 1) throw new Error('Unsupported editor save format.');
    this._validate(state.map);
    for (const field of ['tileLocks', 'unitLocks']) {
      if (!Array.isArray(state[field]) || state[field].some(tile => {
        if (typeof tile !== 'string' || !/^\d+,\d+$/.test(tile)) return true;
        const [x, y] = tile.split(',').map(Number); return x >= state.map['Size X'] || y >= state.map['Size Y'];
      })) throw new Error('The editor save contains invalid locked positions.');
    }
    this.load(state.map); this.tileLocks = new Set(state.tileLocks); this.unitLocks = new Set(state.unitLocks); return this.map;
  }
}
