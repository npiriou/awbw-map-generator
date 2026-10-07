import { BUILDINGS, UNIT_NAMES } from './settings.js';

export const GENERATOR_REVISION = 'property-ownership-v3.6';

export function generatorIsCurrent(status) {
  return status?.generator?.revision === GENERATOR_REVISION;
}

export function issueText(issue) {
  if (typeof issue === 'string') return issue;
  if (issue?.path?.startsWith('categories.')) {
    const name = issue.path.slice(11);
    const observed = issue.actual ?? issue.observed;
    if (typeof issue.expected === 'boolean' && typeof observed === 'boolean') {
      return `${name}: requested ${issue.expected ? 'Yes' : 'No'}, observed ${observed ? 'Yes' : 'No'}.`;
    }
    if (observed === null && issue.reason === 'official category is a soft label without structural certification') {
      return `${name}: category preference unverified.`;
    }
  }
  const details = [];
  if ('expected' in (issue || {})) details.push(`requested: ${JSON.stringify(issue.expected)}`);
  const observed = issue?.actual ?? issue?.observed;
  if (observed !== undefined) details.push(`observed: ${JSON.stringify(observed)}`);
  return [issue?.path || issue?.field || issue?.constraint, issue?.message || issue?.reason, details.join(', ')].filter(Boolean).join(' · ') || JSON.stringify(issue);
}

export function modelDisplayName(checkpoint) {
  const name = checkpoint.model_name || checkpoint.name;
  return `${name}${Number.isInteger(checkpoint.step) ? ` · step ${checkpoint.step.toLocaleString('en-GB')}` : ''}`;
}

export function modelCreatedLabel(checkpoint) {
  const value = checkpoint.created_at || checkpoint.modified_at;
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'shortOffset' }).format(date);
}

export function checkpointLabel(result) {
  const checkpoint = result?.checkpoint;
  const step = result?.actual_checkpoint_step ?? checkpoint?.step;
  const filename = typeof checkpoint === 'string' ? checkpoint.split(/[\\/]/).pop() : checkpoint?.path?.split(/[\\/]/).pop();
  return [filename ? `Checkpoint ${filename}` : '', Number.isInteger(step) ? `step ${step.toLocaleString('en-GB')}` : ''].filter(Boolean).join(' · ');
}

export function repairDescription(repair) {
  const building = BUILDINGS[repair.building] || repair.building;
  switch (repair.kind) {
    case 'tile_symmetry_projection': return `Terrain symmetry: ${repair.changed_tiles} tile(s) changed.`;
    case 'active_player_anchors': return `Active starts: ${(repair.positions || []).length} HQ / lab position(s) established.`;
    case 'structural_ownership': return `Ownership: ${repair.changed_tiles} tile(s) adjusted for ${repair.active_players} active player(s).`;
    case 'lab_ownership_assignment': return `Lab at (${repair.position?.x}, ${repair.position?.y}): owner ${repair.before_owner} → ${repair.after_owner}.`;
    case 'lab_ownership_summary': return `Lab ownership: ${repair.diagnostic?.issue_count ?? 0} remaining issue(s).`;
    case 'lab_ownership_unresolved': return `No compatible Lab ownership: ${(repair.reason || 'constraints require review').replaceAll('_', ' ')}.`;
    case 'hq_ownership_assignment': return `HQ at (${repair.position?.x}, ${repair.position?.y}): owner ${repair.before_owner} → ${repair.after_owner}.`;
    case 'hq_ownership_summary': return `HQ ownership: ${repair.diagnostic?.issue_count ?? 0} remaining issue(s).`;
    case 'hq_ownership_unresolved': return `No compatible HQ ownership: ${(repair.reason || 'constraints require review').replaceAll('_', ' ')}.`;
    case 'hq_symmetry_relocation': return 'Moved HQs from the symmetry axis to corresponding land positions.';
    case 'property_symmetry_assignment': return `${building} at (${repair.position?.x}, ${repair.position?.y}): owner ${repair.before_owner} → ${repair.after_owner}.`;
    case 'property_symmetry_summary': return `Property ownership: ${repair.diagnostic?.issue_count ?? 0} remaining issue(s).`;
    case 'property_symmetry_unresolved': return `Property ownership: ${repair.diagnostic?.issue_count ?? 0} constraint conflict(s).`;
    case 'property_owned_budget_adjustment': return `${building}: free pre-owned count ${repair.before_pre_owned} → ${repair.after_pre_owned} to fit the symmetry.`;
    case 'lab_owned_budget_adjustment': return `Lab: free pre-owned count ${repair.before_pre_owned} → ${repair.after_pre_owned} to fit the symmetry.`;
    case 'hq_owned_budget_adjustment': return `HQ: free pre-owned count ${repair.before_pre_owned} → ${repair.after_pre_owned} to fit the symmetry.`;
    case 'generated_property_active_owner': return `${building}: assigned to an active player.`;
    case 'raw_building_quota': return `${building}: count adjusted to ${repair.requested_raw_tiles} tiles before accessibility checks.`;
    case 'raw_property_ownership_quota': return `${building}: ${repair.pre_owned} pre-owned building(s) enforced.`;
    case 'unit_quota': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: ${repair.count} predeployed unit(s) enforced.`;
    case 'generated_unit_active_owner': return 'Assigned a generated unit to an active player.';
    case 'unit_start_plan_placement': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: moved to its owner’s region.`;
    case 'transport_deployment_placement': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: assigned a transport pickup or blocker role.`;
    case 'immobile_deployment_placement': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: assigned a static building or passage role.`;
    case 'transport_symmetry_placement': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: assigned a corresponding transport position.`;
    case 'transport_symmetry_component_fallback': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: placed in corresponding connected water areas.`;
    case 'immobile_symmetry_placement': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: placed on a corresponding ${BUILDINGS[repair.building_kind] || repair.building_kind}.`;
    case 'immobile_symmetry_demotion': return `${UNIT_NAMES[repair.unit_type] || repair.unit_type}: made mobile to complete the requested transport group.`;
    case 'unit_start_plan_summary':
    case 'transport_deployment_summary':
    case 'transport_symmetry_summary':
    case 'immobile_symmetry_summary':
    case 'immobile_deployment_summary': return `Unit placement: ${repair.after_issue_count ?? repair.diagnostic?.issue_count ?? 0} remaining issue(s).`;
    case 'unit_start_plan_unresolved':
    case 'transport_deployment_unresolved':
    case 'transport_symmetry_unresolved':
    case 'immobile_symmetry_unresolved':
    case 'immobile_deployment_unresolved': return `No compatible unit placement: ${(repair.reason || 'constraints require review').replaceAll('_', ' ')}.`;
    case 'lab_game_remove_hq': return `Lab game: HQ replaced at position ${repair.position}.`;
    default: return `${repair.kind || 'Correction'} : ${JSON.stringify(repair)}`;
  }
}

export function reportExport(result, request) {
  const { map: _map, ...provenance } = result;
  return {
    format: 'awbw-map-generation-report-v1',
    request: request || { settings: result.settings || {}, seed: result.seed, temperature: result.sampling?.temperature, refinement_steps: result.sampling?.refinement_steps },
    result: provenance,
  };
}
