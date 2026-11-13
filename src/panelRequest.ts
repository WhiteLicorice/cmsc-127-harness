export function selectionMatches(
  requestedPanel: string,
  activePanel: string,
  requestedSchema: string,
  selectedSchema: string,
): boolean {
  return requestedPanel === activePanel && requestedSchema === selectedSchema;
}
