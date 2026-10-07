export const TAGS = [
  ['island(s)', 'Islands', 'Islands detected by the accessibility checker.'],
  ['pipe seams', 'Pipe seams', 'At least one breakable pipe seam.'],
  ['predeployed transports', 'Predeployed transports', 'Usable Black Boats / Landers and T-Copters.'],
  ['immobile pre-deploy', 'Immobile predeploy', 'At least one unit with no passable adjacent tile.'],
  ['Flip symmetry', 'Mirror', 'Horizontal or vertical tile symmetry.'],
  ['diagonal symmetry', 'Diagonal', 'Square maps only; tile symmetry.'],
  ['rotational symmetry', 'Rotation', '180°, or 90° / 270° for square maps.'],
  ['Asymmetrical', 'Asymmetric', 'No detected tile symmetry.'],
  ['1vX team play', '1vX team play', 'An asymmetric starting configuration for at least three players.'],
];
export const SYMMETRIES = [['', 'Any'], ['Flip symmetry', 'Mirror'], ['diagonal symmetry', 'Diagonal'], ['rotational symmetry', 'Rotation'], ['Asymmetrical', 'Asymmetric']];
export const CATEGORY_GROUPS = [
  ['Map Quality', ['S-Rank', 'A-Rank', 'B-Rank', 'C-Rank', 'New']],
  ['Map Function', ['Global League', 'Hall of Fame', 'Historical/Geographical', 'Joke', 'Sprite', 'Toy-Box', 'Under Review']],
  ['Map Features', ['Base Light', 'Contested Bases', 'FFA Multiplay', 'Fog of War', 'Gimmick', 'Heavy Naval', 'HFOG', 'High Funds', 'Live Play', 'Medium Funds', 'Mixed Base', 'RBC Playable', 'Standard', 'Team Play', 'Teleport Tile']],
];
export const BUILDINGS = { city: 'Cities', base: 'Bases', airport: 'Airports', port: 'Ports', hq: 'HQ', com_tower: 'Com. towers', lab: 'Labs', silo: 'Silos', silo_empty: 'Empty silos' };
export const UNIT_NAMES = { anti_air: 'Anti-Air', apc: 'APC', artillery: 'Artillery', battle_copter: 'B-Copter', battleship: 'Battleship', black_boat: 'Black Boat', black_bomb: 'Black Bomb', bomber: 'Bomber', carrier: 'Carrier', cruiser: 'Cruiser', fighter: 'Fighter', infantry: 'Infantry', lander: 'Lander', mech: 'Mech', medium_tank: 'Md. Tank', mega_tank: 'Mega Tank', missiles: 'Missiles', neotank: 'Neotank', piperunner: 'Piperunner', recon: 'Recon', rockets: 'Rockets', stealth: 'Stealth', sub: 'Sub', tank: 'Tank', transport_copter: 'T-Copter' };

export function optionalInteger(value, label, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  if (String(value).trim() === '') return undefined;
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < minimum || result > maximum) throw new Error(`${label}: enter an integer between ${minimum} and ${maximum}.`);
  return result;
}

export function buildSettings({ dimensions = {}, tags = {}, symmetry, buildings = {}, units = {}, categories = [] }) {
  const settings = {};
  for (const key of ['width', 'height', 'players']) {
    const value = optionalInteger(dimensions[key] ?? '', { width: 'Width', height: 'Height', players: 'Players' }[key], 1, key === 'players' ? 20 : 50);
    if (value !== undefined) settings[key] = value;
  }
  for (const [key] of TAGS) {
    if (symmetry !== undefined && SYMMETRIES.some(([name]) => name === key)) continue;
    if (tags[key] === 'true' || tags[key] === 'false') (settings.tags ||= {})[key] = tags[key] === 'true';
  }
  if (symmetry !== undefined && !SYMMETRIES.some(([key]) => key === symmetry)) throw new Error('Unknown symmetry.');
  if (symmetry) (settings.tags ||= {})[symmetry] = true;
  for (const kind of Object.keys(BUILDINGS)) {
    const counts = {};
    for (const metric of ['total', 'pre_owned', 'neutral']) {
      const value = optionalInteger(buildings[kind]?.[metric] ?? '', BUILDINGS[kind]);
      if (value !== undefined) counts[metric] = value;
    }
    if (Object.keys(counts).length) (settings.building_counts ||= {})[kind] = counts;
  }
  for (const [kind, value] of Object.entries(units)) {
    if (!(kind in UNIT_NAMES)) continue;
    const count = optionalInteger(value, UNIT_NAMES[kind]);
    if (count !== undefined) (settings.predeployed_counts ||= {})[kind] = count;
  }
  if (Array.isArray(categories)) {
    if (categories.length) settings.categories = [...categories];
  } else {
    for (const [name, value] of Object.entries(categories)) {
      if (value === 'true' || value === 'false' || typeof value === 'boolean')
        (settings.categories ||= {})[name] = value === true || value === 'true';
    }
  }
  return settings;
}

export function constraintCount(settings) {
  let count = ['width', 'height', 'players'].filter(key => key in settings).length;
  count += Object.keys(settings.tags || {}).length + Object.keys(settings.predeployed_counts || {}).length;
  count += Object.values(settings.building_counts || {}).reduce((sum, row) => sum + Object.keys(row).length, 0);
  return count + Object.keys(settings.categories || {}).length;
}

export function generationOptions(values) {
  const seed = optionalInteger(values.seed, 'Seed', 0, 4294967295);
  const temperature = Number(values.temperature);
  if (!Number.isFinite(temperature) || temperature < .05 || temperature > 3) throw new Error('Temperature must be between 0.05 and 3.');
  const refinement_steps = optionalInteger(values.refinement_steps, 'Refinement steps', 1, 64);
  const attempts = optionalInteger(values.attempts, 'Attempts', 1, 16);
  if (refinement_steps === undefined || attempts === undefined) throw new Error('Enter refinement steps and attempts.');
  const guidance_scale = values.guidance_scale === undefined ? undefined : Number(values.guidance_scale);
  if (guidance_scale !== undefined && (!Number.isFinite(guidance_scale) || guidance_scale < 1 || guidance_scale > 5)) throw new Error('Guidance must be between 1 and 5.');
  return { ...(seed !== undefined ? { seed } : {}), temperature, refinement_steps, attempts,
    ...(guidance_scale !== undefined ? { guidance_scale } : {}) };
}
