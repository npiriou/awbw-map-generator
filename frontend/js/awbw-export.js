import { validateMap } from './renderer.js';

export function exportAWBWText(map) {
  const { width, height } = validateMap(map);
  const columns = map['Terrain Map'];
  const rows = [];
  for (let y = 0; y < height; y++) {
    const row = [];
    for (let x = 0; x < width; x++) {
      const id = columns[x][y];
      if (id < 1) throw new Error(`Invalid AWBW terrain ID at ${x}, ${y}: ${id}.`);
      row.push(id);
    }
    rows.push(row.join(','));
  }
  return rows.join('\n') + '\n';
}
