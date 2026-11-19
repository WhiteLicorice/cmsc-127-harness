import { escapeHtml } from "./html.js";
function diagramType(type) {
    return type.replace(/[^A-Za-z0-9_]/g, "_");
}
function relationshipWords(relationship) {
    const parentCount = relationship.unique ? "zero or one" : "zero or many";
    const childCount = relationship.required ? "exactly one" : "zero or one";
    return `${relationship.child}: ${childCount} ${relationship.parent}. ${relationship.parent}: ${parentCount} ${relationship.child}.`;
}
function relationshipLine(relationship) {
    const parentMark = relationship.required ? "||" : "|o";
    const childMark = relationship.unique ? "o|" : "o{";
    const label = relationship.label ?? `fk_${relationship.child}_${relationship.childColumns.join("_")}`;
    return `  ${relationship.parent} ${parentMark}--${childMark} ${relationship.child} : ${label}`;
}
export function buildSchemaView(input) {
    const diagramLines = ["erDiagram"];
    for (const table of input.tables) {
        diagramLines.push(`  ${table.name} {`);
        for (const column of table.columns) {
            const markers = [
                ...(table.primaryKey.includes(column.name) ? ["PK"] : []),
                ...(input.relationships.some((relationship) => relationship.child === table.name && relationship.childColumns.includes(column.name))
                    ? ["FK"]
                    : []),
            ];
            diagramLines.push(`    ${diagramType(column.type)} ${column.name}${markers.length ? ` ${markers.join(",")}` : ""}`);
        }
        diagramLines.push("  }");
    }
    for (const relationship of input.relationships) {
        diagramLines.push(relationshipLine(relationship));
    }
    const constraints = input.tables.flatMap((table) => [
        ...table.columns
            .filter((column) => column.notNull)
            .map((column) => `${table.name}.${column.name}: NOT NULL`),
        ...table.constraints.map((constraint) => `${table.name}.${constraint.name}: ${constraint.definition}`),
    ]);
    return {
        liveDiagram: diagramLines.join("\n"),
        relationships: input.relationships.map(relationshipWords),
        constraints,
        differences: input.differences.map((difference) => difference.message),
    };
}
export function renderSchema(input) {
    const view = buildSchemaView(input);
    const erd = input.erdText || "No Mermaid erDiagram block appears in README.md.";
    const differenceList = view.differences.map((difference) => `<li class="state-fail">${escapeHtml(difference)}</li>`).join("");
    const relationshipList = view.relationships.map((relationship) => `<li>${escapeHtml(relationship)}</li>`).join("");
    const constraintList = view.constraints.map((constraint) => `<li>${escapeHtml(constraint)}</li>`).join("");
    return `<div class="schema-diagrams"><section><h2>ERD from README.md</h2>${input.erdText ? `<pre class="mermaid">${escapeHtml(erd)}</pre>` : `<p>${escapeHtml(erd)}</p>`}</section><section><h2>Live schema</h2><pre${input.tables.length > 0 ? ' class="mermaid"' : ""}>${escapeHtml(view.liveDiagram)}</pre></section></div><section><h2>Differences</h2><ul>${differenceList || '<li class="state-pass">No differences found.</li>'}</ul></section><section><h2>Relationships</h2><ul>${relationshipList || "<li>No foreign keys found.</li>"}</ul></section><section><h2>Columns and constraints</h2><ul>${constraintList || "<li>No constraints found.</li>"}</ul></section>`;
}
