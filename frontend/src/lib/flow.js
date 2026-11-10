// EDSIDER_FLOW=data_entry_only on the server (service.py data_entry_only(),
// reported on /me as `flow`): data entry sends each paper to EdSider itself,
// where EdSider's MASAD assigns its review - so there is no teacher review or
// paper assignment here, and an administrator works in Paper intake.
export function isDataEntryOnly(me) {
  return me?.flow === 'data_entry_only'
}

// Where a signed-in person starts - `homes` is the caller's own role map.
export function homeFor(me, homes) {
  if (isDataEntryOnly(me) && (me.role === 'admin' || me.role === 'superadmin')) {
    return '/app/data-entry/paper-intake'
  }
  return homes[me?.role]
}
