export function selectionMatches(requestedPanel, activePanel, requestedSchema, selectedSchema) {
    return requestedPanel === activePanel && requestedSchema === selectedSchema;
}
