export const TILE = 32;
const PADDING = 16;

export function bottomAlignedSpriteRect(px, py, naturalWidth, naturalHeight, width = TILE) {
  const height = naturalWidth > 0 ? width * naturalHeight / naturalWidth : width;
  return { x: px, y: py + TILE - height, width, height };
}

export function validateMap(map) {
  const width = map?.['Size X'], height = map?.['Size Y'], columns = map?.['Terrain Map'];
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width > 100 || height > 100 ||
      !Array.isArray(columns) || columns.length !== width || columns.some(col => !Array.isArray(col) || col.length !== height || col.some(id => !Number.isInteger(id)))) {
    throw new Error('The map does not contain a valid AWBW grid.');
  }
  return { width, height };
}

export class MapRenderer {
  constructor({ canvas, viewport, frame, onHover, onZoom, onWarning, onEdit, shouldEdit }) {
    Object.assign(this, { canvas, viewport, frame, onHover, onZoom, onWarning, onEdit, shouldEdit });
    this.scale = 1; this.x = 0; this.y = 0; this.grid = false; this.map = null;
    this.images = new Map(); this.loaded = new Map(); this.loadSerial = 0; this.pointer = null;
    this.showUnits = true; this.lockCells = []; this.asymmetricCells = []; this.selectionCell = null; this.hoverCell = null;
    this.theme = 'classic'; this.themeManifest = { themes: {}, units: {} };
    this.manifestPromise = fetch('/assets/sprites.json').then(r => {
      if (!r.ok) throw new Error('Sprite metadata is unavailable.');
      return r.json();
    }).then(data => this.manifest = data);
    this.themeManifestPromise = fetch('/assets/editor-themes.json').then(r => {
      if (!r.ok) throw new Error('Editor theme metadata is unavailable.');
      return r.json();
    }).catch(() => ({ themes: {}, units: {} })).then(data => this.themeManifest = data);
    viewport.addEventListener('wheel', e => {
      if (!this.map) return;
      e.preventDefault();
      const rect = viewport.getBoundingClientRect();
      this.zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });
    viewport.addEventListener('pointerdown', e => {
      if (!this.map || ![0, 1, 2].includes(e.button) || this.pointer || e.target.closest?.('.busy-overlay')) return;
      const edit = e.button === 0 && this.onEdit && (!this.shouldEdit || this.shouldEdit(e));

      this.autoFit = false;
      e.preventDefault();
      this.pointer = { id: e.pointerId, edit, x: e.clientX, y: e.clientY, initialX: this.x, initialY: this.y };
      viewport.setPointerCapture(e.pointerId);
      if (edit) this.onEdit('start', this.tileAtEvent(e), e);
      else viewport.classList.add('dragging');
    });
    viewport.addEventListener('pointermove', e => {
      if (this.pointer?.id === e.pointerId) {
        if (this.pointer.edit) this.onEdit?.('move', this.tileAtEvent(e), e);
        else {
          this.autoFit = false;
          this.x = this.pointer.initialX + e.clientX - this.pointer.x;
          this.y = this.pointer.initialY + e.clientY - this.pointer.y;
          this.transform();
        }
      }
      if (this.map) {
        const tile = this.tileAtEvent(e);
        this.onHover?.(tile);
        const hover = this.hoverEnabled !== false && this.onEdit && (!this.shouldEdit || this.shouldEdit(e)) ? tile : null;
        if (hover?.x !== this.hoverCell?.x || hover?.y !== this.hoverCell?.y) {
          this.hoverCell = hover; this.draw();
        }
      }
    });
    const release = e => {
      if (this.pointer?.id !== e.pointerId) return;
      const edit = this.pointer.edit;
      this.pointer = null; viewport.classList.remove('dragging');
      if (viewport.hasPointerCapture?.(e.pointerId)) viewport.releasePointerCapture(e.pointerId);
      if (edit) this.onEdit?.('end', this.tileAtEvent(e), e);
    };
    viewport.addEventListener('pointerup', release); viewport.addEventListener('pointercancel', release);
    viewport.addEventListener('lostpointercapture', release);
    viewport.addEventListener('contextmenu', e => {
      if (this.onEdit && (!this.shouldEdit || this.shouldEdit(e))) e.preventDefault();
    });
    viewport.addEventListener('pointerleave', () => {
      if (!this.pointer) { this.onHover?.(null); this.hoverCell = null; this.draw(); }
    });
    new ResizeObserver(() => { if (this.map && this.autoFit) this.fit(); }).observe(viewport);
  }

  image(rel) {
    if (!rel) return Promise.resolve(null);
    if (!this.images.has(rel)) {
      this.images.set(rel, new Promise(resolve => {
        const image = new Image();
        image.onload = async () => { try { await image.decode?.(); } catch {} this.loaded.set(rel, image); resolve(image); };
        image.onerror = () => { this.loaded.set(rel, null); resolve(null); };
        image.src = `/assets/${rel}.png`;
      }));
    }
    return this.images.get(rel);
  }

  unitSprite(unit) {
    const def = this.manifest.units[String(unit['Unit ID'])];
    const owner = this.manifest.country_codes[String(unit['Country Code']).toLowerCase()];
    const exact = this.themeManifest.units?.[String(unit['Unit ID'])]?.sprites?.[String(owner)];
    return { def, owner, rel: exact || def?.sprites[String(owner)] || def?.sprites['1'], recolor: owner > 5 && !exact };
  }

  terrainDef(id) {
    const def = this.manifest.terrain[String(id)];
    const themed = this.themeManifest.themes?.[this.theme]?.terrain?.[String(id)];
    return themed ? { ...def, ...themed } : def;
  }

  async setTheme(key) {
    await this.themeManifestPromise;
    if (!this.themeManifest.themes?.[key] && key !== 'classic') throw new Error(`Unknown map theme: ${key}`);
    this.theme = key;
    if (this.map) await this.updateMap(this.map);
  }

  setMap(map) { return this.updateMap(map, { fit: true }); }

  async preloadSprites() {
    await Promise.all([this.manifestPromise, this.themeManifestPromise]);
    const terrain = Object.keys(this.manifest.terrain).map(id => this.terrainDef(id)?.sprite);
    const units = Object.values(this.manifest.units).flatMap(def => Object.values(def.sprites || {}));
    const exactUnits = Object.values(this.themeManifest.units || {}).flatMap(def => Object.values(def.sprites || {}));
    await Promise.all([...new Set([...terrain, ...units, ...exactUnits])].filter(Boolean).map(path => this.image(path)));
  }

  async updateMap(map, { fit = false } = {}) {
    const serial = ++this.loadSerial;
    validateMap(map); await Promise.all([this.manifestPromise, this.themeManifestPromise]);
    const terrain = new Set(map['Terrain Map'].flat().map(id => this.terrainDef(id)?.sprite));
    const units = (map['Predeployed Units'] || []).map(unit => this.unitSprite(unit).rel);
    const paths = [...new Set([...terrain, ...units, this.terrainDef(1).sprite])].filter(Boolean);
    const missingPaths = paths.filter(path => !this.loaded.has(path));
    if (missingPaths.length) await Promise.all(missingPaths.map(path => this.image(path)));
    if (serial !== this.loadSerial) return;
    this.map = map;
    const width = map['Size X'] * TILE + PADDING * 2, height = map['Size Y'] * TILE + PADDING * 2;
    const resized = this.canvas.width !== width || this.canvas.height !== height;
    if (this.canvas.width !== width) this.canvas.width = width;
    if (this.canvas.height !== height) this.canvas.height = height;
    this.frame.style.display = 'block'; this.draw();
    if (fit || resized) this.fit();
    const unknownTerrain = [...new Set(map['Terrain Map'].flat())].filter(id => !this.manifest.terrain[String(id)]);
    const missing = paths.filter(path => !this.loaded.get(path)).length + unknownTerrain.length;
    const recolored = (map['Predeployed Units'] || []).some(unit => this.unitSprite(unit).recolor);
    this.onWarning?.(missing ? `${missing} missing sprite(s)` : recolored ? 'Secondary unit palettes approximated' : 'AWBW sprites');
  }

  paint(ctx, grid = this.grid, { showUnits = this.showUnits } = {}) {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.imageSmoothingEnabled = false;
    const width = this.map['Size X'], height = this.map['Size Y'];
    const plain = this.loaded.get(this.terrainDef(1).sprite);

    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      const px = x * TILE + PADDING, py = y * TILE + PADDING;
      if (plain) ctx.drawImage(plain, px, py, TILE, TILE);
      else { ctx.fillStyle = '#a4c568'; ctx.fillRect(px, py, TILE, TILE); }
    }
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      const id = this.map['Terrain Map'][x][y], def = this.terrainDef(id);
      const image = this.loaded.get(def?.sprite), px = x * TILE + PADDING, py = y * TILE + PADDING;
      if (image) {
        const rect = bottomAlignedSpriteRect(px, py, image.naturalWidth, image.naturalHeight);
        ctx.drawImage(image, rect.x, rect.y, rect.width, rect.height);
      } else {
        ctx.fillStyle = def?.color || '#df8aba'; ctx.fillRect(px, py, TILE, TILE);
        ctx.fillStyle = '#25372b'; ctx.font = '9px monospace'; ctx.fillText(String(id), px + 3, py + 16);
      }
    }
    for (const unit of showUnits ? this.map['Predeployed Units'] || [] : []) {
      const x = unit['Unit X'], y = unit['Unit Y'];
      if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || x >= width || y >= height) continue;
      const { def, owner, rel, recolor } = this.unitSprite(unit), image = this.loaded.get(rel);
      const px = x * TILE + PADDING, py = y * TILE + PADDING;
      ctx.save();
      if (recolor) ctx.filter = `hue-rotate(${owner * 47}deg)`;
      if (image) ctx.drawImage(image, px, py, TILE, TILE);
      else { ctx.fillStyle = '#b44162'; ctx.fillRect(px + 7, py + 7, 18, 18); }
      ctx.restore();
      if (recolor || !def) {
        ctx.fillStyle = '#183727'; ctx.fillRect(px + 20, py + 21, 12, 11);
        ctx.fillStyle = '#fff'; ctx.font = '8px monospace'; ctx.textAlign = 'center';
        ctx.fillText(String(owner || '?'), px + 26, py + 29); ctx.textAlign = 'left';
      }
      const hp = unit['Unit HP'];
      if (typeof hp === 'number' && hp < 10) {
        ctx.fillStyle = '#fff'; ctx.strokeStyle = '#17291f'; ctx.lineWidth = 2; ctx.font = 'bold 10px monospace';
        ctx.strokeText(String(Math.ceil(hp)), px + 2, py + 29); ctx.fillText(String(Math.ceil(hp)), px + 2, py + 29);
      }
    }
    if (grid) {
      ctx.strokeStyle = '#11291f50'; ctx.lineWidth = 1; ctx.beginPath();
      for (let x = 0; x <= width; x++) { ctx.moveTo(PADDING + x * TILE + .5, PADDING); ctx.lineTo(PADDING + x * TILE + .5, PADDING + height * TILE); }
      for (let y = 0; y <= height; y++) { ctx.moveTo(PADDING, PADDING + y * TILE + .5); ctx.lineTo(PADDING + width * TILE, PADDING + y * TILE + .5); }
      ctx.stroke();
    }
  }

  drawOverlays(ctx) {
    const coordinate = cell => typeof cell === 'string' ? cell.split(',').map(Number) :
      Array.isArray(cell) ? cell : [cell?.x, cell?.y];
    const valid = ([x, y]) => Number.isInteger(x) && Number.isInteger(y) && x >= 0 && y >= 0 &&
      x < this.map['Size X'] && y < this.map['Size Y'];
    ctx.save();
    for (const cell of this.asymmetricCells || []) {
      const point = coordinate(cell); if (!valid(point)) continue;
      ctx.strokeStyle = cell.type === 'orientation' ? '#ffa32b' : '#f36d67'; ctx.lineWidth = 2;
      ctx.strokeRect(PADDING + point[0] * TILE + 2, PADDING + point[1] * TILE + 2, TILE - 4, TILE - 4);
    }
    for (const cell of this.lockCells || []) {
      const point = coordinate(cell); if (!valid(point)) continue;
      const px = PADDING + point[0] * TILE, py = PADDING + point[1] * TILE;
      ctx.fillStyle = '#f4c64f'; ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(px + 11, py); ctx.lineTo(px, py + 11); ctx.closePath(); ctx.fill();
      ctx.strokeStyle = '#41340b'; ctx.lineWidth = 1; ctx.stroke();
    }
    for (const [cell, color] of [[this.hoverCell, '#ffffff'], [this.selectionCell, '#66e8ff']]) {
      if (!cell) continue;
      const point = coordinate(cell); if (!valid(point)) continue;
      const px = PADDING + point[0] * TILE, py = PADDING + point[1] * TILE;
      ctx.strokeStyle = '#183027'; ctx.lineWidth = 4; ctx.strokeRect(px + 2, py + 2, TILE - 4, TILE - 4);
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.strokeRect(px + 2, py + 2, TILE - 4, TILE - 4);
    }
    ctx.restore();
  }
  draw() {
    if (!this.map) return;
    const ctx = this.canvas.getContext('2d'); this.paint(ctx); this.drawOverlays(ctx);
  }
  transform() { this.frame.style.transform = `translate(${this.x}px,${this.y}px) scale(${this.scale})`; this.onZoom?.(this.scale); }
  fit() {
    this.autoFit = true;
    this.scale = Math.min(3, (this.viewport.clientWidth - 40) / this.canvas.width, (this.viewport.clientHeight - 40) / this.canvas.height);
    this.x = (this.viewport.clientWidth - this.canvas.width * this.scale) / 2;
    this.y = (this.viewport.clientHeight - this.canvas.height * this.scale) / 2;
    this.transform();
  }
  zoom(factor, anchorX = this.viewport.clientWidth / 2, anchorY = this.viewport.clientHeight / 2) {
    if (!this.map) return;
    this.autoFit = false;
    const next = Math.max(.15, Math.min(6, this.scale * factor)), ratio = next / this.scale;
    this.x = anchorX - (anchorX - this.x) * ratio; this.y = anchorY - (anchorY - this.y) * ratio;
    this.scale = next; this.transform();
  }
  tileInfo(x, y) {
    if (!this.map || !Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || x >= this.map['Size X'] || y >= this.map['Size Y']) return null;
    const id = this.map['Terrain Map'][x][y], terrain = this.terrainDef(id);
    const unit = (this.map['Predeployed Units'] || []).find(u => u['Unit X'] === x && u['Unit Y'] === y);
    return { x, y, id, terrain, unit, unitDef: unit && this.manifest.units[String(unit['Unit ID'])] };
  }
  tileAtEvent(event) {
    const bounds = this.viewport.getBoundingClientRect();
    const x = Math.floor(((event.clientX - bounds.left - this.x) / this.scale - PADDING) / TILE);
    const y = Math.floor(((event.clientY - bounds.top - this.y) / this.scale - PADDING) / TILE);
    return this.tileInfo(x, y);
  }
  async exportPNG() {
    if (!this.map) throw new Error('No map to export.');
    await this.updateMap(this.map);
    const canvas = document.createElement('canvas'); canvas.width = this.canvas.width; canvas.height = this.canvas.height;
    this.paint(canvas.getContext('2d'), false, { showUnits: true });
    return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('PNG export failed.')), 'image/png'));
  }
}
