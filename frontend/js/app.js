import { MapRenderer } from './renderer.js';
import { MapEditor } from './editor.js';
import { exportAWBWText } from './awbw-export.js';
import { TAGS, SYMMETRIES, CATEGORY_GROUPS, BUILDINGS, UNIT_NAMES, buildSettings, generationOptions } from './settings.js';
import { modelDisplayName, modelCreatedLabel, issueText, generatorIsCurrent, GENERATOR_REVISION } from './provenance.js';

const $ = id => document.getElementById(id);
const state = { checkpoint: null, generatorCurrent: false, busy: false, importing: false, current: null, statusTimer: null, jobTimer: null, seedRandom: true };
const multiplayerTags = new Set(['1vX team play']);
const multiplayerCategories = new Set(['Team Play', 'FFA Multiplay']);
const renderer = new MapRenderer({
  canvas: $('map-canvas'), viewport: $('map-viewport'), frame: $('canvas-frame'),
  onWarning: text => $('render-note').textContent = text === 'AWBW sprites' ? '' : text,
  onHover: info => { $('hover-info').textContent = info ? `${info.x}, ${info.y} · ${info.terrain?.name || info.id}${info.unit ? ` · ${info.unitDef?.name || 'Unit'}` : ''}` : ''; editor?.hover(info); },
});
const editor = new MapEditor(renderer, { onChange: editedMap, onError: showError,
  getSettings: () => ({ width: Number($('width').value) || 20, height: Number($('height').value) || 20, players: Number($('players').value) || 2 }),
  isBusy: () => state.busy });
let validationTimer, validationSerial = 0;

function editedMap(map) {
  $('width').value = map['Size X']; $('height').value = map['Size Y'];
  state.current = { ...(state.current || {}), map, edited: true, export_id: null, report: null, repairs: [] };
  $('download-json').disabled = false; $('download-png').disabled = false; $('download-awbw').disabled = false;
  $('map-dimensions').textContent = `${map['Size X']} × ${map['Size Y']} · ${map['Player Count']} players`;
  $('conformity-status').className = 'warning'; $('conformity-status').textContent = 'Edited map · Checking constraints…';
  $('conformity-issues').replaceChildren();
  clearTimeout(validationTimer); const serial = ++validationSerial;
  validationTimer = setTimeout(async () => {
    try {
      const { report } = await api('/api/editor/validate', { method: 'POST', body: JSON.stringify({ map, settings: readSettings() }) });
      if (serial !== validationSerial) return;
      state.current.report = report;
      $('editor-message').textContent = '';
      const matched = report.status === 'matched';
      $('conformity-status').className = matched ? 'good' : report.status === 'unresolved' ? 'warning' : 'bad';
      $('conformity-status').textContent = matched ? 'Edited map · Constraints met' : report.status === 'unresolved' ? 'Edited map · Constraints checked' : 'Edited map · Constraints not met';
      (report.violations || []).forEach(issue => { const node = element('div', issueText(issue)); node.className = 'issue'; $('conformity-issues').append(node); });
    } catch (error) {
      if (serial !== validationSerial) return;
      $('conformity-status').textContent = 'Edited map · Not verified';
      $('editor-message').textContent = error.message;
    }
  }, 400);
}

async function api(path, options) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error(`Invalid response (${response.status}).`); }
  if (!response.ok) {
    const error = data.error || data.detail || data.message || `Error ${response.status}`;
    throw new Error(typeof error === 'string' ? error : JSON.stringify(error));
  }
  return data;
}

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
function input(attrs) {
  const node = element('input');
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}
function choiceRow(name, group, attribute, defaultValue = '') {
  const row = element('div'); row.className = 'choice-row';
  row.setAttribute('role', 'radiogroup'); row.setAttribute('aria-label', name); row.append(element('span', name));
  [['', 'Any'], ['true', 'Yes'], ['false', 'No']].forEach(([value, text]) => {
    const label = element('label'); label.title = text;
    const radio = input({ type: 'radio', name: group, value, ...attribute, 'aria-label': `${name}: ${text}` });
    radio.defaultChecked = radio.checked = value === defaultValue;
    label.append(radio); row.append(label);
  });
  return row;
}
function buildControls(meta, sprites = {}) {
  $('tags').replaceChildren();
  TAGS.filter(([key]) => !SYMMETRIES.some(([name]) => name === key)).forEach(([key, label, description], index) => {
    const row = choiceRow(label, `tag-${index}`, { 'data-tag': key }, multiplayerTags.has(key) ? 'false' : ''); row.title = description; $('tags').append(row);
  });
  $('symmetry').replaceChildren();
  SYMMETRIES.forEach(([value, name]) => {
    const label = element('label');
    const radio = input({ type: 'radio', name: 'symmetry', value, 'aria-label': `Symmetry: ${name}` });
    radio.defaultChecked = radio.checked = value === 'rotational symmetry';
    label.append(radio, document.createTextNode(name)); $('symmetry').append(label);
  });
  $('buildings').replaceChildren();
  Object.entries(BUILDINGS).forEach(([kind, label]) => {
    const row = element('tr'), heading = element('td'), name = element('span', label);
    name.className = 'building-label';
    const tileId = { city: 34, base: 35, airport: 36, port: 37, hq: 42, com_tower: 133, lab: 145, silo: 111, silo_empty: 112 }[kind];
    const sprite = sprites.terrain?.[tileId]?.sprite;
    if (sprite) {
      const icon = element('img'); icon.src = `/assets/${sprite}.png`; icon.alt = ''; icon.className = 'building-icon';
      name.prepend(icon);
    }
    heading.append(name); row.append(heading);
    ['total', 'pre_owned', 'neutral'].forEach(metric => {
      const cell = element('td');
      cell.append(input({ type: 'number', min: '0', placeholder: '—', 'data-building': kind, 'data-metric': metric, 'aria-label': `${label}: ${{ total: 'total', pre_owned: 'owned', neutral: 'neutral' }[metric]}` }));
      row.append(cell);
    });
    $('buildings').append(row);
  });
  $('units').replaceChildren();
  (meta.vocabulary?.unit_types || Object.keys(UNIT_NAMES)).forEach(kind => {
    if (!(kind in UNIT_NAMES)) return;
    const label = element('label'); label.title = UNIT_NAMES[kind];
    const sprite = Object.values(sprites.units || {}).find(unit => unit.type === kind)?.sprites['1'];
    const icon = element('img'); icon.src = `/assets/${sprite}.png`; icon.alt = UNIT_NAMES[kind];
    label.append(icon, input({ type: 'number', min: '0', placeholder: 'Any', value: '0', 'data-unit': kind, 'aria-label': UNIT_NAMES[kind] }));
    $('units').append(label);
  });
  const definition = meta.schema?.properties?.categories || {};
  const categories = [...(definition.items?.enum || Object.keys(definition.properties || {}))];
  $('categories-section').hidden = !categories.length; $('categories').replaceChildren();
  let categoryIndex = 0;
  CATEGORY_GROUPS.forEach(([title, names]) => {
    const available = names.filter(name => categories.includes(name));
    if (!available.length) return;
    const group = element('fieldset'); group.className = 'category-group'; group.append(element('legend', title));
    available.forEach(name => {
      group.append(choiceRow(name, `category-${categoryIndex++}`, { 'data-category': name }, name === 'Standard' ? 'true' : multiplayerCategories.has(name) ? 'false' : ''));
    });
    $('categories').append(group);
  });
  updateMultiplayerChoices();
}
function updateMultiplayerChoices() {
  const players = Number($('players').value);
  const unavailable = Number.isInteger(players) && players > 0 && players < 3;
  document.querySelectorAll('[data-tag], [data-category]').forEach(radio => {
    if (!multiplayerTags.has(radio.dataset.tag) && !multiplayerCategories.has(radio.dataset.category)) return;
    radio.disabled = unavailable;
    if (unavailable) radio.checked = radio.value === 'false';
    radio.closest('.choice-row').setAttribute('aria-disabled', String(unavailable));
  });
}
function readSettings() {
  updateMultiplayerChoices();
  const tags = {}, buildings = {}, units = {}, categories = {};
  document.querySelectorAll('[data-tag]:checked').forEach(node => tags[node.dataset.tag] = node.value);
  document.querySelectorAll('[data-building]').forEach(node => (buildings[node.dataset.building] ||= {})[node.dataset.metric] = node.value);
  document.querySelectorAll('[data-unit]').forEach(node => units[node.dataset.unit] = node.value);
  document.querySelectorAll('[data-category]:checked').forEach(node => categories[node.dataset.category] = node.value);
  const symmetry = document.querySelector('[name=symmetry]:checked')?.value || '';
  return buildSettings({ dimensions: { width: $('width').value, height: $('height').value, players: $('players').value }, tags, symmetry, buildings, units, categories });
}
function applySettings(settings, symmetry) {
  ['width', 'height', 'players'].forEach(key => $(key).value = settings[key] ?? '');
  document.querySelectorAll('[data-tag]').forEach(node => {
    node.checked = node.value === String(settings.tags?.[node.dataset.tag] ?? '');
  });
  document.querySelectorAll('[name=symmetry]').forEach(node => node.checked = node.value === symmetry);
  document.querySelectorAll('[data-building]').forEach(node => {
    node.value = settings.building_counts?.[node.dataset.building]?.[node.dataset.metric] ?? '';
  });
  document.querySelectorAll('[data-unit]').forEach(node => node.value = settings.predeployed_counts?.[node.dataset.unit] ?? '');
  const categories = Array.isArray(settings.categories) ? Object.fromEntries(settings.categories.map(name => [name, true])) : settings.categories || {};
  document.querySelectorAll('[data-category]').forEach(node => node.checked = node.value === String(categories[node.dataset.category] ?? ''));
  updateMultiplayerChoices();
}
async function importMapSettings() {
  if (state.importing || state.busy) return;
  clearError();
  const value = $('import-map-id').value.trim(), id = Number(value);
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(id) || id < 1 || id > 2147483647) {
    showError(new Error('Enter a positive map ID.')); return;
  }
  state.importing = true; updateAvailability();
  $('import-status').hidden = false; $('import-status').textContent = 'Looking up map…'; $('import-notes').hidden = true;
  try {
    const result = await api(`/api/maps/${id}/settings`);
    applySettings(result.settings, result.symmetry || '');
    const link = element('a', result.name || `Map ${id}`); link.href = result.url; link.target = '_blank'; link.rel = 'noopener';
    link.title = [result.author, result.published_at].filter(Boolean).join(' · ');
    $('import-status').replaceChildren(document.createTextNode('Imported '), link, document.createTextNode(` · ${result.source === 'local' ? 'Local' : 'AWBW'}`));
    $('import-notes').textContent = (result.notes || []).join(' '); $('import-notes').hidden = !result.notes?.length;
  } catch (error) {
    $('import-status').hidden = true; showError(error);
  } finally { state.importing = false; updateAvailability(); }
}
function showError(error) { $('form-error').textContent = error?.message || String(error); $('form-error').hidden = false; }
function clearError() { $('form-error').hidden = true; $('form-error').textContent = ''; }
function updateSeedMode() {
  const random = state.seedRandom, button = $('seed-mode');
  $('seed').readOnly = random || state.busy;
  $('seed').required = !random;
  $('seed-mode-label').textContent = random ? 'Random' : 'Fixed';
  button.disabled = state.busy;
  button.setAttribute('aria-pressed', String(!random));
  button.setAttribute('aria-label', `Seed mode: ${random ? 'Random. Switch to fixed.' : 'Fixed. Switch to random.'}`);
  button.title = random ? 'Random: a new seed for each generation. Click to keep the displayed seed fixed.' : 'Fixed: reuse this seed for each generation. Click for a new random seed each time.';
  button.innerHTML = `<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${random ? '<rect x="3" y="3" width="18" height="18" rx="2"/><path stroke-width="3" d="M8 8h0M16 8h0M12 12h0M8 16h0M16 16h0"/>' : '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 15v2"/>'}</svg>`;
}
function showSeed(seed) {
  $('seed').value = String(seed);
  $('generation-seed').textContent = `Seed: ${seed}`;
  $('generation-seed').hidden = false;
}
function updateAvailability() {
  $('generate').disabled = state.busy || state.importing || !state.checkpoint || !state.generatorCurrent;
  $('generate').textContent = state.busy ? 'Generating…' : !state.generatorCurrent ? 'Generator update required' : state.checkpoint ? 'Generate' : 'Model unavailable';
  $('import-map').disabled = state.busy || state.importing;
  $('import-map').textContent = state.importing ? 'Importing…' : 'Import';
  $('clear-settings').disabled = state.busy || state.importing;
  updateSeedMode();
  const selectedModel = document.querySelector('[name=checkpoint]:checked');
  const policy = selectedModel?.dataset.maskPolicy;
  const anchorStaged = selectedModel?.dataset.factorization === 'anchor-staged';
  const staged = anchorStaged || selectedModel?.dataset.factorization === 'staged';
  const scene = policy === 'learned-scene';
  $('model-capabilities').hidden = !staged && !scene;
  const stageOrder = anchorStaged ? 'terrain → HQ/Lab owners → other owners → units' : 'terrain → ownership → units';
  $('model-capabilities').textContent = scene ? 'Raw experiment · requested symmetry is not guaranteed · new maps only' : staged ? `Learned stages: ${stageOrder} · ${anchorStaged ? 4 : 3} × ${selectedModel.dataset.diffusionSteps} steps · new maps only` : '';
  const fixedSchedule = ['raster-frontier', 'discrete-diffusion', 'learned-scene'].includes(policy);
  $('attempts').disabled = scene;
  $('attempts').closest('label').hidden = scene;
  $('refinement_steps').disabled = fixedSchedule;
  $('refinement_steps').closest('label').hidden = fixedSchedule;
  $('guidance_scale').disabled = policy !== 'discrete-diffusion';
  $('guidance_scale').closest('label').hidden = policy !== 'discrete-diffusion';
}
function updateCheckpoint(data) {
  state.generatorCurrent = generatorIsCurrent(data);
  const version = $('generator-version');
  version.hidden = !state.generatorCurrent;
  version.textContent = state.generatorCurrent ? `Generator · loaded ${modelCreatedLabel({ created_at: data.generator.loaded_at })}` : '';
  if (!state.generatorCurrent) showError(new Error('This server is running an older generator. It needs an update before generating.'));
  const names = (data.checkpoints || []).map(checkpoint => typeof checkpoint === 'string' ? checkpoint : checkpoint.name);
  const preferred = names.includes('ppo-48000.pt') ? 'ppo-48000.pt' : null;
  state.checkpoint = preferred;
  document.querySelectorAll('[name=checkpoint]').forEach(radio => {
    radio.disabled = !names.includes(radio.value);
    radio.defaultChecked = radio.value === preferred;
    radio.checked = radio.value === state.checkpoint;
    const checkpoint = data.checkpoints?.find(item => item.name === radio.value);
    const label = radio.closest('label');
    label.hidden = false;
    radio.dataset.maskPolicy = checkpoint?.mask_policy || 'random';
    radio.dataset.factorization = checkpoint?.diffusion_factorization || 'joint';
    radio.dataset.diffusionSteps = checkpoint?.diffusion_steps || '';
    radio.dataset.supportsEditor = String(checkpoint?.supports_editor !== false);
    const name = radio.value === 'ppo-48000.pt' ? 'PPO 48000' : checkpoint ? modelDisplayName(checkpoint) : radio.value;
    label.querySelector('[data-model-name]').textContent = ['staged', 'anchor-staged'].includes(checkpoint?.diffusion_factorization) ? `${name} · Learned stages` : name;
    const date = label.querySelector('[data-model-date]');
    date.textContent = checkpoint ? modelCreatedLabel(checkpoint) : '';
    date.dateTime = checkpoint?.created_at || checkpoint?.modified_at || '';
    radio.setAttribute('aria-label', `Model: ${name}`);
  });
  updateAvailability();
}
async function waitForCheckpoint() {
  clearTimeout(state.statusTimer);
  try { updateCheckpoint(await api('/api/status')); } catch (error) { showError(error); }
  state.statusTimer = setTimeout(waitForCheckpoint, state.checkpoint ? 15000 : 5000);
}
function setBusy(busy) {
  state.busy = busy; $('job-progress').hidden = !busy; updateAvailability();
  if (busy) $('job-progress').value = 0;
}
async function showResult(result) {
  if (!result?.map) throw new Error('No map received.');
  if (result.sampling?.generator_revision !== GENERATOR_REVISION) throw new Error('This result came from an older generator. Refresh the interface before generating again.');
  if (Number.isInteger(result.seed)) showSeed(result.seed);
  clearTimeout(validationTimer); validationSerial++;
  await editor.acceptMap(result.map, { preserveLocks: !!result.editor }); state.current = result; $('empty-preview').hidden = true;
  const map = result.map;
  $('map-dimensions').textContent = `${map['Size X']} × ${map['Size Y']} · ${map['Player Count']} player${map['Player Count'] === 1 ? '' : 's'}`;
  const statuses = { matched: ['good', 'Constraints met'], not_matched: ['bad', 'Constraints not met'], unresolved: ['warning', 'Constraints checked'], invalid_map: ['bad', 'Invalid map'], invalid_settings: ['bad', 'Invalid settings'] };
  const [style, text] = statuses[result.report?.status] || ['warning', 'Not verified'];
  const count = result.repairs?.length || 0;
  const placementWarnings = result.strategy?.warnings || [];
  $('conformity-status').className = placementWarnings.length && style === 'good' ? 'warning' : style;
  $('conformity-status').textContent = `${text}${count ? ` · ${count} correction${count === 1 ? '' : 's'}` : ''}${placementWarnings.length ? ' · Placement warnings' : ''}`;
  $('conformity-issues').replaceChildren();
  (result.report?.violations || []).forEach(issue => {
    const node = element('div', issueText(issue)); node.className = 'issue'; $('conformity-issues').append(node);
  });
  placementWarnings.forEach(warning => {
    const node = element('div', warning); node.className = 'issue'; $('conformity-issues').append(node);
  });
  $('download-json').disabled = false; $('download-png').disabled = false; $('download-awbw').disabled = false;
}
async function pollJob(id) {
  clearTimeout(state.jobTimer);
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (job.status === 'completed') { await showResult(job.result); setBusy(false); return; }
    if (job.status === 'failed') {
      if (job.result?.map) await showResult(job.result);
      throw new Error(typeof job.error === 'string' ? job.error : JSON.stringify(job.error || 'Generation failed.'));
    }
    const progress = typeof job.progress === 'number' ? job.progress : job.progress?.fraction ?? job.progress?.percent;
    if (typeof progress === 'number') $('job-progress').value = progress <= 1 ? progress * 100 : progress;
    else $('job-progress').removeAttribute('value');
    state.jobTimer = setTimeout(() => pollJob(id), 1000);
  } catch (error) { setBusy(false); showError(error); }
}
function download(blob, name) {
  const url = URL.createObjectURL(blob); downloadURL(url, name); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function downloadURL(url, name) {
  const link = element('a'); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
}
function filename(extension) { return `awbw-${state.current?.seed ?? 'edited-map'}.${extension}`; }

$('generation-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.busy || state.importing || !state.checkpoint || !state.generatorCurrent) return; clearError();
  try {
    const settings = readSettings();
    const layout = editor.generationInput();
    if (layout && document.querySelector('[name=checkpoint]:checked')?.dataset.supportsEditor === 'false') throw new Error("This model generates new maps only. Turn off 'Preserve preplaced cells when generating' or choose another model.");
    if (layout && (layout.width > 50 || layout.height > 50)) throw new Error('Generation supports maps up to 50 × 50. Resize the map before generating.');
    const options = generationOptions(Object.fromEntries(['seed', 'temperature', 'refinement_steps', 'attempts', 'guidance_scale'].map(key => [key, key === 'seed' && state.seedRandom ? '' : $(key).value])));
    options.auto_correct = $('auto-correct').checked;
    if ($('guidance_scale').disabled) delete options.guidance_scale;
    if ($('attempts').disabled) options.attempts = 1;
    if (options.seed === undefined) options.seed = crypto.getRandomValues(new Uint32Array(1))[0];
    showSeed(options.seed);
    setBusy(true);
    const job = await api('/api/generate', { method: 'POST', body: JSON.stringify({ settings, ...options, generator_revision: GENERATOR_REVISION, checkpoint: state.checkpoint, ...(layout ? { editor: layout } : {}) }) });
    if (!job.job_id) throw new Error('Generation did not start.');
    await pollJob(job.job_id);
  } catch (error) { setBusy(false); showError(error); }
});
$('clear-settings').addEventListener('click', () => {
  $('generation-form').reset(); state.checkpoint = document.querySelector('[name=checkpoint]:checked')?.value || null;
  state.seedRandom = true;
  updateMultiplayerChoices();
  $('import-status').hidden = true; $('import-notes').hidden = true;
  clearError(); updateAvailability();
});
$('seed-mode').addEventListener('click', () => {
  if (state.busy) return;
  state.seedRandom = !state.seedRandom;
  if (!state.seedRandom && $('seed').value === '') $('seed').value = '0';
  updateSeedMode();
  if (!state.seedRandom) $('seed').focus();
});
$('import-map').addEventListener('click', importMapSettings);
$('load-editor-map').addEventListener('click', async () => {
  if (state.busy || state.importing) return;
  try {
    const value = $('import-map-id').value.trim();
    if (!/^\d+$/.test(value) || Number(value) < 1) throw new Error('Enter a positive map ID.');
    state.importing = true; updateAvailability();
    const result = await api(`/api/maps/${value}/map`);
    if (!result.map) throw new Error('The imported map does not contain a grid.');
    editor.document.load(result.map); await editor.changed(true); editor.setTool('terrain');
    $('editor-message').textContent = `Loaded ${result.name || `map ${value}`}`;
  } catch (error) { showError(error); }
  finally { state.importing = false; updateAvailability(); }
});
$('players').addEventListener('input', updateMultiplayerChoices);
$('players').addEventListener('change', updateMultiplayerChoices);
$('import-map-id').addEventListener('keydown', event => {
  if (event.key === 'Enter') { event.preventDefault(); importMapSettings(); }
});
$('model-options').addEventListener('change', () => {
  state.checkpoint = document.querySelector('[name=checkpoint]:checked')?.value || null; updateAvailability();
});
$('grid').addEventListener('change', () => { renderer.grid = $('grid').checked; renderer.draw(); });
$('fit').addEventListener('click', () => { if (renderer.map) renderer.fit(); });
$('download-json').addEventListener('click', () => {
  if (!state.current) return;
  download(new Blob([JSON.stringify(editor.document.map, null, 2)], { type: 'application/json' }), filename('json'));
});
$('download-awbw').addEventListener('click', () => {
  if (!editor.document?.map) return;
  try {
    clearError();
    download(new Blob([exportAWBWText(editor.document.map)], { type: 'text/plain;charset=utf-8' }), filename('txt'));
  } catch (error) { showError(error); }
});
$('download-png').addEventListener('click', async () => {
  try {
    download(await renderer.exportPNG(), filename('png'));
  } catch (error) { showError(error); }
});

async function initialize() {
  try {
    const [meta, sprites] = await Promise.all([api('/api/meta'), renderer.manifestPromise]);
    buildControls(meta, sprites); updateCheckpoint(meta);
    await editor.initialize(sprites);
    if (editor.document.map) editedMap(editor.document.map);
  } catch (error) {
    buildControls({}, renderer.manifest || {}); showError(error); updateAvailability();
    if (!editor.ready && renderer.manifest) { await editor.initialize(renderer.manifest); if (editor.document.map) editedMap(editor.document.map); }
  }
  if (!state.checkpoint) await waitForCheckpoint();
  else state.statusTimer = setTimeout(waitForCheckpoint, 15000);
}
initialize();
